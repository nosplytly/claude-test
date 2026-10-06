from __future__ import annotations

import asyncio

import pytest
from PlayerokAPI import Chat, ChatEvent, ChatMessage, EventType, MessageEvent

import playerok_bot.watchers as watchers
from playerok_bot.config import DeliveryRule
from playerok_bot.delivery import DELIVERED, SECTION, SKIPPED, AutoDelivery
from playerok_bot.relist import DEALS, AutoRelist
from playerok_bot.storage import State
from playerok_bot.watchers import DealWatcher, watch_messages

from .conftest import FlakyReplace, make_deal


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
    assert notifier.find("Первый запуск: 1 оплаченная сделка уже ждала выдачи")
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


# --- одна сломанная сделка не останавливает остальные ----------------------------------


async def test_poison_deal_does_not_block_newer_deals(
    account, state, notifier, delivery_config, relist_config, tmp_path
):
    ansi = tmp_path / "accounts.txt"
    ansi.write_bytes("логин:пароль\r\n".encode("cp1251"))
    delivery_config.rules.insert(
        0, DeliveryRule(match="Аккаунт", message="Ваш аккаунт: {product}", stock_file=ansi)
    )
    relist_config.after_sale = False
    watcher = make(account, state, notifier, delivery_config, relist_config, first_run=False)
    await watcher.sweep(seeding=True)
    # Новые сверху: d2 (ключ) оплатили позже d1 (аккаунт с битым файлом склада).
    account.deals.page = [
        make_deal("d2", name="Ключ Steam", chat_id="c2"),
        make_deal("d1", name="Аккаунт Fortnite", chat_id="c1"),
    ]

    for _ in range(3):
        await watcher.sweep()

    assert account.chats.sent == [("c2", "Привет, ivan! Ключ: KEY-1")]
    assert state.get(SECTION, "d2")["status"] == DELIVERED
    assert [deal.id for deal in watcher.recent] == ["d2", "d1"]
    # Продавец узнаёт о проблеме один раз и с понятной причиной.
    [alert] = notifier.find("не удалось обработать")
    assert "accounts.txt" in alert and "UTF-8" in alert
    assert len(notifier.find("Новая продажа")) == 2


async def test_first_run_seeding_retries_until_all_old_deals_recorded(
    account, state, notifier, delivery_config, relist_config, monkeypatch
):
    account.deals.page = [make_deal("old1"), make_deal("old2")]
    watcher = make(account, state, notifier, delivery_config, relist_config, first_run=True)
    flaky = FlakyReplace(monkeypatch, failing=False, only="state.json")
    flaky.fail_next = 9  # одна запись (с повторами) не удалась

    with pytest.raises(watchers.SeedingIncomplete):
        await watcher.sweep(seeding=True)
    assert not notifier.find("Первый запуск")

    await watcher.sweep(seeding=True)

    assert state.get(SECTION, "old1")["status"] == SKIPPED
    assert state.get(SECTION, "old2")["status"] == SKIPPED
    assert notifier.find("Первый запуск: 2 оплаченные сделки уже ждали выдачи")
    assert account.chats.sent == []


async def test_first_run_state_failure_is_not_reported_as_no_connection(
    account, state, notifier, delivery_config, relist_config, monkeypatch
):
    """Старые сделки не записались (state.json занят): Playerok при этом отвечает,
    поэтому «Нет связи с Playerok» (и совет про VPN) был бы ложным."""
    account.deals.page = [make_deal("old1")]
    watcher = make(account, state, notifier, delivery_config, relist_config, first_run=True)
    FlakyReplace(monkeypatch, only="state.json")
    sleeps = 0

    async def fake_sleep(seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(watchers.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await watcher.run_polling(20)

    assert not notifier.find("Нет связи")
    assert watcher.problem is None or watcher.problem["kind"] != "network"
    assert notifier.find("не удалось обработать")
    assert account.chats.sent == []


async def test_poll_problems_are_reported_and_cleared(
    account, state, notifier, delivery_config, relist_config, monkeypatch
):
    from PlayerokAPI import NetworkError

    watcher = make(account, state, notifier, delivery_config, relist_config, first_run=False)
    real_search = account.deals.search
    outage = True

    async def search(**kwargs):
        if outage:
            raise NetworkError("connection reset")
        return await real_search(**kwargs)

    account.deals.search = search
    sleeps = 0

    async def fake_sleep(seconds):
        nonlocal sleeps, outage
        sleeps += 1
        if sleeps == 2:
            assert watcher.problem is None  # два сбоя подряд — ещё не тревога
        if sleeps == 4:
            assert watcher.problem["kind"] == "network"
            assert "Нет связи с Playerok" in watcher.problem["text"]
            outage = False
        if sleeps == 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(watchers.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await watcher.run_polling(1)

    assert watcher.problem is None and watcher.last_poll_at is not None
    assert len(notifier.find("Нет связи с Playerok")) == 1
    assert notifier.find("Связь с Playerok восстановлена")


async def test_rejected_token_is_a_problem_until_polls_work(
    account, state, notifier, delivery_config, relist_config, monkeypatch
):
    from PlayerokAPI import GraphQLError

    async def rejected(**kwargs):
        raise GraphQLError([{"message": "no", "extensions": {"code": "UNAUTHENTICATED"}}])

    account.deals.search = rejected

    async def stop(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(watchers.asyncio, "sleep", stop)
    watcher = make(account, state, notifier, delivery_config, relist_config, first_run=False)

    with pytest.raises(asyncio.CancelledError):
        await watcher.run_polling(1)

    assert watcher.problem["kind"] == "auth"
    [alert] = notifier.find("отклонил токен")
    assert "Настройках SupplierHub" in alert or "настройках SupplierHub" in alert


async def test_state_that_cannot_be_saved_raises_alarm(
    account, state, notifier, delivery_config, relist_config, monkeypatch
):
    monkeypatch.setattr(watchers, "_STATE_ALERT_AFTER", 0.0)
    flaky = FlakyReplace(monkeypatch, only="state.json")
    account.deals.page = [make_deal("d1")]
    watcher = make(account, state, notifier, delivery_config, relist_config, first_run=False)

    await watcher.sweep(seeding=True)  # резерв не записывается — товар не уходит
    await watcher._check_state()

    assert account.chats.sent == []
    assert watcher.problem["kind"] == "state"
    assert len(notifier.find("Не удаётся сохранить")) == 1

    flaky.failing = False
    await watcher.sweep()
    await watcher._check_state()

    assert account.chats.sent == [("c1", "Привет, ivan! Ключ: KEY-1")]
    assert watcher.problem is None
    assert notifier.find("снова сохраняется")
