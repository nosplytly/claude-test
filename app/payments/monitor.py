"""Polls every chain, stores incoming transfers, matches them to invoices and credits balances.

Every chain has two workers of its own: the poller (reads the chain, records transfers) and the credit worker
(verifies final transfers and credits them — several at once, woken by the poller the moment one is ready).
A slow explorer or a stuck chain holds up only itself. Crediting never waits for Telegram: the customer's message
is queued in the same transaction as the money (app.outbox) and sent by its own worker.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import events, outbox
from ..config import settings
from ..db import session_scope
from ..ledger import change_balance
from ..models import Claim, Invoice, Order, Transfer, User
from ..money import plain, to_micro, units_to_amount, usd_str
from ..notify import esc, notify_admins
from ..prices import price_feed
from ..tgui import DANGER, PRIMARY, SUCCESS, btn, e, kb, panel, site
from ..utils import background, supervised, utcnow
from ..webhooks import queue_webhook
from . import crosscheck
from .chains.base import Incoming, Watcher
from .chains.evm import EvmWatcher
from .chains.solana import SolanaWatcher
from .chains.ton import TonWatcher
from .chains.tron import TronWatcher
from .chains.utxo import UtxoWatcher
from .invoices import effective_price
from .methods import METHODS, Method, enabled_methods

log = logging.getLogger("sh.monitor")

DUST_USD = Decimal("0.10")  # unmatched transfers below this are spam (address poisoning), not payments


def dust_threshold(m: Method) -> Decimal:
    # BTC spam comes as 546-sat outputs (~$0.5); no real BTC payment can be that small anyway (min_usd=1)
    return max(DUST_USD, m.min_usd * Decimal("0.9"))
ALERT_MAX_AGE = timedelta(hours=3)
TOLERANCE = Decimal("0.10")  # fuzzy match: received within ±10% of the expected amount
# seconds between polls: (while invoices are open, idle)
INTERVALS = {"tron": (10, 120), "ton": (10, 120), "sol": (12, 180), "bsc": (8, 120), "eth": (15, 120),
             "btc": (30, 300), "ltc": (40, 600)}
CREDIT_PARALLEL = 4  # transfers of one chain verified and credited at the same time (each one is claimed atomically)
CREDIT_IDLE = 5.0  # a credit worker looks anyway this often, even if nothing woke it
XCHECK_BACKOFF = [10, 20, 40, 60, 120, 300]  # seconds before asking the second provider again
XCHECK_ALERT_AFTER = timedelta(minutes=15)  # it still can't tell: an admin decides


def build_watchers() -> dict[str, Watcher]:
    by_chain: dict[str, list[Method]] = defaultdict(list)
    for m in enabled_methods():
        by_chain[m.chain].append(m)
    out: dict[str, Watcher] = {}
    for chain, ms in by_chain.items():
        if chain == "tron":
            out[chain] = TronWatcher(ms)
        elif chain in ("eth", "bsc"):
            out[chain] = EvmWatcher(chain, ms)
        elif chain in ("btc", "ltc"):
            out[chain] = UtxoWatcher(chain, ms)
        elif chain == "ton":
            out[chain] = TonWatcher(ms)
        elif chain == "sol":
            out[chain] = SolanaWatcher(ms)
    return out


def _norm_comment(c: str | None) -> str:
    return "".join(ch for ch in (c or "").upper() if ch.isalnum())


async def match_invoice(s: AsyncSession, t: Transfer) -> Invoice | None:
    m = METHODS[t.method]
    when = t.block_time or t.created_at or utcnow()
    # only invoices issued for the very address the money arrived at (the wallet in .env may change over time)
    same_address = func.lower(Invoice.address) == t.to_address.lower()
    # 1) memo (TON): the order id in the comment identifies the invoice regardless of amount
    if m.uses_comment and t.comment:
        c = _norm_comment(t.comment)
        if c:
            inv = (await s.execute(select(Invoice).where(Invoice.method == t.method, same_address,
                                                         Invoice.comment.is_not(None), Invoice.reserved_until > when)
                                   .order_by(Invoice.id.desc()))).scalars().all()
            for i in inv:
                if _norm_comment(i.comment) and _norm_comment(i.comment) in c:
                    return i
    # 2) exact unique amount, invoice created before the transfer happened
    inv = (await s.execute(select(Invoice).where(
        Invoice.method == t.method, same_address, Invoice.units == t.units, Invoice.reserved_until > when,
        Invoice.created_at <= when + timedelta(minutes=3)).order_by(Invoice.id.desc()))).scalars().first()
    if inv:
        return inv
    # 3) fuzzy: an invoice a little ABOVE what arrived (the exchange deducted its fee) — but only when it is the ONLY
    # open invoice anywhere near this amount on the network. With several customers paying at once, an odd amount
    # (someone paid 1.5x by mistake) could otherwise land on a stranger's invoice: one customer's money credited to
    # another. Ambiguous payments go to the claim-by-tx-hash flow, where the payer proves it's theirs.
    open_inv = (await s.execute(select(Invoice).where(
        Invoice.method == t.method, same_address, Invoice.status == "pending",
        Invoice.created_at <= when + timedelta(minutes=3),
        Invoice.expires_at >= when - timedelta(minutes=10)))).scalars().all()
    close = [i for i in open_inv if i.amount * (1 - TOLERANCE) <= t.amount <= i.amount]
    nearby = [i for i in open_inv if t.amount / 2 <= i.amount <= t.amount * 2]
    if len(close) == 1 and len(nearby) == 1:
        return close[0]
    return None


async def ingest(items: list[Incoming]) -> None:
    if not items:
        return
    async with session_scope() as s:
        uids = [i.uid for i in items]
        existing = {t.uid: t for t in (await s.execute(select(Transfer).where(Transfer.uid.in_(uids)))).scalars()}
        for inc in items:
            m = METHODS[inc.method]
            t = existing.get(inc.uid)
            if t:
                if t.final:
                    continue
                t.confirmations = max(t.confirmations, inc.confirmations)
                t.final = inc.final or t.confirmations >= m.confirmations and m.chain in ("eth", "bsc", "btc", "ltc")
                if inc.block_number:
                    t.block_number = inc.block_number
                if inc.block_time:
                    t.block_time = inc.block_time
                continue
            amount = units_to_amount(inc.units, m.decimals)
            t = Transfer(uid=inc.uid, method=inc.method, txid=inc.txid, from_address=inc.from_address,
                         to_address=inc.to_address, units=str(inc.units), amount=amount, comment=inc.comment,
                         block_number=inc.block_number, block_time=inc.block_time, confirmations=inc.confirmations,
                         final=inc.final, status="new")
            s.add(t)
            await s.flush()
            # match first: a small exact payment (e.g. the remainder of a partial one) must not be lost as dust
            inv = await match_invoice(s, t)
            if inv:
                t.status, t.invoice_id, t.user_id = "matched", inv.id, inv.user_id
                if inv.status == "pending":
                    inv.status = "detected"
                log.info("transfer %s %s %s matched invoice %s", t.method, plain(amount), t.txid, inv.id)
                continue
            price = price_feed.price(m.coin)
            if price is not None and amount * price < dust_threshold(m):
                t.status, t.note = "ignored", "dust"  # address-poisoning spam, 546-sat dust, etc.
                continue
            t.status = "unmatched"
            log.info("transfer %s %s %s unmatched", t.method, plain(amount), t.txid)


async def refresh_block_confirmations(chain: str, head: int) -> None:
    """EVM logs are read once; confirmations of already-seen transfers grow with the chain head."""
    codes = [m.code for m in enabled_methods() if m.chain == chain]
    async with session_scope() as s:
        rows = (await s.execute(select(Transfer).where(Transfer.method.in_(codes), Transfer.final.is_(False),
                                                       Transfer.block_number.is_not(None)))).scalars().all()
        for t in rows:
            t.confirmations = max(t.confirmations, head - t.block_number + 1)
            t.final = t.confirmations >= METHODS[t.method].confirmations


async def credit_ready(watchers: dict[str, Watcher], methods: list[str] | None = None) -> int:
    """Credit the final matched transfers (of these methods), CREDIT_PARALLEL at a time. Two workers reaching the same
    transfer is harmless: credit_transfer claims it with an atomic UPDATE, and only one of them gets it."""
    async with session_scope() as s:
        q = select(Transfer.id).where(Transfer.final.is_(True), Transfer.status == "matched",
                                      or_(Transfer.xcheck_after.is_(None), Transfer.xcheck_after <= utcnow()))
        if methods is not None:
            q = q.where(Transfer.method.in_(methods))
        todo = (await s.execute(q.order_by(Transfer.id))).scalars().all()
    if not todo:
        return 0
    sem = asyncio.Semaphore(CREDIT_PARALLEL)

    async def one(tid: int) -> None:
        async with sem:
            try:
                await credit_transfer(tid, watchers)
            except Exception:
                log.exception("credit transfer %s failed", tid)

    await asyncio.gather(*(one(tid) for tid in todo))
    return len(todo)


async def alert_unmatched() -> None:
    """Final transfers that matched no invoice: the admins hear about each once (queued with the flag itself)."""
    async with session_scope() as s:
        lonely = (await s.execute(select(Transfer).where(Transfer.final.is_(True), Transfer.status == "unmatched",
                                                          Transfer.admin_alerted.is_(False)))).scalars().all()
        recent = utcnow() - ALERT_MAX_AGE
        for t in lonely:
            t.admin_alerted = True
            # old history of the address (first poll of a reused wallet) is recorded silently
            if (t.block_time or t.created_at) < recent:
                continue
            m = METHODS[t.method]
            await outbox.to_admins(
                s, f"{e('question')} <b>Неопознанный платёж</b>\n"
                + panel(f"{e('coin')} <b>{plain(t.amount)} {m.coin}</b> · {m.network_title}",
                        f"{e('user')} от <code>{esc(t.from_address or '?')}</code>")
                + "\nНи к одному счёту не подошёл по сумме. Если клиент пришлёт хэш через сайт — придёт заявка с кнопками.",
                kb([btn("Транзакция", url=m.tx_url(t.txid), icon="tx")]))


async def housekeeping() -> None:
    await alert_unmatched()
    await process_claims()


async def settle(watchers: dict[str, Watcher]) -> None:
    """Everything in one go: credit what's ready on every chain, alert, process claims (tests, local dev payments;
    the running service does the same with a worker per chain)."""
    await credit_ready(watchers)
    await housekeeping()


async def credit_transfer(tid: int, watchers: dict[str, Watcher]) -> None:
    async with session_scope() as s:
        t = await s.get(Transfer, tid)
        if not t or t.status != "matched":
            return
        m = METHODS[t.method]
        txid, bn = t.txid, t.block_number
        expect = crosscheck.Expect(m, txid, t.uid.rsplit(":", 1)[-1], int(t.units), t.to_address)
        second_done = t.xcheck_ok
    w = watchers.get(m.chain)
    simulated = settings.is_dev and txid.startswith("dev")  # /api/dev/pay in local mock mode
    if w and not simulated and not await w.verify(txid, bn):
        log.warning("transfer %s failed verification (reorg/failed tx?)", txid)
        async with session_scope() as s:
            t = await s.get(Transfer, tid)
            t.note = "verify failed"
            t.final = False
        return
    if not simulated and settings.crosscheck_enabled and not second_done:
        verdict, why = await crosscheck.checker.check(expect, skip=_primary_base(w))
        if verdict is not crosscheck.Verdict.OK:
            await _second_opinion_failed(tid, verdict, why)
            return
        log.info("transfer %s: %s", txid, why)

    from ..orders import after_deposit, fmt_amount  # local import: orders imports payments

    async with session_scope() as s:
        # claim the transfer first: of several passes crediting it at the same moment, exactly one gets rowcount 1
        claimed = await s.execute(update(Transfer).where(Transfer.id == tid, Transfer.status == "matched")
                                  .values(status="credited").execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            return
        t = await s.get(Transfer, tid)
        t.xcheck_ok = True
        inv = await s.get(Invoice, t.invoice_id)
        when = t.block_time or t.created_at
        eff = inv.coin_price
        late = when > inv.expires_at + timedelta(minutes=10)
        if late and not m.stable:
            cur = effective_price(m)
            if cur is None:
                t.status = "matched"  # wait for a fresh price before valuing a late volatile payment
                return
            eff = min(eff, cur)
        value = to_micro(t.amount * eff)
        await change_balance(s, inv.user_id, value, "deposit", transfer_id=t.id,
                             comment=f"{plain(t.amount)} {m.coin} ({m.network})")
        t.status, t.credited_micro = "credited", value
        inv.received_micro += value
        if inv.received_micro >= inv.usd_micro - 20_000:
            inv.status, inv.paid_at = "paid", utcnow()
        else:
            inv.status = "partial"
        res = await after_deposit(s, inv, value, when)
        order = await s.get(Order, inv.order_id) if inv.order_id else None
        payer = await s.get(User, inv.user_id)
        bal = (await s.execute(select(User.balance_micro).where(User.id == inv.user_id))).scalar_one()
        if order is None:
            await queue_webhook(s, inv.user_id, "deposit.credited", {
                "deposit_id": inv.public_id, "credited_usd": usd_str(value, 6), "amount": plain(t.amount),
                "coin": m.coin, "network": m.network, "txid": t.txid, "balance_usd": usd_str(bal, 6)})
        paid = f"{plain(t.amount)} {m.coin} · {m.network_title if m.network == m.coin else m.network}"
        uid = inv.user_id
        if res.get("paid_order"):
            await outbox.to_user(s, uid, f"{e('card')} <b>Оплата получена</b> · {paid}\n"
                                 + panel(f"{e('wait')} Пополняем Steam <code>{esc(order.steam_login)}</code> на "
                                         f"<b>{fmt_amount(order.amount, order.currency)}</b>…")
                                 + f"\nПришлю сообщение, как только всё будет готово.\n<i>Заказ {order.public_id}</i>",
                                 kb([btn("Мои заказы", cb="m:orders", icon="orders")]))
        elif res.get("late"):
            await outbox.to_user(s, uid, f"{e('money')} <b>Платёж зачислен на баланс</b> · {paid}\n"
                                 + panel(f"+<b>${usd_str(value)}</b> → баланс ${usd_str(bal)}")
                                 + "\nОн пришёл после истечения счёта, поэтому заказ не выполнен автоматически. "
                                 "Оформите заказ заново — он оплатится с баланса.",
                                 kb([btn("Оформить заказ", url=site("/"), style=PRIMARY, icon="rocket")]))
        elif res.get("remainder_invoice") is not None:
            ri, pid = res["remainder_invoice"], order.public_id if order else ""
            await outbox.to_user(s, uid, f"{e('warn')} <b>Не хватает для заказа {pid}</b>\n"
                                 + panel(f"Получено ${usd_str(value)}, не хватает ${usd_str(res.get('missing'))}.")
                                 + f"\nДоплатите <b>{plain(ri.amount)} {m.coin}</b> ({m.network}) — новый счёт уже на сайте.",
                                 kb([btn("Открыть счёт", url=site(f"/#/order/{pid}"), style=PRIMARY, icon="card")]))
        elif order is None:
            await outbox.to_user(s, uid, f"{e('money')} <b>Баланс пополнен</b>\n"
                                 + panel(f"+<b>${usd_str(value)}</b> · {paid}", f"На балансе: <b>${usd_str(bal)}</b>"),
                                 kb([btn("Пополнить Steam", url=site("/"), style=PRIMARY, icon="rocket")],
                                    [btn("Баланс", cb="m:bal", style=SUCCESS, icon="money")]))
            await outbox.to_admins(s, f"{e('money')} <b>Пополнение баланса</b> · +${usd_str(value)}\n"
                                      f"{e('user')} {esc(payer.display_name)} · {paid}",
                                   kb([btn("Транзакция", url=m.tx_url(t.txid), icon="tx")]))


def _primary_base(w: Watcher | None) -> str | None:
    """The provider our watcher read the payment from — the second opinion must come from someone else."""
    if w is None:
        return None
    if getattr(w, "providers", None):  # BTC / LTC: whichever of its providers answered last
        return w.providers[w.active][1]
    return getattr(w, "url", None) or getattr(w, "base", None)


def _held_card(t: Transfer, m: Method, why: str, title: str) -> tuple[str, object]:
    return (f"{e('alarm')} <b>{title}</b>\n"
            + panel(f"{e('coin')} <b>{plain(t.amount)} {m.coin}</b> · {m.network_title}",
                    f"{e('warn')} {esc(why)}")
            + "\nДеньги клиенту не зачислены. Проверь транзакцию в обозревателе и реши:",
            kb([btn("Зачислить", cb=f"tx:ok:{t.id}", style=SUCCESS, icon="ok"),
                btn("Отклонить", cb=f"tx:no:{t.id}", style=DANGER, icon="cancel")],
               [btn("Транзакция", url=m.tx_url(t.txid), icon="tx")]))


async def _second_opinion_failed(tid: int, verdict: crosscheck.Verdict, why: str) -> None:
    """MISMATCH: hold the transfer and ask an admin at once. UNKNOWN: ask the second provider again a bit later;
    if it still can't tell after XCHECK_ALERT_AFTER, ask an admin (once) — the transfer keeps being re-checked."""
    now = utcnow()
    async with session_scope() as s:
        t = await s.get(Transfer, tid)
        if not t or t.status != "matched":
            return
        m = METHODS[t.method]
        t.note = why[:500]
        if verdict is crosscheck.Verdict.MISMATCH:
            t.status = "held"
            log.warning("transfer %s held: the second provider disagrees: %s", t.txid, why)
            text, markup = _held_card(t, m, why, "Платёж не подтвердился вторым источником")
            await outbox.to_admins(s, text, markup)
            return
        t.xcheck_tries += 1
        t.xcheck_since = t.xcheck_since or now
        t.xcheck_after = now + timedelta(seconds=XCHECK_BACKOFF[min(t.xcheck_tries, len(XCHECK_BACKOFF)) - 1])
        log.info("transfer %s: second opinion not yet (%s), again at %s", t.txid, why, t.xcheck_after)
        if now - t.xcheck_since >= XCHECK_ALERT_AFTER and not t.xcheck_alerted:
            t.xcheck_alerted = True
            text, markup = _held_card(t, m, why, f"Платёж ждёт второй источник уже {int((now - t.xcheck_since).total_seconds() // 60)} мин")
            await outbox.to_admins(s, text, markup)


async def release_transfer(tid: int, approve: bool) -> str:
    """The admin's decision on a payment the second provider didn't confirm."""
    async with session_scope() as s:
        t = await s.get(Transfer, tid)
        if not t or t.status not in ("held", "matched"):
            return "Уже решено"
        m = METHODS[t.method]
        if approve:
            t.status, t.xcheck_ok, t.xcheck_after = "matched", True, None
            t.note = "проверено админом"
            events.kick_after_commit(s, events.credit(m.chain))
            return "Зачисляю"
        t.status, t.note = "rejected", "отклонено администратором: второй источник не подтвердил"
        if t.user_id:
            await outbox.to_user(s, t.user_id, f"{e('fail')} <b>Платёж не подтвердился</b>\n"
                                 "Мы не смогли подтвердить эту транзакцию в сети, поэтому не зачислили её. "
                                 "Если это ошибка — напишите в поддержку, разберёмся.",
                                 kb([btn("Поддержка", url=settings.support_url, icon="support")] if settings.support_url else []))
        return "Отклонено"


async def process_claims() -> None:
    """User-submitted tx hashes: bind to a recorded unmatched transfer, then ask an admin to approve."""
    async with session_scope() as s:
        waiting = (await s.execute(select(Claim).where(Claim.status == "waiting"))).scalars().all()
        for c in waiting:
            t = (await s.execute(select(Transfer).where(Transfer.txid.ilike(c.txid)).order_by(Transfer.id))).scalars().first()
            if not t:
                if utcnow() - c.created_at > timedelta(hours=24):
                    c.status, c.note, c.resolved_at = "rejected", "транзакция не найдена", utcnow()
                continue
            c.transfer_id = t.id
            tx_time = t.block_time or t.created_at
            order_for_claim = await s.get(Order, c.order_id) if c.order_id else None
            not_before = (order_for_claim.created_at - timedelta(minutes=10) if order_for_claim
                          else c.created_at - timedelta(hours=48))
            if t.user_id == c.user_id and t.status in ("matched", "credited"):
                c.status, c.note, c.resolved_at = "approved", "уже зачислено автоматически", utcnow()
            elif t.status in ("matched", "credited"):
                c.status, c.note, c.resolved_at = "rejected", "платёж уже зачислен другому заказу", utcnow()
            elif t.status == "ignored":
                c.status, c.note, c.resolved_at = "rejected", "слишком маленькая сумма", utcnow()
            elif tx_time < not_before:
                # tx hashes are public: someone else's old payment must not be claimable
                c.status, c.note, c.resolved_at = "rejected", "транзакция старше заказа", utcnow()
            elif t.status == "unmatched" and t.final:
                c.status = "pending"
                u = await s.get(User, c.user_id)
                o = await s.get(Order, c.order_id) if c.order_id else None
                m = METHODS[t.method]
                inv = (await s.execute(select(Invoice).where(Invoice.order_id == o.id).order_by(Invoice.id.desc()))
                       ).scalars().first() if o else None
                await outbox.to_admins(s,
                               f"{e('claim')} <b>Заявка #{c.id}</b> на зачисление платежа\n"
                               f"{e('user')} {esc(u.display_name)} (tg <code>{u.tg_id}</code>) говорит, что это его платёж:\n"
                               + panel(f"{e('coin')} <b>{plain(t.amount)} {m.coin}</b> · {m.network_title}",
                                       f"от <code>{esc(t.from_address or '?')}</code> · {tx_time:%d.%m %H:%M} UTC")
                               + (f"\n{e('orders')} Заказ <code>{o.public_id}</code>: счёт был на {plain(inv.amount)} "
                                  f"{METHODS[inv.method].coin} ({METHODS[inv.method].network})" if o and inv else ""),
                               kb([btn("Зачислить", cb=f"cl:ok:{c.id}", style=SUCCESS, icon="ok"),
                                   btn("Отклонить", cb=f"cl:no:{c.id}", style=DANGER, icon="cancel")],
                                  [btn("Транзакция", url=m.tx_url(t.txid), icon="tx")]))


async def approve_claim(claim_id: int, approve: bool) -> str:
    async with session_scope() as s:
        c = await s.get(Claim, claim_id)
        if not c or c.status != "pending":
            return "Заявка уже обработана"
        t = await s.get(Transfer, c.transfer_id)
        eff = effective_price(METHODS[t.method]) if approve else None
        if approve and eff is None:
            return "Нет актуального курса, попробуйте через минуту"
        # a double tap (or two admins) must not credit twice: take the claim, then the transfer, atomically
        taken = await s.execute(update(Claim).where(Claim.id == claim_id, Claim.status == "pending")
                                .values(status="approved" if approve else "rejected")
                                .execution_options(synchronize_session=False))
        if taken.rowcount != 1:
            return "Заявка уже обработана"
        if not approve:
            c.status, c.resolved_at, c.note = "rejected", utcnow(), "отклонено администратором"
            await outbox.to_user(s, c.user_id, f"{e('fail')} <b>Заявка на зачисление отклонена</b>\n"
                                 "Если это ошибка — напишите в поддержку, разберёмся.",
                                 kb([btn("Поддержка", url=settings.support_url, icon="support")] if settings.support_url else []))
            return "Отклонено"
        else:
            won = await s.execute(update(Transfer).where(Transfer.id == t.id, Transfer.status == "unmatched")
                                  .values(status="credited").execution_options(synchronize_session=False))
            if won.rowcount != 1:
                c.status, c.resolved_at, c.note = "rejected", utcnow(), "платёж уже зачислен"
                return "Платёж уже зачислен"
            m = METHODS[t.method]
            value = to_micro(t.amount * eff)
            await change_balance(s, c.user_id, value, "deposit", transfer_id=t.id,
                                 comment=f"{plain(t.amount)} {m.coin} ({m.network}), заявка #{c.id}")
            t.status, t.user_id, t.credited_micro = "credited", c.user_id, value
            c.status, c.resolved_at = "approved", utcnow()
            paid = False
            if c.order_id:
                o = await s.get(Order, c.order_id)
                if o and o.status in ("awaiting_payment", "expired"):
                    if o.status == "expired" and utcnow() - o.created_at < timedelta(hours=2):
                        o.status = "awaiting_payment"  # still honour recent orders
                    from ..orders import try_charge
                    paid = await try_charge(s, o)
            usd = usd_str(value)
            await outbox.to_user(s, c.user_id, f"{e('ok')} <b>Платёж подтверждён</b>\n+<b>${usd}</b> на балансе."
                                 + (f"\n{e('wait')} Заказ оплачен, пополняем Steam…" if paid else ""),
                                 kb([btn("Мои заказы" if paid else "Пополнить Steam",
                                         **({"cb": "m:orders"} if paid else {"url": site("/")}),
                                         style=PRIMARY, icon="orders" if paid else "rocket")]))
    return f"Зачислено ${usd}"


class Monitor:
    def __init__(self):
        self.watchers = build_watchers()
        self._active: set[str] = set()
        self._active_at = 0.0
        self.fail_since: dict[str, float] = {}
        self.last_ok: dict[str, float] = {}
        self.last_error: dict[str, str] = {}

    async def _active_chains(self) -> set[str]:
        async with session_scope() as s:
            rows = (await s.execute(select(Invoice.method).where(
                Invoice.status.in_(("pending", "detected")),
                Invoice.expires_at > utcnow() - timedelta(minutes=15)).distinct())).scalars().all()
            rows += (await s.execute(select(Transfer.method).where(
                Transfer.final.is_(False), Transfer.status.in_(("matched", "unmatched"))).distinct())).scalars().all()
        return {METHODS[m].chain for m in rows if m in METHODS}

    async def poll_chain(self, chain: str) -> None:
        w = self.watchers[chain]
        try:
            items = await w.poll()
            await ingest(items)
            if isinstance(w, EvmWatcher) and w._head:
                await refresh_block_confirmations(chain, w._head)
            events.kick(events.credit(chain))  # a transfer may have become final: credit it now
            self.last_ok[chain] = time.time()
            self.fail_since.pop(chain, None)
            self.last_error.pop(chain, None)
            for msg in getattr(w, "gap_warnings", [])[:]:
                background(notify_admins(f"{e('warn')} <b>Мониторинг {chain}</b>\n{esc(msg)}", key=f"gap-{chain}",
                                         every=6 * 3600))
            if hasattr(w, "gap_warnings"):
                w.gap_warnings.clear()
        except Exception as err:  # noqa: BLE001
            self.last_error[chain] = f"{type(err).__name__}: {err}"
            first = self.fail_since.setdefault(chain, time.time())
            log.warning("poll %s failed: %s", chain, self.last_error[chain][:300])
            if time.time() - first > 600:
                background(notify_admins(f"{e('net')} <b>Мониторинг {chain} не работает</b> уже "
                                         f"{int((time.time() - first) // 60)} мин\n"
                                         f"Платежи в этой сети пока не распознаются.\n<i>{esc(self.last_error[chain][:300])}</i>",
                                         key=f"chain-{chain}", every=3600))

    async def active_chains(self) -> set[str]:
        """Chains with open invoices / unconfirmed transfers (cached for 2 s, shared by all chain loops)."""
        if time.time() - self._active_at > 2:
            self._active, self._active_at = await self._active_chains(), time.time()
        return self._active

    async def _chain_loop(self, chain: str) -> None:
        # every chain has its own loop: a slow or rate-limited source never delays the others
        fast, slow = INTERVALS.get(chain, (15, 180))
        while True:
            started = time.time()
            await self.poll_chain(chain)
            while True:  # wait for the next poll; wake up early as soon as an invoice on this chain appears
                target = started + (fast if chain in await self.active_chains() else slow)
                if time.time() >= target:
                    break
                await asyncio.sleep(min(2.0, max(0.2, target - time.time())))

    async def _credit_loop(self, chain: str) -> None:
        from .. import health

        methods = [m.code for m in self.watchers[chain].methods]
        while True:
            health.beat("settle")
            await credit_ready(self.watchers, methods)
            await events.wait(events.credit(chain), CREDIT_IDLE)  # the poller wakes us when a transfer is final

    async def _housekeeping_loop(self) -> None:
        from .. import health

        while True:
            health.beat("settle")  # also alive with no wallets configured (then there's no credit worker)
            await housekeeping()
            await asyncio.sleep(5)

    async def run(self) -> None:
        if not self.watchers:
            log.warning("no wallets configured — crypto payments are disabled")
        tasks = [asyncio.create_task(supervised(f"chain-{c}", lambda c=c: self._chain_loop(c)))
                 for c in self.watchers]
        tasks += [asyncio.create_task(supervised(f"credit-{c}", lambda c=c: self._credit_loop(c)))
                  for c in self.watchers]
        tasks.append(asyncio.create_task(supervised("claims", self._housekeeping_loop)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()

    def status(self) -> dict:
        return {c: {"ok_ago": int(time.time() - self.last_ok[c]) if c in self.last_ok else None,
                    "error": self.last_error.get(c)} for c in self.watchers}


monitor: Monitor | None = None
