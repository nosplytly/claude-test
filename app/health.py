"""Is the service really working, not just answering HTTP?

The loops that move money (order processor, payment settlement) and the bot's link to Telegram each "beat" on
every pass. /healthz reports 503 when one of them has been silent for 3 minutes or the database doesn't answer —
an external uptime monitor (UptimeRobot etc.) turns that into an alert even if the whole box is gone — and a
watchdog inside the app tells the admins in Telegram while the app itself is still up.
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import text

from .config import settings

log = logging.getLogger("sh.health")
STARTED = time.time()
GRACE = 180  # right after a (re)start nothing has beaten yet
MAX_SILENCE = 180
NAMES = {"db": "база данных", "orders": "обработка заказов", "settle": "зачисление платежей",
         "telegram": "связь бота с Telegram"}
_beats: dict[str, float] = {}


def beat(name: str) -> None:
    _beats[name] = time.time()


def _watched() -> list[str]:
    return ["orders", "settle"] + (["telegram"] if settings.bot_token else [])


async def problems() -> list[str]:
    now = time.time()
    bad = []
    try:
        from .db import engine

        async with asyncio.timeout(5):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except Exception as err:  # noqa: BLE001
        log.warning("health: database: %s", err)
        bad.append("db")
    if now - STARTED > GRACE:
        bad += [n for n in _watched() if now - _beats.get(n, STARTED) > MAX_SILENCE]
    return bad


async def check_once(previous: list[str]) -> list[str]:
    """One watchdog pass: the same problem twice in a row (a minute apart) -> alert the admins, at most every
    30 minutes per set of problems. Returns what it saw, for the next pass."""
    from .notify import notify_admins
    from .tgui import e

    bad = await problems()
    if bad and bad == previous:
        await notify_admins(f"{e('alarm')} <b>Сервис работает не полностью</b>\n"
                            + "\n".join(f"• {NAMES.get(b, b)} не отвечает больше 3 минут" for b in bad)
                            + "\n<i>Сайт открывается, но эта часть стоит. Логи: data\\logs\\app.log</i>",
                            key="health-" + ",".join(bad), every=1800)
    return bad


async def watchdog() -> None:
    previous: list[str] = []
    while True:
        await asyncio.sleep(60)
        previous = await check_once(previous)
