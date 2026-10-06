"""Бот в фоновом потоке со своим циклом asyncio и машиной состояний."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from PlayerokAPI import User

from playerok_bot import bot as bot_core
from playerok_bot.bot import BotError
from playerok_bot.config import Config, ConfigError
from playerok_bot.storage import StateError
from playerok_bot.watchers import DealWatcher

from .errors import ApiError
from .feed import EventFeed
from .lock import ProcessLock
from .notifier import FeedNotifier

__all__ = ["ACTIVE", "ERROR", "RUNNING", "STARTING", "STOPPED", "STOPPING", "BotRuntime"]

logger = logging.getLogger(__name__)

STOPPED = "stopped"
STARTING = "starting"
RUNNING = "running"
STOPPING = "stopping"
ERROR = "error"
#: Пока поток бота жив, второй запуск невозможен.
ACTIVE = frozenset({STARTING, RUNNING, STOPPING})
#: Как часто сверять config.toml с работающим ботом (секунды).
CONFIG_CHECK_INTERVAL = 3.0
#: Как часто обновлять баланс аккаунта (секунды).
BALANCE_INTERVAL = 300.0

_T = TypeVar("_T")


def apply_live(live: Config, fresh: Config) -> bool:
    """Перенести в работающего бота то, что меняется без перезапуска:
    правила и флажки автовыдачи, настройки перевыставления. True — что-то изменилось."""
    changed = False
    for target, source, names in (
        (live.delivery, fresh.delivery, ("enabled", "mark_sent", "rules")),
        (live.relist, fresh.relist, ("after_sale", "expired", "interval_minutes", "match")),
    ):
        for name in names:
            value = getattr(source, name)
            if getattr(target, name) != value:
                setattr(target, name, value)
                changed = True
    return changed


def needs_restart(running: Config, fresh: Config, *, expired_task: bool) -> bool:
    """Есть ли в новых настройках то, что бот подхватит только после перезапуска."""
    return (
        running.playerok != fresh.playerok
        or running.telegram != fresh.telegram
        or running.state_file != fresh.state_file
        or (fresh.relist.expired and not expired_task)
    )


class BotRuntime:
    """Запуск и остановка `playerok_bot.bot.run` из окна.

    stopped → starting → running → (stopping →) stopped | error.
    Бот работает в отдельном потоке-демоне со своим циклом событий; окно
    обращается к объекту из разных потоков, поэтому всё под блокировкой.
    После остановки помнит аккаунт и последнего наблюдателя за сделками.
    """

    def __init__(self, feed: EventFeed, *, lock_path: Path | None = None) -> None:
        self._feed = feed
        self._process_lock = ProcessLock(lock_path) if lock_path is not None else None
        self._lock = threading.Lock()
        self._status = STOPPED
        self._error: str | None = None
        self._started_at: datetime | None = None
        self._started_mono: float | None = None
        self._stop_requested = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._me: User | None = None
        self._balance_at: datetime | None = None
        self._watcher: DealWatcher | None = None
        #: Настройки, с которыми работает бот (живой объект: правила меняются на ходу).
        self._live: Config | None = None
        self._reload: Callable[[], Config] | None = None
        self._expired_task = False
        self._restart_required = False
        self._reload_error: str | None = None

    # --- состояние -------------------------------------------------------------

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def active(self) -> bool:
        """Бот запускается, работает или останавливается."""
        return self.status in ACTIVE

    @property
    def me(self) -> User | None:
        with self._lock:
            return self._me

    @property
    def watcher(self) -> DealWatcher | None:
        with self._lock:
            return self._watcher

    def snapshot(self) -> dict[str, Any]:
        """Блок `bot` для get_overview.

        problem — бот запущен, но продажи не обрабатываются (токен, связь, state.json);
        restart_required — в config.toml есть изменения, которые бот возьмёт после перезапуска.
        """
        with self._lock:
            uptime = (
                int(time.monotonic() - self._started_mono)
                if self._started_mono is not None
                else None
            )
            running = self._status == RUNNING
            watcher = self._watcher if running else None
            last_poll = getattr(watcher, "last_poll_at", None)
            return {
                "status": self._status,
                "error": self._error,
                "started_at": (
                    self._started_at.isoformat(timespec="seconds") if self._started_at else None
                ),
                "uptime_sec": uptime,
                "restart_required": running and self._restart_required,
                "problem": getattr(watcher, "problem", None),
                "last_poll_at": (
                    last_poll.isoformat(timespec="seconds")
                    if isinstance(last_poll, datetime)
                    else None
                ),
            }

    def account(self) -> dict[str, Any] | None:
        """Блок `account` для get_overview: известен после входа в аккаунт."""
        with self._lock:
            me, balance_at = self._me, self._balance_at
        if me is None:
            return None
        balance = me.balance.available if me.balance else 0.0
        return {
            "id": me.id,
            "username": me.username,
            "balance": float(balance),
            "balance_at": balance_at.isoformat(timespec="seconds") if balance_at else None,
        }

    # --- управление ----------------------------------------------------------

    def start(self, config: Config, *, reload: Callable[[], Config] | None = None) -> str:
        """Запустить бота в фоне. Ошибка, если он уже работает.

        `reload` перечитывает настройки: пока бот работает, правила автовыдачи
        и перевыставления из config.toml применяются без перезапуска.
        """
        with self._lock:
            if self._status in ACTIVE:
                raise ApiError(
                    "Бот ещё останавливается, подождите пару секунд."
                    if self._status == STOPPING
                    else "Бот уже запущен."
                )
            if self._process_lock is not None and not self._process_lock.acquire():
                raise ApiError(
                    "Бот с этой рабочей папкой уже запущен в другом окне SupplierHub. "
                    "Два бота выдали бы товар дважды."
                )
            loop: asyncio.AbstractEventLoop | None = None
            try:
                loop = asyncio.new_event_loop()
                task = loop.create_task(self._main(config))
                thread = threading.Thread(
                    target=self._thread_main,
                    args=(loop, task),
                    name="supplierhub-bot",
                    daemon=True,
                )
                self._thread, self._loop, self._task = thread, loop, task
                # Статус — до старта потока: быстрый поток не должен его перетереть.
                self._status, self._error = STARTING, None
                self._started_at = self._started_mono = None
                self._stop_requested = False
                self._live, self._reload = config, reload
                self._expired_task = config.relist.expired
                self._restart_required = False
                self._reload_error = None
                thread.start()
            except BaseException:
                self._status = STOPPED
                self._thread = self._loop = self._task = None
                if loop is not None:
                    loop.close()
                if self._process_lock is not None:
                    self._process_lock.release()
                raise
        logger.info("Запускаю бота…")
        return STARTING

    def stop(self, timeout: float = 10.0) -> str:
        """Остановить бота и дождаться потока (не дольше timeout секунд)."""
        with self._lock:
            thread, loop, task = self._thread, self._loop, self._task
            if thread is None or not thread.is_alive():
                return self._status
            self._stop_requested = True
            self._status = STOPPING
        logger.info("Останавливаю бота…")
        if loop is not None and task is not None:
            # RuntimeError — цикл уже закрыт, поток вот-вот завершится.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("Бот не остановился за %g с — дождусь его в фоне", timeout)
        return self.status

    def restart(
        self,
        config: Config,
        timeout: float = 10.0,
        *,
        reload: Callable[[], Config] | None = None,
    ) -> str:
        self.stop(timeout)
        if self.active:
            raise ApiError("Бот ещё останавливается, попробуйте через пару секунд.")
        return self.start(config, reload=reload)

    def sync_config(self, timeout: float = 5.0) -> bool:
        """Сразу применить config.toml к работающему боту.

        True — бот работает с актуальными настройками (или не запущен),
        False — для части изменений нужен перезапуск.
        """
        with self._lock:
            loop = self._loop if self._status in (STARTING, RUNNING) else None
        if loop is None:
            return True
        with contextlib.suppress(ApiError):
            self.call(self._sync_now(), timeout=timeout)
        with self._lock:
            return not self._restart_required

    def call(self, coro: Coroutine[Any, Any, _T], timeout: float = 30.0) -> _T:
        """Выполнить корутину в цикле бота и дождаться результата."""
        with self._lock:
            loop = self._loop if self._status in (STARTING, RUNNING) else None
        if loop is None:
            coro.close()
            raise ApiError("Бот не запущен.")
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            raise ApiError("Бот останавливается, попробуйте ещё раз.") from None
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise ApiError("Бот не ответил вовремя, попробуйте ещё раз.") from None
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            raise ApiError("Бот остановился, не закончив действие.") from None

    # --- поток бота ------------------------------------------------------------

    async def _main(self, config: Config) -> None:
        notifier = FeedNotifier(config.telegram, self._feed)
        upkeep = asyncio.ensure_future(self._upkeep())
        try:
            await bot_core.run(
                config, notifier=notifier, on_started=self._on_started, watch_chats=True
            )
        finally:
            upkeep.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await upkeep
            # run() закрывает уведомитель сам, но не если упал до входа в аккаунт.
            await notifier.aclose()

    async def _upkeep(self) -> None:
        """Фоновые дела рядом с ботом: новые правила из config.toml и свежий баланс."""
        balance_due = time.monotonic() + BALANCE_INTERVAL
        while True:
            await asyncio.sleep(CONFIG_CHECK_INTERVAL)
            await self._sync_now()
            if time.monotonic() >= balance_due:
                balance_due = time.monotonic() + BALANCE_INTERVAL
                await self._refresh_balance()

    async def _sync_now(self) -> None:
        """Сверить config.toml с работающим ботом (в цикле бота)."""
        with self._lock:
            live, reload = self._live, self._reload
        if live is None or reload is None:
            return
        try:
            fresh = reload()
        except (ConfigError, ApiError, OSError) as exc:
            text = str(exc)
            if text != self._reload_error:
                self._reload_error = text
                logger.warning("Новые настройки не применены, бот работает со старыми: %s", text)
            return
        self._reload_error = None
        if apply_live(live, fresh):
            logger.info("Правила автовыдачи и перевыставления обновлены без перезапуска бота")
        restart = needs_restart(live, fresh, expired_task=self._expired_task)
        with self._lock:
            if restart and not self._restart_required:
                logger.info("Часть новых настроек заработает после перезапуска бота")
            self._restart_required = restart

    async def _refresh_balance(self) -> None:
        watcher = self.watcher
        refresh = getattr(watcher, "refresh_me", None)
        if refresh is None or self.status != RUNNING:
            return
        try:
            me = await refresh()
        except Exception as exc:
            logger.debug("Не удалось обновить баланс: %s", exc)
            return
        with self._lock:
            self._me, self._balance_at = me, datetime.now(UTC)

    def _on_started(self, me: User, watcher: DealWatcher) -> None:
        with self._lock:
            self._me, self._watcher = me, watcher
            self._balance_at = datetime.now(UTC)
            if self._status == STARTING:
                self._status = RUNNING
                self._started_at = datetime.now().astimezone()
                self._started_mono = time.monotonic()

    def _thread_main(self, loop: asyncio.AbstractEventLoop, task: asyncio.Task[None]) -> None:
        asyncio.set_event_loop(loop)
        status, error = STOPPED, None
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except (BotError, StateError) as exc:
            status, error = ERROR, str(exc)
            logger.error("%s", exc)
        except Exception as exc:
            status, error = ERROR, f"Неожиданная ошибка: {exc}"
            logger.error("Бот остановился из-за ошибки", exc_info=exc)
        finally:
            _close_loop(loop)
            if self._process_lock is not None:
                self._process_lock.release()
            with self._lock:
                if self._stop_requested and status == ERROR:
                    logger.info("Ошибка при остановке бота: %s", error)
                    status, error = STOPPED, None
                if self._thread is threading.current_thread():
                    self._status, self._error = status, error
                    self._started_at = self._started_mono = None
                    self._loop = self._task = None
                    self._live = self._reload = None
                    self._restart_required = False
        if status == STOPPED:
            logger.info("Бот остановлен")


def _close_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Доделать хвосты (задачи, async-генераторы) и закрыть цикл."""
    try:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    except Exception:
        logger.debug("Сбой при закрытии цикла бота", exc_info=True)
    finally:
        asyncio.set_event_loop(None)
        loop.close()
