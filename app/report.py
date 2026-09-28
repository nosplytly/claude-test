"""Morning report to the admins: yesterday (Moscow time) in numbers + what needs attention now.

Sent once a day at REPORT_HOUR_MSK (default 9); the date of the last report is kept in the database, so restarts
don't send it twice. /report in the bot shows it on demand (today so far).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select

from . import fail2ban as f2b
from .config import settings
from .db import session_scope
from .models import Claim, LedgerEntry, Order, Transfer, User
from .money import usd_str
from .utils import utcnow

log = logging.getLogger("sh.report")
MSK = timedelta(hours=3)


async def report_text(since: datetime, until: datetime, label: str) -> str:
    from .nervixy import nervixy
    from .tgui import e, panel, title

    async with session_scope() as s:
        in_range = (Order.finished_at >= since, Order.finished_at < until)
        done = (await s.execute(select(func.count(), func.coalesce(func.sum(Order.price_micro), 0),
                                       func.coalesce(func.sum(Order.price_micro - func.coalesce(
                                           Order.nervixy_paid_micro, Order.cost_micro)), 0))
                                .where(Order.status == "delivered", *in_range))).one()
        rejected = (await s.execute(select(func.count()).select_from(Order)
                                    .where(Order.status == "rejected", *in_range))).scalar_one()
        dep_n, dep_sum = (await s.execute(select(func.count(), func.coalesce(func.sum(LedgerEntry.delta_micro), 0))
                                          .where(LedgerEntry.kind == "deposit", LedgerEntry.created_at >= since,
                                                 LedgerEntry.created_at < until))).one()
        new_users = (await s.execute(select(func.count()).select_from(User)
                                     .where(User.created_at >= since, User.created_at < until))).scalar_one()
        owed = (await s.execute(select(func.coalesce(func.sum(User.balance_micro), 0)))).scalar_one()
        stuck = (await s.execute(select(func.count()).select_from(Order)
                                 .where(Order.status.in_(("queued", "uncertain"))))).scalar_one()
        claims = (await s.execute(select(func.count()).select_from(Claim).where(Claim.status == "pending"))).scalar_one()
        unmatched = (await s.execute(select(func.count()).select_from(Transfer).where(
            Transfer.status == "unmatched", Transfer.created_at >= since))).scalar_one()
    try:
        acc = await nervixy.account(max_age=600)
        nx = f"<b>${acc['balance']}</b>" + (f" {e('low')} ниже порога ${settings.nervixy_low_balance_usd}"
                                            if acc["balance"] < settings.nervixy_low_balance_usd else "")
    except Exception:  # noqa: BLE001
        nx = "нет связи"
    n, turnover, margin = done
    attention = stuck + claims + unmatched
    return "\n".join([
        title("stats", "Отчёт", label),
        panel(f"{e('orders')} Выполнено заказов: <b>{n}</b>" + (f" · отклонено: {rejected}" if rejected else ""),
              f"{e('card')} Оборот: ${usd_str(turnover)} · {e('margin')} маржа <b>+${usd_str(margin)}</b>",
              f"{e('money')} Пополнений баланса: {dep_n} на ${usd_str(dep_sum)}",
              f"{e('user')} Новых клиентов: {new_users}"),
        panel(f"{e('bank')} Nervixy: {nx}",
              f"{e('wallet')} На балансах клиентов: ${usd_str(owed)}",
              f"{e('lock')} Забанено IP: {len(f2b.active())}",
              (f"{e('warn')} Требуют внимания: <b>{attention}</b> (очередь/зависшие {stuck}, заявки {claims}, "
               f"неопознанные платежи {unmatched})") if attention else f"{e('ok')} Проблем нет"),
    ])


def yesterday_msk(now_utc: datetime) -> tuple[datetime, datetime, str]:
    day = (now_utc + MSK).date() - timedelta(days=1)
    start = datetime(day.year, day.month, day.day) - MSK
    return start, start + timedelta(days=1), f"за {day:%d.%m}"


async def maybe_send(now: datetime) -> bool:
    """Send yesterday's report if it's past the report hour and today's report hasn't gone out yet."""
    from .kv import kv_get, kv_set
    from .notify import notify_admins

    msk = now + MSK
    key = f"{msk:%Y-%m-%d}"
    if not 0 <= settings.report_hour_msk <= 23 or msk.hour < settings.report_hour_msk \
            or await kv_get("report:last") == key:
        return False
    since, until, label = yesterday_msk(now)
    await notify_admins(await report_text(since, until, label))
    await kv_set("report:last", key)
    return True


async def run_reports() -> None:
    while True:
        try:
            await maybe_send(utcnow())
        except Exception:
            log.exception("daily report failed")
        await asyncio.sleep(300)
