"""Telegram news about money, sent by its own worker from the `outbox` table.

    async with session_scope() as s:
        ... credit the payment ...
        await outbox.to_user(s, user_id, text, buttons)   # same transaction: both happen, or neither

Why not send right away: the payment workers used to wait for Telegram after every credit, so a slow or
rate-limited Telegram held up everyone's money, and a crash between the credit and the send lost the message.
Now the credit commits with its message; this worker sends it — in order within each chat, several chats at once,
retrying with a growing pause (Telegram's own "retry after N s" wins), giving up only when retrying can't help
(the user blocked the bot) or after about a day.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import timedelta

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import events, notify
from .config import settings
from .db import session_scope
from .models import OutboxMessage, User
from .utils import utcnow

log = logging.getLogger("sh.outbox")

BACKOFF = [5, 20, 60, 300, 900, 3600, 3 * 3600, 6 * 3600, 12 * 3600]  # then it's given up (~23 h in total)
PARALLEL_CHATS = 8  # Telegram allows ~30 messages a second across chats; one chat gets its messages one by one
BATCH = 200
IDLE = 5.0  # seconds between passes when nothing wakes the worker


def _dump(buttons) -> str | None:
    markup = notify._markup(buttons)
    return markup.model_dump_json(exclude_none=True) if markup else None


def _load(raw: str | None) -> InlineKeyboardMarkup | None:
    return InlineKeyboardMarkup.model_validate_json(raw) if raw else None


async def to_user(s: AsyncSession, user_id: int, text: str, buttons=None) -> None:
    """Queue a message to a customer (their chat is looked up when it's sent)."""
    s.add(OutboxMessage(user_id=user_id, text=text, markup=_dump(buttons)))
    events.kick_after_commit(s, events.OUTBOX)


async def to_admins(s: AsyncSession, text: str, buttons=None) -> None:
    """Queue a message to every admin."""
    if not settings.admin_ids:
        log.warning("[admin] %s", text.replace("\n", " | ")[:300])
        return
    raw = _dump(buttons)
    for chat in settings.admin_ids:
        s.add(OutboxMessage(chat_id=chat, text=text, markup=raw))
    events.kick_after_commit(s, events.OUTBOX)


async def send_due() -> int:
    """One pass: everything that's due, in order per chat. Returns how many went out."""
    now = utcnow()
    async with session_scope() as s:
        rows = (await s.execute(select(OutboxMessage).where(OutboxMessage.status == "pending")
                                .order_by(OutboxMessage.id).limit(BATCH))).scalars().all()
        users = {r.user_id for r in rows if r.user_id and not r.chat_id}
        chats = dict((await s.execute(select(User.id, User.tg_id).where(User.id.in_(users)))).all()) if users else {}
        jobs: dict[int | None, list[tuple]] = defaultdict(list)
        for r in rows:
            chat = r.chat_id or chats.get(r.user_id)
            jobs[chat].append((r.id, r.text, r.markup, r.next_attempt_at <= now, r.attempts))
    if not jobs:
        return 0
    sem = asyncio.Semaphore(PARALLEL_CHATS)
    sent = 0

    async def one_chat(chat, items) -> None:
        nonlocal sent
        async with sem:
            for mid, text, markup, due, attempts in items:
                if not due:
                    return  # an earlier message still waits for its retry: the later ones must not overtake it
                if not chat or chat < 0:
                    await _finish(mid, notify.Delivery(error="нет чата", permanent=True), attempts)
                    continue
                try:
                    d = await notify.deliver(chat, text, _load(markup))
                except Exception as err:  # noqa: BLE001 - a broken keyboard etc.: never kill the worker
                    d = notify.Delivery(error=f"{type(err).__name__}: {err}"[:300])
                await _finish(mid, d, attempts)
                if not d.ok and not d.permanent:
                    return  # keep this chat's order: try again from this message next time
                sent += d.ok

    await asyncio.gather(*(one_chat(chat, items) for chat, items in jobs.items()))
    return sent


async def _finish(mid: int, d: notify.Delivery, attempts: int) -> None:
    async with session_scope() as s:
        m = await s.get(OutboxMessage, mid)
        m.attempts = attempts + 1
        if d.ok:
            m.status, m.sent_at, m.message_id, m.last_error = "sent", utcnow(), d.message_id, None
            return
        m.last_error = d.error
        if d.permanent or m.attempts > len(BACKOFF):
            m.status = "failed"
            log.warning("telegram message %s given up after %s attempt(s): %s", mid, m.attempts, d.error)
            return
        pause = max(BACKOFF[m.attempts - 1], d.retry_after or 0)
        m.next_attempt_at = utcnow() + timedelta(seconds=pause)
        log.info("telegram message %s: attempt %s failed (%s), next in %ss", mid, m.attempts, d.error, int(pause))


async def flush(rounds: int = 3) -> None:
    """Send what's due right now (tests, and the local dev payment button)."""
    for _ in range(rounds):
        if not await send_due():
            return


async def _next_due_in() -> float:
    """Seconds until the earliest waiting retry (IDLE if there's none sooner)."""
    async with session_scope() as s:
        nxt = (await s.execute(select(func.min(OutboxMessage.next_attempt_at))
                               .where(OutboxMessage.status == "pending"))).scalar_one_or_none()
    if nxt is None:
        return IDLE
    return min(IDLE, max(0.05, (nxt - utcnow()).total_seconds()))


async def run() -> None:
    from . import health

    while True:
        health.beat("notify")
        try:
            await send_due()
            pause = await _next_due_in()  # wake for a retry Telegram asked for in 1 s, not 5 s later
        except Exception:
            log.exception("outbox pass failed")
            pause = IDLE
        await events.wait(events.OUTBOX, pause)
