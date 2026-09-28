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

__all__ = ["esc", "send", "edit", "notify_user", "notify_admins", "set_bot", "current_bot"]
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


def _emoji_problem(err: Exception) -> bool:
    s = str(err).lower()
    return any(w in s for w in ("emoji", "entit", "icon", "style", "document_invalid"))


async def send(chat_id: int, text: str, buttons=None) -> int | None:
    """Send a message; returns its message_id (so it can be edited later), None if it didn't go out."""
    markup = _markup(buttons)
    if _bot is None:
        log.info("[tg -> %s] %s", chat_id, tgui.strip_custom(text).replace("\n", " | "))
        return None
    try:
        return (await _bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)).message_id
    except Exception as e:  # noqa: BLE001 - blocked bot, bad chat, network...
        if _emoji_problem(e):
            tgui.disable_custom()
            try:
                return (await _bot.send_message(chat_id, tgui.strip_custom(text), reply_markup=tgui.plain_markup(markup),
                                                disable_web_page_preview=True)).message_id
            except Exception as e2:  # noqa: BLE001
                e = e2
        log.warning("telegram send to %s failed: %s", chat_id, e)
        if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
            async with session_scope() as s:
                u = (await s.execute(select(User).where(User.tg_id == chat_id))).scalar_one_or_none()
                if u:
                    u.bot_blocked = True
        return None


async def edit(chat_id: int, message_id: int, text: str, buttons=None) -> bool:
    """Update a message sent earlier (a card that follows an order instead of a new message per step)."""
    if _bot is None or not isinstance(message_id, int) or isinstance(message_id, bool):
        return False
    markup = _markup(buttons)
    try:
        await _bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup,
                                     disable_web_page_preview=True)
        return True
    except Exception as e:  # noqa: BLE001
        if "not modified" in str(e).lower():
            return True
        if _emoji_problem(e):
            tgui.disable_custom()
            try:
                await _bot.edit_message_text(tgui.strip_custom(text), chat_id=chat_id, message_id=message_id,
                                             reply_markup=tgui.plain_markup(markup), disable_web_page_preview=True)
                return True
            except Exception as e2:  # noqa: BLE001
                e = e2
        log.warning("telegram edit %s/%s failed: %s", chat_id, message_id, e)
        return False


async def notify_user(user_id: int, text: str, buttons=None) -> None:
    async with session_scope() as s:
        u = await s.get(User, user_id)
        tg_id = u.tg_id if u else None
    if tg_id and tg_id > 0:
        await send(tg_id, text, buttons)


async def notify_admins(text: str, buttons=None, *, key: str | None = None,
                        every: float = 3600) -> list[tuple[int, int]]:
    """Send to all admins; returns [(chat_id, message_id)] of what went out.
    With `key`, the same alert goes out at most once per `every` seconds. The last time is kept in the database,
    so restarting the service doesn't repeat alerts (low supplier balance, monitoring gaps...) all over again."""
    if key:
        from .kv import kv_get, kv_set

        now = time.time()
        last = _throttle.get(key)
        if last is None:
            last = float(await kv_get(f"throttle:{key}", 0) or 0)
        if now - last < every:
            _throttle[key] = last
            return []
        _throttle[key] = now
        await kv_set(f"throttle:{key}", now)
    if not settings.admin_ids:
        log.warning("[admin] %s", tgui.strip_custom(text).replace("\n", " | "))
    sent = []
    for aid in settings.admin_ids:
        mid = await send(aid, text, buttons)
        if mid:
            sent.append((aid, mid))
    return sent
