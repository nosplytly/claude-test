"""Методы, которые вызывает окно (pywebview js_api и HTTP-мост).

Каждый публичный метод возвращает конверт `{"ok": true, "data": ...}` или
`{"ok": false, "error": "текст по-русски"}` и никогда не бросает исключений.
Всё служебное начинается с `_`: pywebview отдаёт окну только публичное.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from playerok_bot.bot import BotError
from playerok_bot.config import ConfigError
from playerok_bot.storage import StateError

from . import __version__, checks, desktop
from .configio import STOCK_DIR, ConfigStore, coerce_section, config_view
from .errors import ApiError
from .feed import EventFeed, LogBuffer
from .runtime import RUNNING, BotRuntime
from .stats import HistoryReader, build_sales, compute_stats

__all__ = ["LOCK_NAME", "Api", "endpoint_names"]

logger = logging.getLogger(__name__)

_USER_ERRORS = (ApiError, ConfigError, BotError, StateError)
_F = TypeVar("_F", bound=Callable[..., Any])
LOCK_NAME = ".supplierhub.lock"


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

    def _set_mode(self, mode: str) -> None:
        self._mode = mode

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
        sales = build_sales(list(watcher.recent) if watcher is not None else None, history)

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
        return {"status": self._runtime.start(self._config.load())}

    @_endpoint
    def stop_bot(self) -> dict[str, Any]:
        return {"status": self._runtime.stop()}

    @_endpoint
    def restart_bot(self) -> dict[str, Any]:
        # Сначала проверяем настройки: с битым конфигом бот не должен остаться выключенным.
        config = self._config.load()
        return {"status": self._runtime.restart(config)}

    @_endpoint
    def get_sales(self) -> dict[str, Any]:
        history = self._history.read(self._config.state_file())
        watcher = self._runtime.watcher
        recent = list(watcher.recent) if watcher is not None else None
        return {
            "items": build_sales(recent, history),
            "live": self._runtime.status == RUNNING and watcher is not None,
        }

    # --- настройки -------------------------------------------------------------

    @_endpoint
    def get_config(self) -> dict[str, Any]:
        return self._config.view()

    @_endpoint
    def save_config(self, cfg: Any) -> dict[str, Any]:
        self._config.save_settings(cfg)
        return {"saved": True, "restart_required": self._runtime.active}

    # --- правила автовыдачи -------------------------------------------------------

    @_endpoint
    def list_rules(self) -> dict[str, Any]:
        return {"items": self._config.rules()}

    @_endpoint
    def save_rule(self, rule: Any, rule_id: Any = None) -> dict[str, Any]:
        item = self._config.save_rule(rule, rule_id)
        return {"item": item, "restart_required": self._runtime.active}

    @_endpoint
    def delete_rule(self, rule_id: Any) -> dict[str, Any]:
        self._config.delete_rule(rule_id)
        return {"deleted": True, "restart_required": self._runtime.active}

    @_endpoint
    def move_rule(self, rule_id: Any, direction: Any) -> dict[str, Any]:
        items = self._config.move_rule(rule_id, direction)
        return {"items": items, "restart_required": self._runtime.active}

    # --- склад -----------------------------------------------------------------

    @_endpoint
    def get_stock(self, rule_id: Any) -> dict[str, Any]:
        stock, name = self._config.stock_for(rule_id)
        items = stock.items()
        return {"rule_id": int(rule_id), "file": name, "items": items, "count": len(items)}

    @_endpoint
    def add_stock(self, rule_id: Any, text: Any) -> dict[str, Any]:
        if not isinstance(text, str):
            raise ApiError("Ожидался текст: один товар на строку.")
        stock, _ = self._config.stock_for(rule_id)
        added = stock.add(text.splitlines())
        return {"added": added, "count": stock.count()}

    @_endpoint
    def remove_stock_item(self, rule_id: Any, index: Any, value: Any = None) -> dict[str, Any]:
        if isinstance(index, bool) or not isinstance(index, int | float):
            raise ApiError("Некорректный номер товара.")
        if value is not None and not isinstance(value, str):
            raise ApiError("Некорректный товар.")
        stock, _ = self._config.stock_for(rule_id)
        removed = stock.remove(int(index), expected=value)
        return {"removed": removed, "count": stock.count()}

    @_endpoint
    def clear_stock(self, rule_id: Any) -> dict[str, Any]:
        stock, _ = self._config.stock_for(rule_id)
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


def _cursor(value: Any) -> int:
    """Номер последней полученной записи из окна; всё странное — с начала."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return max(int(value), 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0
