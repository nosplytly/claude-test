"""Languages: every customer-facing text is a key in app/locales/{ru,en}.json, rendered with t().

    t("notify.done.body", lang, login="gaben", amount="500 ₽")

- The bot answers in the language of whoever sent the update (TrackLanguage sets `current`).
- Notifications are rendered in the RECIPIENT's language when they're queued (user_lang / lang_of_user).
- API errors are rendered in the language of the request (the site sends X-SH-Lang; partners Accept-Language).
- Admin screens and alerts are Russian by design (one admin team) and are not keys.

A customer's language: their own choice (/language in the bot, the RU/EN switch on the site) → the language of their
Telegram app (language_code) → Russian. Kept on the User row, and in memory for the chats the bot writes to.
Links from the bot to the site carry ?lang=…, so the site speaks the same language as the bot.
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiogram import BaseMiddleware
from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from sqlalchemy import select

from .config import settings
from .db import session_scope
from .models import User

log = logging.getLogger("sh.i18n")

LANGS = ("ru", "en")
DEFAULT = "ru"
# Russian speakers mostly live with these app languages, and read Russian better than English
RU_FAMILY = {"ru", "uk", "be", "kk", "ky", "uz", "tg", "tk", "hy", "az", "ka"}
CATALOG: dict[str, dict[str, str]] = {
    lang: json.loads((Path(__file__).parent / "locales" / f"{lang}.json").read_text(encoding="utf-8")) for lang in LANGS}

_chats: dict[int, str] = {}  # tg_id → language, for messages the bot sends on its own (order done, payment in…)
_chosen: dict[int, str] = {}  # /language of someone who never signed in: no User row to keep it on
_codes: dict[int, str | None] = {}  # tg_id → the language_code already written to the database
current: ContextVar[str] = ContextVar("sh_lang", default=DEFAULT)  # the language of the update / request at hand


def t(key: str, lang: str | None = None, **params) -> str:
    """The text for `key` in `lang` (default: the current update's or request's language)."""
    lang = lang if lang in LANGS else current.get()
    text = CATALOG[lang].get(key) or CATALOG[DEFAULT].get(key)
    if text is None:
        log.error("no text for %r", key)
        return key
    return text.format(**params) if params else text


class Problem(Exception):
    """An error a person will read: a key + parameters, rendered in the reader's language when it's shown."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key, self.params = key, params

    def text(self, lang: str | None = None) -> str:
        return t(self.key, lang, **self.params)

    def __str__(self) -> str:
        return self.text()


def lang_of(code: str | None) -> str:
    """A language tag (Telegram's language_code, Accept-Language) → the language we talk. Nothing → Russian."""
    if not code:
        return DEFAULT
    return "ru" if code.split("-")[0].split(";")[0].strip().lower() in RU_FAMILY else "en"


def lang_of_request(headers) -> str:
    """The site says which language it shows (X-SH-Lang); anyone else — their Accept-Language."""
    own = (headers.get("x-sh-lang") or "").strip().lower()
    if own in LANGS:
        return own
    accept = headers.get("accept-language") or ""
    return lang_of(accept.split(",")[0]) if accept else DEFAULT


def user_lang(u: User | None) -> str:
    if u is None:
        return DEFAULT
    return u.lang if u.lang in LANGS else lang_of(u.tg_lang)


async def lang_of_user(s, user_id: int) -> str:
    """The language to write to this customer in (inside the caller's transaction)."""
    return user_lang(await s.get(User, user_id))


def remember(tg_id: int, lang: str) -> None:
    _chats[tg_id] = lang


async def lang_for_chat(chat_id: int) -> str:
    if chat_id in settings.admin_ids:
        return "ru"
    if chat_id in _chats:
        return _chats[chat_id]
    async with session_scope() as s:
        row = (await s.execute(select(User.lang, User.tg_lang).where(User.tg_id == chat_id))).first()
    lang = (row[0] if row[0] in LANGS else lang_of(row[1])) if row else _chosen.get(chat_id, DEFAULT)
    _chats[chat_id] = lang
    return lang


async def note_telegram_user(tg_id: int, code: str | None) -> str:
    """Someone wrote to the bot or pressed a button: their app's language is kept for the default (and for
    notifications later), and the language to answer in is returned."""
    if tg_id in settings.admin_ids:
        return "ru"
    if _codes.get(tg_id, "?") == code and tg_id in _chats:
        return _chats[tg_id]
    async with session_scope() as s:
        u = (await s.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
        if u and code and u.tg_lang != code:
            u.tg_lang = code
        lang = user_lang(u) if u else _chosen.get(tg_id) or lang_of(code)
    _codes[tg_id], _chats[tg_id] = code, lang
    return lang


async def choose(tg_id: int, lang: str) -> None:
    """The customer picked a language by hand (bot or site)."""
    async with session_scope() as s:
        u = (await s.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
        if u:
            u.lang = lang
    if not u:
        _chosen[tg_id] = lang
    _chats[tg_id] = lang


# ------------------------------------------------------------------ links from the bot open the site in the same language
def with_lang(url: str, lang: str) -> str:
    """A link to our site, opened in the given language (?lang=en). Other links and the one-time login links stay."""
    base = settings.base_url.rstrip("/")
    if not base or not (url == base or url.startswith(base + "/")) or "/auth/" in url:
        return url
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "lang"] + [("lang", lang)]
    return urlunsplit((p.scheme, p.netloc, p.path or "/", urlencode(q), p.fragment))


def _button(b, lang: str):
    upd = {}
    if b.url:
        url = with_lang(b.url, lang)
        if url != b.url:
            upd["url"] = url
    if b.web_app:
        url = with_lang(b.web_app.url, lang)
        if url != b.web_app.url:
            upd["web_app"] = WebAppInfo(url=url)
    return b.model_copy(update=upd) if upd else b


def site_links(method, lang: str):
    markup = getattr(method, "reply_markup", None)
    if not isinstance(markup, InlineKeyboardMarkup):
        return method
    rows = [[_button(b, lang) for b in row] for row in markup.inline_keyboard]
    if rows == markup.inline_keyboard:
        return method
    return method.model_copy(update={"reply_markup": InlineKeyboardMarkup(inline_keyboard=rows)})


async def outgoing(make_request, bot, method):
    """Bot session middleware: site links in a customer's message open the site in the customer's language."""
    chat_id = getattr(method, "chat_id", None)
    try:
        if isinstance(chat_id, int) and chat_id not in settings.admin_ids:
            method = site_links(method, await lang_for_chat(chat_id))
    except Exception:  # noqa: BLE001 - a link tweak must never stop a message
        log.exception("site links in %s", type(method).__name__)
    return await make_request(bot, method)


class TrackLanguage(BaseMiddleware):
    """Dispatcher middleware: the language of whoever sent the update, for the answers to it."""

    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u is not None and not u.is_bot:
            try:
                current.set(await note_telegram_user(u.id, u.language_code))
            except Exception:  # noqa: BLE001
                log.exception("language of %s not noted", u.id)
        return await handler(event, data)
