from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from playerok_bot.config import DeliveryRule
from playerok_bot.delivery import (
    DELIVERED,
    FAILED,
    MANUAL,
    MAX_ATTEMPTS,
    NO_RULE,
    NO_STOCK,
    RESERVED,
    SECTION,
    AutoDelivery,
    DeliveryError,
    render_message,
)
from playerok_bot.stock import StockError
from playerok_bot.storage import State

from .conftest import FlakyReplace, make_deal


def make(account, delivery_config, state, notifier) -> AutoDelivery:
    return AutoDelivery(account, delivery_config, state, notifier)


async def test_delivers_product_and_marks_sent(
    account, delivery_config, state, notifier, stock_file
):
    delivery = make(account, delivery_config, state, notifier)

    await delivery.process(make_deal())

    assert account.chats.sent == [("c1", "Привет, ivan! Ключ: KEY-1")]
    assert stock_file.read_text(encoding="utf-8") == "KEY-2\nKEY-3\n"
    assert account.deals.updates == [("d1", {"status": "SENT"})]
    entry = state.get(SECTION, "d1")
    assert entry["status"] == DELIVERED
    assert entry["products"] == ["KEY-1"]
    assert notifier.find("Товар выдан")
    assert "Осталось" in notifier.messages[-1]


async def test_second_call_does_not_deliver_again(
    account, delivery_config, state, notifier, stock_file
):
    delivery = make(account, delivery_config, state, notifier)
    deal = make_deal()

    await delivery.process(deal)
    await delivery.process(deal)

    assert len(account.chats.sent) == 1
    assert stock_file.read_text(encoding="utf-8") == "KEY-2\nKEY-3\n"


async def test_delivered_state_survives_restart(
    account, delivery_config, state, notifier, tmp_path
):
    await make(account, delivery_config, state, notifier).process(make_deal())

    reloaded = type(state).load(state.path)
    await make(account, delivery_config, reloaded, notifier).process(make_deal())

    assert len(account.chats.sent) == 1


async def test_rule_without_stock_sends_fixed_text(account, delivery_config, state, notifier):
    delivery = make(account, delivery_config, state, notifier)

    await delivery.process(make_deal(name="Гайд по фарму"))

    assert account.chats.sent == [("c1", "Ссылка: https://example.com")]
    assert state.get(SECTION, "d1")["status"] == DELIVERED


async def test_no_rule_notifies_and_does_not_send(account, delivery_config, state, notifier):
    delivery = make(account, delivery_config, state, notifier)

    await delivery.process(make_deal(name="Аккаунт Fortnite"))

    assert account.chats.sent == []
    assert state.get(SECTION, "d1")["status"] == NO_RULE
    assert notifier.find("нет правила автовыдачи")


async def test_empty_stock_waits_and_delivers_after_refill(
    account, delivery_config, state, notifier, stock_file
):
    stock_file.write_text("", encoding="utf-8")
    delivery = make(account, delivery_config, state, notifier)
    deal = make_deal()

    await delivery.process(deal)
    await delivery.process(deal)

    assert account.chats.sent == []
    assert state.get(SECTION, "d1")["status"] == NO_STOCK
    assert len(notifier.find("Закончился товар")) == 1

    stock_file.write_text("NEW-KEY\n", encoding="utf-8")
    await delivery.process(deal)

    assert account.chats.sent == [("c1", "Привет, ivan! Ключ: NEW-KEY")]
    assert notifier.find("пора пополнить")


async def test_send_error_retries_with_same_product_then_fails(
    account, delivery_config, state, notifier, stock_file
):
    account.chats.fail_with = RuntimeError("chat is closed")
    delivery = make(account, delivery_config, state, notifier)
    deal = make_deal()

    for _ in range(MAX_ATTEMPTS):
        await delivery.process(deal)

    entry = state.get(SECTION, "d1")
    assert entry["status"] == FAILED
    assert entry["products"] == ["KEY-1"]
    # Со склада списан ровно один товар, несмотря на повторы.
    assert stock_file.read_text(encoding="utf-8") == "KEY-2\nKEY-3\n"
    assert len(notifier.find("Ошибка выдачи")) == 1
    failed = notifier.find("Не удалось выдать")
    assert len(failed) == 1 and "KEY-1" in failed[0]

    account.chats.fail_with = None
    await delivery.process(deal)
    assert account.chats.sent == []


async def test_reserved_product_is_resent_after_crash(
    account, delivery_config, state, notifier, stock_file
):
    state.put(SECTION, "d1", status=RESERVED, products=["KEY-0"])
    delivery = make(account, delivery_config, state, notifier)

    await delivery.process(make_deal())

    assert account.chats.sent == [("c1", "Привет, ivan! Ключ: KEY-0")]
    assert stock_file.read_text(encoding="utf-8") == "KEY-1\nKEY-2\nKEY-3\n"


async def test_missing_chat_is_fetched_from_deal(account, delivery_config, state, notifier):
    account.deals.by_id["d1"] = make_deal(chat_id="c-from-api")
    delivery = make(account, delivery_config, state, notifier)

    await delivery.process(make_deal(chat_id=None))

    assert account.chats.sent[0][0] == "c-from-api"


async def test_mark_sent_failure_is_reported(account, delivery_config, state, notifier):
    account.deals.update_fails = True
    delivery = make(account, delivery_config, state, notifier)

    await delivery.process(make_deal())

    assert state.get(SECTION, "d1")["status"] == DELIVERED
    assert notifier.find("Подтвердите вручную")


async def test_disabled_delivery_does_nothing(account, delivery_config, state, notifier):
    delivery_config.enabled = False
    delivery = make(account, delivery_config, state, notifier)

    await delivery.process(make_deal())

    assert account.chats.sent == []
    assert delivery.can_sell("Ключ Steam")


async def test_can_sell_checks_stock(account, delivery_config, state, notifier, stock_file):
    delivery = make(account, delivery_config, state, notifier)
    assert delivery.can_sell("Ключ Steam Witcher")
    assert delivery.can_sell("Что-то без правила")

    stock_file.write_text("", encoding="utf-8")
    assert not delivery.can_sell("Ключ Steam Witcher")


def test_render_does_not_expand_placeholders_inside_product():
    text = render_message("{buyer}: {product}", ["код {buyer} {item}"], make_deal(buyer="petya"))
    assert text == "petya: код {buyer} {item}"


def test_render_joins_several_products(tmp_path: Path):
    text = render_message("{item}\n{product}", ["A", "B"], make_deal(name="Набор"))
    assert text == "Набор\nA\nB"


# --- сбои записи state.json (Windows держит файл) ----------------------------------


async def test_failed_delivered_write_still_marks_sent_and_alerts(
    account, delivery_config, state, notifier, stock_file, monkeypatch
):
    delivery = make(account, delivery_config, state, notifier)
    deal = make_deal()
    real_put = state.put

    # Резерв записался, а запись «выдано» после отправки — нет (файл занят).
    def put(section, key, **fields):
        entry = real_put(section, key, **fields)
        flaky.failing = True
        return entry

    flaky = FlakyReplace(monkeypatch, failing=False)
    monkeypatch.setattr(state, "put", put)

    await delivery.process(deal)

    assert account.chats.sent == [("c1", "Привет, ivan! Ключ: KEY-1")]
    # Сделка всё равно отмечена отправленной и продавец получил уведомление.
    assert account.deals.updates == [("d1", {"status": "SENT"})]
    assert notifier.find("Товар выдан")
    assert state.get(SECTION, "d1")["status"] == DELIVERED and state.dirty
    assert stock_file.read_text(encoding="utf-8") == "KEY-2\nKEY-3\n"

    # Повторный вызов в этом же запуске не выдаёт второй раз.
    await delivery.process(deal)
    assert len(account.chats.sent) == 1

    # Диск освободился — отложенная запись доезжает, и после перезапуска тоже тишина.
    flaky.failing = False
    assert state.flush()
    reloaded = type(state).load(state.path)
    await make(account, delivery_config, reloaded, notifier).process(deal)
    assert len(account.chats.sent) == 1


async def test_failed_reserve_returns_goods_to_stock(
    account, delivery_config, state, notifier, stock_file, monkeypatch
):
    flaky = FlakyReplace(monkeypatch, failing=False, only="state.json")
    real_put = state.put

    def put(section, key, **fields):
        if fields.get("status") == RESERVED:
            flaky.failing = True
        return real_put(section, key, **fields)

    monkeypatch.setattr(state, "put", put)
    delivery = make(account, delivery_config, state, notifier)

    with pytest.raises(PermissionError):
        await delivery.process(make_deal())

    # Товар не ушёл покупателю и не пропал со склада.
    assert account.chats.sent == []
    flaky.failing = False
    assert stock_file.read_text(encoding="utf-8") == "KEY-1\nKEY-2\nKEY-3\n"
    assert state.get(SECTION, "d1") is None

    # После перезапуска покупатель получает тот же первый ключ, а не следующий.
    restarted = make(account, delivery_config, type(state).load(state.path), notifier)
    await restarted.process(make_deal())
    assert account.chats.sent == [("c1", "Привет, ivan! Ключ: KEY-1")]
    assert stock_file.read_text(encoding="utf-8") == "KEY-2\nKEY-3\n"


async def test_goods_lost_alert_when_stock_cannot_be_restored(
    account, delivery_config, state, notifier, stock_file, monkeypatch
):
    flaky = FlakyReplace(monkeypatch, failing=False)
    real_put = state.put

    def put(section, key, **fields):
        if fields.get("status") == RESERVED:
            flaky.failing = True
        return real_put(section, key, **fields)

    monkeypatch.setattr(state, "put", put)

    with pytest.raises(PermissionError):
        await make(account, delivery_config, state, notifier).process(make_deal())

    [alert] = notifier.find("Верните строки в файл")
    assert "KEY-1" in alert


# --- уже выданный товар снова на складе (файл пересохранили из Блокнота) -------------


async def test_line_restored_by_external_editor_is_not_sold_twice(
    account, delivery_config, state, notifier, stock_file
):
    delivery = make(account, delivery_config, state, notifier)
    await delivery.process(make_deal("dA", buyer="alice", chat_id="cA"))
    # Блокнот, открытый до продажи, сохранил свой буфер: KEY-1 вернулся наверх.
    stock_file.write_text("KEY-1\nKEY-2\nKEY-3\nKEY-4\n", encoding="utf-8")

    await delivery.process(make_deal("dB", buyer="bob", chat_id="cB"))

    assert account.chats.sent == [
        ("cA", "Привет, alice! Ключ: KEY-1"),
        ("cB", "Привет, bob! Ключ: KEY-2"),
    ]
    assert stock_file.read_text(encoding="utf-8") == "KEY-3\nKEY-4\n"
    [alert] = notifier.find("уже выдавались")
    assert "https://playerok.com/deal/dA" in alert


async def test_goods_are_not_lost_if_stopped_while_reporting_old_lines(
    account, delivery_config, state, notifier, stock_file
):
    """Бота остановили, пока уходило ⚠️ про уже выданные строки (Telegram медленный):
    забранный со склада товар не должен пропасть ни из файла, ни из state.json."""
    delivery = make(account, delivery_config, state, notifier)
    await delivery.process(make_deal("dA", buyer="alice", chat_id="cA"))
    stock_file.write_text("KEY-1\nKEY-2\nKEY-3\n", encoding="utf-8")
    real_send = notifier.send

    async def stopped_while_sending(text: str) -> bool:
        if "уже выдавались" in text:
            raise asyncio.CancelledError
        return await real_send(text)

    notifier.send = stopped_while_sending
    with pytest.raises(asyncio.CancelledError):
        await delivery.process(make_deal("dB", buyer="bob", chat_id="cB"))

    entry = state.get(SECTION, "dB")
    reserved = entry["products"] if entry and entry.get("status") == RESERVED else []
    left = stock_file.read_text(encoding="utf-8").split()
    assert "KEY-2" in reserved + left
    # После перезапуска уходит тот же KEY-2, а не новый ключ.
    notifier.send = real_send
    await make(account, delivery_config, State.load(state.path), notifier).process(
        make_deal("dB", buyer="bob", chat_id="cB")
    )
    assert account.chats.sent[-1] == ("cB", "Привет, bob! Ключ: KEY-2")
    assert stock_file.read_text(encoding="utf-8") == "KEY-3\n"


async def test_only_already_sold_lines_left_means_no_stock(
    account, delivery_config, state, notifier, stock_file
):
    delivery = make(account, delivery_config, state, notifier)
    await delivery.process(make_deal("dA"))
    stock_file.write_text("KEY-1\n", encoding="utf-8")

    await delivery.process(make_deal("dB"))

    assert len(account.chats.sent) == 1
    assert state.get(SECTION, "dB")["status"] == NO_STOCK
    assert stock_file.read_text(encoding="utf-8") == ""


async def test_stock_not_in_utf8_is_explained(
    account, delivery_config, state, notifier, stock_file
):
    stock_file.write_bytes("Ключ-1\r\n".encode("cp1251"))

    with pytest.raises(StockError, match="UTF-8"):
        await make(account, delivery_config, state, notifier).process(make_deal())

    assert account.chats.sent == []


# --- действия продавца: «выдано вручную» и «выдать заново» ---------------------------


async def test_mark_manual_is_final(account, delivery_config, state, notifier, stock_file):
    stock_file.write_text("", encoding="utf-8")
    delivery = make(account, delivery_config, state, notifier)
    await delivery.process(make_deal())
    assert state.get(SECTION, "d1")["status"] == NO_STOCK

    entry = await delivery.mark_manual("d1")
    assert entry["status"] == MANUAL and entry["handled_at"] and entry["was"] == NO_STOCK

    # Склад пополнили — сделку, решённую вручную, бот уже не выдаёт.
    stock_file.write_text("NEW\n", encoding="utf-8")
    await delivery.process(make_deal())
    assert account.chats.sent == []

    with pytest.raises(DeliveryError):
        await delivery.mark_manual("unknown")


async def test_retry_after_rule_was_added(account, delivery_config, state, notifier):
    delivery = make(account, delivery_config, state, notifier)
    deal = make_deal(name="Аккаунт Minecraft")
    await delivery.process(deal)
    assert state.get(SECTION, "d1")["status"] == NO_RULE

    with pytest.raises(DeliveryError, match="нет правила"):
        await delivery.retry(deal)

    delivery_config.rules.append(DeliveryRule(match="Minecraft", message="Логин: mc"))
    assert await delivery.retry(deal) == DELIVERED
    assert account.chats.sent == [("c1", "Логин: mc")]


async def test_retry_failed_resends_same_goods(
    account, delivery_config, state, notifier, stock_file
):
    account.chats.fail_with = RuntimeError("chat is closed")
    delivery = make(account, delivery_config, state, notifier)
    for _ in range(MAX_ATTEMPTS):
        await delivery.process(make_deal())
    assert state.get(SECTION, "d1")["status"] == FAILED

    account.chats.fail_with = None
    assert await delivery.retry(make_deal()) == DELIVERED

    assert account.chats.sent == [("c1", "Привет, ivan! Ключ: KEY-1")]
    assert stock_file.read_text(encoding="utf-8") == "KEY-2\nKEY-3\n"
    with pytest.raises(DeliveryError):
        await delivery.retry(make_deal())


def test_handles_only_lots_with_rules(account, delivery_config, state, notifier):
    delivery = make(account, delivery_config, state, notifier)

    assert delivery.handles("Ключ Steam Witcher")
    assert delivery.handles("Гайд по фарму")
    assert not delivery.handles("Аккаунт Genshin")
    delivery_config.enabled = False
    assert not delivery.handles("Ключ Steam Witcher")
