"""Telegram look & feel: one registry of emoji (the owner's custom pack where it has one, standard ones
elsewhere), coloured inline buttons and panels (<blockquote> blocks) for message bodies.

Custom emoji (in text via <tg-emoji> and as button icons) only render if the bot owner has Telegram Premium
(or the bot has a Fragment username). Where they come from:
  1. app/tg_emoji.json: {"ok": "5318848256250260091", ...} — chosen by hand, always wins;
  2. the pack itself ("_pack" in that file), read by the bot at start-up: a pack emoji whose base emoji is the
     same as a key's standard one (✅ for "ok", 🚀 for "rocket" ...) is used for that key. So an emoji added to
     the pack shows up in the bot after a restart, without touching the code. /emoji (admin) lists the pack.
If Telegram refuses them, notify.send() retries the same message with the plain fallback emoji.
"""
from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("sh.tgui")

# semantic key -> fallback emoji
EMOJI: dict[str, str] = {
    "logo": "🟦", "steam": "🎮", "rocket": "🚀", "bolt": "⚡", "percent": "💯", "shield": "🛡",
    "money": "💰", "card": "💳", "wallet": "👛", "plus": "➕", "orders": "🧾", "support": "💬",
    "back": "⬅️", "refresh": "🔄", "admin": "🛠", "stats": "📊", "bank": "🏦", "work": "⏳", "list": "📋",
    "user": "👤", "margin": "📈", "ok": "✅", "fail": "❌", "wait": "⏳", "queue": "⏸", "warn": "⚠️",
    "alarm": "🚨", "question": "❓", "claim": "🧾", "low": "🔻", "net": "📡", "new": "🟦", "lock": "🔐",
    "cancel": "✖️", "retry": "🔁", "refund": "↩️", "link": "🔗", "gift": "🎁", "fire": "🔥", "clock": "🕒",
    "tx": "🔎", "coin": "🪙", "sparkle": "✨", "turtle": "🐢", "key": "🔑", "hello": "👋", "swap": "💱",
}

_ids: dict[str, str] = {}
_manual: set[str] = set()  # keys set in tg_emoji.json (the pack never overrides them)
_enabled = True
_FILE = Path(__file__).with_name("tg_emoji.json")
pack_name: str | None = None
pack: list[tuple[str, str]] = []  # (custom_emoji_id, base emoji) as the bot read them from Telegram


def load() -> None:
    global _ids, _manual, pack_name
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else {}
        _ids = {k: str(v) for k, v in data.items() if k in EMOJI and str(v).isdigit()}
        _manual = set(_ids)
        m = re.search(r"addemoji/([A-Za-z0-9_]+)", str(data.get("_pack", "")))
        pack_name = m.group(1) if m else None
        if _ids:
            log.info("custom emoji loaded: %s", ", ".join(sorted(_ids)))
    except Exception as e:  # noqa: BLE001
        log.warning("tg_emoji.json unreadable: %s", e)
        _ids, _manual = {}, set()


def _base(ch: str) -> str:
    return ch.replace("️", "")  # "⚠️" and "⚠" are the same emoji


def use_pack(items: list[tuple[str, str]]) -> list[str]:
    """Take the pack as read from Telegram: every key without a hand-picked ID whose standard emoji is some pack
    emoji's base emoji gets that pack emoji. Returns the keys that got one."""
    global pack
    pack = [(str(cid), ch) for cid, ch in items if str(cid).isdigit()]
    by_base = {}
    for cid, ch in pack:
        by_base.setdefault(_base(ch), cid)
    got = []
    for key, ch in EMOJI.items():
        if key not in _manual and _base(ch) in by_base:
            _ids[key] = by_base[_base(ch)]
            got.append(key)
    return got


def keys_of(cid: str) -> list[str]:
    return sorted(k for k, v in _ids.items() if v == cid)


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


def esc(x) -> str:
    return html.escape(str(x), quote=False)


# ------------------------------------------------------------------ buttons
PRIMARY, SUCCESS, DANGER = "primary", "success", "danger"  # blue, green, red


def btn(text: str, *, cb: str | None = None, url: str | None = None, style: str | None = None,
        icon: str | None = None) -> InlineKeyboardButton | None:
    """Button with optional colour and emoji: the pack's emoji becomes the button's icon, otherwise the standard
    emoji goes in front of the text. URL buttons need https (Telegram rejects localhost links) — else None."""
    if url is not None and not url.startswith("https://"):
        return None
    cid = _ids.get(icon) if (icon and _enabled) else None
    kw: dict = {"text": text if (cid or not icon) else f"{EMOJI.get(icon, icon)} {text}"}
    if cb is not None:
        kw["callback_data"] = cb
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


# ------------------------------------------------------------------ message layout
# A message is a header (emoji + bold title) and panels: <blockquote> blocks that Telegram draws as tinted plates
# with an accent bar. Long lists go in an expandable panel (collapsed to a few lines, «развернуть» shows all).

def title(icon: str, text: str, extra: str = "") -> str:
    return f"{e(icon)} <b>{text}</b>{f' · {extra}' if extra else ''}"


def panel(*lines: str, expandable: bool = False) -> str:
    body = "\n".join(x for x in lines if x)
    return f"<blockquote{' expandable' if expandable else ''}>{body}</blockquote>" if body else ""


def visible_len(text: str) -> int:
    """Length Telegram counts for limits (captions: 1024) — the text without tags."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text or "")))


def site(path: str = "/") -> str:
    from .config import settings

    return settings.base_url.rstrip("/") + path


load()
