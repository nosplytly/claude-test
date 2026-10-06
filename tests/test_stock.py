from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from playerok_bot.stock import Stock, StockError


async def test_take_from_top_and_skip_blank_lines(tmp_path: Path):
    path = tmp_path / "s.txt"
    path.write_text("\nA\n\n  B  \nC\n", encoding="utf-8")
    stock = Stock(path)

    assert stock.count() == 3
    assert await stock.take(2) == ["A", "B"]
    assert path.read_text(encoding="utf-8") == "C\n"


async def test_not_enough_leaves_file_untouched(tmp_path: Path):
    path = tmp_path / "s.txt"
    path.write_text("A\n", encoding="utf-8")

    assert await Stock(path).take(2) is None
    assert path.read_text(encoding="utf-8") == "A\n"


async def test_missing_file_is_empty(tmp_path: Path):
    stock = Stock(tmp_path / "nope.txt")
    assert stock.count() == 0
    assert await stock.take(1) is None


async def test_bom_and_newline_escape(tmp_path: Path):
    path = tmp_path / "s.txt"
    path.write_bytes("﻿логин: a\\nпароль: b\r\nX\r\n".encode())

    assert await Stock(path).take(1) == ["логин: a\nпароль: b"]
    assert path.read_text(encoding="utf-8") == "X\n"


async def test_parallel_takes_never_share_a_product(tmp_path: Path):
    path = tmp_path / "s.txt"
    path.write_text("".join(f"K{i}\n" for i in range(20)), encoding="utf-8")

    results = await asyncio.gather(*(Stock(path).take(1) for _ in range(25)))

    taken = [r[0] for r in results if r is not None]
    assert sorted(taken) == sorted(f"K{i}" for i in range(20))
    assert results.count(None) == 5


def test_take_unique_skips_already_delivered(tmp_path: Path):
    path = tmp_path / "s.txt"
    path.write_text("OLD\nA\nA\nB\\nline\nC\n", encoding="utf-8")

    taken, dropped = Stock(path).take_unique(2, {"OLD"}.__contains__)

    assert taken == ["A", "B\nline"]
    assert dropped == ["OLD", "A"]
    assert path.read_text(encoding="utf-8") == "C\n"


def test_take_unique_not_enough_still_drops_delivered(tmp_path: Path):
    path = tmp_path / "s.txt"
    path.write_text("OLD\nA\n", encoding="utf-8")

    taken, dropped = Stock(path).take_unique(2, {"OLD"}.__contains__)

    assert taken is None and dropped == ["OLD"]
    assert path.read_text(encoding="utf-8") == "A\n"


def test_put_back_restores_order_and_escapes(tmp_path: Path):
    path = tmp_path / "s.txt"
    path.write_text("C\n", encoding="utf-8")

    Stock(path).put_back(["A", "логин\nпароль"])

    assert path.read_text(encoding="utf-8") == "A\nлогин\\nпароль\nC\n"


@pytest.mark.parametrize(
    ("content", "fragment"),
    [("Ключ\r\n".encode("cp1251"), "UTF-8"), (b"\x00" * 32, "нулевые байты")],
)
async def test_unreadable_stock_is_explained(tmp_path: Path, content: bytes, fragment: str):
    path = tmp_path / "keys.txt"
    path.write_bytes(content)

    with pytest.raises(StockError, match=fragment):
        await Stock(path).take(1)
    with pytest.raises(StockError, match=r"keys\.txt"):
        Stock(path).count()
    assert path.read_bytes() == content
