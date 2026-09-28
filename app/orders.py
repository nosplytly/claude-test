r"""Steam top-up orders.

awaiting_payment --(balance covers price)--> paid --submit--> processing --> delivered
                                                   \--> queued (supplier out of funds / rate limit), retried
                                                   \--> uncertain (no answer from supplier) -> reconcile / admin
                                             processing --> rejected (refunded to site balance)
awaiting_payment --> expired | cancelled
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import session_scope
from .ledger import change_balance
from .models import Invoice, Order, Transfer, User
from .money import D, from_micro, plain, to_micro, usd_str
from .nervixy import CURRENCIES, NervixyError, NervixyTransportError, nervixy
from .notify import edit as notify_edit
from .notify import esc, notify_admins, notify_user
from . import promos
from .payments.invoices import PaymentUnavailable, create_invoice
from .payments.methods import get_method
from .tgui import DANGER, PRIMARY, btn, e, kb, panel
from .utils import iso, public_id, utcnow
from .webhooks import queue_webhook

log = logging.getLogger("sh.orders")

ACTIVE = ("awaiting_payment", "paid", "submitting", "queued", "uncertain", "processing")
RESERVING = ("awaiting_payment", "paid", "submitting", "queued", "uncertain")  # supplier money not spent yet
PAY_TOLERANCE_MICRO = 20_000  # we absorb a shortfall of up to $0.02 (rounding by wallets/exchanges)
LATE_GRACE = timedelta(minutes=10)  # payments seen this long after expiry still pay the order
STEAM_LOGIN_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,64}$")
CUR_SIGN = {"RUB": "₽", "KZT": "₸", "UAH": "₴", "USD": "$"}


class OrderError(Exception):
    """Message is shown to the customer."""


class InsufficientBalance(OrderError):
    def __init__(self, balance_micro: int, required_micro: int):
        super().__init__(f"Недостаточно средств на балансе: {usd_str(balance_micro)}$, нужно {usd_str(required_micro)}$")
        self.balance_micro, self.required_micro = balance_micro, required_micro


def discount_for(user: User | None) -> Decimal:
    if user is not None and user.discount is not None:
        return D(user.discount)
    return settings.client_discount


PUBLIC_STATUS = {"paid": "processing", "submitting": "processing", "queued": "processing", "uncertain": "processing"}


def public_order(o: Order) -> dict:
    """Order as seen by partners (API responses and webhooks)."""
    return {"id": o.public_id, "external_id": o.external_id, "status": PUBLIC_STATUS.get(o.status, o.status),
            "steam_login": o.steam_login, "amount": plain(o.amount), "currency": o.currency,
            "price_usd": usd_str(o.price_micro, 6), "charged_usd": usd_str(o.charged_micro, 6),
            "created_at": iso(o.created_at), "finished_at": iso(o.finished_at),
            "error": o.error if o.status == "rejected" else None}


def method_label(code: str | None) -> str:
    if code == "balance":
        return "с баланса сайта"
    m = get_method(code or "")
    return f"{m.coin} · {m.network_title if m.network == m.coin else m.network}" if m else (code or "—")


def site(path: str = "/") -> str:
    return settings.base_url.rstrip("/") + path


def fmt_amount(amount: Decimal, currency: str) -> str:
    q = D(amount).quantize(Decimal("0.01"))
    s = f"{q:,.2f}".replace(",", " ")
    if s.endswith(".00"):
        s = s[:-3]
    return f"{s} {CUR_SIGN.get(currency, currency)}"


SUPPLIER_MIN_RUB = Decimal("15")  # nervixy refuses smaller top-ups (RUB equivalent)


async def limits(currency: str) -> tuple[Decimal, Decimal]:
    rates = await nervixy.rates()
    k = rates[currency] / rates["RUB"]  # units of `currency` per 1 RUB
    # our minimum is set in USD, but never below the supplier's own minimum (+5% for rate drift)
    lo = max(settings.min_order_usd * rates[currency], SUPPLIER_MIN_RUB * Decimal("1.05") * k)
    lo = lo.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    hi = (settings.max_order_rub * k).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    if currency != "USD":
        lo = lo.to_integral_value(rounding=ROUND_CEILING)
        hi = hi.to_integral_value(rounding=ROUND_FLOOR)
    return lo, hi


async def reserved_cost_micro(s: AsyncSession) -> int:
    return int((await s.execute(select(func.coalesce(func.sum(Order.cost_micro), 0))
                                .where(Order.status.in_(RESERVING)))).scalar_one())


async def capacity_micro(s: AsyncSession) -> int:
    acc = await nervixy.account(max_age=120)
    return to_micro(acc["balance"]) - await reserved_cost_micro(s)


async def create_order(s: AsyncSession, user: User, *, steam_login: str, amount: Decimal, currency: str,
                       method_code: str | None, source: str = "web", external_id: str | None = None,
                       balance_only: bool = False, promo_code: str | None = None) -> Order:
    steam_login = (steam_login or "").strip()
    currency = (currency or "").upper()
    if currency not in CURRENCIES:
        raise OrderError("Неизвестная валюта")
    if not STEAM_LOGIN_RE.match(steam_login):
        raise OrderError("Логин Steam: 3–64 символа — латиница, цифры, _ - .")
    try:
        amount = D(amount).quantize(Decimal("0.01"))
    except Exception as err:  # noqa: BLE001
        raise OrderError("Некорректная сумма") from err
    if user.is_banned:
        raise OrderError("Аккаунт заблокирован")

    # Supplier round-trips first (they can take seconds: nervixy is throttled), the database only afterwards —
    # so a DB connection is held for milliseconds, not while waiting on the network. `s` stays untouched until then.
    try:
        lo, hi = await limits(currency)
        rates = await nervixy.rates()
    except (NervixyError, NervixyTransportError) as err:
        log.warning("rates unavailable: %s", err)
        raise OrderError("Сервис пополнения временно недоступен, попробуйте через пару минут") from err
    if amount < lo or amount > hi:
        raise OrderError(f"Сумма должна быть от {fmt_amount(lo, currency)} до {fmt_amount(hi, currency)}")

    try:
        valid, msg = await nervixy.check_login(steam_login)
    except NervixyError as err:
        if err.status == 400:
            raise OrderError("Некорректный логин Steam") from err
        raise OrderError("Не удалось проверить логин Steam, попробуйте ещё раз") from err
    except NervixyTransportError as err:
        raise OrderError("Не удалось проверить логин Steam, попробуйте ещё раз") from err
    if not valid:
        raise OrderError("Этот Steam-аккаунт нельзя пополнить: проверьте логин (именно логин для входа, не никнейм)")

    fx = rates[currency]
    try:
        cd, sd = discount_for(user), await nervixy.supplier_discount()
        supplier_balance = to_micro((await nervixy.account(max_age=120))["balance"])
    except (NervixyError, NervixyTransportError) as err:
        raise OrderError("Сервис пополнения временно недоступен, попробуйте через пару минут") from err
    nominal = amount / fx
    cost_micro = to_micro(nominal * (1 - sd / 100), up=True)

    # ---- database only from here on. Write first: SQLite then serialises this block against parallel orders, so
    # the unpaid-orders limit and the balance check below see each other's rows (a read-first check would not).
    await s.execute(update(User).where(User.id == user.id).values(last_seen_at=utcnow())
                    .execution_options(synchronize_session=False))
    promo = None
    if promo_code:
        try:
            promo = await promos.find(s, promo_code)
            if promo.kind != "discount":
                raise promos.PromoError("Это промокод на баланс — активируйте его отдельно, кнопкой «Применить»")
            if await promos.uses_by(s, promo.id, user.id) >= promo.per_user:
                raise promos.PromoError("Вы уже использовали этот промокод")
        except promos.PromoError as err:
            raise OrderError(str(err)) from err
        cd = promos.combined(cd, sd, promo.value)
    price_micro = to_micro(nominal * (1 - cd / 100), up=True)
    if not balance_only:
        active = (await s.execute(select(func.count()).select_from(Order).where(
            Order.user_id == user.id, Order.status == "awaiting_payment"))).scalar_one()
        if active >= settings.max_active_orders:
            raise OrderError("Слишком много неоплаченных заказов — оплатите или отмените предыдущие")
    balance = (await s.execute(select(User.balance_micro).where(User.id == user.id))).scalar_one()
    if balance_only and balance < price_micro:
        raise InsufficientBalance(balance, price_micro)

    cap = supplier_balance - await reserved_cost_micro(s)
    if cost_micro > cap:  # alert in the background: this transaction holds the DB write lock
        asyncio.create_task(notify_admins(
            f"{e('warn')} <b>Не хватает баланса nervixy</b>\n"
            + panel(f"Клиент пытается оформить заказ на ${usd_str(cost_micro)}, а свободно ${usd_str(max(cap, 0))}.",
                    "Сайт пока отказывает в таких заказах — пополни баланс поставщика."),
            kb([btn("Nervixy", cb="a:nx", style=PRIMARY, icon="bank")]), key="capacity", every=900))
        max_amount = (from_micro(max(cap, 0)) / (1 - sd / 100) * fx).quantize(Decimal("1"), rounding=ROUND_FLOOR)
        if max_amount >= lo:
            raise OrderError(f"Сейчас можем принять заказ максимум на {fmt_amount(max_amount, currency)}. "
                             "Уменьшите сумму или попробуйте позже")
        raise OrderError("Пополнение временно недоступно — идёт пополнение резерва. Попробуйте позже")

    order = Order(public_id=public_id(), user_id=user.id, steam_login=steam_login, currency=currency, amount=amount,
                  fx_rate=fx, nominal_micro=to_micro(nominal), price_micro=price_micro, cost_micro=cost_micro,
                  client_discount=cd, supplier_discount=sd, status="awaiting_payment", method=method_code,
                  source=source, external_id=external_id,
                  promo_id=promo.id if promo else None, promo_pct=promo.value if promo else None)
    s.add(order)
    await s.flush()
    if promo:
        try:
            await promos.take(s, promo, user.id, order.id)  # atomically: the last free use can't go to two orders
        except promos.PromoError as err:
            raise OrderError(str(err)) from err

    if balance >= price_micro - (0 if balance_only else PAY_TOLERANCE_MICRO) and balance > 0:
        if not await try_charge(s, order):  # a parallel order spent the balance in the meantime
            if balance_only:
                now = (await s.execute(select(User.balance_micro).where(User.id == user.id))).scalar_one()
                raise InsufficientBalance(now, price_micro)
            raise OrderError("Не удалось списать баланс, попробуйте ещё раз")
        order.method = "balance"
        return order

    m = get_method(method_code or "")
    if not m or not m.enabled:
        raise OrderError("Выберите способ оплаты")
    try:
        await create_invoice(s, m=m, user_id=user.id, order_id=order.id, usd_micro=price_micro - max(balance, 0),
                             comment=order.public_id)
    except PaymentUnavailable as err:
        raise OrderError(f"Оплата в {m.title} временно недоступна, выберите другую монету") from err
    return order


async def move_status(s: AsyncSession, order_id: int, frm: tuple[str, ...], to: str) -> bool:
    """Atomic status change. When several parties decide about one order at the same moment (supplier webhook,
    status poller, admin button, two payments), exactly one wins; the others see False and must back off."""
    res = await s.execute(update(Order).where(Order.id == order_id, Order.status.in_(frm)).values(status=to)
                          .execution_options(synchronize_session=False))
    return res.rowcount == 1


async def try_charge(s: AsyncSession, order: Order) -> bool:
    """Debit the order price from the user's balance; on success the order becomes `paid`."""
    if order.status != "awaiting_payment":
        return False
    bal = (await s.execute(select(User.balance_micro).where(User.id == order.user_id))).scalar_one()
    amount = order.price_micro
    if bal < amount:
        if bal <= 0 or bal < amount - PAY_TOLERANCE_MICRO:
            return False
        amount = bal
    if not await move_status(s, order.id, ("awaiting_payment",), "paid"):
        return False  # another payment paid it a moment ago
    new_bal = await change_balance(s, order.user_id, -amount, "order", order_id=order.id, require_funds=True,
                                   comment=f"Steam {order.steam_login}: {fmt_amount(order.amount, order.currency)}")
    if new_bal is None:
        await s.execute(update(Order).where(Order.id == order.id).values(status="awaiting_payment")
                        .execution_options(synchronize_session=False))
        return False
    now = utcnow()
    order.charged_micro, order.status, order.paid_at, order.next_check_at = amount, "paid", now, now
    return True


async def refund(s: AsyncSession, order: Order, reason: str, status: str = "rejected") -> None:
    if order.promo_id:
        await promos.give_back(s, order.id)  # the top-up didn't happen: the client keeps their promo use
    if order.charged_micro > 0:
        await change_balance(s, order.user_id, order.charged_micro, "refund", order_id=order.id,
                             comment=f"Возврат по заказу {order.public_id}")
        order.charged_micro = 0
    order.status, order.error, order.finished_at, order.next_check_at = status, reason, utcnow(), None


# ---------------------------------------------------------------- supplier interaction

HISTORY_PAGE = 100
HISTORY_MAX_PAGES = 20
UNSURE = ("нет ответа", "перезапуск")  # errors after which the supplier may or may not have the order


async def history_depth(order_id: int, since) -> tuple[int, set[str]]:
    """How many pages of the supplier's history (newest first) can hide an order we submitted at `since`: everything
    we submitted later is newer there (+ a margin for orders placed by hand). Also the supplier ids already linked."""
    async with session_scope() as s:
        newer = (await s.execute(select(func.count()).select_from(Order).where(
            Order.id != order_id, Order.submitted_at >= (since or utcnow())))).scalar_one()
        linked = set((await s.execute(select(Order.nervixy_order_id).where(Order.nervixy_order_id.is_not(None)))).scalars())
    return min(HISTORY_MAX_PAGES, 1 + (newer + 50) // HISTORY_PAGE), linked


async def find_on_supplier(pid: str, *, pages: int, login: str | None = None, amount: Decimal | None = None,
                           cur: str | None = None, linked: set[str] | None = None) -> dict | None:
    """Our order in the supplier's history. Our public id travels as partner_id, so that hit is authoritative; with
    `login` given, an order stored without partner_id but with the same login, currency and amount (and not linked to
    another of our orders) counts too. Pages deep enough that a busy hour can't push the order out of sight."""
    fuzzy = None
    for page in range(pages):
        rows = (await nervixy.client.orders(limit=HISTORY_PAGE, offset=page * HISTORY_PAGE)).get("orders") or []
        for x in rows:
            if str(x.get("partner_id") or "") == pid:
                return x
            if fuzzy is None and login and not x.get("partner_id") and str(x.get("id")) not in (linked or ()):
                try:
                    if (str(x.get("steam_login", "")).lower() == login and str(x.get("currency", "")).upper() == cur
                            and D(x.get("original_amount")).quantize(Decimal("0.01")) == amount):
                        fuzzy = x
                except Exception:  # noqa: BLE001
                    pass
        if len(rows) < HISTORY_PAGE:
            break
    return fuzzy


async def submit(order_id: int) -> None:
    now = utcnow()
    async with session_scope() as s:
        prev = (await s.execute(select(Order.submitted_at, Order.error).where(Order.id == order_id))).first()
        res = await s.execute(update(Order).where(Order.id == order_id, Order.status.in_(("paid", "queued")))
                              .values(status="submitting", attempts=Order.attempts + 1, submitted_at=now)
                              .execution_options(synchronize_session=False))
        if res.rowcount != 1:
            return
        o = await s.get(Order, order_id)
        amount, currency, login, pid = o.amount, o.currency, o.steam_login, o.public_id
        was_queued = o.attempts > 1

    data, note = None, "из очереди" if was_queued else ""
    if prev and prev.error and prev.error.startswith(UNSURE):
        # «Отправить повторно» after a lost answer: the first attempt may have gone through after all. Look first —
        # sending blindly is how a customer's Steam gets topped up twice.
        pages, _ = await history_depth(order_id, prev.submitted_at)
        try:
            found = await find_on_supplier(pid, pages=pages)
        except (NervixyError, NervixyTransportError) as err:
            log.warning("submit %s: supplier history unavailable, retry later: %s", pid, err)
            async with session_scope() as s:
                if await move_status(s, order_id, ("submitting",), "queued"):
                    (await s.get(Order, order_id)).next_check_at = utcnow() + timedelta(seconds=60)
            return
        if found:
            data, note = {"order_id": found.get("id"), "paid": found.get("paid")}, "уже был у nervixy — повторно не отправлял"

    if data is None:
        try:
            data = await nervixy.client.order(amount=amount, currency=currency, steam_login=login, partner_id=pid)
        except NervixyTransportError as err:
            async with session_scope() as s:
                o = await s.get(Order, order_id)
                o.status, o.error, o.next_check_at = "uncertain", f"нет ответа: {err}", utcnow() + timedelta(seconds=45)
            await notify_admins(f"{e('warn')} Заказ <code>{pid}</code>: nervixy не ответил ({esc(err)}).\n"
                                f"{e('wait')} Сверяю по их истории заказов — повторно вслепую не отправляю.")
            return
        except NervixyError as err:
            await _on_submit_error(order_id, err)
            return

    async with session_scope() as s:
        o = await s.get(Order, order_id)
        o.status, o.error = "processing", None
        o.nervixy_order_id = str(data.get("order_id"))
        if data.get("paid") is not None:
            o.nervixy_paid_micro = to_micro(D(data["paid"]))
        nervixy.note_spent(from_micro(o.nervixy_paid_micro or o.cost_micro))
        o.next_check_at = utcnow() + timedelta(seconds=next_check_delay(0))
        admin_text = new_order_card(o, await s.get(User, o.user_id), note)
    await remember_card(order_id, await notify_admins(admin_text, admin_kb()))


def admin_kb():
    return kb([btn("Админ-панель", cb="a:home", style=PRIMARY, icon="admin")])


def took(seconds: float) -> str:
    sec = max(int(seconds), 1)
    return f"{sec} с" if sec < 90 else f"{sec // 60} мин {sec % 60} с"


def new_order_card(o: Order, user: User, note: str = "", state: str | None = None) -> str:
    """The admin's card for an order the supplier accepted: who, what, what the client paid, what nervixy took,
    margin — and a state line. It is sent once and then edited as the order finishes (no second message)."""
    margin = o.price_micro - (o.nervixy_paid_micro or o.cost_micro)
    return (f"{e('new')} <b>Новый заказ</b> · <code>{o.public_id}</code>{f' · {note}' if note else ''}\n"
            + panel(f"{e('user')} {esc(user.display_name)}",
                    f"{e('steam')} Steam <code>{esc(o.steam_login)}</code> · <b>{fmt_amount(o.amount, o.currency)}</b>",
                    f"{e('card')} Клиент заплатил: <b>${usd_str(o.price_micro)}</b> · {method_label(o.method)}",
                    f"{e('bank')} Nervixy списал: ${usd_str(o.nervixy_paid_micro or o.cost_micro)}",
                    f"{e('margin')} Маржа: <b>+${usd_str(margin)}</b>")
            + "\n" + (state or f"{e('wait')} Пополняется у nervixy…"))


async def remember_card(order_id: int, sent: list) -> None:
    if sent:
        async with session_scope() as s:
            (await s.get(Order, order_id)).admin_msgs = json.dumps(sent)


async def update_card(msgs: list, text: str) -> bool:
    """Edit the admin card in every admin chat; False if there was none or no edit went through."""
    results = [await notify_edit(chat, mid, text, admin_kb()) for chat, mid in msgs]
    return any(results)


async def _on_submit_error(order_id: int, err: NervixyError) -> None:
    requeue = err.insufficient_funds or err.rate_limited or err.status in (401, 403, 429)
    async with session_scope() as s:
        o = await s.get(Order, order_id)
        if requeue:
            first = o.status != "queued" and not o.admin_alerted
            o.status, o.error = "queued", err.message
            o.next_check_at = utcnow() + timedelta(seconds=30 if err.rate_limited else 60)
            o.admin_alerted = True
            pid = o.public_id
        else:
            await refund(s, o, err.message)
            await queue_webhook(s, o.user_id, "order.rejected", public_order(o))
            user_id, pid, login = o.user_id, o.public_id, o.steam_login
            amt, price = fmt_amount(o.amount, o.currency), usd_str(o.price_micro)
    if requeue:
        if err.insufficient_funds:
            await notify_admins(f"{e('queue')} <b>Nervixy: не хватает средств</b>\n"
                                + panel(f"Баланс ${err.data.get('balance')}, нужно ${err.data.get('required')}.",
                                        "Оплаченные заказы ждут в очереди и уйдут сами, как только пополнишь баланс."),
                                kb([btn("Nervixy", cb="a:nx", style=PRIMARY, icon="bank"),
                                    btn("Очередь", cb="a:work", icon="work")]), key="nx-funds", every=1800)
        elif first:
            await notify_admins(f"{e('queue')} Заказ <code>{pid}</code> в очереди: {esc(err.message)}", key=f"q-{pid}")
        return
    await notify_admins(f"{e('fail')} Заказ <code>{pid}</code> отклонён nervixy: {esc(err.message)}\n"
                        f"{e('refund')} ${price} возвращены клиенту на баланс.")
    await notify_user(user_id, f"{e('fail')} <b>Не удалось пополнить Steam</b>\n"
                               + panel(f"Аккаунт <code>{esc(login)}</code> · {amt}", f"Причина: {esc(err.message)}")
                               + f"\n{e('refund')} <b>${price}</b> вернулись на баланс сайта — можно оформить заказ заново.",
                      kb([btn("Оформить заново", url=site("/"), style=PRIMARY, icon="rocket")],
                         [btn("Поддержка", url=settings.support_url, icon="support") if settings.support_url else None]))


def next_check_delay(age: float) -> int:
    """Seconds until the next status check of an order that went to the supplier `age` seconds ago. A top-up usually
    lands within a minute, so look often at first (the customer is watching the page), then back off."""
    return 10 if age < 120 else 20 if age < 600 else 60


def retry_after(err: Exception) -> int:
    """After a failed check: wait out nervixy's rate-limit pause (their 60 s + margin), else retry soon."""
    return 70 if isinstance(err, NervixyError) and err.rate_limited else 30


async def check_status(order_id: int) -> None:
    async with session_scope() as s:
        o = await s.get(Order, order_id)
        if not o or o.status != "processing" or not o.nervixy_order_id:
            return
        nid = o.nervixy_order_id
    try:
        data = await nervixy.client.status(nid)
        st = str(data.get("status") or "").lower()
    except (NervixyError, NervixyTransportError) as err:
        log.warning("status %s failed: %s", nid, err)
        async with session_scope() as s:
            o = await s.get(Order, order_id)
            o.next_check_at = utcnow() + timedelta(seconds=retry_after(err))
        return
    await apply_status(order_id, st)


async def apply_status(order_id: int, st: str) -> None:
    notify: tuple | None = None
    async with session_scope() as s:
        o = await s.get(Order, order_id)
        if not o or o.status != "processing":
            return
        if st in ("delivered", "rejected") and not await move_status(s, order_id, ("processing",), st):
            return  # the webhook, the poller or the admin already settled it
        msgs = json.loads(o.admin_msgs or "[]")
        if st == "delivered":
            o.status, o.finished_at, o.next_check_at, o.error = "delivered", utcnow(), None, None
            await queue_webhook(s, o.user_id, "order.delivered", public_order(o))
            spent = (o.finished_at - (o.submitted_at or o.finished_at)).total_seconds()
            card = new_order_card(o, await s.get(User, o.user_id), state=f"✅ <b>Выполнено</b> за {took(spent)}")
            notify = ("ok", o.user_id, o.public_id, o.steam_login, fmt_amount(o.amount, o.currency),
                      usd_str(o.price_micro - (o.nervixy_paid_micro or o.cost_micro)), msgs, card)
        elif st == "rejected":
            await refund(s, o, "отклонено поставщиком")
            await queue_webhook(s, o.user_id, "order.rejected", public_order(o))
            card = new_order_card(o, await s.get(User, o.user_id),
                                  state=f"❌ <b>Отклонён поставщиком</b> · ↩️ ${usd_str(o.price_micro)} "
                                        "вернулись клиенту на баланс")
            notify = ("rej", o.user_id, o.public_id, o.steam_login, fmt_amount(o.amount, o.currency), usd_str(o.price_micro),
                      msgs, card)
        else:
            age = (utcnow() - (o.submitted_at or utcnow())).total_seconds()
            o.next_check_at = utcnow() + timedelta(seconds=next_check_delay(age))
            if age > 1800 and not o.admin_alerted:
                o.admin_alerted = True
                notify = ("slow", o.user_id, o.public_id, o.nervixy_order_id)
    if not notify:
        return
    if notify[0] == "ok":
        _, uid, pid, login, amt, margin, msgs, card = notify
        await notify_user(uid, f"{e('ok')} <b>Готово!</b>\n"
                               + panel(f"Steam <code>{esc(login)}</code> пополнен на <b>{amt}</b>.")
                               + f"\nПриятных покупок! {e('gift')}\n<i>Заказ {pid}</i>",
                          kb([btn("Пополнить ещё", url=site("/"), style=PRIMARY, icon="rocket")],
                             [btn("Мои заказы", cb="m:orders", icon="orders")]))
        # the admin's "Новый заказ" card turns into "Выполнено" — no second message with the same numbers
        if not await update_card(msgs, card):
            await notify_admins(f"{e('ok')} <code>{pid}</code> выполнен · {amt} · маржа <b>+${margin}</b>")
    elif notify[0] == "rej":
        _, uid, pid, login, amt, price, msgs, card = notify
        await notify_user(uid, f"{e('fail')} <b>Поставщик отклонил пополнение</b>\n"
                               + panel(f"Аккаунт <code>{esc(login)}</code> · {amt}")
                               + f"\n{e('refund')} <b>${price}</b> вернулись на баланс сайта — можно оформить заказ заново.",
                          kb([btn("Оформить заново", url=site("/"), style=PRIMARY, icon="rocket")],
                             [btn("Поддержка", url=settings.support_url, icon="support") if settings.support_url else None]))
        await update_card(msgs, card)  # the card shows the outcome; a rejection also deserves a ping of its own
        await notify_admins(f"{e('fail')} <code>{pid}</code> отклонён поставщиком · {e('refund')} ${price} вернулись клиенту на баланс")
    else:
        await notify_admins(f"{e('turtle')} Заказ <code>{notify[2]}</code> в обработке у nervixy больше 30 минут "
                            f"(id <code>{esc(notify[3])}</code>). Проверь в их панели.")


async def reconcile(order_id: int) -> None:
    """Supplier didn't answer our create call. Look for the order in their history before deciding."""
    async with session_scope() as s:
        o = await s.get(Order, order_id)
        if not o or o.status != "uncertain":
            return
        login, amount, cur, pid = o.steam_login.lower(), o.amount, o.currency, o.public_id
        since = (utcnow() - (o.submitted_at or utcnow())).total_seconds()
        submitted_at = o.submitted_at
    pages, linked = await history_depth(order_id, submitted_at)
    try:
        match = await find_on_supplier(pid, pages=pages, login=login, amount=amount, cur=cur, linked=linked)
    except (NervixyError, NervixyTransportError) as err:
        log.warning("reconcile %s: history unavailable: %s", pid, err)
        async with session_scope() as s:
            (await s.get(Order, order_id)).next_check_at = utcnow() + timedelta(seconds=60)
        return
    card, alert = None, False
    async with session_scope() as s:
        o = await s.get(Order, order_id)
        if match:
            if not await move_status(s, order_id, ("uncertain",), "processing"):
                return  # the admin decided while we were asking the supplier
            o.status, o.nervixy_order_id, o.error = "processing", str(match["id"]), None
            if match.get("paid") is not None:
                o.nervixy_paid_micro = to_micro(D(match["paid"]))
            nervixy.note_spent(from_micro(o.nervixy_paid_micro or o.cost_micro))
            o.next_check_at = utcnow()
            log.info("reconciled %s -> nervixy %s", pid, match["id"])
            card = new_order_card(o, await s.get(User, o.user_id), "найден у nervixy после сбоя связи")
        else:
            o.next_check_at = utcnow() + timedelta(seconds=90)
            alert = since > 300 and not o.admin_alerted
            if alert:
                o.admin_alerted = True
    if card:
        await remember_card(order_id, await notify_admins(card, admin_kb()))
    if alert:
        await notify_admins(
            f"{e('alarm')} <b>Заказ <code>{pid}</code> завис</b>\n"
            + panel("Nervixy не ответил на создание, и в их истории этого заказа нет уже 5+ минут.")
            + "\nПроверь панель nervixy и выбери действие:",
            kb([btn("Отправить повторно", cb=f"ad:retry:{order_id}", style=PRIMARY, icon="retry")],
               [btn("Вернуть на баланс", cb=f"ad:refund:{order_id}", style=DANGER, icon="refund")]))


# ---------------------------------------------------------------- deposits & expiry

async def after_deposit(s: AsyncSession, inv: Invoice | None, credited_micro: int, tx_time) -> dict:
    """Called in the same transaction right after a deposit was credited. Decides what happens to the order."""
    result: dict = {"paid_order": None, "remainder_invoice": None, "late": False}
    if not inv or not inv.order_id:
        return result
    order = await s.get(Order, inv.order_id)
    if not order or order.status != "awaiting_payment":
        result["late"] = order is not None and order.status in ("expired", "cancelled")
        return result
    if tx_time and tx_time > inv.expires_at + LATE_GRACE:
        order.status = "expired"
        result["late"] = True
        return result
    if await try_charge(s, order):
        result["paid_order"] = order.id
        return result
    # partial payment: ask for the rest with a fresh unique amount on the same network
    bal = (await s.execute(select(User.balance_micro).where(User.id == order.user_id))).scalar_one()
    missing = order.price_micro - max(bal, 0)
    m = get_method(inv.method)
    if m and m.enabled and missing > 0:
        try:
            for old in (await s.execute(select(Invoice).where(Invoice.order_id == order.id,
                                                              Invoice.status == "pending"))).scalars():
                old.status = "cancelled"
            result["remainder_invoice"] = await create_invoice(s, m=m, user_id=order.user_id, order_id=order.id,
                                                               usd_micro=missing, comment=order.public_id)
        except PaymentUnavailable:
            pass
    result["missing"] = missing
    return result


async def expire_stale() -> None:
    now = utcnow()
    # two set-based statements: the write lock is held for milliseconds however many orders wait, and an order
    # paid in the meantime is never overwritten (the condition is re-checked by the UPDATE itself)
    open_invoice = select(Invoice.id).where(Invoice.order_id == Order.id,
                                            Invoice.status.in_(("pending", "detected"))).exists()
    async with session_scope() as s:
        await s.execute(update(Invoice).where(Invoice.status == "pending", Invoice.expires_at < now - LATE_GRACE)
                        .values(status="expired").execution_options(synchronize_session=False))
        expired = (await s.execute(update(Order).where(Order.status == "awaiting_payment", ~open_invoice)
                                   .values(status="expired", finished_at=now).returning(Order.id, Order.promo_id)
                                   .execution_options(synchronize_session=False))).all()
        for oid, promo_id in expired:
            if promo_id:
                await promos.give_back(s, oid)  # never paid: the promo use goes back to the client


async def recover_after_restart() -> None:
    """A crash between 'submitting' and the supplier's answer leaves the outcome unknown."""
    async with session_scope() as s:
        await s.execute(update(Order).where(Order.status == "submitting")
                        .values(status="uncertain", next_check_at=utcnow(), error="перезапуск во время отправки")
                        .execution_options(synchronize_session=False))


async def process_once() -> None:
    now = utcnow()
    async with session_scope() as s:
        due = (await s.execute(select(Order.id, Order.status).where(or_(
            Order.status == "paid",
            and_(Order.status.in_(("queued", "processing", "uncertain")), Order.next_check_at <= now),
        )).order_by(Order.id).limit(25))).all()
    processing = [oid for oid, st in due if st == "processing"]
    if processing:
        try:
            await check_statuses(processing)
        except Exception:
            log.exception("status check failed")
    for oid, st in due:
        try:
            if st in ("paid", "queued"):
                await submit(oid)
            elif st == "uncertain":
                await reconcile(oid)
        except Exception:
            log.exception("order %s step failed", oid)
    await expire_stale()


BATCH_FROM = 4  # more orders in work than this: one api_orders call for all of them


async def check_statuses(order_ids: list[int]) -> None:
    """A few orders in work: ask about each one (api_status) — nervixy's order-history call is rate-limited much
    harder, and a 429 there froze status updates for minutes while the customer's Steam was already topped up.
    Many orders in work: one api_orders call covers them all; ones missing from it fall back to api_status."""
    async with session_scope() as s:
        rows = (await s.execute(select(Order.id, Order.nervixy_order_id).where(Order.id.in_(order_ids)))).all()
    ids = {nid: oid for oid, nid in rows if nid}
    if len(ids) <= BATCH_FROM:
        for oid in ids.values():
            await check_status(oid)
        return
    try:
        data = await nervixy.client.orders(limit=100)
        found = {str(x.get("id")): str(x.get("status") or "").lower() for x in data.get("orders") or []}
    except (NervixyError, NervixyTransportError) as err:
        log.warning("status batch failed: %s", err)
        async with session_scope() as s:
            for oid in order_ids:
                (await s.get(Order, oid)).next_check_at = utcnow() + timedelta(seconds=retry_after(err))
        return
    for nid, oid in ids.items():
        if nid in found:
            await apply_status(oid, found[nid])
        else:
            await check_status(oid)


_nx_fail_since: float | None = None


async def watch_supplier_balance() -> None:
    global _nx_fail_since
    try:
        acc = await nervixy.account(max_age=280)
        _nx_fail_since = None
    except (NervixyError, NervixyTransportError) as err:
        # a short outage or a rate-limit pause is normal; only a long one is worth waking the admin
        _nx_fail_since = _nx_fail_since or time.time()
        if time.time() - _nx_fail_since > 600:
            await notify_admins(f"{e('net')} <b>Nervixy API недоступен</b> уже {int((time.time() - _nx_fail_since) // 60)} мин\n"
                                f"<i>{esc(err)}</i>", key="nx-down", every=1800)
        return
    if acc["balance"] < settings.nervixy_low_balance_usd:
        await notify_admins(f"{e('low')} <b>Баланс nervixy: ${acc['balance']}</b>\n"
                            f"Ниже порога ${settings.nervixy_low_balance_usd} — пора пополнить (USDT внутренним переводом Bybit).",
                            kb([btn("Nervixy", cb="a:nx", style=PRIMARY, icon="bank")]), key="nx-low", every=6 * 3600)


async def run_processor() -> None:
    from .webhooks import deliver_due

    from . import health

    await recover_after_restart()
    tick = 0
    while True:
        health.beat("orders")
        await process_once()
        try:
            await deliver_due()
        except Exception:
            log.exception("webhook delivery failed")
        if tick % 20 == 0:  # ~once a minute; the account itself is re-read from nervixy at most every ~5 min
            await watch_supplier_balance()
        tick += 1
        await asyncio.sleep(3)


async def admin_retry(order_id: int) -> str:
    async with session_scope() as s:
        o = await s.get(Order, order_id)
        if not o or not await move_status(s, order_id, ("uncertain", "queued"), "queued"):
            return "Заказ уже не в ожидании решения"
        o.status, o.next_check_at, o.admin_alerted = "queued", utcnow(), False
    return "Отправлю повторно в течение пары секунд"


async def admin_refund(order_id: int) -> str:
    async with session_scope() as s:
        o = await s.get(Order, order_id)
        if not o or not await move_status(s, order_id, ("uncertain", "queued", "processing"), "rejected"):
            return "Заказ уже не в ожидании решения"
        await refund(s, o, "возврат администратором", status="rejected")
        await queue_webhook(s, o.user_id, "order.rejected", public_order(o))
        uid, amt, login, price = o.user_id, fmt_amount(o.amount, o.currency), o.steam_login, usd_str(o.price_micro)
    await notify_user(uid, f"{e('refund')} <b>Заказ отменён</b>\n"
                           + panel(f"Пополнение Steam <code>{esc(login)}</code> ({amt}) не выполнено.",
                                   f"<b>${price}</b> вернулись на баланс сайта."),
                      kb([btn("Оформить заново", url=site("/"), style=PRIMARY, icon="rocket")],
                         [btn("Поддержка", url=settings.support_url, icon="support") if settings.support_url else None]))
    return "Возвращено на баланс клиента"


async def order_transfers(s: AsyncSession, order_id: int) -> list[Transfer]:
    return list((await s.execute(select(Transfer).join(Invoice, Transfer.invoice_id == Invoice.id)
                                 .where(Invoice.order_id == order_id).order_by(Transfer.id))).scalars())
