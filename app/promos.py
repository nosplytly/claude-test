"""Promo codes.

discount: +value % on top of the client's discount for one order. The total never goes past the supplier's
          discount minus MIN_MARGIN — a promo can shrink the margin, never turn an order into a loss.
bonus:    +value USD to the site balance, credited at once (ledger kind "promo").

Every use is taken atomically (the used-counter moves in the same statement that checks the limit), so a code
with 10 uses can't be used 11 times even by 100 people at once, and "once per client" holds the same way.
A discount use is given back if its order ends unpaid (expired / cancelled) or the supplier rejects it.
"""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .i18n import Problem, t
from .money import D, plain
from .models import Promo, PromoUse
from .utils import utcnow

MIN_MARGIN = Decimal("0.1")  # % of the nominal that always stays ours, whatever a promo says
CODE_RE = re.compile(r"^[A-Z0-9_-]{3,32}$")


class PromoError(Problem):
    pass


def normalize(code: str | None) -> str:
    return (code or "").strip().upper()


def combined(client_pct: Decimal, supplier_pct: Decimal, promo_pct: Decimal | None) -> Decimal:
    """Client discount with a promo on top, capped so the order still earns at least MIN_MARGIN."""
    if not promo_pct:
        return client_pct
    return max(client_pct, min(client_pct + promo_pct, supplier_pct - MIN_MARGIN))


def label(p: Promo) -> str:
    return t("promo.label.discount" if p.kind == "discount" else "promo.label.bonus", value=plain(p.value))


async def find(s: AsyncSession, code: str) -> Promo:
    """The code if it can be used right now (not whether this client already did), else PromoError."""
    code = normalize(code)
    if not CODE_RE.match(code):
        raise PromoError("promo.not_found")
    p = (await s.execute(select(Promo).where(Promo.code == code))).scalar_one_or_none()
    if not p or not p.active:
        raise PromoError("promo.not_found")
    if p.expires_at and p.expires_at <= utcnow():
        raise PromoError("promo.expired")
    if p.max_uses is not None and p.used >= p.max_uses:
        raise PromoError("promo.used_up")
    return p


async def uses_by(s: AsyncSession, promo_id: int, user_id: int) -> int:
    return (await s.execute(select(func.count()).select_from(PromoUse).where(
        PromoUse.promo_id == promo_id, PromoUse.user_id == user_id))).scalar_one()


async def take(s: AsyncSession, p: Promo, user_id: int, order_id: int | None = None) -> None:
    """Count one use, atomically. Raises PromoError (the caller's transaction then rolls the counter back)."""
    now = utcnow()
    res = await s.execute(update(Promo).where(
        Promo.id == p.id, Promo.active.is_(True),
        or_(Promo.max_uses.is_(None), Promo.used < Promo.max_uses),
        or_(Promo.expires_at.is_(None), Promo.expires_at > now),
    ).values(used=Promo.used + 1).execution_options(synchronize_session=False))
    if res.rowcount != 1:
        raise PromoError("promo.used_up")
    # the UPDATE above holds the database's write turn, so this count can't race another use by the same client
    if await uses_by(s, p.id, user_id) >= p.per_user:
        raise PromoError("promo.already_used")
    s.add(PromoUse(promo_id=p.id, user_id=user_id, order_id=order_id))
    await s.flush()


async def give_back(s: AsyncSession, order_id: int) -> None:
    """The order this use was taken for ended without being fulfilled: the client may use the code again."""
    rows = (await s.execute(select(PromoUse.id, PromoUse.promo_id).where(PromoUse.order_id == order_id))).all()
    for use_id, promo_id in rows:
        await s.execute(delete(PromoUse).where(PromoUse.id == use_id))
        await s.execute(update(Promo).where(Promo.id == promo_id, Promo.used > 0).values(used=Promo.used - 1)
                        .execution_options(synchronize_session=False))


async def redeem_bonus(s: AsyncSession, user_id: int, code: str) -> tuple[Promo, int]:
    """Credit a bonus code to the balance. Returns (promo, new balance in micro-USD)."""
    from .ledger import change_balance
    from .money import to_micro

    p = await find(s, code)
    if p.kind != "bonus":
        raise PromoError("promo.discount_on_site")
    await take(s, p, user_id)
    new = await change_balance(s, user_id, to_micro(p.value), "promo", comment=p.code)  # the kind says "promo code"
    return p, new


def parse_new(args: list[str]) -> dict:
    """/promo new CODE 0.5% [uses] [7d]   or   /promo new CODE $2 [uses] [30d]"""
    if len(args) < 2:
        raise PromoError("promo.admin_format")
    code, val = normalize(args[0]), args[1].replace(",", ".")
    if not CODE_RE.match(code):
        raise PromoError("promo.admin_code")
    try:
        if val.endswith("%"):
            kind, value = "discount", D(val[:-1])
        elif val.startswith("$"):
            kind, value = "bonus", D(val[1:])
        else:
            raise PromoError("promo.admin_size")
    except (ArithmeticError, ValueError) as err:
        raise PromoError("promo.admin_size_number") from err
    if value <= 0 or (kind == "discount" and value > 50) or (kind == "bonus" and value > 1000):
        raise PromoError("promo.admin_size_range")
    uses, days = None, None
    for extra in args[2:]:
        if extra.endswith("d") and extra[:-1].isdigit():
            days = int(extra[:-1])
        elif extra.isdigit():
            uses = int(extra)
        else:
            raise PromoError("promo.admin_extra", part=extra)
    return {"code": code, "kind": kind, "value": value, "max_uses": uses,
            "expires_at": utcnow() + timedelta(days=days) if days else None}
