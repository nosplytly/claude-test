"""Фейковый аккаунт Playerok и уведомитель для тестов без сети."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from PlayerokAPI import Deal, Event, Item, ItemProfile, Page

from playerok_bot import fileio
from playerok_bot.config import DeliveryConfig, DeliveryRule, RelistConfig
from playerok_bot.storage import State


def make_deal(
    deal_id: str = "d1",
    *,
    status: str = "PAID",
    direction: str = "OUT",
    name: str = "Ключ Steam Witcher 3",
    price: float = 100,
    item_id: str = "i1",
    chat_id: str | None = "c1",
    buyer: str = "ivan",
    has_problem: bool = False,
) -> Deal:
    return Deal.from_dict(
        {
            "id": deal_id,
            "status": status,
            "direction": direction,
            "hasProblem": has_problem,
            "item": {"__typename": "Item", "id": item_id, "name": name, "price": price},
            "user": {"id": "buyer-id", "username": buyer},
            "chat": {"id": chat_id} if chat_id else None,
        }
    )


class FakeChats:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_with: Exception | None = None

    async def send(self, chat_id: str, text: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((chat_id, text))


class FakeDeals:
    def __init__(self) -> None:
        self.page: list[Deal] = []
        self.by_id: dict[str, Deal] = {}
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.update_fails = False
        self.search_calls: list[dict[str, Any]] = []

    async def search(self, *, filter: dict[str, Any], first: int) -> Page[Deal]:
        self.search_calls.append(filter)
        return Page(items=list(self.page))

    async def get(self, deal_id: str) -> Deal:
        return self.by_id[deal_id]

    async def update(self, deal_id: str, changes: dict[str, Any]) -> Deal:
        if self.update_fails:
            raise RuntimeError("update failed")
        self.updates.append((deal_id, dict(changes)))
        return self.by_id.get(deal_id) or make_deal(deal_id)


class FakeItems:
    def __init__(self) -> None:
        self.status: dict[str, str] = {}
        self.published: list[str] = []
        self.publish_returns: dict[str, str] = {}
        self.publish_fails = False
        self.expired: list[ItemProfile] = []

    async def get(self, *, item_id: str) -> Item:
        return Item.from_dict({"id": item_id, "status": self.status.get(item_id, "SOLD")})

    async def publish(self, item_id: str) -> Item:
        if self.publish_fails:
            raise RuntimeError("publish failed")
        self.published.append(item_id)
        new_id = self.publish_returns.get(item_id, item_id)
        return Item.from_dict({"id": new_id, "status": "PENDING_APPROVAL"})

    async def search(self, **kwargs: Any) -> Page[ItemProfile]:
        return Page(items=list(self.expired))


class FakePolling:
    def __init__(self, events: list[Event]) -> None:
        self._events = events

    async def events(self) -> AsyncIterator[Event]:
        for event in self._events:
            yield event


class FakeAccount:
    def __init__(self) -> None:
        self.chats = FakeChats()
        self.deals = FakeDeals()
        self.items = FakeItems()
        self.polling_events: list[Event] = []

    def polling(self, *, interval: float) -> FakePolling:
        return FakePolling(self.polling_events)


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True

    def find(self, fragment: str) -> list[str]:
        return [message for message in self.messages if fragment in message]


@pytest.fixture
def account() -> FakeAccount:
    return FakeAccount()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def state(tmp_path: Path) -> State:
    return State.load(tmp_path / "state.json")


@pytest.fixture
def stock_file(tmp_path: Path) -> Path:
    path = tmp_path / "keys.txt"
    path.write_text("KEY-1\nKEY-2\nKEY-3\n", encoding="utf-8")
    return path


@pytest.fixture
def delivery_config(stock_file: Path) -> DeliveryConfig:
    return DeliveryConfig(
        enabled=True,
        mark_sent=True,
        rules=[
            DeliveryRule(
                match="ключ steam",
                message="Привет, {buyer}! Ключ: {product}",
                stock_file=stock_file,
                low_stock_alert=1,
            ),
            DeliveryRule(match="Гайд", message="Ссылка: https://example.com"),
        ],
    )


@pytest.fixture
def relist_config() -> RelistConfig:
    return RelistConfig(after_sale=True, expired=True)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторы записи занятого файла в тестах — без пауз."""
    monkeypatch.setattr(fileio, "RETRY_DELAYS", (0.0,) * len(fileio.RETRY_DELAYS))


class FlakyReplace:
    """os.replace, который падает как на Windows, когда файл держит другая программа.

    `failing` — падать на каждом вызове; `fail_next` — упасть ещё столько раз;
    `only` — падать только для файла с этим именем.
    """

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, failing: bool = True, only: str | None = None
    ) -> None:
        self.calls = 0
        self.failing = failing
        self.fail_next = 0
        #: Падать только при подмене файла с таким именем (например, state.json).
        self.only = only
        self._real = os.replace
        monkeypatch.setattr(fileio.os, "replace", self)

    def __call__(self, src, dst) -> None:
        self.calls += 1
        target = os.path.basename(os.fspath(dst))
        if (self.failing or self.fail_next > 0) and self.only in (None, target):
            self.fail_next = max(self.fail_next - 1, 0)
            raise PermissionError(13, "Отказано в доступе", str(dst))
        self._real(src, dst)
