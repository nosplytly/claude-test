"""Склад товаров для автовыдачи: текстовый файл, одна строка — один товар."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from .fileio import atomic_write_text, read_text

__all__ = ["Stock", "StockError"]

# Один файл может обслуживать несколько правил, а править его может и бот,
# и окно приложения из другого потока — поэтому блокировка общая на путь.
_locks: dict[Path, threading.Lock] = {}
_locks_guard = threading.Lock()


class StockError(Exception):
    """Файл склада нельзя прочитать — сообщение показывается пользователю как есть."""


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(path, threading.Lock())


def _expand(line: str) -> str:
    return line.replace("\\n", "\n")


def _collapse(product: str) -> str:
    return product.replace("\n", "\\n")


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
        taken, _ = self.take_unique(amount)
        return taken

    def take_unique(
        self, amount: int, used: Callable[[str], bool] | None = None
    ) -> tuple[list[str] | None, list[str]]:
        """Забрать `amount` товаров, пропуская уже выданные.

        `used(товар)` — выдавался ли товар раньше (например, файл пересохранили
        из Блокнота, открытого до продажи, и выданная строка вернулась). Такие
        строки и повторы внутри одной выдачи убираются из файла и не выдаются.
        Возвращает (товары или None, если их не хватает; убранные строки).
        """
        with _lock_for(self.path):
            lines = self._read()
            taken: list[str] = []
            kept_raw: list[str] = []
            dropped: list[str] = []
            rest: list[str] = []
            for line in lines:
                if len(taken) >= amount:
                    rest.append(line)
                    continue
                product = _expand(line)
                if product in taken or (used is not None and used(product)):
                    dropped.append(product)
                else:
                    taken.append(product)
                    kept_raw.append(line)
            if len(taken) < amount:
                # Не хватает — ничего не забираем, но выданные повторно строки убираем.
                if dropped:
                    self._write(kept_raw)
                return None, dropped
            self._write(rest)
        return taken, dropped

    def put_back(self, products: list[str]) -> None:
        """Вернуть забранные товары в начало файла (выдача сорвалась до записи в state)."""
        if not products:
            return
        with _lock_for(self.path):
            self._write([_collapse(product) for product in products] + self._read())

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
        try:
            # utf-8-sig съедает BOM, который ставит Блокнот Windows.
            text = read_text(self.path, encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise StockError(
                f"Файл склада {self.path.name} не в кодировке UTF-8 — откройте его "
                "в Блокноте и сохраните с кодировкой UTF-8."
            ) from exc
        if "\x00" in text:
            # Так выглядит файл, недописанный при сбое питания: выдавать из него нельзя.
            raise StockError(
                f"Файл склада {self.path.name} повреждён (в нём нулевые байты) — "
                "проверьте его содержимое и сохраните заново."
            )
        return [line.strip() for line in text.splitlines() if line.strip()]

    def _write(self, lines: list[str]) -> None:
        # Пишем во временный файл и подменяем: при сбое старый файл останется целым.
        text = "".join(f"{line}\n" for line in lines)
        atomic_write_text(self.path, text, prefix=".stock-", newline="\n")
