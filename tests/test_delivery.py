from __future__ import annotations

from pathlib import Path

from playerok_bot.delivery import (
    DELIVERED,
    FAILED,
    MAX_ATTEMPTS,
    NO_RULE,
    NO_STOCK,
    RESERVED,
    SECTION,
    AutoDelivery,
    render_message,
)

from .conftest import make_deal


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
