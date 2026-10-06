from __future__ import annotations

import asyncio

import pytest
from PlayerokAPI import Chat, ChatEvent, ChatMessage, EventType, MessageEvent

from playerok_bot.delivery import DELIVERED, SECTION, SKIPPED, AutoDelivery
from playerok_bot.relist import DEALS, AutoRelist
from playerok_bot.storage import State
from playerok_bot.watchers import DealWatcher, watch_messages

from .conftest import make_deal


def make(account, state, notifier, delivery_config, relist_config, *, first_run: bool):
    delivery = AutoDelivery(account, delivery_config, state, notifier)
    relist = AutoRelist(account, "me", relist_config, state, notifier, delivery.can_sell)
    return DealWatcher(account, state, notifier, delivery, relist, first_run=first_run)


async def test_first_run_leaves_old_paid_deals_alone(
    account, state, notifier, delivery_config, relist_config
):
    account.deals.page = [
        make_deal("old"),
        make_deal("sent", status="SENT"),
        make_deal("done", status="CONFIRMED"),
    ]
    watcher = make(account, state, notifier, delivery_config, relist_config, first_run=True)

    await watcher.sweep(seeding=True)
    await watcher.sweep()

    assert account.chats.sent == []
    assert account.items.published == []
    assert state.get(SECTION, "old")["status"] == SKIPPED
    assert state.get(DEALS, "old")["result"] == "skipped"
    assert state.get(DEALS, "sent")["result"] == "skipped"
    assert notifier.find("Первый запуск: 1 оплаченных")
    assert account.deals.search_calls == [{"direction": "OUT"}] * 2

    # Новая продажа после старта обрабатывается как обычно.
    account.deals.page = [make_deal("new"), *account.deals.page]
    await watcher.sweep()

    assert account.chats.sent == [("c1", "Привет, ivan! Ключ: KEY-1")]
    assert account.items.published == ["i1"]
    assert notifier.find("Новая продажа")
    assert state.get(SECTION, "old")["status"] == SKIPPED


async def test_restart_delivers_deals_paid_while_offline(
    account, state, notifier, delivery_config, relist_config
):
    state.save()
    restarted = State.load(state.path)
    account.deals.page = [make_deal("missed")]
    watcher = make(account, restarted, notifier, delivery_config, relist_config, first_run=False)

    await watcher.sweep(seeding=True)

    assert restarted.get(SECTION, "missed")["status"] == DELIVERED
    assert notifier.find("пока бот был выключен")


async def test_status_changes_are_notified_once(
    account, state, notifier, delivery_config, relist_config
):
    watcher = make(account, state, notifier, delivery_config, relist_config, first_run=False)
    await watcher.sweep(seeding=True)

    await watcher.handle(make_deal(status="PAID"))
    await watcher.handle(make_deal(status="PAID"))
    await watcher.handle(make_deal(status="SENT"))
    await watcher.handle(make_deal(status="SENT", has_problem=True))
    await watcher.handle(make_deal(status="CONFIRMED", has_problem=True))

    assert len(notifier.find("Новая продажа")) == 1
    assert len(notifier.find("сообщил о проблеме")) == 1
    assert len(notifier.find("подтвердил сделку")) == 1
    assert len(account.chats.sent) == 1


async def test_sale_sent_while_offline_is_relisted(
    account, state, notifier, delivery_config, relist_config
):
    account.deals.page = [make_deal("sent", status="SENT")]
    watcher = make(account, state, notifier, delivery_config, relist_config, first_run=False)

    await watcher.sweep(seeding=True)

    assert account.chats.sent == []
    assert account.items.published == ["i1"]


async def test_token_rejection_alerts_once(
    account, state, notifier, delivery_config, relist_config, monkeypatch
):
    from PlayerokAPI import GraphQLError

    import playerok_bot.watchers as watchers

    async def rejected(**kwargs):
        raise GraphQLError([{"message": "no", "extensions": {"code": "UNAUTHENTICATED"}}])

    sleeps = 0

    async def fake_sleep(seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            raise asyncio.CancelledError

    account.deals.search = rejected
    monkeypatch.setattr(watchers.asyncio, "sleep", fake_sleep)
    watcher = make(account, state, notifier, delivery_config, relist_config, first_run=False)

    with pytest.raises(asyncio.CancelledError):
        await watcher.run_polling(1)

    assert len(notifier.find("отклонил токен")) == 1


async def test_purchases_are_ignored(account, state, notifier, delivery_config, relist_config):
    watcher = make(account, state, notifier, delivery_config, relist_config, first_run=False)

    await watcher.handle(make_deal(direction="IN"))

    assert account.chats.sent == []
    assert notifier.messages == []


def _message(msg_id: str, chat_id: str, user_id: str | None, text: str, event=None) -> MessageEvent:
    raw = {"id": msg_id, "text": text, "event": event}
    if user_id:
        raw["user"] = {"id": user_id, "username": f"user-{user_id}"}
    return MessageEvent(type=EventType.NEW_MESSAGE, message=ChatMessage.from_dict(raw, chat_id))


async def test_message_errors_do_not_stop_the_bot(account, notifier):
    account.polling_events = [_message("m1", "c1", "buyer", "привет")]

    async def broken(text):
        raise RuntimeError("boom")

    notifier.send = broken
    await watch_messages(account, "me", notifier, interval=1)


async def test_messages_forwarded_except_own_system_and_notifications(account, notifier):
    account.polling_events = [
        ChatEvent(type=EventType.CHAT_UPDATED, chat=Chat.from_dict({"id": "c1", "type": "PM"})),
        _message("m1", "c1", "buyer", "Здравствуйте <b>!"),
        _message("m2", "c1", "me", "мой ответ"),
        _message("m3", "c1", None, "системное", event="CHAT_STARTED"),
        ChatEvent(
            type=EventType.CHAT_UPDATED,
            chat=Chat.from_dict({"id": "n1", "type": "NOTIFICATIONS"}),
        ),
        _message("m4", "n1", "playerok", "У вас новый заказ"),
    ]

    await watch_messages(account, "me", notifier, interval=1)

    assert len(notifier.messages) == 1
    text = notifier.messages[0]
    assert "user-buyer" in text and "Здравствуйте &lt;b&gt;!" in text
    assert "https://playerok.com/chats/c1" in text
