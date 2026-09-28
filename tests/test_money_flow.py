"""End-to-end money flow against the mock supplier and simulated on-chain transfers.

Run:  .venv\\Scripts\\python.exe tests\\test_money_flow.py
Uses a throw-away database; touches no real blockchain and no real supplier.
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="sh-test-")
os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "dev", "DATA_DIR": TMP, "NERVIXY_MOCK": "true", "BOT_TOKEN": "", "ADMIN_TG_IDS": "",
                   "MONITOR_ENABLED": "false", "CLIENT_DISCOUNT": "3.75", "VOLATILE_MARKUP": "0.5",
                   # placeholder addresses: nothing is ever sent anywhere in this test
                   "WALLET_USDT_TRC20": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb", "WALLET_TRX": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
                   "WALLET_USDT_TON": "UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJKZ",
                   "WALLET_TON": "UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJKZ",
                   "WALLET_USDT_BEP20": "0x000000000000000000000000000000000000dEaD",
                   "WALLET_USDT_ERC20": "0x000000000000000000000000000000000000dEaD",
                   "WALLET_ETH": "0x000000000000000000000000000000000000dEaD",
                   "WALLET_BTC": "1BitcoinEaterAddressDontSendf59kuE"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app import orders as O  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Claim, Invoice, LedgerEntry, Order, Transfer, User  # noqa: E402
from app.money import usd_str  # noqa: E402
from app.nervixy import NervixyTransportError, nervixy  # noqa: E402
from app.payments import monitor as M  # noqa: E402
from app.payments.chains.base import Incoming  # noqa: E402
from app.payments.methods import METHODS  # noqa: E402
from app.prices import price_feed  # noqa: E402
from app.utils import utcnow  # noqa: E402

OK = 0


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


async def user(tg_id: int) -> int:
    async with session_scope() as s:
        u = User(tg_id=tg_id, username=f"u{tg_id}")
        s.add(u)
        await s.flush()
        return u.id


async def order(uid: int, method: str | None, amount="1000", login="gooduser", currency="RUB") -> Order:
    async with session_scope() as s:
        u = await s.get(User, uid)
        return await O.create_order(s, u, steam_login=login, amount=Decimal(amount), currency=currency,
                                    method_code=method)


async def invoice_of(order_id: int) -> Invoice:
    async with session_scope() as s:
        return (await s.execute(select(Invoice).where(Invoice.order_id == order_id).order_by(Invoice.id.desc()))).scalars().first()


async def get(model, id_):
    async with session_scope() as s:
        return await s.get(model, id_)


_tx = 0


async def pay(method: str, units: int, *, comment=None, final=True, when=None, txid=None) -> str:
    global _tx
    _tx += 1
    txid = txid or f"dev{_tx:060d}"
    m = METHODS[method]
    await M.ingest([Incoming(method=method, txid=txid, idx="0", to_address=m.address, units=units,
                             from_address="sender", comment=comment, block_time=when or utcnow(),
                             confirmations=m.confirmations if final else 0, final=final)])
    await M.settle({})
    return txid


async def _find_order_id(public_id: str) -> int:
    async with session_scope() as s:
        return (await s.execute(select(Order.id).where(Order.public_id == public_id))).scalar_one()


async def balance(uid: int) -> int:
    return (await get(User, uid)).balance_micro


async def drive(order_id: int):
    """Run the processor until the order leaves the transient states."""
    for _ in range(10):
        await O.process_once()
        o = await get(Order, order_id)
        if o.status == "processing":
            await O.check_status(order_id)
            o = await get(Order, order_id)
        if o.status in ("delivered", "rejected", "queued", "uncertain", "awaiting_payment", "expired"):
            return o
    return await get(Order, order_id)


async def main():
    await init_db()
    nervixy.client.delay = 0
    price_feed.prices = {"BTC": Decimal("80000"), "ETH": Decimal("3000"), "LTC": Decimal("90"), "TON": Decimal("3"),
                         "TRX": Decimal("0.3"), "SOL": Decimal("150")}
    price_feed.updated_at = time.time()

    print("1. exact unique amount -> paid -> delivered")
    u1 = await user(1)
    o = await order(u1, "usdt_trc20")
    inv = await invoice_of(o.id)
    check(inv.amount > 0 and inv.status == "pending", f"invoice {inv.amount} USDT created")
    await pay("usdt_trc20", int(inv.units), final=False)
    check((await get(Invoice, inv.id)).status == "detected", "unconfirmed transfer marks invoice detected")
    check(await balance(u1) == 0, "nothing credited before confirmation")
    await M.ingest([Incoming(method="usdt_trc20", txid=f"dev{_tx:060d}", idx="0", to_address=inv.address,
                             units=int(inv.units), confirmations=1, final=True)])
    await M.settle({})
    o2 = await get(Order, o.id)
    check(o2.status == "paid", "confirmed -> order paid from balance")
    check(await balance(u1) >= 0, "balance never negative")
    o2 = await drive(o.id)
    check(o2.status == "delivered", "supplier delivered")

    print("2. same transfer seen twice is credited once")
    before = await balance(u1)
    await M.ingest([Incoming(method="usdt_trc20", txid=f"dev{_tx:060d}", idx="0", to_address=inv.address,
                             units=int(inv.units), confirmations=1, final=True)])
    await M.settle({})
    check(await balance(u1) == before, "no double credit")

    print("3. two invoices in flight get different amounts")
    u2 = await user(2)
    a = await order(u2, "usdt_trc20")
    b = await order(u2, "usdt_trc20")
    ia, ib = await invoice_of(a.id), await invoice_of(b.id)
    check(ia.units != ib.units, f"unique amounts {ia.amount} vs {ib.amount}")

    print("4. underpayment (exchange fee) -> fuzzy match -> partial -> remainder invoice -> paid")
    short = int(Decimal(ia.units) * Decimal("0.97"))
    await O.expire_stale()
    async with session_scope() as s:  # make `a` the only open invoice on this network
        (await s.get(Invoice, ib.id)).status = "cancelled"
        (await s.get(Order, b.id)).status = "cancelled"
    await pay("usdt_trc20", short)
    oa = await get(Order, a.id)
    rem = await invoice_of(a.id)
    check(oa.status == "awaiting_payment" and rem.id != ia.id and rem.status == "pending",
          f"partial kept order open, remainder invoice {rem.amount} USDT")
    check(await balance(u2) > 0, f"partial amount held on balance ({usd_str(await balance(u2))}$)")
    await pay("usdt_trc20", int(rem.units))
    oa = await drive(a.id)
    check(oa.status == "delivered", "remainder paid -> delivered")
    check(abs(await balance(u2)) < 30_000, f"balance after ~0 ({usd_str(await balance(u2), 4)}$)")

    print("5. TON memo matches even with a wrong amount")
    u3 = await user(3)
    t = await order(u3, "ton")
    it = await invoice_of(t.id)
    check(it.comment == t.public_id, "TON invoice carries the order id as memo")
    await pay("ton", int(it.units) + 5_000_000, comment=f"order {t.public_id.lower()}")
    ot = await drive(t.id)
    check(ot.status == "delivered", "memo-matched overpayment -> delivered")
    check(await balance(u3) > 0, f"overpayment stays on balance ({usd_str(await balance(u3))}$)")

    print("6. late payment after expiry -> balance only, order expired")
    u4 = await user(4)
    lo = await order(u4, "usdt_bep20")
    li = await invoice_of(lo.id)
    async with session_scope() as s:
        x = await s.get(Invoice, li.id)
        x.expires_at = utcnow() - timedelta(hours=1)
        x.created_at = utcnow() - timedelta(hours=2)
    await O.expire_stale()
    check((await get(Order, lo.id)).status == "expired", "unpaid order expired")
    await pay("usdt_bep20", int(li.units))
    check((await get(Order, lo.id)).status == "expired", "late payment does not revive the order")
    check(await balance(u4) >= li.usd_micro - 1, f"late payment credited to balance ({usd_str(await balance(u4))}$)")

    print("7. next order is paid from balance instantly")
    lo2 = await order(u4, None, amount="500")
    check(lo2.status == "paid" and lo2.method == "balance", "balance covers -> paid without invoice")
    check((await drive(lo2.id)).status == "delivered", "delivered")

    print("8. unknown transfer + claim -> admin approve -> order paid")
    u5 = await user(5)
    co = await order(u5, "usdt_erc20")
    ci = await invoice_of(co.id)
    weird = int(Decimal(ci.units) * Decimal("0.5"))
    txid = await pay("usdt_erc20", weird)
    tr = None
    async with session_scope() as s:
        tr = (await s.execute(select(Transfer).where(Transfer.txid == txid))).scalar_one()
    check(tr.status == "unmatched", "50% payment is not auto-matched")
    async with session_scope() as s:
        s.add(Claim(user_id=u5, order_id=co.id, txid=txid, status="waiting"))
    await M.process_claims()
    async with session_scope() as s:
        cl = (await s.execute(select(Claim).where(Claim.txid == txid))).scalar_one()
    check(cl.status == "pending", "claim bound to the transfer, waiting for admin")
    msg = await M.approve_claim(cl.id, True)
    check("Зачислено" in msg, f"admin approved: {msg}")
    check(await balance(u5) > 0, "claimed amount credited")
    check((await get(Order, co.id)).status == "awaiting_payment", "half is not enough to pay the order")

    print("8b. somebody else's old tx hash cannot be claimed")
    old_tx = await pay("usdt_erc20", 7_000_000, when=utcnow() - timedelta(days=2))
    u5b = await user(55)
    ob = await order(u5b, "usdt_erc20")
    async with session_scope() as s:
        s.add(Claim(user_id=u5b, order_id=ob.id, txid=old_tx, status="waiting"))
    await M.process_claims()
    async with session_scope() as s:
        cl = (await s.execute(select(Claim).where(Claim.txid == old_tx))).scalar_one()
    check(cl.status == "rejected" and "старше" in (cl.note or ""), f"claim rejected: {cl.note}")

    print("9. supplier out of funds -> queued -> retried after top-up")
    u6 = await user(6)
    saved = nervixy.client.balance
    q = await order(u6, "usdt_trc20", amount="1000")
    nervixy.client.balance = Decimal("1")
    qi = await invoice_of(q.id)
    await pay("usdt_trc20", int(qi.units))
    oq = await drive(q.id)
    check(oq.status == "queued", "queued when supplier balance is too low")
    nervixy.client.balance = saved
    async with session_scope() as s:
        (await s.get(Order, q.id)).next_check_at = utcnow()
    check((await drive(q.id)).status == "delivered", "delivered after supplier top-up")

    print("10. supplier timeout after creating the order -> reconciled via partner_id")
    u7 = await user(7)
    r = await order(u7, "usdt_trc20")
    ri = await invoice_of(r.id)
    real = nervixy.client.order

    async def flaky(**kw):
        await real(**kw)
        raise NervixyTransportError("ReadTimeout")
    nervixy.client.order = flaky
    await pay("usdt_trc20", int(ri.units))
    await O.process_once()
    nervixy.client.order = real
    check((await get(Order, r.id)).status == "uncertain", "unknown outcome -> uncertain (no blind retry)")
    await O.reconcile(r.id)
    o7 = await get(Order, r.id)
    check(o7.status == "processing" and o7.nervixy_order_id, "found in supplier history by partner_id")
    check((await drive(r.id)).status == "delivered", "delivered, charged once")
    check(len([x for x in nervixy.client._orders.values() if x["partner_id"] == r.public_id]) == 1,
          "exactly one supplier order")

    print("11. supplier rejects -> refund to balance")
    u8 = await user(8)
    j = await order(u8, "usdt_trc20", login="rejectme")
    ji = await invoice_of(j.id)
    await pay("usdt_trc20", int(ji.units))
    oj = await drive(j.id)
    check(oj.status == "rejected", "rejected")
    check(await balance(u8) >= j.price_micro - 20_000, f"refunded to balance ({usd_str(await balance(u8))}$)")

    print("12. dust is ignored")
    txid = await pay("usdt_trc20", 10)
    async with session_scope() as s:
        check((await s.execute(select(Transfer.status).where(Transfer.txid == txid))).scalar_one() == "ignored",
              "0.00001 USDT ignored")

    print("13. balance top-up without an order (+ partner webhook queued)")
    from app.deposits import create_deposit
    from app.models import WebhookEvent
    u9 = await user(9)
    async with session_scope() as s:
        uu = await s.get(User, u9)
        uu.webhook_url, uu.webhook_secret = "https://example.com/hook", "whsec_test"
        dep = await create_deposit(s, uu, amount_usd="25", method_code="usdt_bep20")
        dep_id, dep_units = dep.public_id, int(dep.units)
    check(dep_id.startswith("DP"), f"deposit {dep_id} created")
    await pay("usdt_bep20", dep_units)
    check(abs(await balance(u9) - 25_000_000) < 20_000, f"balance topped up to {usd_str(await balance(u9))}$")
    async with session_scope() as s:
        evs = (await s.execute(select(WebhookEvent.event).where(WebhookEvent.user_id == u9))).scalars().all()
    check(evs == ["deposit.credited"], "deposit.credited webhook queued")

    print("14. partner API")
    import httpx
    from app.main import app
    from app.models import ApiKey
    from app.utils import sha256
    raw = "SH-testkey-123456789"
    async with session_scope() as s:
        s.add(ApiKey(user_id=u9, key_hash=sha256(raw), prefix=raw[:10]))
    tr = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t") as c:
        H = {"X-API-Key": raw}
        r = await c.get("/api/v1/me", headers={"X-API-Key": "nope"})
        check(r.status_code == 401 and r.json()["ok"] is False, "bad key -> 401 {ok:false}")
        r = await c.get("/api/v1/me", headers=H)
        check(r.json()["ok"] and float(r.json()["balance_usd"]) > 24, f"/me balance {r.json()['balance_usd']}")
        r = await c.post("/api/v1/quote", headers=H, json={"amount": 1000, "currency": "RUB"})
        check(r.json()["ok"] and r.json()["price_usd"], f"/quote 1000 RUB = {r.json()['price_usd']}$")
        r = await c.post("/api/v1/orders", headers=H, json={"steam_login": "gooduser", "amount": 5000, "currency": "RUB"})
        check(r.status_code == 402 and "required_usd" in r.json(), "not enough balance -> 402 with required_usd")
        r = await c.post("/api/v1/orders", headers=H, json={"steam_login": "gooduser", "amount": 1000, "currency": "RUB",
                                                            "external_id": "ext-1"})
        j = r.json()
        check(j["ok"] and j["order"]["status"] == "processing" and not j["duplicate"], "order created from balance")
        r2 = await c.post("/api/v1/orders", headers=H, json={"steam_login": "gooduser", "amount": 1000,
                                                             "currency": "RUB", "external_id": "ext-1"})
        check(r2.json()["duplicate"] and r2.json()["order"]["id"] == j["order"]["id"], "same external_id -> same order")
        oid = (await _find_order_id(j["order"]["id"]))
        await drive(oid)
        r = await c.get(f"/api/v1/orders/{j['order']['id']}", headers=H)
        check(r.json()["order"]["status"] == "delivered", "status delivered via API")
        r = await c.get("/api/v1/orders/external/ext-1", headers=H)
        check(r.json()["order"]["id"] == j["order"]["id"], "lookup by external_id")
        r = await c.get("/api/v1/balance/history", headers=H)
        check([x["kind"] for x in r.json()["items"]][:2] == ["order", "deposit"], "balance history: order, deposit")
        r = await c.post("/api/v1/deposits", headers=H, json={"amount_usd": 10, "method": "usdt_trc20"})
        check(r.json()["ok"] and r.json()["deposit"]["address"], f"deposit via API: {r.json()['deposit']['amount']} USDT")
        r = await c.post("/api/v1/orders", headers=H, json={"steam_login": "x", "amount": "abc"})
        check(r.status_code == 400 and r.json()["ok"] is False, "garbage input -> 400 {ok:false}")
        async with session_scope() as s:
            k = (await s.execute(select(ApiKey).where(ApiKey.user_id == u9))).scalar_one()
            k.allowed_ips = "10.1.2.3"
        r = await c.get("/api/v1/me", headers=H)
        check(r.status_code == 403, "IP not in key whitelist -> 403")
    async with session_scope() as s:
        evs = (await s.execute(select(WebhookEvent.event).where(WebhookEvent.user_id == u9))).scalars().all()
    check("order.delivered" in evs, "order.delivered webhook queued")

    print("15. minimum order ~$0.20 in every currency")
    rates = await nervixy.rates()
    for cur in ("RUB", "KZT", "UAH", "USD"):
        lo, hi = await O.limits(cur)
        usd = lo / rates[cur]
        rub = lo / rates[cur] * rates["RUB"]
        check(Decimal("0.19") <= usd <= Decimal("0.22") and rub >= 15, f"{cur}: минимум {lo} (≈ ${usd:.3f}, ≈ {rub:.2f} ₽ ≥ 15 ₽)")
    lo_rub, _ = await O.limits("RUB")
    u10 = await user(10)
    try:
        await order(u10, "usdt_trc20", amount=str(lo_rub - 1))
        check(False, "ниже минимума должно отказать")
    except O.OrderError as e:
        check("от" in str(e), f"{lo_rub - 1} ₽ → отказ: {e}")
    tiny = await order(u10, "usdt_trc20", amount=str(lo_rub))
    ti = await invoice_of(tiny.id)
    check(ti.amount < Decimal("0.25"), f"заказ на {lo_rub} ₽ → счёт {ti.amount} USDT")
    await pay("usdt_trc20", int(ti.units))
    check((await drive(tiny.id)).status == "delivered", "копеечный заказ оплачен точной суммой и доставлен (не принят за «пыль»)")
    u11 = await user(11)
    tb = await order(u11, "btc", amount=str(lo_rub))
    bi = await invoice_of(tb.id)
    check(abs(bi.usd_micro - 1_000_000) < 5, f"BTC-счёт поднят до минимума сети: ${usd_str(bi.usd_micro)} = {bi.amount} BTC")
    await pay("btc", int(bi.units))
    check((await drive(tb.id)).status == "delivered", "BTC-заказ доставлен")
    check(await balance(u11) > 700_000, f"лишнее (${usd_str(await balance(u11))}) осталось у клиента на балансе")

    print("16. balance top-up from $0.20")
    u12 = await user(12)
    async with session_scope() as s:
        uu = await s.get(User, u12)
        try:
            await create_deposit(s, uu, amount_usd="0.19", method_code="usdt_ton")
            check(False, "$0.19 должно отказать")
        except O.OrderError as e:
            check("0.20" in str(e), f"$0.19 → отказ: {e}")
    async with session_scope() as s:
        uu = await s.get(User, u12)
        d1 = await create_deposit(s, uu, amount_usd="0.2", method_code="usdt_ton")
        d1_units, d1_amount = int(d1.units), d1.amount
    check(d1_amount < Decimal("0.21"), f"пополнение $0.20 → счёт {d1_amount} USDT (TON)")
    await pay("usdt_ton", d1_units)
    check(abs(await balance(u12) - 200_000) < 1000, f"зачислено ${usd_str(await balance(u12))}")
    async with session_scope() as s:
        uu = await s.get(User, u12)
        d2 = await create_deposit(s, uu, amount_usd="0.2", method_code="btc")
        d2_units, d2_usd = int(d2.units), d2.usd_micro
    check(d2_usd == 1_000_000, "BTC-пополнение на $0.20 → счёт на минимум сети $1.00")
    await pay("btc", d2_units)
    check(abs(await balance(u12) - 1_200_000) < 10_000, f"зачислена вся сумма BTC, баланс ${usd_str(await balance(u12), 4)}")

    print("17. ledger == balances for every user")
    async with session_scope() as s:
        for u in (await s.execute(select(User))).scalars():
            total = (await s.execute(select(func.coalesce(func.sum(LedgerEntry.delta_micro), 0))
                                     .where(LedgerEntry.user_id == u.id))).scalar_one()
            check(total == u.balance_micro and u.balance_micro >= 0, f"user {u.tg_id}: ledger {total} == balance")

    print(f"\nALL GOOD: {OK} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
