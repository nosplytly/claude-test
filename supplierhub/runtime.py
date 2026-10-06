"""Бот в фоновом потоке со своим циклом asyncio и машиной состояний."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PlayerokAPI import User

from playerok_bot import bot as bot_core
from playerok_bot.bot import BotError
from playerok_bot.config import Config
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
        self._watcher: DealWatcher | None = None

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
        """Блок `bot` для get_overview."""
        with self._lock:
            uptime = (
                int(time.monotonic() - self._started_mono)
                if self._started_mono is not None
                else None
            )
            return {
                "status": self._status,
                "error": self._error,
                "started_at": (
                    self._started_at.isoformat(timespec="seconds") if self._started_at else None
                ),
                "uptime_sec": uptime,
            }

    def account(self) -> dict[str, Any] | None:
        """Блок `account` для get_overview: известен после входа в аккаунт."""
        me = self.me
        if me is None:
            return None
        balance = me.balance.available if me.balance else 0.0
        return {"id": me.id, "username": me.username, "balance": float(balance)}

    # --- управление ----------------------------------------------------------

    def start(self, config: Config) -> str:
        """Запустить бота в фоне. Ошибка, если он уже работает."""
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

    def restart(self, config: Config, timeout: float = 10.0) -> str:
        self.stop(timeout)
        if self.active:
            raise ApiError("Бот ещё останавливается, попробуйте через пару секунд.")
        return self.start(config)

    # --- поток бота ------------------------------------------------------------

    async def _main(self, config: Config) -> None:
        notifier = FeedNotifier(config.telegram, self._feed)
        try:
            await bot_core.run(
                config, notifier=notifier, on_started=self._on_started, watch_chats=True
            )
        finally:
            # run() закрывает уведомитель сам, но не если упал до входа в аккаунт.
            await notifier.aclose()

    def _on_started(self, me: User, watcher: DealWatcher) -> None:
        with self._lock:
            self._me, self._watcher = me, watcher
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
