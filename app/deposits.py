"""Top up the site balance directly (no Steam order attached)."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import Invoice, Transfer, User
from .money import D, plain, to_micro, usd_str
from .orders import OrderError
from .payments.chains.tonutil import to_friendly
from .payments.invoices import PaymentUnavailable, create_invoice
from .payments.methods import METHODS, get_method
from .utils import iso, public_id

STAGE = {"pending": "pay", "detected": "confirming", "paid": "done", "partial": "done", "expired": "expired",
         "cancelled": "cancelled"}


async def create_deposit(s: AsyncSession, user: User, *, amount_usd, method_code: str) -> Invoice:
    try:
        usd = D(amount_usd).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as e:
        raise OrderError("Некорректная сумма") from e
    if user.is_banned:
        raise OrderError("Аккаунт заблокирован")
    if usd < settings.min_deposit_usd or usd > settings.max_deposit_usd:
        raise OrderError(f"Сумма пополнения от ${settings.min_deposit_usd:.2f} до ${settings.max_deposit_usd:.2f}")
    m = get_method(method_code or "")
    if not m or not m.enabled:
        raise OrderError("Выберите способ оплаты")
    open_ = (await s.execute(select(func.count()).select_from(Invoice).where(
        Invoice.user_id == user.id, Invoice.order_id.is_(None), Invoice.status == "pending"))).scalar_one()
    if open_ >= 3:
        raise OrderError("У вас уже есть 3 неоплаченных счёта на пополнение — оплатите или дождитесь их истечения")
    pid = public_id("DP")
    try:
        inv = await create_invoice(s, m=m, user_id=user.id, order_id=None, usd_micro=to_micro(usd), comment=pid)
    except PaymentUnavailable as e:
        raise OrderError(f"Оплата в {m.title} временно недоступна, выберите другую монету") from e
    inv.public_id = pid
    return inv


def display_address(m, addr: str) -> str:
    if m.chain == "ton":
        try:
            return to_friendly(addr, bounceable=False)
        except ValueError:
            return addr
    return addr


async def deposit_view(s: AsyncSession, inv: Invoice) -> dict:
    m = METHODS[inv.method]
    trs = (await s.execute(select(Transfer).where(Transfer.invoice_id == inv.id).order_by(Transfer.id))).scalars().all()
    stage = STAGE.get(inv.status, "pay")
    if inv.status == "expired" and trs:
        stage = "done" if any(t.status == "credited" for t in trs) else "confirming"
    payment = None
    if trs:
        t = trs[-1]
        payment = {"amount": plain(t.amount), "coin": m.coin, "confirmations": t.confirmations,
                   "required": m.confirmations, "final": t.final, "tx_url": m.tx_url(t.txid),
                   "credited": t.status == "credited"}
    return {"id": inv.public_id, "status": inv.status, "stage": stage, "usd": usd_str(inv.usd_micro),
            "received_usd": usd_str(inv.received_micro), "method": m.code, "coin": m.coin, "network": m.network,
            "network_title": m.network_title, "address": display_address(m, inv.address), "amount": plain(inv.amount),
            "comment": inv.comment, "expires_at": iso(inv.expires_at), "created_at": iso(inv.created_at),
            "paid_at": iso(inv.paid_at), "invoice_id": inv.id, "payment": payment,
            "qr": f"/api/invoices/{inv.id}/qr.svg?u={inv.units}"}
