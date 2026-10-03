"""Invoices with unique amounts: the exact amount received identifies the payer."""
from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import serialize
from ..models import Invoice
from ..money import amount_to_units, ceil_to, from_micro, to_micro, units_to_amount
from ..prices import price_feed
from ..utils import utcnow
from .methods import Method

RESERVE = timedelta(hours=24)  # a unique amount is not reused for this long after the invoice expires
# Amounts handed out but possibly not committed yet. Two invoices created at the same moment can't see each other
# in the DB until commit; this in-process set closes that gap (the app is a single process by design).
_handed_out: dict[tuple[str, str], float] = {}
HANDED_OUT_TTL = 600  # long after any commit: by then the DB's reserved_until does the job


class PaymentUnavailable(Exception):
    pass


def effective_price(m: Method) -> Decimal | None:
    """USD value we credit per 1 coin. Volatile coins carry a markup, so the payer sends a bit more."""
    p = price_feed.price(m.coin)
    if p is None:
        return None
    if m.stable:
        return p
    return p / (1 + settings.volatile_markup / 100)


def coin_amount_estimate(m: Method, usd_micro: int) -> Decimal | None:
    eff = effective_price(m)
    if not eff:
        return None
    return ceil_to(from_micro(usd_micro) / eff, m.step)


async def create_invoice(s: AsyncSession, *, m: Method, user_id: int, order_id: int | None, usd_micro: int,
                         comment: str | None) -> Invoice:
    eff = effective_price(m)
    if not eff:
        raise PaymentUnavailable(f"нет актуального курса {m.coin}")
    # the network can't carry less than this; whatever exceeds the order price stays on the user's balance
    usd_micro = max(usd_micro, to_micro(m.min_usd))
    now = utcnow()
    base_units = amount_to_units(ceil_to(from_micro(usd_micro) / eff, m.step), m.decimals)
    step_units = amount_to_units(m.step, m.decimals)
    await serialize(s, f"invoice-amount:{m.code}")  # another process picking for the same coin waits for our commit
    taken = set((await s.execute(
        select(Invoice.units).where(Invoice.method == m.code, Invoice.reserved_until > now))).scalars())
    # no await from here to the reservation below: the pick and the reservation are atomic for the event loop
    clock = time.monotonic()
    for key in [k for k, t in _handed_out.items() if clock - t > HANDED_OUT_TTL]:
        del _handed_out[key]
    for k in range(1, 20000):
        units = base_units + k * step_units
        if str(units) not in taken and (m.code, str(units)) not in _handed_out:
            break
    else:
        raise PaymentUnavailable("нет свободной уникальной суммы")
    _handed_out[(m.code, str(units))] = clock
    expires = now + timedelta(minutes=settings.invoice_ttl_min)
    inv = Invoice(order_id=order_id, user_id=user_id, method=m.code, address=m.address,
                  amount=units_to_amount(units, m.decimals), units=str(units), coin_price=eff, usd_micro=usd_micro,
                  comment=comment if m.uses_comment else None, status="pending", expires_at=expires,
                  reserved_until=expires + RESERVE)
    s.add(inv)
    await s.flush()
    return inv
