"""Склад товаров для автовыдачи: текстовый файл, одна строка — один товар."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

__all__ = ["Stock"]

# Один файл может обслуживать несколько правил — блокировка общая на путь.
_locks: dict[Path, asyncio.Lock] = {}


class Stock:
    """Строки файла — товары. Пустые строки пропускаются.

    Последовательность `\\n` внутри строки превращается в перенос строки
    при выдаче, чтобы многострочный товар помещался в одну строку файла.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def count(self) -> int:
        return len(self._read())

    async def take(self, amount: int) -> list[str] | None:
        """Забрать `amount` товаров из начала файла; None, если их не хватает."""
        async with _locks.setdefault(self.path, asyncio.Lock()):
            lines = self._read()
            if len(lines) < amount:
                return None
            taken, rest = lines[:amount], lines[amount:]
            self._write(rest)
        return [line.replace("\\n", "\n") for line in taken]

    def _read(self) -> list[str]:
        if not self.path.is_file():
            return []
        # utf-8-sig съедает BOM, который ставит Блокнот Windows.
        text = self.path.read_text(encoding="utf-8-sig")
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _write(self, lines: list[str]) -> None:
        # Пишем во временный файл и подменяем: при сбое старый файл останется целым.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".stock-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("".join(f"{line}\n" for line in lines))
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
