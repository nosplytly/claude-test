"""Wake-ups between the money workers, so each step starts the moment the previous one is done instead of on its
next timer tick:

    chain poller ──▶ credit worker (per chain) ──▶ order processor ──▶ supplier
                              │
                              ├──▶ notifier (Telegram, from the outbox table)
                              └──▶ partner webhooks

A wake-up raised inside a database transaction fires only after that transaction commits (kick_after_commit):
a worker woken earlier would look, find nothing yet, and go back to sleep. Every worker also runs on a timer, so a
lost wake-up (or a restart) only costs the timer interval, never a payment. In-process only: the app is one process.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

_signals: dict[str, asyncio.Event] = {}


def _sig(name: str) -> asyncio.Event:
    ev = _signals.get(name)
    if ev is None:
        ev = _signals[name] = asyncio.Event()
    return ev


def kick(name: str) -> None:
    """Wake whoever waits for `name` (now)."""
    _sig(name).set()


def kick_after_commit(s: AsyncSession, name: str) -> None:
    """Wake whoever waits for `name` once this session's transaction has committed; nothing if it rolls back."""
    s.info.setdefault("sh_kicks", set()).add(name)


async def wait(name: str, timeout: float) -> bool:
    """Sleep until kicked or `timeout` seconds pass; True if kicked. A kick that came while nobody waited still counts."""
    ev = _sig(name)
    try:
        await asyncio.wait_for(ev.wait(), timeout)
        return True
    except TimeoutError:
        return False
    finally:
        ev.clear()


@event.listens_for(Session, "after_commit")
def _fire(session: Session) -> None:
    for name in session.info.pop("sh_kicks", ()):
        kick(name)


@event.listens_for(Session, "after_rollback")
def _drop(session: Session) -> None:
    session.info.pop("sh_kicks", None)


# names
ORDERS = "orders"  # an order became "paid": submit it to the supplier
OUTBOX = "outbox"  # a Telegram message was queued
WEBHOOKS = "webhooks"  # a partner webhook was queued


def credit(chain: str) -> str:
    """A transfer on `chain` may be ready to credit."""
    return f"credit:{chain}"
