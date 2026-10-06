"""Склад товаров для автовыдачи: текстовый файл, одна строка — один товар."""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path

__all__ = ["Stock"]

# Один файл может обслуживать несколько правил, а править его может и бот,
# и окно приложения из другого потока — поэтому блокировка общая на путь.
_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


class Stock:
    """Строки файла — товары. Пустые строки пропускаются.

    Последовательность `\\n` внутри строки превращается в перенос строки
    при выдаче, чтобы многострочный товар помещался в одну строку файла.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def count(self) -> int:
        return len(self._read())

    def items(self) -> list[str]:
        """Товары как они записаны в файле (с `\\n`, без раскрытия)."""
        return self._read()

    async def take(self, amount: int) -> list[str] | None:
        """Забрать `amount` товаров из начала файла; None, если их не хватает."""
        with _lock_for(self.path):
            lines = self._read()
            if len(lines) < amount:
                return None
            taken, rest = lines[:amount], lines[amount:]
            self._write(rest)
        return [line.replace("\\n", "\n") for line in taken]

    def add(self, lines: Iterable[str]) -> int:
        """Дописать товары в конец файла. Возвращает, сколько добавлено."""
        new = [line.strip() for line in lines if line.strip()]
        if not new:
            return 0
        with _lock_for(self.path):
            self._write(self._read() + new)
        return len(new)

    def remove(self, index: int, expected: str | None = None) -> bool:
        """Удалить товар по номеру. `expected` защищает от удаления не той строки,
        если бот успел выдать товар между показом списка и нажатием кнопки."""
        with _lock_for(self.path):
            lines = self._read()
            if not 0 <= index < len(lines):
                return False
            if expected is not None and lines[index] != expected:
                return False
            del lines[index]
            self._write(lines)
        return True

    def clear(self) -> int:
        """Очистить склад. Возвращает, сколько товаров было удалено."""
        with _lock_for(self.path):
            removed = len(self._read())
            self._write([])
        return removed

    def _read(self) -> list[str]:
        if not self.path.is_file():
            return []
        # utf-8-sig съедает BOM, который ставит Блокнот Windows.
        text = self.path.read_text(encoding="utf-8-sig")
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _write(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Пишем во временный файл и подменяем: при сбое старый файл останется целым.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".stock-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("".join(f"{line}\n" for line in lines))
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
