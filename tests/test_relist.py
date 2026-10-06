from __future__ import annotations

from PlayerokAPI import ItemProfile

from playerok_bot.relist import DEALS, ITEMS, AutoRelist

from .conftest import make_deal


def make(account, relist_config, state, notifier, can_sell=lambda name: True) -> AutoRelist:
    return AutoRelist(account, "me", relist_config, state, notifier, can_sell)


async def test_after_sale_publishes_sold_item_once(account, relist_config, state, notifier):
    relist = make(account, relist_config, state, notifier)
    deal = make_deal()

    await relist.after_sale(deal)
    await relist.after_sale(deal)

    assert account.items.published == ["i1"]
    assert state.get(DEALS, "d1")["result"] == "ok"
    assert notifier.find("снова в продаже")


async def test_after_sale_skips_item_still_on_sale(account, relist_config, state, notifier):
    account.items.status["i1"] = "APPROVED"
    relist = make(account, relist_config, state, notifier)

    for _ in range(4):
        await relist.after_sale(make_deal())

    assert account.items.published == []
    assert state.get(DEALS, "d1")["result"] == "still_active"
    assert state.get(DEALS, "d1")["checks"] == 3


async def test_after_sale_waits_for_item_to_become_sold(account, relist_config, state, notifier):
    account.items.status["i1"] = "APPROVED"
    relist = make(account, relist_config, state, notifier)

    await relist.after_sale(make_deal())
    account.items.status["i1"] = "SOLD"
    await relist.after_sale(make_deal())

    assert account.items.published == ["i1"]
    assert state.get(DEALS, "d1")["result"] == "ok"


async def test_after_sale_respects_stock(account, relist_config, state, notifier):
    relist = make(account, relist_config, state, notifier, can_sell=lambda name: False)

    await relist.after_sale(make_deal())

    assert account.items.published == []
    assert notifier.find("не хватает товара")


async def test_after_sale_respects_match_filter(account, relist_config, state, notifier):
    relist_config.match = ["Гайд"]
    relist = make(account, relist_config, state, notifier)

    await relist.after_sale(make_deal(name="Ключ Steam"))

    assert account.items.published == []


async def test_after_sale_disabled(account, relist_config, state, notifier):
    relist_config.after_sale = False
    await make(account, relist_config, state, notifier).after_sale(make_deal())
    assert account.items.published == []


async def test_publish_error_is_reported(account, relist_config, state, notifier):
    account.items.publish_fails = True
    relist = make(account, relist_config, state, notifier)

    await relist.after_sale(make_deal())

    assert state.get(DEALS, "d1")["result"] == "error"
    assert notifier.find("Не удалось перевыставить")


def _expired(item_id: str, name: str = "Ключ Steam") -> ItemProfile:
    return ItemProfile.from_dict({"id": item_id, "name": name, "status": "EXPIRED"})


async def test_expired_items_relisted_with_cooldown(account, relist_config, state, notifier):
    account.items.expired = [_expired("e1"), _expired("e2", "Гайд")]
    relist_config.match = ["ключ"]
    relist = make(account, relist_config, state, notifier)

    await relist.relist_expired()
    await relist.relist_expired()

    assert account.items.published == ["e1"]
    assert state.get(ITEMS, "e1")["status"] == "PENDING_APPROVAL"


async def test_expired_copy_is_never_relisted_again(account, relist_config, state, notifier):
    account.items.expired = [_expired("e1")]
    account.items.publish_returns["e1"] = "e1-copy"
    relist = make(account, relist_config, state, notifier)

    await relist.relist_expired()
    # Даже когда пауза между попытками давно прошла.
    state.get(ITEMS, "e1")["at"] = "2000-01-01T00:00:00+00:00"
    await relist.relist_expired()

    assert account.items.published == ["e1"]
    assert state.get(ITEMS, "e1")["replaced_by"] == "e1-copy"


async def test_expired_without_stock_is_skipped(account, relist_config, state, notifier):
    account.items.expired = [_expired("e1")]
    relist = make(account, relist_config, state, notifier, can_sell=lambda name: False)

    await relist.relist_expired()

    assert account.items.published == []
