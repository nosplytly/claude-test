"""100 simultaneous users against the whole app: web + partner API, mock supplier that fails in every way it can,
simulated blockchain, fake Telegram. Checks correctness only — no 500s, no double money, no stuck orders,
users can't see each other's data — plus race drills aimed at every place money could be counted twice.

Run:  .venv\\Scripts\\python.exe tests\\test_load_100.py
Isolated: throw-away database, NERVIXY_MOCK, no bot. Every outgoing network connection is blocked and recorded;
the test fails if the app tried to reach anything.
"""
import asyncio
import hashlib
import hmac
import json
import os
import random
import socket
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="sh-load-")
os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "dev", "DATA_DIR": TMP, "NERVIXY_MOCK": "true", "BOT_TOKEN": "", "ADMIN_TG_IDS": "999",
                   "MONITOR_ENABLED": "false", "NERVIXY_WEBHOOK_SECRET": "whsec_load", "MAX_ACTIVE_ORDERS": "3",
                   # placeholder addresses: nothing is ever sent anywhere
                   "WALLET_USDT_TRC20": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb", "WALLET_TRX": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
                   "WALLET_USDT_TON": "UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJKZ",
                   "WALLET_TON": "UQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJKZ",
                   "WALLET_USDT_BEP20": "0x000000000000000000000000000000000000dEaD",
                   "WALLET_USDT_ERC20": "0x000000000000000000000000000000000000dEaD",
                   "WALLET_ETH": "0x000000000000000000000000000000000000dEaD",
                   "WALLET_BTC": "1BitcoinEaterAddressDontSendf59kuE"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ------------------------------------------------------------------ network guard (before the app is imported)
BLOCKED: list[str] = []
_LOCAL = {None, "127.0.0.1", "::1", "localhost"}
_orig_connect, _orig_gai = socket.socket.connect, socket.getaddrinfo


def _deny(what) -> None:
    BLOCKED.append(str(what)[:160])
    raise OSError("network is disabled in this test")


def _connect(self, addr):
    return _orig_connect(self, addr) if (addr[0] if isinstance(addr, tuple) else addr) in _LOCAL else _deny(addr)


def _gai(host, *a, **k):
    return _orig_gai(host, *a, **k) if host in _LOCAL else _deny(host)


async def _no_create_connection(self, protocol_factory, host=None, port=None, **k):
    _deny(f"{host}:{port}")


socket.socket.connect = _connect
socket.getaddrinfo = _gai
asyncio.base_events.BaseEventLoop.create_connection = _no_create_connection

import httpx  # noqa: E402
from sqlalchemy import func, select, update  # noqa: E402

from app import fail2ban as f2b  # noqa: E402
from app import notify  # noqa: E402
from app import orders as O  # noqa: E402
from app.db import engine, init_db, session_scope  # noqa: E402
from app.deposits import create_deposit  # noqa: E402
from app.ledger import change_balance  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Claim, Invoice, LedgerEntry, Order, Transfer, User  # noqa: E402
from app.money import usd_str  # noqa: E402
from app.nervixy import MockNervixyClient, NervixyError, NervixyTransportError, nervixy  # noqa: E402
from app.payments import monitor as M  # noqa: E402
from app.payments.chains.base import Incoming  # noqa: E402
from app.payments.methods import METHODS, enabled_methods  # noqa: E402
from app.prices import price_feed  # noqa: E402
from app.utils import utcnow  # noqa: E402

N = 100
rng = random.Random(20260928)

# ------------------------------------------------------------------ write-lock profiler: who holds SQLite's lock long
LOCK_HOLDS: list[tuple[float, str, list]] = []
_write_started: dict[int, tuple[float, str, list]] = {}


def _profile_engine():
    from sqlalchemy import event

    def _short(statement):
        return " ".join(statement.split()[:4])[:70]

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _pre(conn, cursor, statement, *a):
        held = _write_started.get(id(conn))
        if held:
            held[2].append((time.monotonic() - held[0], "→ " + _short(statement)))

    @event.listens_for(engine.sync_engine, "after_cursor_execute")  # after: the lock is ours, waiting excluded
    def _post(conn, cursor, statement, *a):
        key = id(conn)
        held = _write_started.get(key)
        if held:
            held[2].append((time.monotonic() - held[0], "✓ " + _short(statement)))
        elif statement.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE"):
            _write_started[key] = (time.monotonic(), _short(statement), [])

    def _end(conn):
        started = _write_started.pop(id(conn), None)
        if started:
            LOCK_HOLDS.append((time.monotonic() - started[0], started[1], started[2]))
    event.listen(engine.sync_engine, "commit", _end)
    event.listen(engine.sync_engine, "rollback", _end)


LOOP_LAG: list[float] = []


async def loop_lag_watch():
    while RUN:
        t = time.monotonic()
        await asyncio.sleep(0.05)
        LOOP_LAG.append(time.monotonic() - t - 0.05)
OK = 0
PROBLEMS: list[str] = []  # collected, reported together at the end
STATUS = Counter()


def check(cond, msg):
    global OK
    if not cond:
        PROBLEMS.append(msg)
        print("  ✗", msg)
        return False
    OK += 1
    print("  ✓", msg)
    return True


# ------------------------------------------------------------------ fake Telegram: record, never send
SENT: list[tuple[int, str]] = []


async def fake_send(chat_id, text, buttons=None):
    SENT.append((chat_id, text))
    return True


notify.send = fake_send


# ------------------------------------------------------------------ supplier on a bad day
def fate(pid: str) -> str:
    """Deterministic per order: ~10% lose the answer after creating, ~5% never arrive, ~8% get a 429 first."""
    h = int(hashlib.sha256(pid.encode()).hexdigest(), 16) % 100
    return "lost_after" if h < 10 else "lost_before" if h < 15 else "429" if h < 23 else "ok"


class ChaosSupplier(MockNervixyClient):
    def __init__(self):
        super().__init__()
        self.balance = Decimal("1000")
        self.delay = 0.3  # seconds in "processing"
        self.lock = asyncio.Lock()  # like the real client: one request at a time
        self.calls = Counter()
        self.creates = Counter()  # partner_id -> orders actually created on the supplier side
        self.seen: dict[str, set] = defaultdict(set)
        self.topups = Decimal(0)
        self.lose_answer: set[str] = set()  # drills: create the order, then "lose" the answer once

    async def _wire(self, name):
        self.calls[name] += 1
        async with self.lock:
            await asyncio.sleep(0.002)

    async def check_login(self, steam_login):
        await self._wire("check_login")
        return await super().check_login(steam_login)

    async def me(self):
        await self._wire("me")
        return await super().me()

    async def status(self, order_id):
        await self._wire("status")
        return await super().status(order_id)

    async def orders(self, limit=50, offset=0):
        await self._wire("orders")
        return await super().orders(limit, offset)

    async def order(self, *, amount, currency, steam_login, partner_id):
        await self._wire("order")
        f, seen = fate(partner_id), self.seen[partner_id]
        if f == "lost_before" and "lb" not in seen:
            seen.add("lb")
            raise NervixyTransportError("timeout (injected, request never reached the supplier)")
        if f == "429" and "429" not in seen:
            seen.add("429")
            raise NervixyError("Слишком много попыток. Подождите 60 секунд.", 429)
        data = await super().order(amount=amount, currency=currency, steam_login=steam_login, partner_id=partner_id)
        self.creates[partner_id] += 1
        if (f == "lost_after" and "la" not in seen) or partner_id in self.lose_answer:
            seen.add("la")
            self.lose_answer.discard(partner_id)
            raise NervixyTransportError("connection reset (injected, the order WAS created)")
        return data


supplier = ChaosSupplier()
nervixy.client = supplier


# ------------------------------------------------------------------ blockchain simulator
_tx = 0


async def chain_pay(inv: Invoice, fraction: Decimal = Decimal(1), *, when=None, txid=None, final=True) -> str:
    global _tx
    _tx += 1
    txid = txid or f"dev{_tx:061d}"
    m = METHODS[inv.method]
    units = int(Decimal(inv.units) * fraction)
    await M.ingest([Incoming(method=m.code, txid=txid, idx="0", to_address=inv.address, units=units,
                             from_address=f"payer{_tx}", comment=inv.comment, block_time=when or utcnow(),
                             confirmations=m.confirmations if final else 0, final=final)])
    return txid


async def open_invoice(order_pid: str | None = None, deposit_pid: str | None = None) -> Invoice | None:
    async with session_scope() as s:
        if deposit_pid:
            return (await s.execute(select(Invoice).where(Invoice.public_id == deposit_pid))).scalar_one_or_none()
        oid = (await s.execute(select(Order.id).where(Order.public_id == order_pid))).scalar_one()
        return (await s.execute(select(Invoice).where(Invoice.order_id == oid, Invoice.status == "pending")
                                .order_by(Invoice.id.desc()))).scalars().first()


async def order_row(pid: str) -> Order:
    async with session_scope() as s:
        return (await s.execute(select(Order).where(Order.public_id == pid))).scalar_one()


async def wait_for(pred, timeout=30.0, step=0.5):  # a harness that polls harder than the real site would only measure itself
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if await pred():
            return True
        await asyncio.sleep(step)
    return False


# ------------------------------------------------------------------ a site visitor
class Visitor:
    def __init__(self, i: int):
        self.i, self.tg = i, 70_000 + i
        self.ip = f"198.18.{i // 200}.{i % 200 + 1}"
        self.c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(self.ip, 40000 + i)),
                                   base_url="https://t", timeout=60)
        self.orders: list[str] = []
        self.deposits: list[str] = []
        self.key: str | None = None
        self.notes: list[str] = []

    async def req(self, method, url, *, expect=(200,), api_key=None, **kw):
        headers = {"X-SH": "1", **({"X-API-Key": api_key} if api_key else {})}
        r = await self.c.request(method, url, headers=headers, **kw)
        STATUS[r.status_code] += 1
        if r.status_code >= 500 or r.status_code not in expect:
            PROBLEMS.append(f"user {self.i}: {method} {url} -> {r.status_code} {r.text[:160]}")
        return r

    async def login(self):
        r = await self.req("POST", "/api/dev/login", json={"tg_id": self.tg, "name": f"load{self.i}"})
        self.uid_name = r.json()["user"]["name"]
        await self.req("GET", "/api/me")
        await self.req("GET", "/api/config")

    async def web_order(self, amount, currency="RUB", login=None, method=None, expect=(200,)):
        method = method or rng.choice(METHOD_CODES)
        r = await self.req("POST", "/api/orders", expect=expect, json={
            "steam_login": login or f"steam_{self.i}", "amount": str(amount), "currency": currency, "method": method})
        if r.status_code == 200:
            self.orders.append(r.json()["id"])
        return r

    async def poll(self, pid, times=3):  # the payment page polls every 3 s
        for _ in range(times):
            await self.req("GET", f"/api/orders/{pid}")
            await asyncio.sleep(rng.uniform(2.5, 3.5))

    async def deposit(self, usd, method="usdt_trc20"):
        r = await self.req("POST", "/api/deposits", json={"amount_usd": str(usd), "method": method})
        self.deposits.append(r.json()["id"])
        return r.json()["id"]

    async def pay_deposit_and_wait(self, pid):
        await chain_pay(await open_invoice(deposit_pid=pid))

        async def credited():
            return (await self.req("GET", f"/api/deposits/{pid}")).json()["status"] == "paid"
        if not await wait_for(credited, timeout=90, step=3):
            PROBLEMS.append(f"user {self.i}: deposit {pid} not credited within 90 s")


METHOD_CODES = [m.code for m in enabled_methods()]
AMOUNTS = {"RUB": (100, 400), "KZT": (600, 2500), "UAH": (50, 200), "USD": (1, 5)}


def rand_amount(cur):
    lo, hi = AMOUNTS[cur]
    return rng.randint(lo, hi)


# ------------------------------------------------------------------ what each of the 100 visitors does
async def scenario(v: Visitor, all_visitors: list[Visitor]):
    i = v.i
    await v.login()
    await asyncio.sleep(rng.uniform(0, 0.5))
    if i < 30:  # pays the exact invoice, any coin, any currency
        cur = rng.choice(list(AMOUNTS))
        r = await v.web_order(rand_amount(cur), cur)
        pid = r.json()["id"]
        await asyncio.sleep(rng.uniform(0, 0.4))
        await chain_pay(await open_invoice(pid))
        await v.poll(pid)
    elif i < 45:  # TON with the memo: pays 60%, gets a remainder invoice, pays that
        r = await v.web_order(rand_amount("RUB"), "RUB", method=rng.choice(["ton", "usdt_ton"]))
        pid = r.json()["id"]
        first = await open_invoice(pid)
        await chain_pay(first, Decimal("0.6"))

        async def remainder_ready():
            inv = await open_invoice(pid)
            return inv is not None and inv.id != first.id
        if await wait_for(remainder_ready, timeout=60):
            await chain_pay(await open_invoice(pid))
        else:
            PROBLEMS.append(f"user {i}: no remainder invoice after a partial payment")
        await v.poll(pid)
    elif i < 50:  # TON with the memo, overpays 1.5x: the memo identifies the order, the surplus stays on the balance
        r = await v.web_order(rand_amount("RUB"), "RUB", method="ton" if i % 2 else "usdt_ton")
        await chain_pay(await open_invoice(r.json()["id"]), Decimal("1.5"))
    elif i < 55:  # TRC20 1.5x: an amount like that identifies nobody -> the customer claims it by tx hash
        r = await v.web_order(rand_amount("RUB"), "RUB", method="usdt_trc20")
        pid = r.json()["id"]
        txid = await chain_pay(await open_invoice(pid), Decimal("1.5"))
        await asyncio.sleep(0.3)
        await v.req("POST", f"/api/orders/{pid}/claim", json={"txid": txid})

        async def claim_pending():
            async with session_scope() as s:
                return (await s.execute(select(Claim.id).where(Claim.txid == txid, Claim.status == "pending"))
                        ).scalar_one_or_none()
        if await wait_for(claim_pending, timeout=60):
            v.claim_answer = await M.approve_claim(await claim_pending(), True)  # the admin taps «Зачислить»
        else:
            PROBLEMS.append(f"user {i}: claim never reached the admin")
    elif i < 65:  # $5 on the balance, then 5 parallel API orders of ~$1.96 — only two fit
        await v.pay_deposit_and_wait(await v.deposit("5.00"))
        v.key = (await v.req("POST", "/api/account/api-key")).json()["key"]
        rs = await asyncio.gather(*(v.req("POST", "/api/v1/orders", api_key=v.key, expect=(200, 402), json={
            "steam_login": f"steam_{i}", "amount": 160, "currency": "RUB"}) for _ in range(5)))
        v.api_results = [r.status_code for r in rs]
        v.orders += [r.json()["order"]["id"] for r in rs if r.status_code == 200]
    elif i < 75:  # partner retries the same external_id 10 times in parallel
        await v.pay_deposit_and_wait(await v.deposit("5.00", method=rng.choice(["usdt_ton", "usdt_bep20"])))
        v.key = (await v.req("POST", "/api/account/api-key")).json()["key"]
        rs = await asyncio.gather(*(v.req("POST", "/api/v1/orders", api_key=v.key, json={
            "steam_login": f"steam_{i}", "amount": 100, "currency": "RUB", "external_id": f"ext-{i}"}) for _ in range(10)))
        v.idem_ids = {r.json()["order"]["id"] for r in rs if r.status_code == 200}
        v.orders += list(v.idem_ids)
    elif i < 80:  # the supplier rejects this Steam account after taking the order
        r = await v.web_order(rand_amount("RUB"), "RUB", login=f"rejectme_{i}")
        await chain_pay(await open_invoice(r.json()["id"]))
    elif i < 85:  # a login Steam doesn't know: refused before any invoice
        await v.web_order(200, "RUB", login=f"badlogin_{i}", expect=(400,))
    elif i < 90:  # cancels, then pays anyway: money goes to the balance, the order stays cancelled
        r = await v.web_order(rand_amount("RUB"), "RUB")
        pid = r.json()["id"]
        inv = await open_invoice(pid)
        await v.req("POST", f"/api/orders/{pid}/cancel")
        await chain_pay(inv)
    elif i < 95:  # pays two hours after the invoice expired
        r = await v.web_order(rand_amount("RUB"), "RUB", method="usdt_trc20")
        pid = r.json()["id"]
        inv = await open_invoice(pid)
        async with session_scope() as s:
            await s.execute(update(Invoice).where(Invoice.id == inv.id).values(
                expires_at=utcnow() - timedelta(hours=2), created_at=utcnow() - timedelta(hours=3)))
        await O.expire_stale()
        await chain_pay(inv)
    else:  # snoops on everybody else, then orders normally
        await asyncio.sleep(4)  # let the others create something worth snooping on
        others = [o for w in all_visitors if w is not v for o in w.orders][:40]
        deps = [d for w in all_visitors if w is not v for d in w.deposits][:10]
        v.snooped = len(others) + len(deps)
        for n, pid in enumerate(others):
            await v.req("GET", f"/api/orders/{pid}", expect=(404,))
            await v.req("POST", f"/api/orders/{pid}/cancel", expect=(404,))
            if n < 4:  # the claim form is rate limited (5 per 10 min) on purpose
                await v.req("POST", f"/api/orders/{pid}/claim", json={"txid": "a" * 64}, expect=(404,))
        for d in deps:
            await v.req("GET", f"/api/deposits/{d}", expect=(404,))
        async with session_scope() as s:
            foreign_inv = (await s.execute(select(Invoice.id).limit(5))).scalars().all()
        for iid in foreign_inv:
            await v.req("GET", f"/api/invoices/{iid}/qr.svg?u=1", expect=(404,))
        mine = (await v.req("GET", "/api/orders")).json()["orders"]
        v.snoop_saw = len(mine)
        r = await v.web_order(rand_amount("UAH"), "UAH")
        await chain_pay(await open_invoice(r.json()["id"]))
    for _ in range(3):  # everyone keeps the page open for a bit
        await v.req("GET", "/api/me")
        await v.req("GET", "/api/orders")
        await asyncio.sleep(rng.uniform(2.5, 3.5))


# ------------------------------------------------------------------ background machinery (like the real app)
RUN = True


async def processor_loop():
    while RUN:
        async with session_scope() as s:  # fast-forward the app's timers (20-90 s waits) so the test takes seconds
            await s.execute(update(Order).where(Order.status.in_(("processing", "uncertain")),
                                                Order.next_check_at > utcnow()).values(next_check_at=utcnow()))
        await O.process_once()
        await asyncio.sleep(0.3)  # the real loop: 3 s


async def settle_loop():
    while RUN:
        await M.settle({})
        await asyncio.sleep(0.2)  # the real loop: 2 s


async def webhook_hints():
    """Nervixy's webhook (one per finished order) arrives while the poller is checking the same orders."""
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("185.10.10.10", 443)), base_url="https://t")
    sent: set[str] = set()
    while RUN:
        async with session_scope() as s:
            nids = (await s.execute(select(Order.nervixy_order_id).where(Order.status == "processing",
                                                                         Order.nervixy_order_id.is_not(None)))).scalars().all()
        done = [n for n in nids if n not in sent and n in supplier._orders
                and supplier._status_of(supplier._orders[n]) != "processing"]
        for nid in done:
            sent.add(nid)
            body = json.dumps({"event": "order.status", "order_id": nid}).encode()
            sig = hmac.new(b"whsec_load", body, hashlib.sha256).hexdigest()
            r = await c.post("/api/webhooks/nervixy", content=body, headers={"X-Nervixy-Signature": f"sha256={sig}"})
            STATUS[r.status_code] += 1
            if r.status_code != 200:
                PROBLEMS.append(f"webhook -> {r.status_code}")
        await asyncio.sleep(0.2)
    await c.aclose()


async def supplier_admin():
    """The supplier's balance drains in the middle of the rush; the admin tops it up a few seconds later."""
    supplier.balance = Decimal("8")  # spent elsewhere: our cached balance still says ~$1000
    ok = await wait_for(lambda: _count_status("queued", at_least=3), timeout=90, step=0.2)
    queued_seen.append(ok)
    await asyncio.sleep(0.5)
    supplier.balance += Decimal("2000")
    supplier.topups += Decimal("2000")
    nervixy._account_at = 0  # the site re-reads the balance
    async with session_scope() as s:  # queued orders retry every 60 s in real life; don't make the test wait
        await s.execute(update(Order).where(Order.status == "queued").values(next_check_at=utcnow()))


queued_seen: list[bool] = []


async def _count_status(status, at_least):
    async with session_scope() as s:
        return (await s.execute(select(func.count()).select_from(Order).where(Order.status == status))).scalar_one() >= at_least


# ------------------------------------------------------------------ invariants
async def money_invariants(title: str):
    async with session_scope() as s:
        users = (await s.execute(select(User))).scalars().all()
        sums = dict((await s.execute(select(LedgerEntry.user_id, func.sum(LedgerEntry.delta_micro))
                                     .group_by(LedgerEntry.user_id))).all())
        bad = [(u.id, u.balance_micro, sums.get(u.id, 0)) for u in users if u.balance_micro != (sums.get(u.id) or 0)]
        neg = [u.id for u in users if u.balance_micro < 0]
        dup_dep = (await s.execute(select(LedgerEntry.transfer_id, func.count()).where(
            LedgerEntry.kind == "deposit", LedgerEntry.transfer_id.is_not(None)).group_by(LedgerEntry.transfer_id)
            .having(func.count() > 1))).all()
        credited = (await s.execute(select(Transfer).where(Transfer.status == "credited"))).scalars().all()
        dep_rows = dict((await s.execute(select(LedgerEntry.transfer_id, LedgerEntry.delta_micro).where(
            LedgerEntry.kind == "deposit", LedgerEntry.transfer_id.is_not(None)))).all())
        mismatch = [t.id for t in credited if dep_rows.get(t.id) != t.credited_micro]
        charges = (await s.execute(select(LedgerEntry.order_id, func.count()).where(LedgerEntry.kind == "order")
                                   .group_by(LedgerEntry.order_id).having(func.count() > 1))).all()
        refunds = (await s.execute(select(LedgerEntry.order_id, func.count()).where(LedgerEntry.kind == "refund")
                                   .group_by(LedgerEntry.order_id).having(func.count() > 1))).all()
    check(not bad, f"{title}: баланс каждого клиента = сумма его операций ({len(users)} клиентов)" + (f" — расходятся {bad[:5]}" if bad else ""))
    check(not neg, f"{title}: ни одного отрицательного баланса" + (f" — {neg}" if neg else ""))
    check(not dup_dep, f"{title}: каждая транзакция зачислена не больше одного раза" + (f" — дубли {dup_dep}" if dup_dep else ""))
    check(not mismatch, f"{title}: сумма зачисления = записи в журнале ({len(credited)} транзакций)")
    check(not charges, f"{title}: ни один заказ не оплачен дважды" + (f" — {charges}" if charges else ""))
    check(not refunds, f"{title}: ни один возврат не сделан дважды" + (f" — {refunds}" if refunds else ""))


async def main():
    global RUN
    await init_db()
    await f2b.load()
    price_feed.prices = {"BTC": Decimal("80000"), "ETH": Decimal("3000"), "LTC": Decimal("90"), "TON": Decimal("3"),
                         "TRX": Decimal("0.3"), "SOL": Decimal("150"), "USDT": Decimal("1")}
    price_feed.updated_at = time.time()
    price_feed.refresh = lambda *a, **k: asyncio.sleep(0)  # never leaves the machine
    await nervixy.account()
    t0 = time.monotonic()

    print(f"1. {N} посетителей одновременно")
    visitors = [Visitor(i) for i in range(N)]
    _profile_engine()
    bg = [asyncio.create_task(x()) for x in (processor_loop, settle_loop, webhook_hints, supplier_admin, loop_lag_watch)]
    peak = 0

    async def pool_watch():
        nonlocal peak
        while RUN:
            peak = max(peak, engine.pool.checkedout())
            await asyncio.sleep(0.01)
    watcher = asyncio.create_task(pool_watch())
    results = await asyncio.gather(*(scenario(v, visitors) for v in visitors), return_exceptions=True)
    crashed = [(v.i, repr(r)[:200]) for v, r in zip(visitors, results) if isinstance(r, BaseException)]
    check(not crashed, f"все {N} сценариев дошли до конца без исключений" + (f" — {crashed[:5]}" if crashed else ""))

    holds = sorted(LOCK_HOLDS, reverse=True)
    lags = sorted(LOOP_LAG)
    print(f"     блокировка записи: {len(holds)} транзакций, дольше 1 с: {sum(h[0] > 1 for h in holds)}, "
          f"макс {holds[0][0] if holds else 0:.2f} с; задержка цикла событий: медиана {lags[len(lags) // 2] if lags else 0:.3f} с, "
          f"макс {lags[-1] if lags else 0:.2f} с")
    for h, first, steps in holds[:3]:
        print(f"       {h:.2f} с  {first}")
        for t, what in steps[:14]:
            print(f"           +{t:6.2f} {what}")
        print(f"           +{h:6.2f} COMMIT/ROLLBACK")
    print("2. дожидаемся, пока всё оплаченное выполнится")
    TRANSIENT = ("paid", "submitting", "processing", "queued")

    async def calm():
        async with session_scope() as s:
            n = (await s.execute(select(func.count()).select_from(Order).where(Order.status.in_(TRANSIENT)))).scalar_one()
            m = (await s.execute(select(func.count()).select_from(Transfer).where(Transfer.status == "matched",
                                                                                  Transfer.final.is_(True)))).scalar_one()
        return n == 0 and m == 0
    check(await wait_for(calm, timeout=120), "очередь разобрана: нет заказов в paid/submitting/processing/queued")
    check(queued_seen == [True], "когда у поставщика кончились деньги, оплаченные заказы встали в очередь, а не потерялись")

    print("3. заказы, которые поставщик мог и не получить (обрыв связи до отправки)")

    async def only_lost_before_left():  # the «lost after» ones get reconciled on the next processor passes
        async with session_scope() as s:
            left = (await s.execute(select(Order.public_id).where(Order.status == "uncertain"))).scalars().all()
        return all(fate(p) == "lost_before" for p in left)
    await wait_for(only_lost_before_left, timeout=30)
    async with session_scope() as s:
        stuck = (await s.execute(select(Order).where(Order.status == "uncertain"))).scalars().all()
        stuck_ids = [o.id for o in stuck]
        stuck_pids = [o.public_id for o in stuck]
        tried = (await s.execute(select(Order.public_id).where(Order.attempts > 0))).scalars().all()
        await s.execute(update(Order).where(Order.id.in_(stuck_ids)).values(submitted_at=utcnow() - timedelta(minutes=10)))
    expected = sorted(p for p in tried if fate(p) == "lost_before")
    lost_after = [p for p in tried if fate(p) == "lost_after"]
    check(len(stuck_pids) > 0 and sorted(stuck_pids) == expected,
          f"в «uncertain» ровно те {len(stuck_pids)}, что до поставщика не дошли")
    check(all(supplier.creates[p] == 0 for p in stuck_pids), "повторно вслепую сайт их не отправлял")
    check(lost_after and all(supplier.creates[p] == 1 for p in lost_after),
          f"{len(lost_after)} заказов, где оборвался ответ ПОСЛЕ создания, найдены в истории поставщика — без повтора")
    before = len([t for _, t in SENT if "завис" in t])
    await wait_for(lambda: _alerts_for(len(stuck_pids) + before), timeout=15)
    check(len([t for _, t in SENT if "завис" in t]) - before == len(stuck_pids),
          "админу пришла кнопка «Отправить повторно / Вернуть на баланс» по каждому")
    for oid in stuck_ids:  # admin: «Отправить повторно» (the refund button is drilled separately below)
        await O.admin_retry(oid)
    check(await wait_for(calm, timeout=30) and not await _count_status("uncertain", 1),
          "после «Отправить повторно» всё выполнено, очередь пустая")

    RUN = False
    await asyncio.gather(*bg, watcher, return_exceptions=True)
    elapsed = time.monotonic() - t0

    print("4. итог по каждому клиенту")
    async with session_scope() as s:
        rows = (await s.execute(select(Order, User).join(User, Order.user_id == User.id))).all()
        by_user = defaultdict(list)
        for o, u in rows:
            by_user[u.tg_id - 70_000].append(o)
        users = {u.tg_id - 70_000: u for u in (await s.execute(select(User))).scalars() if u.tg_id >= 70_000}
        led = defaultdict(list)
        dep = Counter()
        for e in (await s.execute(select(LedgerEntry))).scalars():
            led[e.order_id].append(e)
            if e.kind == "deposit":
                dep[e.user_id] += e.delta_micro
    uid_of = {i: u.id for i, u in users.items()}

    def st(i):
        return by_user[i][0].status if by_user[i] else None

    def spent(i):  # what visitor i paid for orders (refunds already netted in charged_micro)
        return sum(o.charged_micro for o in by_user[i])
    print("     статусы заказов:", dict(Counter(o.status for o, _ in rows)))
    async with session_scope() as s:
        print("     переводы:", dict(Counter((await s.execute(select(Transfer.status))).scalars())))
        shown = 0
        for o, _ in rows:
            if o.status != "awaiting_payment" or shown >= 4:
                continue
            shown += 1
            invs = (await s.execute(select(Invoice).where(Invoice.order_id == o.id))).scalars().all()
            trs = (await s.execute(select(Transfer).where(Transfer.invoice_id.in_([i.id for i in invs])))).scalars().all()
            print(f"     ждёт {o.public_id}: счета {[(i.id, i.status, i.method, i.units) for i in invs]} "
                  f"переводы {[(t.id, t.status, t.units) for t in trs]}")
        for t in (await s.execute(select(Transfer).where(Transfer.status != "credited").limit(6))).scalars():
            same = (await s.execute(select(Invoice.id, Invoice.status, Invoice.user_id).where(
                Invoice.method == t.method, Invoice.units == t.units))).all()
            print(f"     перевод {t.id} {t.status} {t.method} {t.units} note={t.note} → счета с такой суммой {same}")
    check(not [o for o, _ in rows if o.status in ("paid", "submitting", "processing", "queued", "uncertain")],
          "ни один заказ не завис в промежуточном статусе")
    delivered = [o for o, _ in rows if o.status == "delivered"]
    check(all(supplier.creates[o.public_id] == 1 for o in delivered),
          f"каждый из {len(delivered)} выполненных заказов создан у поставщика ровно один раз")
    extra = [p for p, n in supplier.creates.items() if n > 1]
    check(not extra, "ни одного двойного пополнения Steam" + (f" — {extra}" if extra else ""))
    check(all(sum(e.delta_micro for e in led[o.id] if e.kind == "order") == -o.charged_micro
              and o.charged_micro >= o.price_micro - O.PAY_TOLERANCE_MICRO for o in delivered),
          "каждый выполненный заказ оплачен полностью и ровно один раз")
    rejected = [o for o, _ in rows if o.status == "rejected"]
    check(all(sum(e.delta_micro for e in led[o.id]) == 0 and o.charged_micro == 0 for o in rejected),
          f"{len(rejected)} отклонённых: деньги вернулись на баланс полностью, один раз")
    exact = [i for i in range(30)]
    check(all(st(i) == "delivered" for i in exact), "30 оплативших точную сумму → Steam пополнен")
    partial = range(30, 45)
    ok_partial = [i for i in partial if st(i) == "delivered"]
    check(len(ok_partial) == 15, f"15 оплативших частями (60% + доплата) → пополнено {len(ok_partial)}")
    check(all(st(i) == "delivered" and users[i].balance_micro > 0 for i in range(45, 50)),
          "5 переплативших ×1.5 в TON (с мемо) → Steam пополнен, излишек лежит на балансе")
    check(all(st(i) == "delivered" and users[i].balance_micro > 0 and "Зачислено" in getattr(visitors[i], "claim_answer", "")
              for i in range(50, 55)),
          "5 переплативших ×1.5 в TRC20 → платёж неопознан, клиент прислал хэш, админ нажал «Зачислить» → "
          "Steam пополнен, излишек на балансе")
    two = [v for v in visitors[55:65]]
    check(all(sorted(getattr(v, "api_results", [])) == [200, 200, 402, 402, 402] for v in two),
          "API: $5 на балансе и 5 параллельных заказов по ~$1.96 → ровно 2 приняты, 3 получили 402")
    check(all(users[v.i].balance_micro == dep[uid_of[v.i]] - spent(v.i) and len(by_user[v.i]) == 2 for v in two),
          "API: остаток = пополнение минус ровно два заказа")
    check(all(users[i].balance_micro == dep[uid_of[i]] - spent(i) for i in users),
          "у всех 100: баланс = зачисленные платежи − оплаченные заказы (+ возвраты)")
    idem = visitors[65:75]
    check(all(len(getattr(v, "idem_ids", ())) == 1 and len(by_user[v.i]) == 1 for v in idem),
          "API: 10 одновременных запросов с одним external_id → один заказ, все ответы про него")
    rej = range(75, 80)
    check(all(st(i) == "rejected" and users[i].balance_micro == dep[uid_of[i]] > 0 for i in rej),
          "5 заказов, отклонённых поставщиком → всё, что клиент заплатил, на его балансе")
    check(all(not by_user[i] for i in range(80, 85)), "5 несуществующих Steam-логинов → заказ не создан, счёт не выставлен")
    canc = range(85, 90)
    check(all(st(i) == "cancelled" and users[i].balance_micro > 0 for i in canc),
          "5 отменивших и всё равно заплативших → заказ отменён, деньги на балансе")
    late = range(90, 95)
    check(all(st(i) == "expired" and users[i].balance_micro > 0 for i in late),
          "5 заплативших через 2 часа после истечения счёта → заказ не выполнен, деньги на балансе")
    snoops = visitors[95:]
    check(all(getattr(v, "snoop_saw", -1) == 0 and getattr(v, "snooped", 0) >= 5 for v in snoops),
          f"любопытные: {sum(v.snooped for v in snoops)} попыток открыть чужие заказы/пополнения/QR — всё 404, "
          "в «Моих заказах» только свои")
    check(all(st(v.i) == "delivered" for v in snoops),
          "после попыток подсмотреть чужое их собственные заказы работают")
    await money_invariants("после нагрузки")

    async with session_scope() as s:
        lag = sorted((le.created_at - t.created_at).total_seconds() for t, le in (await s.execute(
            select(Transfer, LedgerEntry).join(LedgerEntry, LedgerEntry.transfer_id == Transfer.id))).all())
        work = sorted((o.finished_at - o.paid_at).total_seconds() for o in (await s.execute(
            select(Order).where(Order.status == "delivered"))).scalars())

    def pct(xs, q):
        return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0
    print(f"     платёж → зачислен: медиана {pct(lag, .5):.1f} с, 95% {pct(lag, .95):.1f} с, макс {lag[-1] if lag else 0:.1f} с")
    print(f"     оплачен → Steam пополнен: медиана {pct(work, .5):.1f} с, 95% {pct(work, .95):.1f} с, макс {work[-1] if work else 0:.1f} с")
    check(pct(lag, .95) < 60, "95% платежей зачислены быстрее минуты даже при 100 оплатах за полминуты")

    print("5. уникальные суммы счетов")
    async with session_scope() as s:
        dups = (await s.execute(select(Invoice.method, Invoice.units, func.count()).group_by(Invoice.method, Invoice.units)
                                .having(func.count() > 1))).all()
        n_inv = (await s.execute(select(func.count()).select_from(Invoice))).scalar_one()
    check(not dups, f"{n_inv} счетов: ни одной повторяющейся суммы в одной сети" + (f" — дубли {dups[:5]}" if dups else ""))

    print("6. уведомления")
    ready = Counter(t.split("Заказ ")[-1].split("<")[0] for c, t in SENT if "Готово!" in t)
    check(all(ready.get(o.public_id, 0) == 1 for o in delivered) and sum(ready.values()) == len(delivered),
          f"«Готово!» пришло по каждому выполненному заказу ровно один раз ({sum(ready.values())})")
    cards = Counter(t.split("<code>")[1].split("</code>")[0] for c, t in SENT if c == 999 and "Новый заказ" in t)
    check(all(cards.get(o.public_id, 0) == 1 for o in delivered),
          "админу по каждому выполненному заказу одна карточка «Новый заказ» с маржой")
    check(all("Маржа" in t for c, t in SENT if c == 999 and "Новый заказ" in t), "в каждой карточке указана маржа")

    print("7. сеть, ошибки, баны")
    check(not BLOCKED, "приложение ни разу не пыталось выйти в сеть" + (f" — {BLOCKED[:5]}" if BLOCKED else ""))
    fivexx = sum(n for code, n in STATUS.items() if code >= 500)
    check(fivexx == 0, f"ни одной ошибки 5xx из {sum(STATUS.values())} запросов")
    check(not [v.ip for v in visitors if f2b.banned_for(v.ip)], "ни один обычный посетитель не забанен")
    check(any(f2b.banned_for(v.ip) for v in snoops) is False, "даже любопытные (много 404 по API) не забанены")
    print(f"     запросов: {sum(STATUS.values())}, коды: {dict(sorted(STATUS.items()))}, "
          f"пик соединений с БД: {peak}, вызовы поставщика: {dict(supplier.calls)}, время: {elapsed:.1f} с")

    await race_drills()
    await money_invariants("после гонок")

    print(f"\n{'ALL GOOD' if not PROBLEMS else 'PROBLEMS'}: {OK} checks passed, {len(PROBLEMS)} failed")
    for p in PROBLEMS[:40]:
        print("   -", p)
    sys.exit(1 if PROBLEMS else 0)


async def _alerts_for(n):
    return sum(1 for _, t in SENT if "завис" in t) >= n


# ------------------------------------------------------------------ race drills
async def fresh_user(tg: int, balance_micro: int = 0) -> User:
    async with session_scope() as s:
        u = User(tg_id=tg, username=f"race{tg}")
        s.add(u)
        await s.flush()
        if balance_micro:
            await change_balance(s, u.id, balance_micro, "adjust", comment="test")
        return u


async def force_processing(oid: int) -> None:
    """Get an order to «processing» at the supplier whatever fate the chaos supplier picked for it."""
    for _ in range(6):
        o = await get_order(oid)
        if o.status == "processing":
            return
        if o.status in ("paid", "queued"):
            await O.submit(oid)
        elif o.status == "uncertain":
            await O.reconcile(oid)
            if (await get_order(oid)).status == "uncertain":
                await O.admin_retry(oid)
    raise AssertionError(f"order {oid} stuck in {(await get_order(oid)).status}")


async def get_order(oid: int) -> Order:
    async with session_scope() as s:
        return await s.get(Order, oid)

async def race_drills():
    print("8. гонки: одно и то же действие одновременно")
    # a) the same transfer credited by two settle passes at once
    u = await fresh_user(90_001)
    async with session_scope() as s:
        inv = await create_deposit(s, u, amount_usd="3.00", method_code="usdt_trc20")
    await chain_pay(inv)
    async with session_scope() as s:
        tid = (await s.execute(select(Transfer.id).where(Transfer.invoice_id == inv.id))).scalar_one()
    await asyncio.gather(*(M.credit_transfer(tid, {}) for _ in range(6)))
    async with session_scope() as s:
        n = (await s.execute(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.transfer_id == tid))).scalar_one()
        bal = (await s.get(User, u.id)).balance_micro
        one = (await s.get(Transfer, tid)).credited_micro
    check(n == 1 and bal == one and 3_000_000 <= one < 3_010_000,
          f"одна транзакция, 6 параллельных зачислений → зачислено один раз (записей: {n}, баланс ${usd_str(bal)})")

    # b) the same tx seen twice at once by the chain reader
    u = await fresh_user(90_002)
    async with session_scope() as s:
        inv = await create_deposit(s, u, amount_usd="2.00", method_code="usdt_bep20")
    res = await asyncio.gather(*(chain_pay(inv, txid="dev" + "7" * 61) for _ in range(3)), return_exceptions=True)
    await M.settle({})
    async with session_scope() as s:
        n = (await s.execute(select(func.count()).select_from(Transfer).where(Transfer.txid == "dev" + "7" * 61))).scalar_one()
        bal = (await s.get(User, u.id)).balance_micro
    check(n == 1 and 2_000_000 <= bal < 2_010_000,
          f"одну транзакцию прочитали 3 раза одновременно → одна запись, зачислено $2 ({sum(isinstance(r, Exception) for r in res)} повторов отбито базой)")

    # c) a supplier verdict arrives from the webhook, the poller and the admin at the same moment
    u = await fresh_user(90_003, 10_000_000)
    async with session_scope() as s:
        o = await O.create_order(s, await s.get(User, u.id), steam_login="race_reject", amount=Decimal("300"),
                                 currency="RUB", method_code=None, balance_only=True)
        oid = o.id
    await force_processing(oid)
    await asyncio.gather(*(O.apply_status(oid, "rejected") for _ in range(5)), O.admin_refund(oid), O.admin_refund(oid))
    async with session_scope() as s:
        refunds = (await s.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.order_id == oid, LedgerEntry.kind == "refund"))).scalar_one()
        bal = (await s.get(User, u.id)).balance_micro
    check(refunds == 1 and bal == 10_000_000, f"отказ поставщика ×5 + «Вернуть» админом ×2 одновременно → один возврат (возвратов: {refunds})")

    u = await fresh_user(90_004, 10_000_000)
    async with session_scope() as s:
        o = await O.create_order(s, await s.get(User, u.id), steam_login="race_ok", amount=Decimal("300"),
                                 currency="RUB", method_code=None, balance_only=True)
        oid = o.id
    await force_processing(oid)
    await asyncio.gather(O.apply_status(oid, "delivered"), O.apply_status(oid, "rejected"), O.admin_refund(oid))
    o = await order_row((await _pid(oid)))
    async with session_scope() as s:
        net = (await s.execute(select(func.sum(LedgerEntry.delta_micro)).where(LedgerEntry.order_id == oid))).scalar_one()
    check((o.status == "delivered" and net == -o.price_micro) or (o.status == "rejected" and net == 0),
          f"«выполнен» и «отклонён» одновременно → одно решение ({o.status}), деньги сходятся")

    # d) one order, two full payments credited at the same moment
    u = await fresh_user(90_005)
    async with session_scope() as s:
        o = await O.create_order(s, await s.get(User, u.id), steam_login="race_twice", amount=Decimal("250"),
                                 currency="RUB", method_code="usdt_trc20")
        oid, pid = o.id, o.public_id
    inv = await open_invoice(pid)
    await chain_pay(inv)
    await chain_pay(inv, txid="dev" + "8" * 61)
    async with session_scope() as s:
        tids = (await s.execute(select(Transfer.id).where(Transfer.invoice_id == inv.id))).scalars().all()
    await asyncio.gather(*(M.credit_transfer(t, {}) for t in tids))
    async with session_scope() as s:
        charges = (await s.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.order_id == oid, LedgerEntry.kind == "order"))).scalar_one()
        u2 = await s.get(User, u.id)
        deposits = (await s.execute(select(func.sum(LedgerEntry.delta_micro)).where(
            LedgerEntry.user_id == u.id, LedgerEntry.kind == "deposit"))).scalar_one()
    check(len(tids) == 2 and charges == 1 and u2.balance_micro == deposits - (await order_row(pid)).charged_micro,
          f"заказ оплатили дважды одновременно → списан один раз, второй платёж на балансе (списаний: {charges})")

    # e) the admin double-taps «Зачислить» on a claim
    u = await fresh_user(90_006)
    async with session_scope() as s:
        o = await O.create_order(s, await s.get(User, u.id), steam_login="race_claim", amount=Decimal("200"),
                                 currency="RUB", method_code="usdt_trc20")
        oid, pid = o.id, o.public_id
    m = METHODS["usdt_trc20"]
    await M.ingest([Incoming(method="usdt_trc20", txid="dev" + "9" * 61, idx="0", to_address=m.address,
                             units=987_654_321, from_address="exchange", block_time=utcnow(),
                             confirmations=m.confirmations, final=True)])
    async with session_scope() as s:
        s.add(Claim(user_id=u.id, order_id=oid, txid="dev" + "9" * 61, status="waiting"))
    await M.process_claims()
    async with session_scope() as s:
        cid = (await s.execute(select(Claim.id).where(Claim.user_id == u.id))).scalar_one()
    answers = await asyncio.gather(*(M.approve_claim(cid, True) for _ in range(5)))
    async with session_scope() as s:
        credits = (await s.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.user_id == u.id, LedgerEntry.kind == "deposit"))).scalar_one()
    check(credits == 1, f"«Зачислить» нажали 5 раз одновременно → зачислено один раз (ответы: {Counter(answers)})")

    # f) one visitor opens 10 orders at once while the limit is 3 unpaid
    v = Visitor(500)
    await v.login()
    await asyncio.gather(*(v.web_order(200, "RUB", method="usdt_trc20", expect=(200, 400, 429)) for _ in range(10)))
    async with session_scope() as s:
        uid = (await s.execute(select(User.id).where(User.tg_id == v.tg))).scalar_one()
        n = (await s.execute(select(func.count()).select_from(Order).where(Order.user_id == uid,
                                                                           Order.status == "awaiting_payment"))).scalar_one()
    check(n <= 3, f"10 заказов разом при лимите 3 неоплаченных → открыто {n}")

    # h) the answer to "create" was lost, 150 newer orders then pushed it deep into the supplier's history
    #    (their API lists newest first), and the admin taps «Отправить повторно» — must NOT top up Steam twice
    for tap_retry in (False, True):
        rich = await fresh_user(92_000 + tap_retry, 900_000_000)
        async with session_scope() as s:
            o = await O.create_order(s, await s.get(User, rich.id), steam_login="deep_lost", amount=Decimal("100"),
                                     currency="RUB", method_code=None, balance_only=True)
            lost_id, lost_pid = o.id, o.public_id
        supplier.seen[lost_pid].update({"lb", "429"})  # no other injected fault for this one
        supplier.lose_answer.add(lost_pid)
        await O.submit(lost_id)
        filler = []
        for _ in range(150):
            async with session_scope() as s:
                f = await O.create_order(s, await s.get(User, rich.id), steam_login="deep_filler", amount=Decimal("100"),
                                         currency="RUB", method_code=None, balance_only=True)
                filler.append(f.id)
        for fid in filler:
            await force_processing(fid)
        async with session_scope() as s:
            await s.execute(update(Order).where(Order.id == lost_id).values(submitted_at=utcnow() - timedelta(minutes=10)))
        if tap_retry:
            await O.admin_retry(lost_id)  # the admin didn't wait for the automatic check
            await O.submit(lost_id)
        else:
            await O.reconcile(lost_id)
        lost = await get_order(lost_id)
        where = ("«Отправить повторно»: сайт сначала заглянул в историю и не отправил второй раз" if tap_retry
                 else "автосверка нашла его на 2-й странице истории")
        check(supplier.creates[lost_pid] == 1 and lost.status == "processing" and lost.nervixy_order_id,
              f"ответ потерян, сверху ещё 150 заказов → {where} (создано у поставщика: {supplier.creates[lost_pid]})")

    # g) 50 people ask for the same amount in the same coin at the same instant
    crowd = [await fresh_user(91_000 + k) for k in range(50)]

    async def dep(user):
        async with session_scope() as s:
            return await create_deposit(s, await s.get(User, user.id), amount_usd="1.00", method_code="ton")
    invs = await asyncio.gather(*(dep(x) for x in crowd), return_exceptions=True)
    made = [x for x in invs if not isinstance(x, Exception)]
    units = Counter(x.units for x in made)
    check(len(made) == 50 and all(n == 1 for n in units.values()),
          f"50 счетов в TON на $1 одновременно → {len(units)} разных сумм из {len(made)}")


async def _pid(oid):
    async with session_scope() as s:
        return (await s.get(Order, oid)).public_id


asyncio.run(main())
