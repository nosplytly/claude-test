"""Promo codes: prices, the no-loss cap, limits under concurrency, once per client, giving uses back, site API.

Run:  .venv\\Scripts\\python.exe tests\\test_promos.py
Throw-away database, mock supplier: nothing leaves the machine.
"""
import asyncio
import os
import sys
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "dev", "DATA_DIR": tempfile.mkdtemp(prefix="sh-promo-"),
                   "NERVIXY_MOCK": "true", "BOT_TOKEN": "", "ADMIN_TG_IDS": "", "MONITOR_ENABLED": "false",
                   "CLIENT_DISCOUNT": "3.75",
                   "WALLET_USDT_TRC20": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import func, select, update  # noqa: E402

from app import orders as O  # noqa: E402
from app import promos  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.ledger import change_balance  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Invoice, LedgerEntry, Order, Promo, PromoUse, User  # noqa: E402
from app.nervixy import nervixy  # noqa: E402
from app.utils import utcnow  # noqa: E402

OK = 0


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


async def mk_user(tg, balance=0):
    async with session_scope() as s:
        u = User(tg_id=tg, username=f"u{tg}")
        s.add(u)
        await s.flush()
        if balance:
            await change_balance(s, u.id, balance, "adjust", comment="t")
        return u


async def mk_promo(code, kind, value, uses=None, per_user=1, expires=None, active=True):
    async with session_scope() as s:
        s.add(Promo(code=code, kind=kind, value=Decimal(value), max_uses=uses, per_user=per_user,
                    expires_at=expires, active=active))


async def order(u, promo=None, amount="1000", method="usdt_trc20"):
    async with session_scope() as s:
        return await O.create_order(s, await s.get(User, u.id), steam_login="promo_user", amount=Decimal(amount),
                                    currency="RUB", method_code=method, promo_code=promo)


async def promo_row(code):
    async with session_scope() as s:
        return (await s.execute(select(Promo).where(Promo.code == code))).scalar_one()


async def main():
    await init_db()
    nervixy.client.delay = 0
    from app.prices import price_feed
    import time
    price_feed.prices = {"USDT": Decimal("1")}
    price_feed.updated_at = time.time()
    sd = await nervixy.supplier_discount()

    print("1. скидочный промокод в цене заказа")
    await mk_promo("SALE05", "discount", "0.5", uses=100)
    u = await mk_user(1)
    plain_o = await order(await mk_user(2))
    o = await order(u, "sale05")  # lower case works too
    check(o.client_discount == Decimal("4.25") and o.promo_pct == Decimal("0.5"),
          f"3.75% + промокод 0.5% = {o.client_discount}% (регистр кода не важен)")
    check(o.price_micro < plain_o.price_micro, f"цена ниже обычной: ${o.price_micro / 1e6:.4f} против ${plain_o.price_micro / 1e6:.4f}")
    check((await promo_row("SALE05")).used == 1, "использование засчитано")

    print("2. в минус не уходим")
    await mk_promo("HUGE", "discount", "5")
    o2 = await order(await mk_user(3), "HUGE")
    check(o2.client_discount == sd - promos.MIN_MARGIN,
          f"промокод 5% сверх 3.75% упёрся в потолок: {o2.client_discount}% (скидка поставщика {sd}% − {promos.MIN_MARGIN}%)")
    check(o2.price_micro > o2.cost_micro, f"клиент платит больше, чем берёт поставщик: ${o2.price_micro / 1e6:.4f} > ${o2.cost_micro / 1e6:.4f}")

    print("3. один раз на клиента")
    try:
        await order(u, "SALE05")
        check(False, "второй раз тот же код должен отклоняться")
    except O.OrderError as err:
        check("уже использовали" in str(err), "тот же клиент второй раз → «Вы уже использовали этот промокод»")

    print("4. лимит под нагрузкой")
    await mk_promo("GIFT2", "bonus", "2", uses=10)
    crowd = [await mk_user(100 + k) for k in range(40)]

    async def redeem(user):
        try:
            async with session_scope() as s:
                await promos.redeem_bonus(s, user.id, "GIFT2")
            return True
        except promos.PromoError:
            return False
    got = await asyncio.gather(*(redeem(x) for x in crowd))
    async with session_scope() as s:
        credited = (await s.execute(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "promo"))).scalar_one()
    check(sum(got) == 10 and credited == 10 and (await promo_row("GIFT2")).used == 10,
          f"код на 10 использований, 40 человек одновременно → зачислено ровно {credited}")
    lucky = crowd[got.index(True)]
    again = await asyncio.gather(*(redeem(lucky) for _ in range(5)))
    check(not any(again), "один клиент 5 раз одновременно — повторно не зачислено")
    async with session_scope() as s:
        bal = (await s.get(User, lucky.id)).balance_micro
    check(bal == 2_000_000, "у счастливчика на балансе ровно $2")

    print("5. срок, выключенный, несуществующий, не тот тип")
    await mk_promo("OLD", "discount", "0.3", expires=utcnow() - timedelta(minutes=1))
    await mk_promo("OFF", "bonus", "1", active=False)
    for code, text in (("OLD", "истёк"), ("OFF", "нет"), ("NOPE", "нет"), ("GIFT2", "")):
        try:
            await order(await mk_user(300 + len(code) * 7 + OK), code)
            check(False, f"{code} должен отклоняться")
        except O.OrderError as err:
            check(text in str(err) or code == "GIFT2", f"{code} → «{err}»")
    try:
        async with session_scope() as s:
            await promos.redeem_bonus(s, u.id, "SALE05")
        check(False, "скидочный код не зачисляется на баланс")
    except promos.PromoError as err:
        check("на скидку" in str(err), "скидочный код через «на баланс» → подсказка ввести его при заказе")

    print("6. возврат использования")
    await mk_promo("BACK", "discount", "0.3", uses=1)
    v = await mk_user(400)
    ov = await order(v, "BACK")
    check((await promo_row("BACK")).used == 1, "код на 1 раз использован")
    async with session_scope() as s:  # the invoice runs out unpaid
        await s.execute(update(Invoice).where(Invoice.order_id == ov.id).values(expires_at=utcnow() - timedelta(hours=1)))
    await O.expire_stale()
    async with session_scope() as s:
        uses = (await s.execute(select(func.count()).select_from(PromoUse).where(PromoUse.order_id == ov.id))).scalar_one()
        st = (await s.get(Order, ov.id)).status
    check(st == "expired" and uses == 0 and (await promo_row("BACK")).used == 0, "заказ истёк неоплаченным → код снова свободен")
    ov2 = await order(v, "BACK")
    check(ov2.promo_pct == Decimal("0.3"), "и клиент может применить его снова")

    w = await mk_user(401, balance=50_000_000)
    await mk_promo("REJ", "discount", "0.2", uses=1)
    ow = await order(w, "REJ", method=None)  # paid from balance straight away
    await O.submit(ow.id)
    await O.apply_status(ow.id, "rejected")
    check((await promo_row("REJ")).used == 0, "поставщик отклонил оплаченный заказ → деньги и промокод вернулись")

    print("7. через сайт")
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("198.51.100.77", 1)), base_url="https://t")
    r = await c.post("/api/dev/login", json={"tg_id": 555, "name": "web"}, headers={"X-SH": "1"})
    await mk_promo("WEB05", "discount", "0.5")
    await mk_promo("WEBGIFT", "bonus", "1.5")
    r = await c.post("/api/promo/check", json={"code": "web05"}, headers={"X-SH": "1"})
    check(r.status_code == 200 and r.json()["kind"] == "discount" and r.json()["total"] == "4.25",
          f"проверка кода на сайте: {r.json().get('label')}, итоговая скидка {r.json().get('total')}%")
    r = await c.post("/api/promo/redeem", json={"code": "WEBGIFT"}, headers={"X-SH": "1"})
    check(r.status_code == 200 and r.json()["balance"] == "1.50", "бонусный код на сайте → $1.50 на балансе")
    r = await c.post("/api/promo/check", json={"code": "WEBGIFT"}, headers={"X-SH": "1"})
    check(r.status_code == 400 and "уже использовали" in r.json()["detail"], "повторная проверка того же бонуса → «уже использовали»")
    codes = [(await c.post("/api/promo/check", json={"code": f"GUESS{k}"}, headers={"X-SH": "1"})).status_code for k in range(12)]
    check(codes.count(429) >= 1, f"перебор кодов упирается в лимит (429) — дальше бан: {codes[-4:]}")
    await c.aclose()

    print("8. журнал сходится")
    async with session_scope() as s:
        users = (await s.execute(select(User))).scalars().all()
        sums = dict((await s.execute(select(LedgerEntry.user_id, func.sum(LedgerEntry.delta_micro))
                                     .group_by(LedgerEntry.user_id))).all())
    check(all(x.balance_micro == (sums.get(x.id) or 0) for x in users), f"у всех {len(users)} клиентов баланс = сумме операций")

    print(f"\nALL GOOD: {OK} checks passed")


asyncio.run(main())
