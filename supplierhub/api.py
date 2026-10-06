"""Методы, которые вызывает окно (pywebview js_api и HTTP-мост).

Каждый публичный метод возвращает конверт `{"ok": true, "data": ...}` или
`{"ok": false, "error": "текст по-русски"}` и никогда не бросает исключений.

Окну доступны только методы из `endpoint_names()`: HTTP-мост проверяет имя
по этому списку, а окну pywebview передаются только эти связанные методы
(`window.expose`), а не сам объект Api — иначе страница могла бы вызвать
любой атрибут по пути вида `_config._write`.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from playerok_bot.bot import BotError
from playerok_bot.config import ConfigError
from playerok_bot.delivery import DeliveryError, mark_manual
from playerok_bot.lock import state_lock
from playerok_bot.stock import StockError
from playerok_bot.storage import State, StateError

from . import __version__, checks, desktop
from .configio import STOCK_DIR, ConfigStore, coerce_section, config_view
from .errors import ApiError
from .feed import EventFeed, LogBuffer
from .runtime import RUNNING, BotRuntime
from .stats import HistoryReader, build_sales, compute_stats, sale_delivery

__all__ = ["LOCK_NAME", "Api", "endpoint_names"]

logger = logging.getLogger(__name__)

_USER_ERRORS = (ApiError, ConfigError, BotError, StateError, StockError, DeliveryError)
_F = TypeVar("_F", bound=Callable[..., Any])
LOCK_NAME = ".supplierhub.lock"
#: Через сколько секунд после ответа quit_app закрывать приложение.
QUIT_DELAY = 0.5
#: Какие файлы журнала можно открыть программой по умолчанию.
_LOG_SUFFIXES = (".log", ".txt")


def _endpoint(func: _F) -> _F:
    """Обернуть метод в конверт {ok, data|error}; исключения не выходят наружу."""
    signature = inspect.signature(func)

    @functools.wraps(func)
    def wrapper(self: Api, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            signature.bind(self, *args, **kwargs)
        except TypeError:
            return {"ok": False, "error": f"Неверные аргументы для {func.__name__}."}
        try:
            return {"ok": True, "data": func(self, *args, **kwargs)}
        except _USER_ERRORS as exc:
            return {"ok": False, "error": str(exc) or "Ошибка"}
        except UnicodeDecodeError:
            return {
                "ok": False,
                "error": "Файл не в кодировке UTF-8 — пересохраните его в UTF-8 и повторите.",
            }
        except OSError as exc:
            logger.warning("%s: ошибка файла: %s", func.__name__, exc)
            where = f" ({exc.filename})" if exc.filename else ""
            return {"ok": False, "error": f"Ошибка доступа к файлу{where}: {exc.strerror or exc}"}
        except Exception as exc:
            logger.exception("Сбой в %s", func.__name__)
            return {"ok": False, "error": f"Внутренняя ошибка: {exc}"}

    wrapper._sh_endpoint = True  # type: ignore[attr-defined]
    return wrapper  # type: ignore[return-value]


def endpoint_names() -> frozenset[str]:
    """Имена методов Api, доступных окну."""
    return frozenset(
        name
        for name, member in vars(Api).items()
        if not name.startswith("_") and getattr(member, "_sh_endpoint", False)
    )


class Api:
    """API окна. Методы вызываются из рабочих потоков pywebview/HTTP-сервера."""

    def __init__(
        self,
        workdir: Path,
        *,
        mode: str = "window",
        feed: EventFeed | None = None,
        logs: LogBuffer | None = None,
        runtime: BotRuntime | None = None,
        open_path: Callable[[Path], None] | None = None,
        open_url: Callable[[str], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self._workdir = workdir
        self._mode = mode
        self._config = ConfigStore(workdir)
        self._feed = feed if feed is not None else EventFeed()
        self._logs = logs if logs is not None else LogBuffer()
        self._runtime = (
            runtime
            if runtime is not None
            else BotRuntime(self._feed, lock_path=workdir / LOCK_NAME)
        )
        self._history = HistoryReader()
        self._open_path = open_path or desktop.open_in_system
        self._open_url = open_url or desktop.open_in_browser
        self._on_quit = on_quit

    def _set_mode(self, mode: str) -> None:
        self._mode = mode

    def _set_quit(self, on_quit: Callable[[], None] | None) -> None:
        """Как закрыть приложение по кнопке «Выйти» (задаёт app для своего режима)."""
        self._on_quit = on_quit

    def _start_bot(self, *, restart: bool = False) -> str:
        config = self._config.load()
        if restart:
            return self._runtime.restart(config, reload=self._config.load)
        return self._runtime.start(config, reload=self._config.load)

    def _restart_required(self) -> bool:
        """Применить изменения к работающему боту; True — нужен перезапуск."""
        return self._runtime.active and not self._runtime.sync_config()

    def _shutdown(self) -> None:
        """Остановить бота при закрытии приложения."""
        self._runtime.stop()

    # --- общее -----------------------------------------------------------------

    @_endpoint
    def get_app_info(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "workdir": str(self._workdir),
            "config_exists": self._config.exists(),
            "mode": self._mode,
        }

    @_endpoint
    def get_overview(self) -> dict[str, Any]:
        exists = self._config.exists()
        raw, read_error = self._config.read_raw_safe()
        view = config_view(raw, exists=exists)
        rules = self._config.rules(raw)
        history = self._history.read(self._config.state_file(raw))
        watcher = self._runtime.watcher
        recent = list(watcher.recent) if watcher is not None else None
        sales = build_sales(recent, history, limit=None)

        stock_total = 0
        seen: set[Path] = set()
        low_stock = []
        for rule in rules:
            if rule["stock_file"] is None or rule["stock_count"] is None:
                continue
            path = (self._workdir / rule["stock_file"]).resolve()
            if path not in seen:
                seen.add(path)
                stock_total += rule["stock_count"]
            if rule["stock_count"] <= rule["low_stock_alert"]:
                low_stock.append(
                    {
                        "rule_id": rule["id"],
                        "match": rule["match"],
                        "count": rule["stock_count"],
                        "threshold": rule["low_stock_alert"],
                    }
                )

        telegram = view["telegram"]
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN") or telegram["bot_token"]
        return {
            "bot": self._runtime.snapshot(),
            "account": self._runtime.account(),
            "stats": compute_stats(sales, history, stock_total=stock_total),
            "low_stock": low_stock,
            "features": {
                "delivery": view["delivery"]["enabled"],
                "telegram": telegram["enabled"],
                "relist_after_sale": view["relist"]["after_sale"],
                "relist_expired": view["relist"]["expired"],
            },
            "setup": {
                "token": bool(self._config.token(raw)),
                "telegram": bool(telegram["enabled"] and tg_token and telegram["chat_ids"]),
                "rules": bool(rules),
            },
            "config_error": read_error or (self._config.validation_error() if exists else None),
        }

    @_endpoint
    def get_feed(self, after_id: Any = 0) -> dict[str, Any]:
        return self._feed.since(_cursor(after_id))

    @_endpoint
    def get_logs(self, after_id: Any = 0) -> dict[str, Any]:
        return self._logs.since(_cursor(after_id))

    # --- бот -----------------------------------------------------------------

    @_endpoint
    def start_bot(self) -> dict[str, Any]:
        if self._runtime.active:
            raise ApiError("Бот уже запущен.")
        return {"status": self._start_bot()}

    @_endpoint
    def stop_bot(self) -> dict[str, Any]:
        return {"status": self._runtime.stop()}

    @_endpoint
    def restart_bot(self) -> dict[str, Any]:
        # Сначала проверяем настройки: с битым конфигом бот не должен остаться выключенным.
        return {"status": self._start_bot(restart=True)}

    @_endpoint
    def get_sales(self) -> dict[str, Any]:
        raw, _ = self._config.read_raw_safe()
        history = self._history.read(self._config.state_file(raw))
        watcher = self._runtime.watcher
        recent = list(watcher.recent) if watcher is not None else None
        return {
            "items": build_sales(recent, history, has_rule=self._rule_matcher(raw)),
            "live": self._runtime.status == RUNNING and watcher is not None,
        }

    @_endpoint
    def mark_handled(self, deal_id: Any) -> dict[str, Any]:
        """Продавец сам выдал товар или решил вопрос: сделка уходит из «Требуют внимания»,
        и бот больше никогда не выдаёт по ней товар."""
        deal_id = _deal_id(deal_id)
        watcher = self._runtime.watcher
        if self._runtime.status == RUNNING and hasattr(watcher, "mark_handled"):
            # Бот держит состояние в памяти и перезаписал бы файл — меняем через него.
            entry = self._runtime.call(watcher.mark_handled(deal_id))
        elif self._runtime.active:
            raise ApiError("Бот запускается или останавливается — повторите через пару секунд.")
        else:
            entry = self._mark_handled_offline(deal_id)
        return {"deal_id": deal_id, "delivery": sale_delivery(entry)}

    @_endpoint
    def retry_delivery(self, deal_id: Any) -> dict[str, Any]:
        """Выдать заново: «Без автовыдачи» (правило уже создано) или «Ошибка выдачи»."""
        deal_id = _deal_id(deal_id)
        watcher = self._runtime.watcher
        if self._runtime.status != RUNNING or not hasattr(watcher, "retry_delivery"):
            raise ApiError("Запустите бота — выдать товар может только работающий бот.")
        status = self._runtime.call(watcher.retry_delivery(deal_id), timeout=60.0)
        return {"deal_id": deal_id, "status": status}

    # --- приложение ------------------------------------------------------------

    @_endpoint
    def quit_app(self) -> dict[str, Any]:
        """Остановить бота и закрыть SupplierHub (ответ уходит до закрытия)."""
        self._runtime.stop()
        on_quit = self._on_quit
        if on_quit is not None:
            timer = threading.Timer(QUIT_DELAY, on_quit)
            timer.daemon = True
            timer.start()
        return {}

    # --- настройки -------------------------------------------------------------

    @_endpoint
    def get_config(self) -> dict[str, Any]:
        return self._config.view()

    @_endpoint
    def save_config(self, cfg: Any) -> dict[str, Any]:
        self._config.save_settings(cfg)
        return {"saved": True, "restart_required": self._restart_required()}

    # --- правила автовыдачи -------------------------------------------------------
    # `expected` — правило, каким его видело окно (match, stock_file): защита от
    # правки не того правила, если список успел измениться.

    @_endpoint
    def list_rules(self) -> dict[str, Any]:
        return {"items": self._config.rules()}

    @_endpoint
    def save_rule(self, rule: Any, rule_id: Any = None, expected: Any = None) -> dict[str, Any]:
        item, reused = self._config.save_rule_ex(rule, rule_id, expected)
        return {
            "item": item,
            "restart_required": self._restart_required(),
            "reused_stock": reused,
        }

    @_endpoint
    def delete_rule(self, rule_id: Any, expected: Any = None) -> dict[str, Any]:
        self._config.delete_rule(rule_id, expected)
        return {"deleted": True, "restart_required": self._restart_required()}

    @_endpoint
    def move_rule(self, rule_id: Any, direction: Any, expected: Any = None) -> dict[str, Any]:
        items = self._config.move_rule(rule_id, direction, expected)
        return {"items": items, "restart_required": self._restart_required()}

    # --- склад -----------------------------------------------------------------

    @_endpoint
    def get_stock(self, rule_id: Any, expected: Any = None) -> dict[str, Any]:
        stock, name = self._config.stock_for(rule_id, expected)
        items = stock.items()
        return {"rule_id": int(rule_id), "file": name, "items": items, "count": len(items)}

    @_endpoint
    def add_stock(self, rule_id: Any, text: Any, expected: Any = None) -> dict[str, Any]:
        if not isinstance(text, str):
            raise ApiError("Ожидался текст: один товар на строку.")
        stock, _ = self._config.stock_for(rule_id, expected)
        added = stock.add(text.splitlines())
        return {"added": added, "count": stock.count()}

    @_endpoint
    def remove_stock_item(
        self, rule_id: Any, index: Any, value: Any = None, expected: Any = None
    ) -> dict[str, Any]:
        if isinstance(index, bool) or not isinstance(index, int | float):
            raise ApiError("Некорректный номер товара.")
        if value is not None and not isinstance(value, str):
            raise ApiError("Некорректный товар.")
        stock, _ = self._config.stock_for(rule_id, expected)
        removed = stock.remove(int(index), expected=value)
        return {"removed": removed, "count": stock.count()}

    @_endpoint
    def clear_stock(self, rule_id: Any, expected: Any = None) -> dict[str, Any]:
        stock, _ = self._config.stock_for(rule_id, expected)
        return {"removed": stock.clear()}

    # --- проверки ----------------------------------------------------------------

    @_endpoint
    def check_token(self, token: Any = None) -> dict[str, Any]:
        if token is not None and not isinstance(token, str):
            raise ApiError("Токен должен быть строкой.")
        token = (token or "").strip() or self._config.token()
        if not token:
            raise ApiError("Токен не задан — вставьте cookie token с playerok.com.")
        proxy = self._config.view()["playerok"]["proxy"] or None
        return asyncio.run(checks.fetch_account(token, proxy, timeout=checks.TIMEOUT))

    @_endpoint
    def test_telegram(self, settings: Any = None) -> dict[str, Any]:
        if settings is not None and not isinstance(settings, dict):
            raise ApiError("Некорректные настройки Telegram.")
        saved = self._config.view()["telegram"]
        if not settings:
            saved["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN") or saved["bot_token"]
        merged = {**saved, **(settings or {})}
        # Остальные поля (enabled, интервалы) для проверки связи не нужны.
        parsed = coerce_section(
            "telegram", {key: merged.get(key) for key in ("bot_token", "chat_ids", "proxy")}
        )
        if not parsed.get("bot_token"):
            raise ApiError("Укажите токен Telegram-бота — его выдаёт @BotFather.")
        if not parsed.get("chat_ids"):
            raise ApiError("Добавьте хотя бы один chat id — узнать его можно у @userinfobot.")
        asyncio.run(
            checks.send_telegram_test(
                parsed["bot_token"], parsed["chat_ids"], parsed.get("proxy") or None
            )
        )
        return {"sent": True}

    # --- система -------------------------------------------------------------------

    @_endpoint
    def open_path(self, which: Any) -> dict[str, Any]:
        if which == "workdir":
            target = self._workdir
        elif which == "stock":
            target = self._workdir / STOCK_DIR
            target.mkdir(parents=True, exist_ok=True)
        elif which == "log":
            log_file = self._config.log_file()
            if log_file is None:
                raise ApiError("Запись журнала в файл отключена ([bot] log_file в config.toml).")
            if not safe_log_file(self._workdir, log_file):
                # Открыть «программой по умолчанию» можно что угодно, в том числе .bat —
                # поэтому только .log/.txt внутри рабочей папки.
                raise ApiError(
                    "Журнал записывается в файл вне папки SupplierHub или с необычным "
                    f"расширением ({log_file}) — откройте его вручную."
                )
            if not log_file.is_file():
                raise ApiError("Файл журнала ещё не создан.")
            target = log_file
        else:
            raise ApiError("Неизвестная папка.")
        self._open_path(target)
        return {}

    @_endpoint
    def open_url(self, url: Any) -> dict[str, Any]:
        self._open_url(desktop.check_https_url(url))
        return {}

    # --- внутреннее ------------------------------------------------------------------

    def _rule_matcher(self, raw: dict[str, Any]) -> Callable[[str | None], bool]:
        """Есть ли сейчас правило автовыдачи для лота с таким названием."""
        enabled = config_view(raw, exists=self._config.exists())["delivery"]["enabled"]
        matches = [
            rule["match"].strip().casefold()
            for rule in self._config.rules(raw)
            if rule["match"].strip()
        ]

        def has_rule(name: str | None) -> bool:
            folded = (name or "").casefold()
            return enabled and bool(folded) and any(match in folded for match in matches)

        return has_rule

    def _mark_handled_offline(self, deal_id: str) -> dict[str, Any]:
        """Бот остановлен: отметить в state.json напрямую, не мешая другому боту."""
        path = self._config.state_file()
        lock = state_lock(path)
        if not lock.acquire():
            raise ApiError(
                "Бот с этой папкой работает в другом окне SupplierHub — отметьте сделку там."
            )
        try:
            state = State.load(path)
            if state.fresh:
                raise ApiError("Сделка не найдена — обновите страницу.")
            return mark_manual(state, deal_id)
        finally:
            lock.release()


def safe_log_file(workdir: Path, log_file: Path) -> bool:
    """Журнал — обычный .log/.txt внутри рабочей папки (его можно открыть системой)."""
    try:
        root = workdir.resolve()
        path = log_file.resolve()
    except (OSError, ValueError):
        return False
    return path.is_relative_to(root) and path.suffix.casefold() in _LOG_SUFFIXES


def _deal_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ApiError("Некорректный номер сделки.")
    return value.strip()


def _cursor(value: Any) -> int:
    """Номер последней полученной записи из окна; всё странное — с начала."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return max(int(value), 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0
