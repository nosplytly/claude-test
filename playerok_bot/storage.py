"""Состояние бота между перезапусками: какие сделки уже обработаны."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .fileio import atomic_write_text, read_text

__all__ = ["State", "StateError"]

logger = logging.getLogger(__name__)

#: Сколько хранить записи о сделках и лотах.
_RETENTION = timedelta(days=90)
_SECTIONS = ("deliveries", "relisted_deals", "relisted_items")


class StateError(Exception):
    """Файл состояния повреждён или недоступен."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class State:
    """JSON-файл с тремя разделами, каждый — словарь `id → запись`.

    - `deliveries` — автовыдача по сделке: статус, выданные товары, попытки;
    - `relisted_deals` — по каким продажам лот уже перевыставлен;
    - `relisted_items` — когда и во что перевыставлялся лот.

    У каждой записи есть поле `at` — по нему старые записи вычищаются.

    Две записи: `put` — «сначала диск, потом память» (если файл записать не
    вышло, исключение, а в памяти всё как было), и `record` — для того, что
    уже случилось на площадке: память меняется всегда, а файл дописывается
    при первой удачной записи (`flush`).
    """

    def __init__(self, path: Path, data: dict[str, dict[str, dict[str, Any]]], *, fresh: bool):
        self.path = path
        self._data = data
        #: True, если файла не было — бот запущен впервые.
        self.fresh = fresh
        #: В памяти есть записи, которых ещё нет в файле.
        self.dirty = False
        #: С какого момента (time.monotonic) файл не удаётся записать.
        self._failing_since: float | None = None
        #: Последняя ошибка записи (для уведомления).
        self.last_error: OSError | None = None

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.is_file():
            return cls(path, {name: {} for name in _SECTIONS}, fresh=True)
        try:
            text = read_text(path)
        except UnicodeDecodeError as exc:
            raise _corrupt(path, exc) from exc
        except OSError as exc:
            raise StateError(
                f"Не удалось открыть {path}: {exc.strerror or exc}. Похоже, файл занят другой "
                "программой или к нему нет доступа — закройте её и запустите бота снова. "
                "Не удаляйте файл: в нём записано, кому что уже выдано."
            ) from exc
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise _corrupt(path, exc) from exc
        data = {
            name: dict(raw.get(name) or {}) if isinstance(raw, dict) else {} for name in _SECTIONS
        }
        state = cls(path, data, fresh=False)
        state._prune()
        return state

    def get(self, section: str, key: str) -> dict[str, Any] | None:
        return self._data[section].get(key)

    def entries(self, section: str) -> Iterator[tuple[str, dict[str, Any]]]:
        """Все записи раздела (копия списка — можно менять состояние по ходу)."""
        return iter(list(self._data[section].items()))

    def put(self, section: str, key: str, **fields: Any) -> dict[str, Any]:
        """Обновить запись и сразу сохранить файл.

        Если файл записать не вышло — исключение, и память не меняется:
        по такой записи бот ещё ничего не сделал и повторит шаг позже.
        """
        entry = self._updated(section, key, fields)
        snapshot = {name: dict(items) for name, items in self._data.items()}
        snapshot[section][key] = entry
        self._write(snapshot)
        self._data[section][key] = entry
        return entry

    def record(self, section: str, key: str, **fields: Any) -> dict[str, Any]:
        """Запомнить то, что уже произошло (товар отправлен, лот выставлен).

        Память меняется всегда: повторять сделанное нельзя. Если файл не
        записался, запись дойдёт до диска при следующем удачном сохранении.
        """
        entry = self._updated(section, key, fields)
        self._data[section][key] = entry
        self.dirty = True
        try:
            self._write(self._data)
        except OSError as exc:
            logger.error(
                "Не удалось сохранить %s (%s), запись сохраню при следующей попытке",
                self.path,
                exc,
            )
        return entry

    def save(self) -> None:
        self._write(self._data)

    def flush(self) -> bool:
        """Дописать на диск отложенные записи. False — файл снова не записался.

        После неудачной записи файл пробуется и без отложенных записей: так
        видно, что диск освободился, даже если новых записей пока нет.
        """
        if not self.dirty and self._failing_since is None:
            return True
        try:
            self._write(self._data)
        except OSError as exc:
            logger.warning("Не удалось сохранить %s: %s", self.path, exc)
            return False
        return True

    def failing_for(self) -> float:
        """Сколько секунд подряд файл не удаётся записать (0 — всё в порядке)."""
        if self._failing_since is None:
            return 0.0
        return time.monotonic() - self._failing_since

    def _updated(self, section: str, key: str, fields: dict[str, Any]) -> dict[str, Any]:
        return {**(self._data[section].get(key) or {}), **fields, "at": now_iso()}

    def _write(self, data: dict[str, dict[str, dict[str, Any]]]) -> None:
        text = json.dumps(data, ensure_ascii=False, indent=2)
        try:
            atomic_write_text(self.path, text, prefix=".state-")
        except OSError as exc:
            if self._failing_since is None:
                self._failing_since = time.monotonic()
            self.last_error = exc
            raise
        # Снимок всегда включает всю память — после удачной записи она на диске.
        self.dirty = False
        self._failing_since = None
        self.last_error = None

    def _prune(self) -> None:
        border = datetime.now(UTC) - _RETENTION
        for section in self._data.values():
            for key in [k for k, v in section.items() if _older(v, border)]:
                del section[key]


def _corrupt(path: Path, exc: Exception) -> StateError:
    return StateError(
        f"Файл {path} повреждён: {exc}. Верните его из резервной копии или удалите "
        "(после удаления бот не тронет уже оплаченные сделки)."
    )


def _older(entry: Any, border: datetime) -> bool:
    if not isinstance(entry, dict):
        return True
    try:
        return datetime.fromisoformat(entry.get("at", "")) < border
    except (TypeError, ValueError):
        return False
