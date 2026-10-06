"""Настройки из окна: чтение, слияние и атомарная запись config.toml.

Файл правится частично: окно меняет только известные ему ключи, а правила
`[[delivery.rules]]`, раздел `[bot]` и незнакомые ключи остаются как были.
Перед записью итог проверяется тем же `load_config`, что и у бота.
"""

from __future__ import annotations

import copy
import math
import os
import re
import tempfile
import threading
import tomllib
from pathlib import Path, PureWindowsPath
from typing import Any

import tomli_w

from playerok_bot.config import Config, ConfigError, load_config
from playerok_bot.stock import Stock

from .errors import ApiError

__all__ = [
    "BOT_DEFAULTS",
    "CONFIG_NAME",
    "FILE_DEFAULTS",
    "NEW_CONFIG",
    "STOCK_DIR",
    "ConfigStore",
    "coerce_section",
    "config_view",
    "slugify",
]

CONFIG_NAME = "config.toml"
STOCK_DIR = "stock"
#: Подставляется вместо пустого токена при проверке: так можно сохранить,
#: например, Telegram ещё до того, как пользователь нашёл токен Playerok.
_PLACEHOLDER_TOKEN = "supplierhub-placeholder-token"

#: Настройки нового файла, когда config.toml ещё нет.
NEW_CONFIG: dict[str, dict[str, Any]] = {
    "playerok": {"token": "", "proxy": "", "deals_interval": 20, "use_websocket": True},
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_ids": [],
        "notify_messages": True,
        "messages_interval": 10,
        "proxy": "",
    },
    "delivery": {"enabled": True, "mark_sent": True},
    "relist": {"after_sale": True, "expired": False, "interval_minutes": 30, "match": []},
}

#: Чем бот считает ключи, которых нет в существующем файле (как в load_config).
FILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "playerok": {"token": "", "proxy": "", "deals_interval": 20, "use_websocket": True},
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_ids": [],
        "notify_messages": True,
        "messages_interval": 10,
        "proxy": "",
    },
    "delivery": {"enabled": False, "mark_sent": True},
    "relist": {"after_sale": False, "expired": False, "interval_minutes": 30, "match": []},
}

BOT_DEFAULTS: dict[str, str] = {"state_file": "state.json", "log_file": "bot.log"}

#: Какие ключи окно может менять и какого они типа.
_FIELDS: dict[str, dict[str, str]] = {
    "playerok": {
        "token": "str",
        "proxy": "str",
        "deals_interval": "number",
        "use_websocket": "bool",
    },
    "telegram": {
        "enabled": "bool",
        "bot_token": "str",
        "chat_ids": "chat_ids",
        "notify_messages": "bool",
        "messages_interval": "number",
        "proxy": "str",
    },
    "delivery": {"enabled": "bool", "mark_sent": "bool"},
    "relist": {
        "after_sale": "bool",
        "expired": "bool",
        "interval_minutes": "number",
        "match": "str_list",
    },
}

_HEADER = (
    "# Настройки SupplierHub (бот Playerok). Файл можно править и вручную.\n"
    "# Здесь хранится токен от аккаунта — никому не показывайте этот файл.\n\n"
)

_STOCK_OUTSIDE = (
    "Файл склада должен лежать в папке stock рядом с config.toml, например stock/keys.txt."
)

_CYRILLIC = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
_LATIN = "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch - y - e yu ya".split()  # noqa: SIM905
_TRANSLIT = str.maketrans(
    {char: "" if latin == "-" else latin for char, latin in zip(_CYRILLIC, _LATIN, strict=True)}
)


def slugify(text: str) -> str:
    """Имя файла склада из названия лота: «Ключ Steam» → «klyuch-steam»."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold().translate(_TRANSLIT)).strip("-")
    return slug[:40].strip("-") or "tovar"


def config_view(raw: dict[str, Any], *, exists: bool) -> dict[str, Any]:
    """Настройки в виде для окна (get_config): все ключи на месте, типы верные."""
    base = FILE_DEFAULTS if exists else NEW_CONFIG
    view: dict[str, Any] = {}
    for section, fields in _FIELDS.items():
        table = raw.get(section)
        table = table if isinstance(table, dict) else {}
        view[section] = {
            key: _view_value(kind, table.get(key), base[section][key])
            for key, kind in fields.items()
        }
    return view


def coerce_section(section: str, values: dict[str, Any]) -> dict[str, Any]:
    """Привести значения раздела из окна к типам конфига (ConfigError при ошибке)."""
    return {
        key: _coerce(kind, values[key], f"{section}.{key}")
        for key, kind in _FIELDS[section].items()
        if key in values
    }


class ConfigStore:
    """config.toml в рабочей папке. Все изменения — под одной блокировкой."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.path = workdir / CONFIG_NAME
        self.stock_root = workdir / STOCK_DIR
        self._lock = threading.RLock()
        self._cache: tuple[tuple[int, int, int], dict[str, Any]] | None = None
        self._check: tuple[tuple[int, int, int], str | None] | None = None

    # --- чтение ------------------------------------------------------------

    def exists(self) -> bool:
        return self.path.is_file()

    def read_raw(self) -> dict[str, Any]:
        """Содержимое файла как есть; {} — файла нет. Битый TOML → ConfigError."""
        with self._lock:
            key = self._stat_key()
            if key is None:
                return {}
            if self._cache is not None and self._cache[0] == key:
                return copy.deepcopy(self._cache[1])
            try:
                with self.path.open("rb") as handle:
                    raw = tomllib.load(handle)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"Ошибка синтаксиса в {self.path.name}: {exc}") from exc
            except UnicodeDecodeError as exc:
                raise ConfigError(
                    f"{self.path.name} должен быть в кодировке UTF-8 — пересохраните файл."
                ) from exc
            self._cache = (key, raw)
            return copy.deepcopy(raw)

    def read_raw_safe(self) -> tuple[dict[str, Any], str | None]:
        """Как read_raw, но ошибку возвращает текстом вместо исключения."""
        try:
            return self.read_raw(), None
        except ConfigError as exc:
            return {}, str(exc)

    def view(self) -> dict[str, Any]:
        """Ответ get_config."""
        return config_view(self.read_raw(), exists=self.exists())

    def token(self, raw: dict[str, Any] | None = None) -> str:
        """Токен Playerok, как его увидит бот (переменная окружения важнее файла)."""
        env = os.environ.get("PLAYEROK_TOKEN")
        if env:
            return env
        raw = self.read_raw() if raw is None else raw
        table = raw.get("playerok")
        value = table.get("token") if isinstance(table, dict) else None
        return value.strip() if isinstance(value, str) else ""

    def bot_path(self, key: str, raw: dict[str, Any] | None = None) -> Path | None:
        """Путь из раздела [bot] (state_file / log_file); None — запись отключена."""
        if raw is None:
            raw, _ = self.read_raw_safe()
        table = raw.get("bot")
        value = table.get(key, BOT_DEFAULTS[key]) if isinstance(table, dict) else None
        if not isinstance(value, str):
            value = BOT_DEFAULTS[key]
        return self.workdir / value if value else None

    def state_file(self, raw: dict[str, Any] | None = None) -> Path:
        return self.bot_path("state_file", raw) or self.workdir / BOT_DEFAULTS["state_file"]

    def log_file(self, raw: dict[str, Any] | None = None) -> Path | None:
        return self.bot_path("log_file", raw)

    def load(self) -> Config:
        """Настройки для запуска бота — с понятными ошибками до load_config."""
        if not self.exists():
            raise ApiError(
                "Сначала заполните настройки: файла config.toml ещё нет. "
                "Откройте «Настройки», впишите токен Playerok и сохраните."
            )
        if not self.token():
            raise ApiError("Не задан токен Playerok — укажите его в «Настройках».")
        return load_config(self.path)

    def validation_error(self) -> str | None:
        """Ошибка в текущем config.toml (без учёта пустого токена) или None."""
        with self._lock:
            key = self._stat_key()
            if key is None:
                return None
            if self._check is not None and self._check[0] == key:
                return self._check[1]
            try:
                self._validate(self.read_raw())
                error = None
            except ConfigError as exc:
                error = str(exc)
            self._check = (key, error)
            return error

    # --- запись настроек ---------------------------------------------------

    def save_settings(self, cfg: Any) -> None:
        """Слить изменения из окна в config.toml (save_config)."""
        if not isinstance(cfg, dict):
            raise ApiError("Некорректные данные настроек.")
        with self._lock:
            raw = self.read_raw() if self.exists() else _new_raw()
            for section, fields in _FIELDS.items():
                changes = cfg.get(section)
                if changes is None:
                    continue
                if not isinstance(changes, dict):
                    raise ApiError(f"Некорректные данные в разделе {section}.")
                table = raw.get(section)
                if not isinstance(table, dict):
                    table = raw[section] = {}
                for key, kind in fields.items():
                    if key in changes:
                        table[key] = _coerce(kind, changes[key], f"{section}.{key}")
            self._validate(raw)
            self._write(raw)

    # --- правила автовыдачи --------------------------------------------------

    def rules(self, raw: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Правила в виде для окна (list_rules)."""
        raw = self.read_raw() if raw is None else raw
        return [self._rule_view(index, rule) for index, rule in enumerate(_rules(raw))]

    def save_rule(self, rule: Any, rule_id: Any = None) -> dict[str, Any]:
        """Создать правило (rule_id=None) или изменить существующее."""
        match, message, use_stock, wanted_file, per_sale, low = _rule_input(rule)
        with self._lock:
            raw = self.read_raw() if self.exists() else _new_raw()
            rules = _rules(raw, create=True)
            index = None if rule_id is None else _index(rule_id, len(rules))
            old = rules[index] if index is not None and isinstance(rules[index], dict) else {}

            stock_file: str | None = None
            if use_stock:
                old_file = _clean_str(old.get("stock_file"))
                if wanted_file and old_file and _same_path(wanted_file, old_file):
                    # Путь не меняли: оставляем как записан, даже если его
                    # вручную вписали вне stock/ — окно новых путей не вводит.
                    stock_file = old_file
                elif wanted_file:
                    stock_file = self.safe_stock_path(wanted_file)
                else:
                    stock_file = old_file or self._new_stock_file(match, rules)

            # Порядок ключей — как в config.example.toml, чтобы файл было удобно читать.
            entry: dict[str, Any] = {"match": match}
            if stock_file:
                entry["stock_file"] = stock_file
            entry.update(message=message, products_per_sale=per_sale, low_stock_alert=low)
            entry.update((k, v) for k, v in old.items() if k not in entry and k != "stock_file")
            if index is None:
                rules.append(entry)
                index = len(rules) - 1
            else:
                rules[index] = entry

            self._validate(raw)
            self._write(raw)
            if stock_file:
                self._touch_stock(stock_file)
            return self._rule_view(index, entry)

    def delete_rule(self, rule_id: Any) -> None:
        """Удалить правило; файл склада остаётся на диске."""
        with self._lock:
            raw = self.read_raw()
            rules = _rules(raw)
            del rules[_index(rule_id, len(rules))]
            self._write(raw)

    def move_rule(self, rule_id: Any, direction: Any) -> list[dict[str, Any]]:
        """Сдвинуть правило вверх (-1) или вниз (+1): порядок важен для поиска."""
        if direction not in (-1, 1) or isinstance(direction, bool):
            raise ApiError("Направление должно быть -1 (вверх) или 1 (вниз).")
        with self._lock:
            raw = self.read_raw()
            rules = _rules(raw)
            index = _index(rule_id, len(rules))
            target = index + int(direction)
            if 0 <= target < len(rules):
                rules[index], rules[target] = rules[target], rules[index]
                self._write(raw)
            return self.rules(raw)

    def stock_for(self, rule_id: Any) -> tuple[Stock, str]:
        """Склад правила и путь к нему, как он записан в конфиге."""
        rules = _rules(self.read_raw())
        rule = rules[_index(rule_id, len(rules))]
        stock_file = _clean_str(rule.get("stock_file")) if isinstance(rule, dict) else ""
        if not stock_file:
            raise ApiError("У этого правила нет склада — включите «Выдавать товар со склада».")
        return Stock(self.workdir / stock_file), stock_file

    def safe_stock_path(self, value: str) -> str:
        """Проверить путь склада из окна: только внутри <workdir>/stock.

        Возвращает относительный путь вида `stock/keys.txt`. Имя без папки
        кладётся в stock/. Абсолютные пути, `..` и другие папки — ошибка.
        """
        text = value.strip()
        if (
            not text
            or "\x00" in text
            or any(char in text for char in '<>:"|?*')
            or text.startswith(("/", "\\"))
            or text.endswith(("/", "\\"))
            or PureWindowsPath(text).is_absolute()
            or PureWindowsPath(text).drive
        ):
            raise ApiError(_STOCK_OUTSIDE)
        parts = [part for part in re.split(r"[\\/]+", text) if part not in ("", ".")]
        if not parts or ".." in parts:
            raise ApiError(_STOCK_OUTSIDE)
        if len(parts) == 1:
            parts.insert(0, STOCK_DIR)
        if parts[0].casefold() != STOCK_DIR or len(parts) < 2:
            raise ApiError(_STOCK_OUTSIDE)
        if any(part != part.strip() or part.endswith(".") for part in parts):
            raise ApiError(
                "Имя файла склада не должно начинаться или заканчиваться пробелом/точкой."
            )
        relative = "/".join([STOCK_DIR, *parts[1:]])
        root = self.stock_root.resolve()
        resolved = (self.workdir / relative).resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ApiError(_STOCK_OUTSIDE)
        return relative

    # --- внутреннее ----------------------------------------------------------

    def _rule_view(self, index: int, rule: Any) -> dict[str, Any]:
        rule = rule if isinstance(rule, dict) else {}
        stock_file = _clean_str(rule.get("stock_file")) or None
        count: int | None = None
        if stock_file:
            try:
                count = Stock(self.workdir / stock_file).count()
            except (OSError, UnicodeError):
                count = None
        per_sale = rule.get("products_per_sale", 1)
        low = rule.get("low_stock_alert", 0)
        return {
            "id": index,
            "match": rule.get("match") if isinstance(rule.get("match"), str) else "",
            "message": rule.get("message") if isinstance(rule.get("message"), str) else "",
            "stock_file": stock_file,
            "products_per_sale": per_sale if _is_int(per_sale) else 1,
            "low_stock_alert": low if _is_int(low) else 0,
            "stock_count": count,
        }

    def _new_stock_file(self, match: str, rules: list[Any]) -> str:
        used = {
            _clean_str(rule.get("stock_file")).replace("\\", "/").casefold()
            for rule in rules
            if isinstance(rule, dict)
        }
        slug = slugify(match)
        for number in range(1, 1000):
            name = f"{STOCK_DIR}/{slug}{'' if number == 1 else f'-{number}'}.txt"
            if name.casefold() not in used and not (self.workdir / name).exists():
                return name
        raise ApiError("Не удалось подобрать имя файла склада — укажите его вручную.")

    def _touch_stock(self, stock_file: str) -> None:
        path = (self.workdir / stock_file).resolve()
        if path.is_relative_to(self.stock_root.resolve()) and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def _validate(self, raw: dict[str, Any]) -> Config:
        """Прогнать настройки через load_config бота (временный файл в рабочей папке)."""
        candidate = copy.deepcopy(raw)
        table = candidate.get("playerok")
        if table is None:
            table = candidate["playerok"] = {}
        if isinstance(table, dict) and not _clean_str(table.get("token")):
            table["token"] = _PLACEHOLDER_TOKEN
        try:
            text = tomli_w.dumps(candidate)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Настройки нельзя записать в TOML: {exc}") from exc
        self.workdir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.workdir, prefix=".config-check-", suffix=".toml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            return load_config(tmp)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _write(self, raw: dict[str, Any]) -> None:
        """Атомарно записать config.toml: при сбое старый файл останется целым."""
        text = _HEADER + tomli_w.dumps(raw, multiline_strings=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.workdir, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        finally:
            self._cache = None
            self._check = None

    def _stat_key(self) -> tuple[int, int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        if not self.path.is_file():
            return None
        return stat.st_mtime_ns, stat.st_size, stat.st_ino


# --- разбор значений из окна ---------------------------------------------------


def _new_raw() -> dict[str, Any]:
    raw = copy.deepcopy(NEW_CONFIG)
    raw["bot"] = dict(BOT_DEFAULTS)
    return raw


def _rules(raw: dict[str, Any], *, create: bool = False) -> list[Any]:
    """Список [[delivery.rules]] из сырого конфига (живой, для правки)."""
    delivery = raw.get("delivery")
    if not isinstance(delivery, dict):
        if not create:
            return []
        delivery = raw["delivery"] = {}
    rules = delivery.get("rules")
    if not isinstance(rules, list):
        if not create:
            return []
        rules = delivery["rules"] = []
    return rules


def _index(rule_id: Any, count: int) -> int:
    if isinstance(rule_id, float) and rule_id.is_integer():
        rule_id = int(rule_id)
    if not _is_int(rule_id) or not 0 <= rule_id < count:
        raise ApiError("Правило не найдено — обновите страницу.")
    return rule_id


def _rule_input(rule: Any) -> tuple[str, str, bool, str, int, int]:
    if not isinstance(rule, dict):
        raise ApiError("Некорректные данные правила.")
    match = _text(rule.get("match"), "Название лота").strip()
    if not match:
        raise ApiError("Укажите, какие слова должны быть в названии лота.")
    message = _text(rule.get("message"), "Сообщение покупателю").replace("\r\n", "\n")
    if not message.strip():
        raise ApiError("Напишите сообщение, которое получит покупатель.")
    use_stock = rule.get("use_stock", False)
    if not isinstance(use_stock, bool):
        raise ApiError("use_stock должен быть true или false.")
    stock_file = _text(rule.get("stock_file"), "Файл склада").strip()
    if use_stock and "{product}" not in message:
        raise ApiError(
            "Добавьте в сообщение {product} — туда подставится товар со склада, "
            "иначе покупатель его не получит."
        )
    per_sale = _whole(rule.get("products_per_sale", 1), "Сколько товаров выдавать", minimum=1)
    low = _whole(rule.get("low_stock_alert", 0), "Порог «мало товара»", minimum=0)
    return match, message, use_stock, stock_file, per_sale, low


def _coerce(kind: str, value: Any, name: str) -> Any:
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{name} должен быть true или false")
    if kind == "str":
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ConfigError(f"{name} должен быть строкой")
        return value.strip()
    if kind == "number":
        number = _number(value, name)
        return int(number) if number.is_integer() else number
    if kind == "chat_ids":
        return _chat_ids(value)
    if kind == "str_list":
        return _str_list(value, name)
    raise AssertionError(kind)


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{name} должен быть числом")
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().replace(",", "."))
        except ValueError:
            raise ConfigError(f"{name} должен быть числом") from None
    else:
        raise ConfigError(f"{name} должен быть числом")
    if not math.isfinite(number):
        raise ConfigError(f"{name} должен быть числом")
    return number


def _chat_ids(value: Any) -> list[int]:
    if value is None:
        return []
    items = re.split(r"[\s,;]+", value) if isinstance(value, str) else value
    if not isinstance(items, list):
        raise ConfigError("telegram.chat_ids должен быть списком чисел, например [123456789]")
    result: list[int] = []
    for item in items:
        if isinstance(item, int) and not isinstance(item, bool):
            result.append(item)
        elif isinstance(item, float) and item.is_integer():
            result.append(int(item))
        elif isinstance(item, str) and not item.strip():
            continue
        elif isinstance(item, str) and re.fullmatch(r"-?\d{1,20}", item.strip()):
            result.append(int(item.strip()))
        else:
            raise ConfigError(
                f"telegram.chat_ids: «{item}» — не число. "
                "Chat id — это число, например 123456789 (узнать у @userinfobot)."
            )
    return list(dict.fromkeys(result))


def _str_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    items = re.split(r"[,\n]", value) if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise ConfigError(f"{name} должен быть списком строк")
    return list(dict.fromkeys(item.strip() for item in items if item.strip()))


def _view_value(kind: str, value: Any, default: Any) -> Any:
    if value is None:
        return copy.deepcopy(default)
    if kind == "bool":
        return value if isinstance(value, bool) else default
    if kind == "str":
        return value if isinstance(value, str) else default
    if kind == "number":
        ok = isinstance(value, int | float) and not isinstance(value, bool)
        return value if ok else default
    if kind == "chat_ids":
        return [item for item in value if _is_int(item)] if isinstance(value, list) else []
    if kind == "str_list":
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
    raise AssertionError(kind)


def _text(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(f"{name}: ожидался текст.")
    return value


def _whole(value: Any, name: str, *, minimum: int) -> int:
    integral_float = isinstance(value, float) and value.is_integer()
    if integral_float or (isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value)):
        value = int(value)
    if not _is_int(value) or value < minimum:
        raise ApiError(f"{name}: нужно целое число не меньше {minimum}.")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _same_path(left: str, right: str) -> bool:
    def norm(path: str) -> str:
        return "/".join(part for part in re.split(r"[\\/]+", path.strip()) if part not in ("", "."))

    return norm(left) == norm(right)
