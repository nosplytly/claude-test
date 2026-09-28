"""Telegram look & feel: one registry of emoji (standard ones now, the owner's custom pack later) and
coloured inline buttons.

Custom emoji (in text via <tg-emoji> and as button icons) only render if the bot owner has Telegram Premium
(or the bot has a Fragment username). IDs live in app/tg_emoji.json: {"ok": "5368324170671202286", ...}.
If Telegram refuses them, with_emoji_fallback() retries the same message with the plain fallback emoji.
"""
from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("sh.tgui")

# semantic key -> fallback emoji
EMOJI: dict[str, str] = {
    "logo": "🟦", "steam": "🎮", "rocket": "🚀", "bolt": "⚡", "percent": "💯", "shield": "🛡",
    "money": "💰", "card": "💳", "wallet": "👛", "plus": "➕", "orders": "🧾", "support": "🛟",
    "back": "⬅️", "refresh": "🔄", "admin": "🛠", "stats": "📊", "bank": "🏦", "work": "⏳", "list": "📋",
    "user": "👤", "margin": "📈", "ok": "✅", "fail": "❌", "wait": "⏳", "queue": "⏸", "warn": "⚠️",
    "alarm": "🚨", "question": "❓", "claim": "🧾", "low": "🔻", "net": "📡", "new": "🟦", "lock": "🔐",
    "cancel": "✖️", "retry": "🔁", "refund": "↩️", "link": "🔗", "gift": "🎁", "fire": "🔥", "clock": "🕒",
    "tx": "🔎", "coin": "🪙", "sparkle": "✨", "turtle": "🐢", "key": "🔑", "hello": "👋", "swap": "💱",
}

_ids: dict[str, str] = {}
_enabled = True
_FILE = Path(__file__).with_name("tg_emoji.json")


def load() -> None:
    global _ids
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else {}
        _ids = {k: str(v) for k, v in data.items() if k in EMOJI and str(v).isdigit()}
        if _ids:
            log.info("custom emoji loaded: %s", ", ".join(sorted(_ids)))
    except Exception as e:  # noqa: BLE001
        log.warning("tg_emoji.json unreadable: %s", e)
        _ids = {}


def disable_custom() -> None:
    """Telegram refused custom emoji (no Premium on the owner account): stay on standard ones."""
    global _enabled
    if _enabled and _ids:
        log.warning("custom emoji refused by Telegram — falling back to standard emoji")
    _enabled = False


def e(key: str) -> str:
    """Emoji for message text (HTML parse mode)."""
    ch = EMOJI.get(key, key)
    cid = _ids.get(key) if _enabled else None
    return f'<tg-emoji emoji-id="{cid}">{ch}</tg-emoji>' if cid else ch


def strip_custom(text: str) -> str:
    return re.sub(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', r"\1", text or "")


def emoji_refused(err: Exception) -> bool:
    s = str(err).lower()
    return any(w in s for w in ("emoji", "entit", "icon", "style", "document_invalid"))


async def with_emoji_fallback(put: Callable[[str, InlineKeyboardMarkup | None], Awaitable], text: str,
                              markup: InlineKeyboardMarkup | None):
    """put(text, markup) sends or edits a message. If Telegram refuses the custom emoji, switch them off for good
    and put the same message again with the standard ones."""
    try:
        return await put(text, markup)
    except Exception as err:
        if not emoji_refused(err):
            raise
        disable_custom()
        return await put(strip_custom(text), plain_markup(markup))


def esc(x) -> str:
    return html.escape(str(x), quote=False)


# ------------------------------------------------------------------ buttons
PRIMARY, SUCCESS, DANGER = "primary", "success", "danger"  # blue, green, red


def btn(text: str, *, cb: CallbackData | str | None = None, url: str | None = None, style: str | None = None,
        icon: str | None = None) -> InlineKeyboardButton | None:
    """Button with optional colour and emoji. The emoji goes in front of the text; with a custom pack it becomes
    the button's icon instead. `cb` is one of app.bot.callbacks. URL buttons need https (Telegram rejects
    localhost links) — else None."""
    if url is not None and not url.startswith("https://"):
        return None
    cid = _ids.get(icon) if (icon and _enabled) else None
    label = text if (cid or not icon) else f"{EMOJI.get(icon, icon)} {text}"
    kw: dict = {"text": label}
    if cb is not None:
        kw["callback_data"] = cb if isinstance(cb, str) else cb.pack()
    if url is not None:
        kw["url"] = url
    if style:
        kw["style"] = style
    if cid:
        kw["icon_custom_emoji_id"] = cid
    return InlineKeyboardButton(**kw)


def kb(*rows) -> InlineKeyboardMarkup | None:
    clean = [[b for b in row if b is not None] for row in rows if row]
    clean = [r for r in clean if r]
    return InlineKeyboardMarkup(inline_keyboard=clean) if clean else None


def plain_markup(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """Same keyboard without custom emoji icons (fallback)."""
    if not markup:
        return markup
    rows = []
    for row in markup.inline_keyboard:
        r = []
        for b in row:
            data = b.model_dump(exclude_none=True)
            cid = data.pop("icon_custom_emoji_id", None)
            if cid:
                key = next((k for k, v in _ids.items() if v == cid), None)
                if key:
                    data["text"] = f"{EMOJI[key]} {data['text']}"
            r.append(InlineKeyboardButton(**data))
        rows.append(r)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def line(char: str = "─", n: int = 18) -> str:
    return char * n


def site(path: str = "/") -> str:
    from .config import settings

    return settings.base_url.rstrip("/") + path


load()
