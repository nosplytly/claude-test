"""Загрузка и проверка config.toml."""

from __future__ import annotations

import importlib.util
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

__all__ = [
    "Config",
    "ConfigError",
    "DeliveryConfig",
    "DeliveryRule",
    "PlayerokConfig",
    "RelistConfig",
    "TelegramConfig",
    "load_config",
    "parse_toml",
]


class ConfigError(Exception):
    """Ошибка в конфиге — сообщение показывается пользователю как есть."""


@dataclass
class PlayerokConfig:
    token: str
    proxy: str | None = None
    deals_interval: float = 20.0
    use_websocket: bool = True


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_ids: list[int] = field(default_factory=list)
    notify_messages: bool = True
    messages_interval: float = 10.0
    proxy: str | None = None


@dataclass
class DeliveryRule:
    match: str
    message: str
    stock_file: Path | None = None
    products_per_sale: int = 1
    low_stock_alert: int = 0

    def matches(self, item_name: str) -> bool:
        return self.match.casefold() in item_name.casefold()


@dataclass
class DeliveryConfig:
    enabled: bool = False
    mark_sent: bool = True
    rules: list[DeliveryRule] = field(default_factory=list)

    def find_rule(self, item_name: str | None) -> DeliveryRule | None:
        if not item_name:
            return None
        return next((rule for rule in self.rules if rule.matches(item_name)), None)


@dataclass
class RelistConfig:
    after_sale: bool = False
    expired: bool = False
    interval_minutes: float = 30.0
    match: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.after_sale or self.expired

    def matches(self, item_name: str | None) -> bool:
        if not self.match:
            return True
        name = (item_name or "").casefold()
        return any(part.casefold() in name for part in self.match)


@dataclass
class Config:
    playerok: PlayerokConfig
    telegram: TelegramConfig
    delivery: DeliveryConfig
    relist: RelistConfig
    state_file: Path
    log_file: Path | None


def load_config(path: str | os.PathLike[str]) -> Config:
    """Прочитать конфиг; относительные пути считаются от папки конфига."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"Не найден файл настроек {path}. Скопируйте config.example.toml в config.toml "
            "и заполните его."
        )
    raw = parse_toml(path.read_bytes(), str(path))

    base = path.resolve().parent
    bot = _table(raw, "bot")
    return Config(
        playerok=_playerok(_table(raw, "playerok")),
        telegram=_telegram(_table(raw, "telegram")),
        delivery=_delivery(_table(raw, "delivery"), base),
        relist=_relist(_table(raw, "relist")),
        state_file=base / _str(bot, "bot.state_file", "state.json"),
        log_file=(base / log) if (log := _str(bot, "bot.log_file", "bot.log")) else None,
    )


def parse_toml(data: bytes, name: str) -> dict[str, Any]:
    """Разобрать TOML; BOM от Блокнота («UTF-8 с BOM») не мешает."""
    try:
        text = data.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{name} должен быть в кодировке UTF-8 — пересохраните файл.") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Ошибка синтаксиса в {name}: {exc}") from exc


def _playerok(raw: dict[str, Any]) -> PlayerokConfig:
    token = os.environ.get("PLAYEROK_TOKEN") or _str(raw, "playerok.token", "")
    if not token:
        raise ConfigError(
            "Не задан токен Playerok: заполните playerok.token в конфиге "
            "или переменную окружения PLAYEROK_TOKEN."
        )
    return PlayerokConfig(
        token=token,
        proxy=_proxy(raw, "playerok.proxy"),
        deals_interval=_positive(raw, "playerok.deals_interval", 20.0, minimum=5.0),
        use_websocket=_bool(raw, "playerok.use_websocket", True),
    )


def _telegram(raw: dict[str, Any]) -> TelegramConfig:
    enabled = _bool(raw, "telegram.enabled", False)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or _str(raw, "telegram.bot_token", "")
    chat_ids = raw.get("chat_ids", [])
    if not isinstance(chat_ids, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in chat_ids
    ):
        raise ConfigError("telegram.chat_ids должен быть списком чисел, например [123456789]")
    if enabled and not bot_token:
        raise ConfigError(
            "Telegram включён, но не задан telegram.bot_token "
            "(или переменная окружения TELEGRAM_BOT_TOKEN)."
        )
    if enabled and not chat_ids:
        raise ConfigError("Telegram включён, но список telegram.chat_ids пуст.")
    return TelegramConfig(
        enabled=enabled,
        bot_token=bot_token,
        chat_ids=list(chat_ids),
        notify_messages=_bool(raw, "telegram.notify_messages", True),
        messages_interval=_positive(raw, "telegram.messages_interval", 10.0, minimum=3.0),
        proxy=_proxy(raw, "telegram.proxy"),
    )


def _delivery(raw: dict[str, Any], base: Path) -> DeliveryConfig:
    rules_raw = raw.get("rules", [])
    if not isinstance(rules_raw, list):
        raise ConfigError("delivery.rules должен быть массивом таблиц [[delivery.rules]]")
    rules = [_rule(item, index, base) for index, item in enumerate(rules_raw, start=1)]
    return DeliveryConfig(
        enabled=_bool(raw, "delivery.enabled", False),
        mark_sent=_bool(raw, "delivery.mark_sent", True),
        rules=rules,
    )


def _rule(raw: Any, index: int, base: Path) -> DeliveryRule:
    where = f"delivery.rules №{index}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: ожидалась таблица")
    match = _str(raw, f"{where}.match", "").strip()
    message = _str(raw, f"{where}.message", "")
    if not match:
        raise ConfigError(f"{where}: не задано match — часть названия лота")
    if not message.strip():
        raise ConfigError(f"{where}: не задан текст message")
    stock = _str(raw, f"{where}.stock_file", "")
    if stock and "{product}" not in message:
        raise ConfigError(
            f"{where}: указан stock_file, но в message нет {{product}} — "
            "покупатель не получил бы товар."
        )
    per_sale = raw.get("products_per_sale", 1)
    if not isinstance(per_sale, int) or isinstance(per_sale, bool) or per_sale < 1:
        raise ConfigError(f"{where}: products_per_sale должен быть целым числом ≥ 1")
    low = raw.get("low_stock_alert", 0)
    if not isinstance(low, int) or isinstance(low, bool) or low < 0:
        raise ConfigError(f"{where}: low_stock_alert должен быть целым числом ≥ 0")
    return DeliveryRule(
        match=match,
        message=message,
        stock_file=(base / stock) if stock else None,
        products_per_sale=per_sale,
        low_stock_alert=low,
    )


def _relist(raw: dict[str, Any]) -> RelistConfig:
    match = raw.get("match", [])
    if not isinstance(match, list) or not all(isinstance(item, str) for item in match):
        raise ConfigError('relist.match должен быть списком строк, например ["Ключ"]')
    return RelistConfig(
        after_sale=_bool(raw, "relist.after_sale", False),
        expired=_bool(raw, "relist.expired", False),
        interval_minutes=_positive(raw, "relist.interval_minutes", 30.0, minimum=1.0),
        match=[item for item in match if item.strip()],
    )


# --- примитивы -------------------------------------------------------------


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] должен быть таблицей")
    return value


def _str(raw: dict[str, Any], name: str, default: str) -> str:
    value = raw.get(name.rsplit(".", 1)[-1], default)
    if not isinstance(value, str):
        raise ConfigError(f"{name} должен быть строкой")
    return value


def _bool(raw: dict[str, Any], name: str, default: bool) -> bool:
    value = raw.get(name.rsplit(".", 1)[-1], default)
    if not isinstance(value, bool):
        raise ConfigError(f"{name} должен быть true или false")
    return value


def _proxy(raw: dict[str, Any], name: str) -> str | None:
    """Адрес прокси или None. Ошибку видно сразу при сохранении, а не при запуске бота."""
    value = _str(raw, name, "").strip()
    if not value:
        return None
    scheme = value.partition("://")[0].casefold() if "://" in value else ""
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ConfigError(
            f"{name}: адрес прокси должен начинаться с http://, https:// или socks5://, "
            "например http://user:pass@1.2.3.4:8080"
        )
    if scheme.startswith("socks") and importlib.util.find_spec("socksio") is None:
        raise ConfigError(
            f"{name}: SOCKS-прокси в этой сборке не поддерживается — укажите HTTP-прокси "
            "(адрес вида http://…)."
        )
    try:
        url = httpx.Proxy(value).url
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: неверный адрес прокси ({exc})") from None
    if not url.host:
        raise ConfigError(f"{name}: в адресе прокси нет сервера, например http://1.2.3.4:8080")
    return value


def _positive(raw: dict[str, Any], name: str, default: float, *, minimum: float) -> float:
    value = raw.get(name.rsplit(".", 1)[-1], default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < minimum:
        raise ConfigError(f"{name} должен быть числом не меньше {minimum:g}")
    return float(value)
