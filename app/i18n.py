"""English for customers whose Telegram speaks English.

The bot's texts are written in Russian where they're built (bot.py, orders.py, payments/monitor.py). For a customer
whose language is English, every outgoing Bot API call — message text, photo caption, button labels, the little
pop-up of a pressed button — goes through to_en(): a phrase book of the customer-facing Russian fragments.
Messages to the admins stay Russian.

A customer's language: their own choice (/language in the bot, the RU/EN switch on the site) → the language of their
Telegram app (language_code) → Russian. Kept on the User row, and in memory for the chats the bot writes to.
Links from the bot to the site carry ?lang=…, so the site speaks the same language as the bot.
"""
from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiogram import BaseMiddleware
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from sqlalchemy import select

from .config import settings
from .db import session_scope
from .models import User

log = logging.getLogger("sh.i18n")

LANGS = ("ru", "en")
# Russian speakers mostly live with these app languages, and read Russian better than English
RU_FAMILY = {"ru", "uk", "be", "kk", "ky", "uz", "tg", "tk", "hy", "az", "ka"}
CYR = re.compile(r"[А-Яа-яЁё]")

_chats: dict[int, str] = {}  # tg_id → language, for messages the bot sends on its own (order done, payment in…)
_chosen: dict[int, str] = {}  # /language of someone who never signed in: no User row to keep it on
_codes: dict[int, str | None] = {}  # tg_id → the language_code already written to the database
current: ContextVar[str] = ContextVar("sh_lang", default="ru")  # the language of the update being handled


def lang_of(code: str | None) -> str:
    """Telegram's language_code → the language we talk. No code at all (Telegram didn't say) → Russian."""
    if not code:
        return "ru"
    return "ru" if code.split("-")[0].lower() in RU_FAMILY else "en"


def user_lang(u: User) -> str:
    return u.lang if u.lang in LANGS else lang_of(u.tg_lang)


def remember(tg_id: int, lang: str) -> None:
    _chats[tg_id] = lang


async def lang_for_chat(chat_id: int) -> str:
    if chat_id in settings.admin_ids:
        return "ru"
    if chat_id in _chats:
        return _chats[chat_id]
    async with session_scope() as s:
        row = (await s.execute(select(User.lang, User.tg_lang).where(User.tg_id == chat_id))).first()
    lang = (row[0] if row[0] in LANGS else lang_of(row[1])) if row else _chosen.get(chat_id, "ru")
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


# ------------------------------------------------------------------ phrase book: customer-facing Russian → English
EN: dict[str, str] = {
    # home, balance, orders
    "— пополнение Steam криптой": "— Steam top-ups with crypto",
    "Привет,": "Hi,",
    "Автоматически, сразу после подтверждения платежа": "Automatic, right after the payment is confirmed",
    "Если что-то пошло не так — деньги вернутся на баланс": "If anything goes wrong, the money goes back to your balance",
    "Войдите на сайте через Telegram — и здесь появятся ваши заказы и баланс.":
        "Sign in on the site with Telegram, and your orders and balance will show up here.",
    "Вы ещё не входили на сайт": "You haven't signed in on the site yet",
    "Нажмите «Войти» на сайте — бот пришлёт код.": "Tap “Sign in” on the site and the bot will send you a code.",
    "Открыть сайт": "Open the site",
    "Пополнить Steam": "Top up Steam",
    "Пополнить баланс": "Add funds",
    "Пополнить ещё": "Top up again",
    "Мои заказы": "My orders",
    "Поддержка": "Support",
    "Обновить": "Refresh",
    "Назад": "Back",
    "Меню": "Menu",
    "Баланс": "Balance",
    "Движений по балансу пока нет.": "No balance activity yet.",
    "Баланс автоматически оплачивает следующие заказы.": "Your balance automatically pays for your next orders.",
    "Заказов пока нет.": "No orders yet.",
    "Пополнение": "Top-up",
    "Заказ": "Order",
    "Возврат": "Refund",
    "Корректировка": "Adjustment",
    "Промокод": "Promo code",
    "ждёт оплату": "awaiting payment",
    "оплачен": "paid",
    "пополняется": "in progress",
    "в очереди": "queued",
    "проверяется": "checking",
    "готово": "done",
    "возврат на баланс": "refunded to balance",
    "истёк": "expired",
    "отменён": "cancelled",
    "Нет доступа": "Access denied",
    # site login
    "Ссылка для входа устарела. Нажмите «Войти» на сайте ещё раз.": "This sign-in link has expired. Tap “Sign in” on the site again.",
    "Вход на сайт": "Signing in to the site",
    "Нажмите код, который сейчас показан на сайте.": "Tap the code that's shown on the site right now.",
    "Если вы не входили — нажмите «Это не я».": "If this wasn't you, tap “Not me”.",
    "Это не я": "Not me",
    "Яндекс Браузер": "Yandex Browser",
    "браузер": "browser",
    "Код не совпал — вход отменён. Попробуйте ещё раз на сайте.": "Wrong code — sign-in cancelled. Try again on the site.",
    "Вход подтверждён": "Sign-in confirmed",
    "Возвращайтесь на сайт — вы уже вошли.": "Head back to the site — you're signed in.",
    "Вернуться на сайт": "Back to the site",
    "Готово": "Done",
    "Вход отменён. Никто не получил доступ к вашему аккаунту.": "Sign-in cancelled. Nobody got access to your account.",
    # balance and promo codes
    "Баланс изменён": "Balance changed",
    "Теперь на балансе:": "Now on your balance:",
    "На балансе:": "Balance:",
    "Отправьте <code>/promo КОД</code> — бонус зачислится на баланс. Промокод на скидку вводится на сайте при оформлении заказа.":
        "Send <code>/promo CODE</code> and the bonus goes to your balance. Discount codes are entered on the site when you order.",
    "Сначала войдите на сайте через Telegram — потом активируйте промокод.":
        "Sign in on the site with Telegram first, then redeem the promo code.",
    "Такого промокода нет": "No such promo code",
    "Срок действия промокода истёк": "This promo code has expired",
    "Промокод закончился": "This promo code has run out",
    "Вы уже использовали этот промокод": "You've already used this promo code",
    "Это промокод на скидку — введите его при оформлении заказа": "This is a discount code — enter it on the site when you order",
    # orders (orders.py)
    "Готово!": "Done!",
    "пополнен на": "topped up with",
    "Приятных покупок!": "Enjoy!",
    "Не удалось пополнить Steam": "Steam top-up failed",
    "Поставщик отклонил пополнение": "The supplier declined the top-up",
    "Аккаунт": "Account",
    "Причина:": "Reason:",
    "вернулись на баланс сайта — можно оформить заказ заново.": "went back to your site balance — you can place the order again.",
    "вернулись на баланс сайта.": "went back to your site balance.",
    "Оформить заново": "Order again",
    "Заказ отменён": "Order cancelled",
    "Пополнение Steam": "Steam top-up",
    "не выполнено.": "was not completed.",
    "отклонено поставщиком": "rejected by the supplier",
    # payments (payments/monitor.py)
    "Оплата получена": "Payment received",
    "Пополняем Steam": "Topping up Steam",
    "</code> на <b>": "</code> with <b>",
    "Пришлю сообщение, как только всё будет готово.": "I'll message you as soon as it's done.",
    "Платёж зачислен на баланс": "Payment credited to your balance",
    "→ баланс": "→ balance",
    "Он пришёл после истечения счёта, поэтому заказ не выполнен автоматически. Оформите заказ заново — он оплатится с баланса.":
        "It arrived after the invoice expired, so the order wasn't completed automatically. "
        "Place the order again — it will be paid from your balance.",
    "Оформить заказ": "Place an order",
    "Получено": "Received",
    ", не хватает": ", missing",
    "Доплатите": "Please pay the remaining",
    "— новый счёт уже на сайте.": "— a new invoice is already on the site.",
    "Открыть счёт": "Open invoice",
    "Баланс пополнен": "Balance topped up",
    "Заявка на зачисление отклонена": "Your crediting request was declined",
    "Если это ошибка — напишите в поддержку, разберёмся.": "If this is a mistake, message support and we'll sort it out.",
    "Платёж подтверждён": "Payment confirmed",
    "</b> на балансе.": "</b> on your balance.",
    "Заказ оплачен, пополняем Steam…": "Order paid, topping up Steam…",
    # waiting list for the FunPay / Playerok plugin
    "Вы в списке!": "You're on the list!",
    "Вы уже в списке": "You're already on the list",
    "Напишу сюда, как только выйдет плагин для FunPay и Playerok.": "I'll message you here as soon as the FunPay & Playerok plugin is out.",
    "Платёж не подтвердился": "Payment not confirmed",
    "Мы не смогли подтвердить эту транзакцию в сети, поэтому не зачислили её.": "We couldn't confirm this transaction on the blockchain, so it wasn't credited.",
    "Если это ошибка — напишите в поддержку, разберёмся.": "If this is a mistake, message support and we'll sort it out.",    # the bot's own language command
    "Язык": "Language",
    "Открыть": "Open",
}

PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Скидка ([\d.,]+)%"), r"\1% off"),
    (re.compile(r"Не хватает для заказа (\S+?)</b>"), r"Not enough for order \1</b>"),
    (re.compile(r"\+\$([\d.,]+) на баланс"), r"+$\1 to your balance"),
    (re.compile(r"−([\d.,]+)% к скидке"), r"extra −\1% off"),
]


def _phrase_regex(keys) -> re.Pattern:
    """One pass over the text, the longest phrase first; a phrase that starts or ends with a letter must not be
    glued to another letter (so «Заказ» doesn't bite into «Заказов»)."""
    parts = []
    for k in sorted(keys, key=len, reverse=True):
        p = re.escape(k)
        if CYR.match(k[0]):
            p = r"(?<![А-Яа-яЁё])" + p
        if CYR.match(k[-1]):
            p += r"(?![А-Яа-яЁё])"
        parts.append(p)
    return re.compile("|".join(parts))


_PHRASES = _phrase_regex(EN)


def to_en(text: str) -> str:
    if not text or not CYR.search(text):
        return text
    for rx, rep in PATTERNS:
        text = rx.sub(rep, text)
    return _PHRASES.sub(lambda m: EN[m.group(0)], text)


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
    if lang == "en" and CYR.search(b.text or ""):
        upd["text"] = to_en(b.text)
    if b.url:
        url = with_lang(b.url, lang)
        if url != b.url:
            upd["url"] = url
    if b.web_app:
        url = with_lang(b.web_app.url, lang)
        if url != b.web_app.url:
            upd["web_app"] = WebAppInfo(url=url)
    return b.model_copy(update=upd) if upd else b


def localize(method, lang: str):
    """The Bot API call as the customer should get it."""
    upd = {}
    if lang == "en":
        for field in ("text", "caption"):
            v = getattr(method, field, None)
            if isinstance(v, str) and CYR.search(v):
                upd[field] = to_en(v)
    markup = getattr(method, "reply_markup", None)
    if isinstance(markup, InlineKeyboardMarkup):
        rows = [[_button(b, lang) for b in row] for row in markup.inline_keyboard]
        if rows != markup.inline_keyboard:
            upd["reply_markup"] = InlineKeyboardMarkup(inline_keyboard=rows)
    return method.model_copy(update=upd) if upd else method


async def outgoing(make_request, bot, method):
    """Bot session middleware: customers get their language; admins, and calls that aren't to a chat, stay as they are."""
    chat_id = getattr(method, "chat_id", None)
    try:
        if isinstance(chat_id, int) and chat_id not in settings.admin_ids:
            method = localize(method, await lang_for_chat(chat_id))
        elif isinstance(method, AnswerCallbackQuery) and current.get() == "en":
            method = localize(method, "en")
    except Exception:  # noqa: BLE001 - a translation problem must never stop a message; Russian is better than nothing
        log.exception("translating %s failed", type(method).__name__)
    return await make_request(bot, method)


class TrackLanguage(BaseMiddleware):
    """Dispatcher middleware: notes the language of whoever sent the update, for the answers to it."""

    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u is not None and not u.is_bot:
            try:
                current.set(await note_telegram_user(u.id, u.language_code))
            except Exception:  # noqa: BLE001
                log.exception("language of %s not noted", u.id)
        return await handler(event, data)
