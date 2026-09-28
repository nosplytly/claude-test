"""Waiting list for things that aren't out yet — the FunPay / Playerok plugin. The "Узнать о запуске" button on the
site opens the bot with /start plugin; the bot puts the person on the list. On launch the admin sends
/waitlist send <text>, and everyone on the list gets it once."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import notify
from .db import session_scope
from .models import Waitlist
from .utils import utcnow

log = logging.getLogger("sh.waitlist")

PLUGIN = "plugin"
SEND_PAUSE = 0.05  # 20 messages a second: under Telegram's limit for a bot writing to different chats


async def join(tg_id: int, username: str | None, topic: str = PLUGIN) -> bool:
    """Put someone on the list; False if they were already on it."""
    try:
        async with session_scope() as s:
            if (await s.execute(select(Waitlist.id).where(Waitlist.topic == topic, Waitlist.tg_id == tg_id))).first():
                return False
            s.add(Waitlist(topic=topic, tg_id=tg_id, username=username))
        return True
    except IntegrityError:  # two taps at once: the unique index let one through
        return False


async def has(tg_id: int, topic: str = PLUGIN) -> bool:
    async with session_scope() as s:
        return (await s.execute(select(Waitlist.id).where(Waitlist.topic == topic, Waitlist.tg_id == tg_id))).first() is not None


async def stats(topic: str = PLUGIN) -> tuple[int, int, list[Waitlist]]:
    """(on the list, not told yet, the latest few)."""
    async with session_scope() as s:
        total = (await s.execute(select(func.count()).select_from(Waitlist).where(Waitlist.topic == topic))).scalar_one()
        waiting = (await s.execute(select(func.count()).select_from(Waitlist).where(
            Waitlist.topic == topic, Waitlist.notified_at.is_(None)))).scalar_one()
        last = (await s.execute(select(Waitlist).where(Waitlist.topic == topic)
                                .order_by(Waitlist.id.desc()).limit(10))).scalars().all()
    return total, waiting, list(last)


async def broadcast(text: str, buttons=None, topic: str = PLUGIN) -> tuple[int, int]:
    """Tell everyone on the list who hasn't been told yet; returns (delivered, tried). Each person is marked right
    after their message, so a repeated /waitlist send (after a crash, or a second announcement) skips them."""
    async with session_scope() as s:
        rows = (await s.execute(select(Waitlist.id, Waitlist.tg_id).where(
            Waitlist.topic == topic, Waitlist.notified_at.is_(None)).order_by(Waitlist.id))).all()
    sent = 0
    for wid, tg_id in rows:
        if await notify.send(tg_id, text, buttons):
            sent += 1
        async with session_scope() as s:  # blocked the bot or not: don't try the same person again and again
            w = await s.get(Waitlist, wid)
            w.notified_at = utcnow()
        await asyncio.sleep(SEND_PAUSE)
    log.info("waitlist %s: told %d of %d", topic, sent, len(rows))
    return sent, len(rows)
