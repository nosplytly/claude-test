from __future__ import annotations

import asyncio
from pathlib import Path

from playerok_bot.stock import Stock


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
