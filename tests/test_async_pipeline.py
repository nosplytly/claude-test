"""The payment pipeline is asynchronous: no step waits for another's slow outside service.

Run:  .venv\\Scripts\\python.exe tests\\test_async_pipeline.py
Throw-away database, mock supplier, fake explorers and a fake Telegram: nothing leaves the machine.
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "prod", "DATA_DIR": tempfile.mkdtemp(prefix="sh-async-"),
                   "NERVIXY_MOCK": "true", "BOT_TOKEN": "", "ADMIN_TG_IDS": "999", "MONITOR_ENABLED": "false", "CROSSCHECK_ENABLED": "false",
                   "BASE_URL": "https://sh.test",
                   "WALLET_USDT_TRC20": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
                   "WALLET_BTC": "1BitcoinEaterAddressDontSendf59kuE"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram.types import Chat, Message  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app import events, notify, outbox, webhooks  # noqa: E402
from app import orders as O  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import LedgerEntry, Order, OutboxMessage, Transfer, User, WebhookEvent  # noqa: E402
from app.nervixy import nervixy  # noqa: E402
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


class FakeTelegram:
    """Stands in for aiogram's Bot: records messages, can be told to fail."""

    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.fail_next: list[Exception] = []
        self.delay = 0.0

    async def send_message(self, chat_id, text, **kw):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.sent.append((chat_id, text))
        return Message(message_id=len(self.sent), date=datetime.now(timezone.utc), chat=Chat(id=chat_id, type="private"),
                       text=text)


class RetryAfter(Exception):
    def __init__(self, s):
        super().__init__(f"Flood control exceeded, retry in {s}")
        self.retry_after = s


async def mk_user(tg_id, bal=0):
    async with session_scope() as s:
        u = User(tg_id=tg_id, username=f"u{tg_id}", balance_micro=bal)
        s.add(u)
        await s.flush()
        return u.id


async def new_order(uid, method, amount="1000"):
    async with session_scope() as s:
        return await O.create_order(s, await s.get(User, uid), steam_login="gooduser", amount=Decimal(amount),
                                    currency="RUB", method_code=method)


async def invoice_units(order_id):
    from app.models import Invoice
    async with session_scope() as s:
        return int((await s.execute(select(Invoice.units).where(Invoice.order_id == order_id)
                                    .order_by(Invoice.id.desc()))).scalars().first())


_n = 0


async def arrive(method, units):
    """A final on-chain transfer shows up (as the chain's poller would record it)."""
    global _n
    _n += 1
    m = METHODS[method]
    await M.ingest([Incoming(method=method, txid=f"tx{_n:060d}", idx="0", to_address=m.address, units=units,
                             from_address="payer", block_time=utcnow(), confirmations=m.confirmations, final=True)])
    events.kick(events.credit(m.chain))


async def status(order_id):
    async with session_scope() as s:
        return (await s.get(Order, order_id)).status


async def until(cond, timeout, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await cond():
            return True
        await asyncio.sleep(step)
    return False


async def main():
    await init_db()
    nervixy.client.delay = 0
    price_feed.prices = {"BTC": Decimal("80000"), "TRX": Decimal("0.3")}
    price_feed.updated_at = time.time()
    tg = FakeTelegram()
    notify.set_bot(tg)
    outbox.BACKOFF = [0.2, 0.4, 0.8, 1.6]  # seconds instead of minutes, same logic

    mon = M.Monitor()
    check(set(mon.watchers) == {"tron", "btc"}, "две сети: TRON и BTC, у каждой свой воркер зачисления")
    verify_log = []

    def fake_verify(chain, seconds):
        async def verify(txid, bn):
            verify_log.append((chain, time.monotonic()))
            await asyncio.sleep(seconds)
            return True
        return verify
    mon.watchers["btc"].verify = fake_verify("btc", 6.0)  # a slow explorer
    mon.watchers["tron"].verify = fake_verify("tron", 0.5)
    workers = [asyncio.create_task(mon._credit_loop(c)) for c in mon.watchers]
    workers.append(asyncio.create_task(outbox.run()))

    print("1. медленная сеть не держит другие")
    a, b = await mk_user(1001), await mk_user(1002)
    ob, ot = await new_order(a, "btc"), await new_order(b, "usdt_trc20")
    await arrive("btc", await invoice_units(ob.id))
    t0 = time.monotonic()
    await arrive("usdt_trc20", await invoice_units(ot.id))
    got = await until(lambda: _paid(ot.id), 3)
    took = time.monotonic() - t0
    check(got and await status(ob.id) == "awaiting_payment",
          f"USDT зачислен за {took:.1f} с, пока BTC ещё проверяется в медленном эксплорере (6 с)")
    check(await until(lambda: _paid(ob.id), 8), "BTC тоже зачислен, когда его эксплорер ответил")

    print("2. платежи одной сети — параллельно")
    users = [await mk_user(2000 + i) for i in range(12)]
    orders = [await new_order(u, "usdt_trc20") for u in users]
    verify_log.clear()
    t0 = time.monotonic()
    for o in orders:
        await arrive("usdt_trc20", await invoice_units(o.id))
    done = await until(lambda: _all_paid([o.id for o in orders]), 10)
    took = time.monotonic() - t0
    check(done and took < 12 * 0.5 * 0.6, f"12 платежей по 0.5 с проверки: {took:.1f} с вместо 6 с подряд")
    starts = sorted(t for c, t in verify_log if c == "tron")
    at_once = max(sum(1 for x in starts if s <= x < s + 0.45) for s in starts)
    check(at_once <= M.CREDIT_PARALLEL, f"одновременно проверяется не больше {M.CREDIT_PARALLEL} (было {at_once})")
    async with session_scope() as s:
        credited = (await s.execute(select(func.count()).select_from(Transfer).where(Transfer.status == "credited"))).scalar_one()
        deposits = (await s.execute(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "deposit"))).scalar_one()
    check(credited == deposits == 14, f"каждый платёж зачислен ровно один раз ({credited} переводов = {deposits} проводок)")

    print("3. Telegram лежит — деньги идут, сообщения догоняют по порядку")
    c = await mk_user(3001)
    oc = await new_order(c, "usdt_trc20")
    tg.fail_next = [ConnectionError("Telegram is down")] * 3
    t0 = time.monotonic()
    await arrive("usdt_trc20", await invoice_units(oc.id))
    check(await until(lambda: _paid(oc.id), 3), f"заказ оплачен за {time.monotonic() - t0:.1f} с, хотя Telegram не отвечает")
    async with session_scope() as s:
        pending = (await s.execute(select(OutboxMessage).where(OutboxMessage.user_id == c))).scalars().all()
    check(pending and pending[0].status == "pending" and pending[0].attempts >= 1,
          "сообщение клиенту не потерялось: ждёт в очереди с повтором")
    async with session_scope() as s:  # a second message for the same customer comes while the first one waits
        await outbox.to_user(s, c, "второе сообщение")
    ok = await until(lambda: _sent_to(tg, 3001, 2), 6)
    texts = [t for chat, t in tg.sent if chat == 3001]
    check(ok and "Оплата получена" in texts[0] and texts[1] == "второе сообщение",
          "Telegram ожил → оба сообщения дошли, и именно в том порядке, в каком были")

    print("4. что Telegram говорит — то и делаем")
    d = await mk_user(3002)
    tg.fail_next = [RetryAfter(1.2)]
    async with session_scope() as s:
        await outbox.to_user(s, d, "после паузы")
    await until(lambda: _attempts(d, 1), 2)
    async with session_scope() as s:
        m = (await s.execute(select(OutboxMessage).where(OutboxMessage.user_id == d))).scalar_one()
    wait = (m.next_attempt_at - utcnow()).total_seconds()
    check(0.6 < wait <= 1.3, f"«flood control, повторите через 1.2 с» → следующая попытка через {wait:.1f} с, не раньше")
    check(await until(lambda: _sent_to(tg, 3002, 1), 4), "и после паузы сообщение ушло")
    e = await mk_user(3003)
    tg.fail_next = [Exception("Telegram server says - Forbidden: bot was blocked by the user")]
    async with session_scope() as s:
        await outbox.to_user(s, e, "не дойдёт")
    await until(lambda: _status(e, "failed"), 3)
    async with session_scope() as s:
        m = (await s.execute(select(OutboxMessage).where(OutboxMessage.user_id == e))).scalar_one()
        blocked = (await s.get(User, e)).bot_blocked
    check(m.status == "failed" and m.attempts == 1 and blocked,
          "клиент заблокировал бота → одна попытка, без бесконечных повторов, отмечен в профиле")

    print("5. сообщение и деньги — одна транзакция")
    f = await mk_user(3004)
    try:
        async with session_scope() as s:
            await outbox.to_user(s, f, "не должно уйти")
            raise RuntimeError("зачисление сорвалось")
    except RuntimeError:
        pass
    async with session_scope() as s:
        n = (await s.execute(select(func.count()).select_from(OutboxMessage).where(OutboxMessage.user_id == f))).scalar_one()
    check(n == 0, "зачисление откатилось → и сообщения о нём нет")
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)
    g = await mk_user(3005)
    og = await new_order(g, "usdt_trc20")
    await arrive("usdt_trc20", await invoice_units(og.id))
    await M.credit_ready(mon.watchers)  # credited while the sender is down (as if the app crashed right after)
    before = len(tg.sent)
    await outbox.flush()  # after the restart
    check(any(chat == 3005 and "Оплата получена" in t for chat, t in tg.sent[before:]),
          "сервис упал сразу после зачисления → после перезапуска клиент всё равно получил сообщение")

    print("6. заказ уходит поставщику сразу после оплаты")
    for _ in range(3):  # the orders paid above go to the supplier first; then the processor idles
        await O.process_once()
    proc = asyncio.create_task(O.run_processor())
    await asyncio.sleep(0.5)  # its first pass is over: now it sleeps (up to 3 s) until something wakes it
    h = await mk_user(4001, bal=50_000_000)
    t0 = time.monotonic()
    oh = await new_order(h, None, amount="500")  # paid from the balance on the spot
    ok = await until(lambda: _status_is(oh.id, ("processing", "delivered")), 2.5, step=0.02)
    took = time.monotonic() - t0
    check(ok and took < 1.0, f"оплаченный заказ отправлен поставщику через {took:.2f} с (раньше — до 3 с ожидания)")

    print("7. медленный партнёр никого не держит")
    slow_hits, fast_hits = [], []

    class Resp:
        status_code = 200

    async def fake_post(url, **kw):
        if "slow" in url:
            slow_hits.append(time.monotonic())
            await asyncio.sleep(5)
        else:
            fast_hits.append(time.monotonic())
        return Resp()

    async def any_url(url):
        return url
    webhooks._http.post, webhooks.check_url = fake_post, any_url
    async with session_scope() as s:
        p1 = User(tg_id=5001, webhook_url="https://slow.partner/hook", webhook_secret="s1")
        p2 = User(tg_id=5002, webhook_url="https://fast.partner/hook", webhook_secret="s2")
        s.add_all([p1, p2])
        await s.flush()
        p1_id, p2_id = p1.id, p2.id
    wh = asyncio.create_task(webhooks.run())
    t0 = time.monotonic()
    async with session_scope() as s:
        await webhooks.queue_webhook(s, p1_id, "deposit.credited", {"n": 1})
        await webhooks.queue_webhook(s, p2_id, "deposit.credited", {"n": 2})
    ok = await until(lambda: _webhook_sent(p2_id), 2)
    check(ok and slow_hits and time.monotonic() - t0 < 2,
          f"быстрый партнёр получил вебхук за {time.monotonic() - t0:.1f} с, пока медленный отвечает 5 с")
    i = await mk_user(4002, bal=50_000_000)
    t0 = time.monotonic()
    oi = await new_order(i, None, amount="500")
    ok = await until(lambda: _status_is(oi.id, ("processing", "delivered")), 2.5, step=0.02)
    check(ok and time.monotonic() - t0 < 1.0, "и пополнения Steam в это время идут без задержки")
    for t in (proc, wh):
        t.cancel()
    await asyncio.gather(proc, wh, return_exceptions=True)

    print("8. воркеры на связи")
    from app import health
    check({"notify", "webhooks", "settle", "orders"} <= set(health._beats), "все воркеры отмечаются для /healthz")

    print(f"\nALL GOOD: {OK} checks passed")


async def _paid(order_id):
    return await status(order_id) in ("paid", "submitting", "processing", "delivered")


async def _all_paid(ids):
    for oid in ids:
        if not await _paid(oid):
            return False
    return True


async def _status_is(order_id, statuses):
    return await status(order_id) in statuses


async def _sent_to(tg, chat, n):
    return sum(1 for c, _ in tg.sent if c == chat) >= n


async def _attempts(user_id, n):
    async with session_scope() as s:
        m = (await s.execute(select(OutboxMessage).where(OutboxMessage.user_id == user_id))).scalar_one_or_none()
    return m is not None and m.attempts >= n


async def _status(user_id, st):
    async with session_scope() as s:
        m = (await s.execute(select(OutboxMessage).where(OutboxMessage.user_id == user_id))).scalar_one_or_none()
    return m is not None and m.status == st


async def _webhook_sent(user_id):
    async with session_scope() as s:
        return (await s.execute(select(func.count()).select_from(WebhookEvent).where(
            WebhookEvent.user_id == user_id, WebhookEvent.status == "sent"))).scalar_one() > 0


asyncio.run(main())
