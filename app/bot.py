"""Telegram bot: button menus for customers and the admin, site login confirmation, notifications."""
from __future__ import annotations

import logging
import re
from contextlib import suppress
from datetime import timedelta
from decimal import InvalidOperation
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, CallbackQuery, FSInputFile, Message
from sqlalchemy import func, select

from . import fail2ban as f2b
from . import notify, tgui
from .auth import code_choices, upsert_tg_user
from .config import settings
from .db import session_scope
from .ledger import change_balance
from .models import Claim, LedgerEntry, LoginRequest, Order, Transfer, User
from .money import D, to_micro, usd_str
from .nervixy import nervixy
from .orders import admin_refund, admin_retry, discount_for, fmt_amount, reserved_cost_micro
from .payments import monitor as monitor_mod
from .tgui import DANGER, PRIMARY, SUCCESS, btn, e, esc, kb, panel, title
from .utils import sha256, token, utcnow

log = logging.getLogger("sh.bot")
router = Router()
bot_username: str | None = None
_dp: Dispatcher | None = None

# customer screens are a photo (this banner) with the screen as its caption; source: tools/bot-banner.html
BANNER = Path(__file__).parent / "web" / "assets" / "img" / "bot-banner.jpg"
CAPTION_MAX = 1024  # Telegram's limit for a photo caption; a longer screen goes out as plain text
_banner_id: str | None = None  # the banner's file_id after the first upload: later sends don't re-upload it

STATUS = {"awaiting_payment": ("wait", "ждёт оплату"), "paid": ("card", "оплачен"), "submitting": ("wait", "пополняется"),
          "queued": ("queue", "в очереди"), "uncertain": ("wait", "проверяется"), "processing": ("wait", "пополняется"),
          "delivered": ("ok", "готово"), "rejected": ("fail", "возврат на баланс"), "expired": ("clock", "истёк"),
          "cancelled": ("cancel", "отменён")}
KIND = {"deposit": "Пополнение", "order": "Заказ", "refund": "Возврат", "adjust": "Корректировка", "promo": "Промокод"}
KIND_ICON = {"deposit": "money", "order": "steam", "refund": "refund", "adjust": "swap", "promo": "gift"}


def join(*parts: str) -> str:
    """Header, panels and footer lines of a message, one per line (empty parts skipped)."""
    return "\n".join(p for p in parts if p)


def site(path: str = "/") -> str:
    return settings.base_url.rstrip("/") + path


def is_admin(uid: int | None) -> bool:
    return uid is not None and uid in settings.admin_ids


def short_ua(ua: str | None) -> str:
    ua = ua or ""
    os_ = next((n for k, n in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Windows", "Windows"),
                               ("Mac OS", "macOS"), ("Linux", "Linux")) if k in ua), "")
    br = next((n for k, n in (("YaBrowser", "Яндекс Браузер"), ("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox", "Firefox"),
                              ("Chrome", "Chrome"), ("Safari", "Safari")) if k in ua), "браузер")
    return f"{br}{' · ' + os_ if os_ else ''}"


async def _user(tg_id: int) -> User | None:
    async with session_scope() as s:
        return (await s.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()


def back(to: str = "m:home") -> list:
    return [btn("Назад", cb=to, icon="back")]


# ------------------------------------------------------------------ customer screens

def _not_logged_in(hint: str = "") -> tuple[str, object]:
    return (join(title("lock", "Вы ещё не входили на сайт"), panel(hint) if hint else ""),
            kb([btn("Открыть сайт", url=site("/"), style=PRIMARY, icon="rocket")], back()))


async def screen_home(tg_id: int) -> tuple[str, object]:
    u = await _user(tg_id)
    text = join(f"{e('logo')} <b>SupplierHub</b> — пополнение Steam криптой",
                f"Привет, {esc(u.first_name or u.display_name)}! {e('hello')}" if u else "",
                panel(f"{e('bolt')} Автоматически, сразу после подтверждения платежа",
                      f"{e('percent')} Скидка {discount_for(u)}%",
                      f"{e('shield')} Если что-то пошло не так — деньги вернутся на баланс"),
                "" if u else "Войдите на сайте через Telegram — и здесь появятся ваши заказы и баланс.")
    # the balance lives on its button (once), not repeated in the text above it
    bal = f"Баланс · ${usd_str(u.balance_micro)}" if u else "Баланс"
    rows = [
        [btn("Пополнить Steam", url=site("/"), style=PRIMARY, icon="rocket")],
        [btn(bal, cb="m:bal", style=SUCCESS, icon="money"), btn("Мои заказы", cb="m:orders", icon="orders")],
        [btn("Пополнить баланс", url=site("/#/deposit"), style=SUCCESS, icon="plus"),
         btn("Поддержка", url=settings.support_url, icon="support") if settings.support_url else None],
    ]
    if is_admin(tg_id):
        rows.append([btn("Админ-панель", cb="a:home", style=PRIMARY, icon="admin")])
    return text, kb(*rows)


async def screen_balance(tg_id: int) -> tuple[str, object]:
    u = await _user(tg_id)
    if not u:
        return _not_logged_in("Нажмите «Войти» на сайте — бот пришлёт код.")
    async with session_scope() as s:
        rows = (await s.execute(select(LedgerEntry).where(LedgerEntry.user_id == u.id)
                                .order_by(LedgerEntry.id.desc()).limit(6))).scalars().all()
    moves = [f"{e(KIND_ICON.get(r.kind, 'coin'))} <b>{'+' if r.delta_micro > 0 else '−'}${usd_str(abs(r.delta_micro))}</b>"
             f" · {KIND.get(r.kind, r.kind)} · {r.created_at + timedelta(hours=3):%d.%m %H:%M}" for r in rows]
    text = join(title("money", f"Баланс: ${usd_str(u.balance_micro)}"),
                panel(*moves) if moves else panel("Движений по балансу пока нет."),
                "<i>Баланс автоматически оплачивает следующие заказы.</i>" if moves else "")
    return text, kb([btn("Пополнить баланс", url=site("/#/deposit"), style=SUCCESS, icon="plus")],
                    [btn("Обновить", cb="m:bal", icon="refresh"), *back()])


async def screen_orders(tg_id: int) -> tuple[str, object]:
    u = await _user(tg_id)
    if not u:
        return _not_logged_in()
    async with session_scope() as s:
        rows = (await s.execute(select(Order).where(Order.user_id == u.id).order_by(Order.id.desc()).limit(8))).scalars().all()
    lines = []
    for o in rows:
        ek, label = STATUS.get(o.status, ("question", o.status))
        lines.append(f"{e(ek)} <b>{fmt_amount(o.amount, o.currency)}</b> → <code>{esc(o.steam_login)}</code> · {label}")
    text = join(title("orders", "Мои заказы"), panel(*lines) if lines else panel("Заказов пока нет."))
    return text, kb([btn("Пополнить Steam", url=site("/"), style=PRIMARY, icon="rocket")],
                    [btn("Обновить", cb="m:orders", icon="refresh"), *back()])


# ------------------------------------------------------------------ admin screens

async def _period(s, since):
    q = select(func.count(), func.coalesce(func.sum(Order.price_micro), 0),
               func.coalesce(func.sum(func.coalesce(Order.nervixy_paid_micro, Order.cost_micro)), 0)
               ).where(Order.status == "delivered")
    if since:
        q = q.where(Order.finished_at >= since)
    n, turnover, cost = (await s.execute(q)).one()
    dq = select(func.coalesce(func.sum(LedgerEntry.delta_micro), 0)).where(LedgerEntry.kind == "deposit")
    if since:
        dq = dq.where(LedgerEntry.created_at >= since)
    deposits = (await s.execute(dq)).scalar_one()
    return n, turnover, turnover - cost, deposits


async def _nx() -> dict | None:
    try:
        return await nervixy.account(max_age=60)
    except Exception:  # noqa: BLE001
        return None


async def screen_admin(tg_id: int) -> tuple[str, object]:
    now = utcnow()
    async with session_scope() as s:
        n, turnover, margin, deposits = await _period(s, now - timedelta(days=1))
        by = dict((await s.execute(select(Order.status, func.count()).where(Order.status.in_(
            ("awaiting_payment", "queued", "uncertain", "processing", "paid", "submitting"))).group_by(Order.status))).all())
        claims = (await s.execute(select(func.count()).select_from(Claim).where(Claim.status == "pending"))).scalar_one()
    acc = await _nx()
    problems = by.get("queued", 0) + by.get("uncertain", 0) + claims
    text = join(title("admin", "Админ-панель"),
                panel(f"{e('stats')} <b>За 24 часа</b>",
                      f"{n} заказов · оборот ${usd_str(turnover)} · маржа <b>+${usd_str(margin)}</b>",
                      f"{e('money')} Пополнений баланса: ${usd_str(deposits)}"),
                panel(f"{e('bank')} Nervixy: " + (f"<b>${acc['balance']}</b> (скидка {acc['discount']}%)" if acc else "нет связи"),
                      f"{e('work')} В работе: {by.get('processing', 0) + by.get('paid', 0) + by.get('submitting', 0)} · "
                      f"ждут оплату: {by.get('awaiting_payment', 0)}",
                      f"{e('lock')} Заблокировано IP: {len(f2b.active())}"),
                f"{e('warn')} Требуют внимания: <b>{problems}</b>" if problems else f"{e('ok')} Проблем нет")
    return text, kb(
        [btn("Статистика", cb="a:stats", style=PRIMARY, icon="stats"), btn("Nervixy", cb="a:nx", icon="bank")],
        [btn("Проблемы и очередь", cb="a:work", style=DANGER if problems else None, icon="warn" if problems else "work"),
         btn("Последние заказы", cb="a:last", icon="list")],
        [btn("Клиенты: баланс и скидки", cb="a:help", icon="user"), btn("Баны IP", cb="a:bans", icon="lock")],
        [btn("Обновить", cb="a:home", icon="refresh"), *back()],
    )


async def screen_stats() -> tuple[str, object]:
    now = utcnow()
    async with session_scope() as s:
        periods = [("24 часа", now - timedelta(days=1)), ("7 дней", now - timedelta(days=7)),
                   ("30 дней", now - timedelta(days=30)), ("Всё время", None)]
        rows = [(name, *await _period(s, since)) for name, since in periods]
        users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
        balances = (await s.execute(select(func.coalesce(func.sum(User.balance_micro), 0)))).scalar_one()
    text = join(title("stats", "Статистика"),
                *(panel(f"<b>{name}</b>",
                        f"{e('orders')} {n} заказов · оборот ${usd_str(turnover)}",
                        f"{e('margin')} маржа <b>+${usd_str(margin)}</b> · {e('money')} пополнений ${usd_str(deposits)}")
                  for name, n, turnover, margin, deposits in rows),
                f"{e('user')} Клиентов: {users} · на их балансах ${usd_str(balances)}")
    return text, kb([btn("Обновить", cb="a:stats", icon="refresh"), *back("a:home")])


async def screen_nx() -> tuple[str, object]:
    acc = await _nx()
    async with session_scope() as s:
        reserved = await reserved_cost_micro(s)
    if acc:
        free = to_micro(acc["balance"]) - reserved
        text = join(title("bank", "Nervixy"),
                    panel(f"Баланс: <b>${acc['balance']}</b>", f"Скидка поставщика: {acc['discount']}%"),
                    panel(f"Зарезервировано под заказы: ${usd_str(reserved)}",
                          f"Свободно для новых заказов: <b>${usd_str(max(free, 0))}</b>"),
                    f"<i>Алерт при балансе ниже ${settings.nervixy_low_balance_usd}</i>",
                    f"{e('low')} Пора пополнить (USDT внутренним переводом Bybit)."
                    if acc["balance"] < settings.nervixy_low_balance_usd else "")
    else:
        text = join(title("bank", "Nervixy"),
                    panel(f"{e('net')} Нет связи с API: "
                          f"{esc(nervixy.last_error or 'лимит запросов, попробуйте через минуту')}"))
    return text, kb([btn("Обновить", cb="a:nx", icon="refresh"), *back("a:home")])


async def screen_work() -> tuple[str, object]:
    since = utcnow() - timedelta(days=1)
    async with session_scope() as s:
        problem = (await s.execute(select(Order).where(Order.status.in_(("queued", "uncertain")))
                                   .order_by(Order.id).limit(10))).scalars().all()
        active = (await s.execute(select(Order).where(Order.status.in_(("paid", "submitting", "processing")))
                                  .order_by(Order.id).limit(10))).scalars().all()
        waiting = (await s.execute(select(func.count()).select_from(Order).where(Order.status == "awaiting_payment"))).scalar_one()
        unmatched = (await s.execute(select(func.count()).select_from(Transfer).where(
            Transfer.status == "unmatched", Transfer.created_at >= since))).scalar_one()
        claims = (await s.execute(select(func.count()).select_from(Claim).where(Claim.status == "pending"))).scalar_one()
    text = join(
        title("work", "Проблемы и очередь"),
        panel(f"{e('warn')} <b>Требуют внимания</b>",
              *(f"{e(STATUS[o.status][0])} <code>{o.public_id}</code> · {fmt_amount(o.amount, o.currency)} · "
                f"{STATUS[o.status][1]}" for o in problem)) if problem else "",
        panel(f"{e('wait')} <b>Пополняются сейчас: {len(active)}</b>",
              *(f"<code>{o.public_id}</code> · {fmt_amount(o.amount, o.currency)} → <code>{esc(o.steam_login)}</code>"
                for o in active), expandable=len(active) > 4),
        panel(f"{e('card')} Ждут оплату: {waiting}",
              f"{e('question')} Неопознанных платежей за сутки: {unmatched}",
              f"{e('claim')} Заявок по хэшу на проверке: {claims}"),
        "" if problem else f"{e('ok')} Всё идёт штатно.")
    return text, kb([btn("Обновить", cb="a:work", icon="refresh"), *back("a:home")])


async def screen_last() -> tuple[str, object]:
    async with session_scope() as s:
        rows = (await s.execute(select(Order, User).join(User, Order.user_id == User.id)
                                .order_by(Order.id.desc()).limit(10))).all()
    lines = []
    for o, u in rows:
        ek, label = STATUS.get(o.status, ("question", o.status))
        margin = o.price_micro - (o.nervixy_paid_micro or o.cost_micro)
        lines.append(f"{e(ek)} <b>{fmt_amount(o.amount, o.currency)}</b> → <code>{esc(o.steam_login)}</code>\n"
                     f"      {esc(u.display_name)} · {label}"
                     + (f" · маржа +${usd_str(margin)}" if o.status == "delivered" else ""))
    text = join(title("list", "Последние заказы"),
                panel(*lines, expandable=len(lines) > 4) if lines else panel("Заказов пока нет."))
    return text, kb([btn("Обновить", cb="a:last", icon="refresh"), *back("a:home")])


def screen_help() -> tuple[str, object]:
    text = join(title("user", "Клиенты: баланс и скидки"),
                "Отправьте боту команду:",
                panel(f"{e('money')} <b>Баланс</b>",
                      "<code>/bal @ник</code> — баланс клиента",
                      "<code>/bal @ник +5 бонус</code> — начислить $5",
                      "<code>/bal @ник -2 списание</code> — списать $2"),
                panel(f"{e('percent')} <b>Скидки</b>",
                      "<code>/disc @ник 4.1</code> — персональная скидка партнёру",
                      "<code>/disc @ник reset</code> — вернуть общую скидку"),
                panel(f"{e('admin')} <b>Сервис</b>",
                      "<code>/report</code> — отчёт за сегодня (каждое утро приходит сам)",
                      "<code>/backup</code> — бэкап базы прямо сейчас",
                      "<code>/bans</code> — забаненные IP · <code>/emoji</code> — пак эмодзи"),
                "<i>Вместо @ника можно Telegram ID. Клиенту приходит уведомление об изменении баланса.</i>")
    return text, kb(back("a:home"))


def left_str(sec: int) -> str:
    if sec >= 86400:
        return f"{sec // 86400} д {sec % 86400 // 3600} ч"
    if sec >= 3600:
        return f"{sec // 3600} ч {sec % 3600 // 60} мин"
    return f"{max(sec // 60, 1)} мин"


async def screen_bans() -> tuple[str, object]:
    rows = f2b.active()
    banned = [f"<code>{esc(k)}</code> · ещё {left_str(left)} · {esc(reason)}" for k, left, reason in rows[:15]]
    if len(rows) > 15:
        banned.append(f"…и ещё {len(rows) - 15}")
    text = join(title("lock", "Заблокированные IP"),
                panel(*banned, expandable=len(banned) > 5) if banned else panel(f"{e('ok')} Сейчас никто не заблокирован."),
                "<i>Бан получает IP, который ищет уязвимости, подбирает API-ключи или долбит лимиты. "
                "На сервере эти IP закрываются и в брандмауэре Windows.</i>",
                panel("<code>/unban IP</code> — снять бан", "<code>/ban IP 24</code> — забанить вручную на 24 ч"))
    return text, kb([btn("Обновить", cb="a:bans", icon="refresh"), *back("a:home")])


SCREENS = {"m:home": screen_home, "m:bal": screen_balance, "m:orders": screen_orders}
ADMIN_SCREENS = {"a:home": lambda uid: screen_admin(uid), "a:stats": lambda uid: screen_stats(),
                 "a:nx": lambda uid: screen_nx(), "a:work": lambda uid: screen_work(),
                 "a:last": lambda uid: screen_last(), "a:bans": lambda uid: screen_bans()}


async def _send_banner(message: Message, caption: str, markup) -> None:
    global _banner_id
    if _banner_id:
        try:
            await message.answer_photo(_banner_id, caption=caption, reply_markup=markup)
            return
        except TelegramBadRequest as err:
            if "file" not in str(err).lower():
                raise
            _banner_id = None  # the id belongs to another bot token: upload the picture again
    sent = await message.answer_photo(FSInputFile(BANNER), caption=caption, reply_markup=markup)
    if getattr(sent, "photo", None):
        _banner_id = sent.photo[-1].file_id


async def show(message: Message, text: str, markup, *, edit: bool, banner: bool = False) -> None:
    """Send a screen, or (edit=True) put it in place of the message whose button was pressed.
    banner=True: the screen is the SupplierHub picture with the text as its caption. A photo message can't become
    a text one and back, so switching between the two kinds replaces the message instead of editing it."""
    photo = banner and BANNER.exists() and tgui.visible_len(text) <= CAPTION_MAX

    async def put(text: str, markup) -> None:
        if edit and bool(message.photo) == photo:
            if photo:
                await message.edit_caption(caption=text, reply_markup=markup)
            else:
                await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
            return
        if edit:
            with suppress(Exception):  # older than 48 h can't be deleted: then the old one just stays
                await message.delete()
        if photo:
            await _send_banner(message, text, markup)
        else:
            await message.answer(text, reply_markup=markup, disable_web_page_preview=True)

    try:
        await put(text, markup)
    except Exception as err:  # noqa: BLE001
        if "not modified" in str(err):
            return
        if notify._emoji_problem(err):
            tgui.disable_custom()
            await put(tgui.strip_custom(text), tgui.plain_markup(markup))
            return
        raise


# ------------------------------------------------------------------ commands & callbacks

@router.message(CommandStart(deep_link=True))
async def start_deeplink(message: Message, command: CommandObject):
    arg = command.args or ""
    if arg.startswith("login_"):
        await handle_login(message, arg[6:])
    else:
        await start_plain(message)


@router.message(CommandStart())
async def start_plain(message: Message):
    await show(message, *await screen_home(message.from_user.id), edit=False, banner=True)


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    await show(message, *await screen_home(message.from_user.id), edit=False, banner=True)


@router.message(Command("balance"))
async def cmd_balance(message: Message):
    await show(message, *await screen_balance(message.from_user.id), edit=False, banner=True)


@router.message(Command("orders"))
async def cmd_orders(message: Message):
    await show(message, *await screen_orders(message.from_user.id), edit=False, banner=True)


@router.message(Command("stats", "admin"))
async def cmd_admin(message: Message):
    if is_admin(message.from_user.id):
        await show(message, *await screen_admin(message.from_user.id), edit=False)


@router.callback_query(F.data.in_(SCREENS.keys()))
async def cb_screen(cb: CallbackQuery):
    await show(cb.message, *await SCREENS[cb.data](cb.from_user.id), edit=True, banner=True)
    await cb.answer()


@router.callback_query(F.data.startswith("a:"))
async def cb_admin_screen(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    if cb.data == "a:help":
        await show(cb.message, *screen_help(), edit=True)
    elif cb.data in ADMIN_SCREENS:
        await show(cb.message, *await ADMIN_SCREENS[cb.data](cb.from_user.id), edit=True)
    await cb.answer()


# ------------------------------------------------------------------ site login

async def handle_login(message: Message, tok: str):
    async with session_scope() as s:
        req = await s.get(LoginRequest, tok)
        if not req or req.status != "pending" or req.expires_at < utcnow():
            await message.answer(f"{e('clock')} Ссылка для входа устарела. Нажмите «Войти» на сайте ещё раз.")
            return
        choices, ua, ip = code_choices(req.code), req.user_agent, req.ip
    # all code buttons look the same on purpose: the colour must not hint the right one
    markup = kb([btn(c, cb=f"lc:{tok}:{c}") for c in choices],
                [btn("Это не я", cb=f"lx:{tok}", style=DANGER, icon="cancel")])
    await show(message, join(title("lock", "Вход на сайт"),
                             "Нажмите код, который сейчас показан на сайте.",
                             panel(f"{e('user')} {esc(short_ua(ua))}", f"{e('net')} IP {esc(ip)}"),
                             "Если вы не входили — нажмите «Это не я»."), markup, edit=False)


@router.callback_query(F.data.startswith("lc:"))
async def login_confirm(cb: CallbackQuery):
    _, tok, code = cb.data.split(":", 2)
    link_key = token(18)
    async with session_scope() as s:
        req = await s.get(LoginRequest, tok)
        if not req or req.status != "pending" or req.expires_at < utcnow():
            await cb.message.edit_text(f"{e('clock')} Ссылка для входа устарела. Нажмите «Войти» на сайте ещё раз.")
            return await cb.answer()
        if code != req.code:
            req.status = "cancelled"
            await cb.message.edit_text(f"{e('fail')} Код не совпал — вход отменён. Попробуйте ещё раз на сайте.")
            return await cb.answer()
        u = cb.from_user
        user = await upsert_tg_user(s, u.id, u.username, u.first_name, u.last_name)
        req.status, req.user_id, req.link_key_hash = "confirmed", user.id, sha256(link_key)
    await show(cb.message, join(title("ok", "Вход подтверждён"), panel("Возвращайтесь на сайт — вы уже вошли.")),
               kb([btn("Вернуться на сайт", url=site(f"/auth/tg?t={tok}&k={link_key}"), style=SUCCESS, icon="rocket")],
                  [btn("Меню", cb="m:home", icon="logo")]), edit=True)
    await cb.answer("Готово")


@router.callback_query(F.data.startswith("lx:"))
async def login_cancel(cb: CallbackQuery):
    tok = cb.data.split(":", 1)[1]
    async with session_scope() as s:
        req = await s.get(LoginRequest, tok)
        if req and req.status == "pending":
            req.status = "cancelled"
    await cb.message.edit_text(f"{e('shield')} Вход отменён. Никто не получил доступ к вашему аккаунту.")
    await cb.answer()


# ------------------------------------------------------------------ admin text commands

async def _find_user(s, who: str) -> User | None:
    who = who.lstrip("@")
    q = select(User).where(User.tg_id == int(who)) if re.fullmatch(r"-?\d+", who) else select(User).where(
        func.lower(User.username) == who.lower())
    return (await s.execute(q)).scalar_one_or_none()


@router.message(Command("bal"))
async def cmd_bal(message: Message, command: CommandObject):
    """/bal <tg_id|@username> [+5.5|-3 комментарий] — show or adjust a customer's balance."""
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split(maxsplit=2)
    if not parts:
        return await show(message, *screen_help(), edit=False)
    async with session_scope() as s:
        u = await _find_user(s, parts[0])
        if not u:
            return await message.answer(f"{e('question')} Пользователь не найден")
        if len(parts) == 1:
            return await message.answer(f"{e('user')} {esc(u.display_name)} (tg {u.tg_id})\n"
                                        f"{e('money')} Баланс: <b>${usd_str(u.balance_micro)}</b>\n"
                                        f"{e('percent')} Скидка: {discount_for(u)}%")
        try:
            delta = to_micro(D(parts[1]))
        except (InvalidOperation, ValueError):
            return await message.answer("Сумма в USD, например +5 или -2.5")
        comment = parts[2] if len(parts) > 2 else "корректировка админом"
        new = await change_balance(s, u.id, delta, "adjust", comment=comment, require_funds=delta < 0)
        name, uid = u.display_name, u.id
    if new is None:  # answer outside the transaction: it holds the database's write turn until it ends
        return await message.answer(f"{e('warn')} Недостаточно средств на балансе клиента")
    await message.answer(f"{e('ok')} {esc(name)}: баланс теперь <b>${usd_str(new)}</b>")
    await notify.notify_user(uid, f"{e('money')} <b>Баланс изменён</b>: {'+' if delta > 0 else '−'}${usd_str(abs(delta))}\n"
                                  f"{esc(comment)}\n\nТеперь на балансе: <b>${usd_str(new)}</b>",
                             kb([btn("Пополнить Steam", url=site("/"), style=PRIMARY, icon="rocket")]))


@router.message(Command("disc"))
async def cmd_disc(message: Message, command: CommandObject):
    """/disc <tg_id|@username> <процент|reset> — personal discount for a partner."""
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if len(parts) != 2:
        return await show(message, *screen_help(), edit=False)
    sd = await nervixy.supplier_discount()
    async with session_scope() as s:
        u = await _find_user(s, parts[0])
        if not u:
            return await message.answer(f"{e('question')} Пользователь не найден")
        if parts[1].lower() == "reset":
            u.discount = None
            return await message.answer(f"{e('ok')} {esc(u.display_name)}: общая скидка {settings.client_discount}%")
        try:
            d = D(parts[1].replace(",", "."))
        except InvalidOperation:
            return await message.answer("Процент числом, например 4.1")
        if d < 0 or d >= sd:
            return await message.answer(f"{e('warn')} Скидка должна быть от 0 до {sd}% (скидка поставщика), "
                                        "иначе работаем в минус")
        u.discount = d
        name = u.display_name
    await message.answer(f"{e('ok')} {esc(name)}: персональная скидка <b>{d}%</b> (ваша маржа {sd - d}%)")


@router.message(Command("bans"))
async def cmd_bans(message: Message):
    if is_admin(message.from_user.id):
        await show(message, *await screen_bans(), edit=False)


@router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject):
    """Admin: /promo · /promo new CODE 0.5%|$2 [uses] [7d] · /promo off CODE.   Client: /promo CODE (balance bonus)."""
    from sqlalchemy.exc import IntegrityError

    from . import promos
    from .models import Promo

    args = (command.args or "").split()
    uid = message.from_user.id
    if is_admin(uid) and (not args or args[0].lower() in ("new", "off", "list")):
        if args and args[0].lower() == "new":
            try:
                spec = promos.parse_new(args[1:])
                async with session_scope() as s:
                    s.add(Promo(created_by=uid, **spec))
            except promos.PromoError as err:
                return await message.answer(f"{e('warn')} {err}")
            except IntegrityError:
                return await message.answer(f"{e('warn')} Код <code>{esc(spec['code'])}</code> уже есть")
            limit = f"{spec['max_uses']} раз" if spec["max_uses"] else "без лимита"
            until = f"до {spec['expires_at'] + timedelta(hours=3):%d.%m %H:%M} МСК" if spec["expires_at"] else "бессрочно"
            kind = f"−{spec['value']}% к скидке на заказ" if spec["kind"] == "discount" else f"+${spec['value']} на баланс"
            return await message.answer(f"{e('ok')} Промокод <code>{spec['code']}</code>: {kind} · {limit} · {until}\n"
                                        "<i>Скидка складывается с обычной, но итог не больше скидки поставщика минус "
                                        f"{promos.MIN_MARGIN}% — в минус не уйдём.</i>")
        if args and args[0].lower() == "off":
            if len(args) < 2:
                return await message.answer("Формат: <code>/promo off КОД</code>")
            async with session_scope() as s:
                p = (await s.execute(select(Promo).where(Promo.code == promos.normalize(args[1])))).scalar_one_or_none()
                if p:
                    p.active = False
            return await message.answer(f"{e('ok')} Промокод <code>{esc(promos.normalize(args[1]))}</code> выключен"
                                        if p else f"{e('question')} Такого кода нет")
        async with session_scope() as s:
            rows = (await s.execute(select(Promo).order_by(Promo.id.desc()).limit(15))).scalars().all()
        lines = [f"<code>{p.code}</code> · {promos.label(p)} · {p.used}/{p.max_uses or '∞'}"
                 + (f" · до {p.expires_at + timedelta(hours=3):%d.%m}" if p.expires_at else "")
                 + ("" if p.active else " · выключен") for p in rows]
        return await message.answer("\n".join([title("gift", "Промокоды"),
                                               panel(*lines) if lines else "Промокодов пока нет.",
                                               panel("<code>/promo new SALE 0.5% 100 7d</code> — скидка −0.5%, 100 раз, 7 дней",
                                                     "<code>/promo new GIFT $2 50</code> — $2 на баланс, 50 раз",
                                                     "<code>/promo off SALE</code> — выключить")]))
    # a client
    if not args:
        return await message.answer("Отправьте <code>/promo КОД</code> — бонус зачислится на баланс. "
                                    "Промокод на скидку вводится на сайте при оформлении заказа.")
    u = await _user(uid)
    if not u:
        return await message.answer(f"{e('lock')} Сначала войдите на сайте через Telegram — потом активируйте промокод.")
    try:
        async with session_scope() as s:
            p, new = await promos.redeem_bonus(s, u.id, args[0])
    except promos.PromoError as err:
        return await message.answer(f"{e('warn')} {err}")
    await message.answer(f"{e('gift')} Промокод <code>{p.code}</code>: {promos.label(p)}\n"
                         f"{e('money')} На балансе: <b>${usd_str(new)}</b>")


@router.message(Command("report"))
async def cmd_report(message: Message):
    """/report — the morning report on demand: today so far (Moscow time)."""
    if not is_admin(message.from_user.id):
        return
    from datetime import datetime

    from .report import MSK, report_text

    now = utcnow()
    day = (now + MSK).date()
    since = datetime(day.year, day.month, day.day) - MSK
    await show(message, await report_text(since, now, f"сегодня до {now + MSK:%H:%M}"), None, edit=False)


@router.message(Command("backup"))
async def cmd_backup(message: Message):
    """/backup — make, check and send a database backup right now."""
    if not is_admin(message.from_user.id):
        return
    from .backup import backup_now

    await message.answer(f"{e('wait')} Делаю бэкап и проверяю базу…")
    info = await backup_now()
    if info.get("error"):
        return await message.answer(f"{e('warn')} {esc(info['error'])}")
    if not info["sent"]:
        await message.answer(f"{e('ok' if info['ok'] else 'alarm')} Бэкап сделан на диске сервера "
                             f"({'база цела' if info['ok'] else 'есть проблема — смотри алерт'}), "
                             "но в Telegram не отправлен: нет BACKUP_PASSWORD в .env.")


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    """/ban <ip> [часы] — block an IP by hand (default: the automatic ban length)."""
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if not parts:
        return await show(message, *await screen_bans(), edit=False)
    hours = None
    if len(parts) > 1:
        try:
            hours = float(parts[1].replace(",", "."))
        except ValueError:
            hours = 0
        if not 0 < hours <= 24 * 365:
            return await message.answer("Часы числом, например <code>/ban 1.2.3.4 24</code>")
    res = f2b.ban(parts[0], hours)
    if not res:
        return await message.answer(f"{e('warn')} Это не внешний IP-адрес или он в белом списке (F2B_WHITELIST)")
    key, sec = res
    await message.answer(f"{e('lock')} <code>{esc(key)}</code> заблокирован на {left_str(sec)}")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    """/unban <ip> — lift a ban (the firewall on the server follows within a minute)."""
    if not is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if not parts:
        return await show(message, *await screen_bans(), edit=False)
    key = f2b.unban(parts[0])
    await message.answer(f"{e('ok')} Бан снят: <code>{esc(key)}</code>" if key
                         else f"{e('question')} <code>{esc(parts[0])}</code> не был заблокирован")


@router.message(Command("emoji"))
async def cmd_emoji(message: Message):
    """/emoji — the owner's pack as the bot sees it: each emoji, its ID and where the bot uses it."""
    if not is_admin(message.from_user.id):
        return
    if not tgui.pack:
        return await message.answer(f"{e('question')} Пак не загружен"
                                    + (f" ({esc(tgui.pack_name)}) — смотри лог запуска бота" if tgui.pack_name
                                       else ": в app/tg_emoji.json нет ссылки «_pack»"))
    rows = [f'<tg-emoji emoji-id="{cid}">{esc(ch or "·")}</tg-emoji> <code>{cid}</code> · '
            + (", ".join(tgui.keys_of(cid)) or "<i>не используется</i>") for cid, ch in tgui.pack]
    for i in range(0, len(rows), 40):  # a message holds 4096 characters: ~40 rows
        head = title("sparkle", f"Пак {esc(tgui.pack_name)}: {len(rows)} эмодзи") if i == 0 else ""
        await show(message, join(head, panel(*rows[i:i + 40])), None, edit=False)
    await show(message, join("Поставить эмодзи на другое место: впиши его ID в <code>app/tg_emoji.json</code> "
                             "под нужным ключом и перезапусти сервис.",
                             panel(f"<b>Ключи:</b> {', '.join(sorted(tgui.EMOJI))}", expandable=True)), None, edit=False)


@router.callback_query(F.data.startswith("ad:"))
async def admin_order_action(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    _, action, oid = cb.data.split(":")
    msg = await (admin_retry(int(oid)) if action == "retry" else admin_refund(int(oid)))
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"{e('retry' if action == 'retry' else 'refund')} {esc(msg)}")
    await cb.answer()


@router.callback_query(F.data.startswith("cl:"))
async def admin_claim_action(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    _, action, cid = cb.data.split(":")
    msg = await monitor_mod.approve_claim(int(cid), action == "ok")
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"{e('claim')} Заявка #{cid}: {esc(msg)}")
    await cb.answer()


async def load_pack(bot: Bot) -> None:
    """Read the owner's emoji pack from Telegram (see app/tgui.py): new emoji in it are used without a code change."""
    if not tgui.pack_name:
        return
    try:
        pack = await bot.get_sticker_set(tgui.pack_name)
    except Exception as err:  # noqa: BLE001 - the hand-picked IDs from tg_emoji.json still work
        log.warning("emoji pack %s not loaded: %s", tgui.pack_name, err)
        return
    got = tgui.use_pack([(s.custom_emoji_id, s.emoji or "") for s in pack.stickers if s.custom_emoji_id])
    log.info("emoji pack %s: %d emoji; %s", tgui.pack_name, len(tgui.pack),
             f"also used by base emoji for: {', '.join(got)}" if got else "all used keys come from tg_emoji.json")


async def _telegram_heartbeat(make_request, bot, method):
    """Every answered Bot API call (long polling returns every few seconds) proves the link to Telegram is alive."""
    from . import health

    result = await make_request(bot, method)
    health.beat("telegram")
    return result


async def run_bot() -> None:
    global bot_username, _dp
    if not settings.bot_token:
        log.warning("BOT_TOKEN is not set — Telegram login and notifications are disabled")
        return
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(_telegram_heartbeat)
    me = await bot.get_me()
    bot_username = me.username
    notify.set_bot(bot)
    log.info("bot @%s started", bot_username)
    await load_pack(bot)
    await bot.set_my_commands([BotCommand(command="start", description="Меню")])
    if _dp is None:  # once per process: run_bot restarts after a crash, and a router can't be attached twice
        _dp = Dispatcher()
        _dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=False)
    await _dp.start_polling(bot, handle_signals=False)
