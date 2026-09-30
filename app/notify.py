"""Telegram notifications. Works without a bot configured (messages just go to the log)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select

from . import tgui
from .config import settings
from .db import session_scope
from .models import User
from .tgui import esc  # noqa: F401  (re-exported: other modules import esc from here)

__all__ = ["esc", "send", "send_document", "edit", "notify_user", "notify_admins", "set_bot", "current_bot"]
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


@dataclass
class Delivery:
    """How a send went: message_id when it went out; else the error, whether trying again can help at all
    (a user who blocked the bot won't unblock it for a retry), and Telegram's own "wait N seconds" if it said so."""
    message_id: int | None = None
    error: str | None = None
    permanent: bool = False
    retry_after: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


PERMANENT = ("blocked", "deactivated", "chat not found", "user not found", "kicked", "have no rights", "user is deleted")


async def deliver(chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> Delivery:
    """One Telegram message, with the plain-emoji fallback; never raises."""
    if _bot is None:  # no bot configured (local dev, tests): the log is the delivery
        log.info("[tg -> %s] %s", chat_id, tgui.strip_custom(text).replace("\n", " | "))
        return Delivery()
    try:
        msg = await _bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
        return Delivery(message_id=msg.message_id)
    except Exception as e:  # noqa: BLE001 - blocked bot, bad chat, network, flood control...
        err = e
        if _emoji_problem(e):
            tgui.disable_custom()
            try:
                msg = await _bot.send_message(chat_id, tgui.strip_custom(text), reply_markup=tgui.plain_markup(markup),
                                              disable_web_page_preview=True)
                return Delivery(message_id=msg.message_id)
            except Exception as e2:  # noqa: BLE001
                err = e2
    low = str(err).lower()
    if "blocked" in low or "deactivated" in low:
        async with session_scope() as s:
            u = (await s.execute(select(User).where(User.tg_id == chat_id))).scalar_one_or_none()
            if u:
                u.bot_blocked = True
    return Delivery(error=f"{type(err).__name__}: {err}"[:300], permanent=any(w in low for w in PERMANENT),
                    retry_after=getattr(err, "retry_after", None))


async def send(chat_id: int, text: str, buttons=None) -> int | None:
    """Send a message now; returns its message_id (so it can be edited later), None if it didn't go out.
    For news about money (payments, orders) use app.outbox instead: queued in the same transaction, sent with retries."""
    d = await deliver(chat_id, text, _markup(buttons))
    if not d.ok:
        log.warning("telegram send to %s failed: %s", chat_id, d.error)
    return d.message_id


async def send_document(chat_id: int, data: bytes, filename: str, caption: str = "") -> int | None:
    """Send a file (e.g. the encrypted DB backup); returns the message_id or None."""
    if _bot is None:
        log.info("[tg file -> %s] %s (%d bytes)", chat_id, filename, len(data))
        return None
    from aiogram.types import BufferedInputFile

    try:
        msg = await _bot.send_document(chat_id, BufferedInputFile(data, filename=filename), caption=caption or None)
        return msg.message_id
    except Exception as e:  # noqa: BLE001
        if caption and _emoji_problem(e):
            tgui.disable_custom()
            try:
                msg = await _bot.send_document(chat_id, BufferedInputFile(data, filename=filename),
                                               caption=tgui.strip_custom(caption))
                return msg.message_id
            except Exception as e2:  # noqa: BLE001
                e = e2
        log.warning("telegram document to %s failed: %s", chat_id, e)
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
