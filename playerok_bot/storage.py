"""Состояние бота между перезапусками: какие сделки уже обработаны."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = ["State", "StateError"]

#: Сколько хранить записи о сделках и лотах.
_RETENTION = timedelta(days=90)
_SECTIONS = ("deliveries", "relisted_deals", "relisted_items")


class StateError(Exception):
    """Файл состояния повреждён."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class State:
    """JSON-файл с тремя разделами, каждый — словарь `id → запись`.

    - `deliveries` — автовыдача по сделке: статус, выданные товары, попытки;
    - `relisted_deals` — по каким продажам лот уже перевыставлен;
    - `relisted_items` — когда и во что перевыставлялся лот.

    У каждой записи есть поле `at` — по нему старые записи вычищаются.
    """

    def __init__(self, path: Path, data: dict[str, dict[str, dict[str, Any]]], *, fresh: bool):
        self.path = path
        self._data = data
        #: True, если файла не было — бот запущен впервые.
        self.fresh = fresh

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.is_file():
            return cls(path, {name: {} for name in _SECTIONS}, fresh=True)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StateError(
                f"Не удалось прочитать {path}: {exc}. Исправьте или удалите файл "
                "(после удаления бот не тронет уже оплаченные сделки)."
            ) from exc
        data = {
            name: dict(raw.get(name) or {}) if isinstance(raw, dict) else {} for name in _SECTIONS
        }
        state = cls(path, data, fresh=False)
        state._prune()
        return state

    def get(self, section: str, key: str) -> dict[str, Any] | None:
        return self._data[section].get(key)

    def put(self, section: str, key: str, **fields: Any) -> dict[str, Any]:
        """Обновить запись и сразу сохранить файл."""
        entry = self._data[section].setdefault(key, {})
        entry.update(fields)
        entry["at"] = now_iso()
        self.save()
        return entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _prune(self) -> None:
        border = datetime.now(UTC) - _RETENTION
        for section in self._data.values():
            for key in [k for k, v in section.items() if _older(v, border)]:
                del section[key]


def _older(entry: Any, border: datetime) -> bool:
    if not isinstance(entry, dict):
        return True
    try:
        return datetime.fromisoformat(entry.get("at", "")) < border
    except (TypeError, ValueError):
        return False
