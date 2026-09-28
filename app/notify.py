"""Telegram notifications. Works without a bot configured (messages just go to the log)."""
from __future__ import annotations

import logging
import time
from typing import Any

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select

from . import tgui
from .config import settings
from .db import session_scope
from .models import User
from .tgui import esc  # noqa: F401  (re-exported: other modules import esc from here)

__all__ = ["esc", "send", "notify_user", "notify_admins", "set_bot", "current_bot"]
log = logging.getLogger("sh.notify")

# legacy button spec: (text, "cb:<data>" | "url:<https://...>"[, style[, emoji key]])
Button = tuple

_bot: Any = None  # aiogram Bot, set by app.bot on startup
_throttle: dict[str, float] = {}


def set_bot(bot) -> None:
    global _bot
    _bot = bot


def current_bot():
    return _bot


def _markup(buttons) -> InlineKeyboardMarkup | None:
    if buttons is None or isinstance(buttons, InlineKeyboardMarkup):
        return buttons
    rows = []
    for row in buttons:
        r = []
        for spec in row:
            text, target, *rest = spec
            style = rest[0] if rest else None
            icon = rest[1] if len(rest) > 1 else None
            if target.startswith("url:"):
                r.append(tgui.btn(text, url=target[4:], style=style, icon=icon))
            else:
                r.append(tgui.btn(text, cb=target.removeprefix("cb:"), style=style, icon=icon))
        rows.append(r)
    return tgui.kb(*rows)


async def send(chat_id: int, text: str, buttons=None) -> bool:
    markup = _markup(buttons)
    if _bot is None:
        log.info("[tg -> %s] %s", chat_id, tgui.strip_custom(text).replace("\n", " | "))
        return False

    async def put(t, m):
        await _bot.send_message(chat_id, t, reply_markup=m, disable_web_page_preview=True)

    try:
        await tgui.with_emoji_fallback(put, text, markup)
        return True
    except Exception as e:  # noqa: BLE001 - blocked bot, bad chat, network...
        log.warning("telegram send to %s failed: %s", chat_id, e)
        if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
            async with session_scope() as s:
                u = (await s.execute(select(User).where(User.tg_id == chat_id))).scalar_one_or_none()
                if u:
                    u.bot_blocked = True
        return False


async def notify_user(user_id: int, text: str, buttons=None) -> None:
    async with session_scope() as s:
        u = await s.get(User, user_id)
        tg_id = u.tg_id if u else None
    if tg_id and tg_id > 0:
        await send(tg_id, text, buttons)


async def notify_admins(text: str, buttons=None, *, key: str | None = None, every: float = 3600) -> None:
    """Send to all admins. With `key`, the same alert is sent at most once per `every` seconds."""
    if key:
        now = time.time()
        if now - _throttle.get(key, 0) < every:
            return
        _throttle[key] = now
    if not settings.admin_ids:
        log.warning("[admin] %s", tgui.strip_custom(text).replace("\n", " | "))
    for aid in settings.admin_ids:
        await send(aid, text, buttons)
