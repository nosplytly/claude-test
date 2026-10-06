"""Общее для тестов SupplierHub: рабочая папка, Api, фейковый бот."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PlayerokAPI import User

from playerok_bot import bot as bot_core
from supplierhub.api import Api
from supplierhub.feed import EventFeed

CONFIG = """
[playerok]
token = "tok"
deals_interval = 20

[telegram]
enabled = false

[delivery]
enabled = true

[[delivery.rules]]
match = "Ключ Steam"
stock_file = "stock/keys.txt"
message = "Ключ: {product}"
low_stock_alert = 2

[[delivery.rules]]
match = "Гайд"
message = "Ссылка: https://example.com"

[bot]
state_file = "state.json"
log_file = ""
"""


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Дождаться условия (без длинных sleep), иначе упасть."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("условие не выполнилось за отведённое время")


def make_me(username: str = "seller", balance: float = 1234.5) -> User:
    return User.from_dict({"id": "me-1", "username": username, "balance": {"available": balance}})


class FakeBot:
    """Подмена playerok_bot.bot.run: входит «в аккаунт» и ждёт отмены."""

    def __init__(self) -> None:
        self.notifiers: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.watcher = SimpleNamespace(recent=[])
        self.me = make_me()
        self.fail_with: BaseException | None = None
        self.release = threading.Event()
        self.cancelled = threading.Event()
        #: Сколько секунд «упираться» отмене (для проверки статуса stopping).
        self.stubborn = 0.0

    async def run(self, config: Any, *, notifier=None, on_started=None, watch_chats=None) -> None:
        self.notifiers.append(notifier)
        self.calls.append({"config": config, "watch_chats": watch_chats})
        if self.fail_with is not None:
            raise self.fail_with
        await notifier.send("🤖 Бот запущен: <b>seller</b>\nБаланс: 1234.50 ₽")
        on_started(self.me, self.watcher)
        try:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled.set()
            if self.stubborn:
                await asyncio.shield(asyncio.sleep(self.stubborn))
            raise
        finally:
            await notifier.aclose()


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def config_file(workdir: Path) -> Path:
    path = workdir / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    (workdir / "stock").mkdir()
    (workdir / "stock" / "keys.txt").write_text("K1\nK2\nK3\n", encoding="utf-8")
    return path


@pytest.fixture
def opened() -> dict[str, list[Any]]:
    return {"paths": [], "urls": []}


@pytest.fixture
def api(workdir: Path, opened: dict[str, list[Any]]):
    instance = Api(
        workdir,
        feed=EventFeed(),
        open_path=opened["paths"].append,
        open_url=opened["urls"].append,
    )
    yield instance
    instance._shutdown()


@pytest.fixture
def fake_bot(monkeypatch: pytest.MonkeyPatch) -> FakeBot:
    fake = FakeBot()
    monkeypatch.setattr(bot_core, "run", fake.run)
    yield fake
    fake.release.set()


@pytest.fixture(autouse=True)
def _no_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Переменные окружения с токенами меняют поведение load_config — убираем их."""
    monkeypatch.delenv("PLAYEROK_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
