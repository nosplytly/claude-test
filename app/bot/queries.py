"""Database reads behind the bot screens. Writes stay where the money logic is (ledger, orders, monitor)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Claim, LedgerEntry, Order, Transfer, User

IN_PROGRESS = ("paid", "submitting", "processing")
NEED_ATTENTION = ("queued", "uncertain")


class Totals(NamedTuple):
    """Delivered orders in a period, amounts in micro-USD."""
    orders: int
    turnover: int
    margin: int
    deposits: int


async def user_by_tg(s: AsyncSession, tg_id: int) -> User | None:
    return (await s.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()


async def find_user(s: AsyncSession, who: str) -> User | None:
    """`who` is a Telegram ID or @username."""
    who = who.lstrip("@")
    if re.fullmatch(r"-?\d+", who):
        return await user_by_tg(s, int(who))
    return (await s.execute(select(User).where(func.lower(User.username) == who.lower()))).scalar_one_or_none()


async def recent_ledger(s: AsyncSession, user_id: int, limit: int = 6) -> list[LedgerEntry]:
    return list((await s.execute(select(LedgerEntry).where(LedgerEntry.user_id == user_id)
                                 .order_by(LedgerEntry.id.desc()).limit(limit))).scalars())


async def recent_orders(s: AsyncSession, user_id: int, limit: int = 8) -> list[Order]:
    return list((await s.execute(select(Order).where(Order.user_id == user_id)
                                 .order_by(Order.id.desc()).limit(limit))).scalars())


async def totals(s: AsyncSession, since: datetime | None) -> Totals:
    q = select(func.count(), func.coalesce(func.sum(Order.price_micro), 0),
               func.coalesce(func.sum(func.coalesce(Order.nervixy_paid_micro, Order.cost_micro)), 0)
               ).where(Order.status == "delivered")
    if since:
        q = q.where(Order.finished_at >= since)
    n, turnover, cost = (await s.execute(q)).one()
    dq = select(func.coalesce(func.sum(LedgerEntry.delta_micro), 0)).where(LedgerEntry.kind == "deposit")
    if since:
        dq = dq.where(LedgerEntry.created_at >= since)
    return Totals(n, turnover, turnover - cost, (await s.execute(dq)).scalar_one())


async def status_counts(s: AsyncSession, statuses: tuple[str, ...]) -> dict[str, int]:
    rows = await s.execute(select(Order.status, func.count()).where(Order.status.in_(statuses)).group_by(Order.status))
    return dict(rows.all())


async def orders_with_status(s: AsyncSession, statuses: tuple[str, ...], limit: int = 10) -> list[Order]:
    return list((await s.execute(select(Order).where(Order.status.in_(statuses)).order_by(Order.id).limit(limit))).scalars())


async def pending_claims(s: AsyncSession) -> int:
    return (await s.execute(select(func.count()).select_from(Claim).where(Claim.status == "pending"))).scalar_one()


async def unmatched_transfers(s: AsyncSession, since: datetime) -> int:
    return (await s.execute(select(func.count()).select_from(Transfer).where(
        Transfer.status == "unmatched", Transfer.created_at >= since))).scalar_one()


async def last_orders(s: AsyncSession, limit: int = 10) -> list[tuple[Order, User]]:
    return list((await s.execute(select(Order, User).join(User, Order.user_id == User.id)
                                 .order_by(Order.id.desc()).limit(limit))).tuples())


async def clients(s: AsyncSession) -> tuple[int, int]:
    """Number of users and the money on their balances (micro-USD)."""
    users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
    balances = (await s.execute(select(func.coalesce(func.sum(User.balance_micro), 0)))).scalar_one()
    return users, balances
