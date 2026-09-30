from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timezone

log = logging.getLogger("sh")

_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def utcnow() -> datetime:
    """Naive UTC now (SQLite stores naive datetimes; everything in the DB is UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime | None) -> str | None:
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def public_id(prefix: str = "SH", n: int = 8) -> str:
    return prefix + "".join(secrets.choice(_ALPHABET) for _ in range(n))


def token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


_background: set[asyncio.Task] = set()


def background(coro, name: str = "background") -> asyncio.Task:
    """Run a side job (an admin's Telegram card, an alert) without making the caller — a money worker — wait for it.
    The task is kept referenced until done, and its error is logged instead of vanishing."""
    task = asyncio.create_task(coro, name=name)
    _background.add(task)

    def _done(t: asyncio.Task) -> None:
        _background.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("%s failed", name, exc_info=t.exception())

    task.add_done_callback(_done)
    return task


async def supervised(name: str, coro_factory, *, restart_delay: float = 5.0):
    """Run a long-lived coroutine forever, restarting it if it crashes."""
    while True:
        try:
            await coro_factory()
            log.warning("task %s exited, restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("task %s crashed, restarting in %ss", name, restart_delay)
        await asyncio.sleep(restart_delay)
