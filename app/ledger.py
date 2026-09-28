"""User balances. Every change goes through change_balance(): atomic SQL update + ledger row."""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import LedgerEntry, User


async def change_balance(s: AsyncSession, user_id: int, delta_micro: int, kind: str, *, order_id: int | None = None,
                         transfer_id: int | None = None, comment: str | None = None,
                         require_funds: bool = False) -> int | None:
    """Add delta (may be negative). With require_funds, refuses (returns None) instead of going below zero."""
    stmt = update(User).where(User.id == user_id)
    if delta_micro < 0 and require_funds:
        stmt = stmt.where(User.balance_micro >= -delta_micro)
    stmt = stmt.values(balance_micro=User.balance_micro + delta_micro).execution_options(synchronize_session=False)
    res = await s.execute(stmt)
    if res.rowcount != 1:
        return None
    bal = (await s.execute(select(User.balance_micro).where(User.id == user_id))).scalar_one()
    s.add(LedgerEntry(user_id=user_id, delta_micro=delta_micro, balance_after_micro=bal, kind=kind,
                      order_id=order_id, transfer_id=transfer_id, comment=comment))
    # keep an already-loaded User object in this session consistent
    for obj in s.identity_map.values():
        if isinstance(obj, User) and obj.id == user_id:
            obj.balance_micro = bal
    return bal
