"""Customer side of the bot: main menu, balance, orders."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from ..config import settings
from ..db import session_scope
from ..money import usd_str
from ..orders import fmt_amount
from ..tgui import PRIMARY, SUCCESS, btn, e, esc, kb, line, site
from . import queries as q
from .callbacks import Admin, Menu
from .common import KIND, STATUS, Screen, back, is_admin, msk, show


def _not_logged_in(hint: str = "") -> Screen:
    return Screen(f"{e('lock')} Вы ещё не входили на сайт.{hint}",
                  kb([btn("Открыть сайт", url=site("/"), style=PRIMARY, icon="rocket")], back()))


# ------------------------------------------------------------------ screens

async def home(tg_id: int) -> Screen:
    async with session_scope() as s:
        u = await q.user_by_tg(s, tg_id)
    hello = f"{e('hello')} Привет, {esc(u.first_name or u.display_name)}!\n\n" if u else ""
    text = f"{hello}{e('logo')} <b>SupplierHub</b> — пополнение Steam криптой\n\n"
    text += (f"{e('wallet')} Ваш баланс: <b>${usd_str(u.balance_micro)}</b>" if u
             else "Войдите на сайте через Telegram — и здесь появятся ваши заказы и баланс.")
    rows = [
        [btn("Пополнить Steam", url=site("/"), style=PRIMARY, icon="rocket")],
        [btn("Баланс", cb=Menu(screen="bal"), style=SUCCESS, icon="money"),
         btn("Мои заказы", cb=Menu(screen="orders"), icon="orders")],
        [btn("Пополнить баланс", url=site("/#/deposit"), style=SUCCESS, icon="plus"),
         btn("Поддержка", url=settings.support_url, icon="support") if settings.support_url else None],
    ]
    if is_admin(tg_id):
        rows.append([btn("Админ-панель", cb=Admin(screen="home"), style=PRIMARY, icon="admin")])
    return Screen(text, kb(*rows))


async def balance(tg_id: int) -> Screen:
    async with session_scope() as s:
        u = await q.user_by_tg(s, tg_id)
        entries = await q.recent_ledger(s, u.id) if u else []
    if not u:
        return _not_logged_in("\nНажмите «Войти» на сайте — бот пришлёт код.")
    text = f"{e('money')} <b>Баланс: ${usd_str(u.balance_micro)}</b>\n{line()}\n"
    if entries:
        text += "\n".join(f"{'+' if r.delta_micro > 0 else '−'}${usd_str(abs(r.delta_micro))} · {KIND.get(r.kind, r.kind)} · "
                          f"{msk(r.created_at)}" for r in entries)
        text += "\n\n<i>Баланс автоматически оплачивает следующие заказы.</i>"
    else:
        text += "Движений по балансу пока нет."
    return Screen(text, kb([btn("Пополнить баланс", url=site("/#/deposit"), style=SUCCESS, icon="plus")],
                           [btn("Обновить", cb=Menu(screen="bal"), icon="refresh"), *back()]))


async def orders(tg_id: int) -> Screen:
    async with session_scope() as s:
        u = await q.user_by_tg(s, tg_id)
        rows = await q.recent_orders(s, u.id) if u else []
    if not u:
        return _not_logged_in()
    text = f"{e('orders')} <b>Мои заказы</b>\n{line()}\n"
    for o in rows:
        ek, label = STATUS.get(o.status, ("question", o.status))
        text += f"{e(ek)} <b>{fmt_amount(o.amount, o.currency)}</b> → <code>{esc(o.steam_login)}</code> · {label}\n"
    if not rows:
        text += "Заказов пока нет."
    return Screen(text, kb([btn("Пополнить Steam", url=site("/"), style=PRIMARY, icon="rocket")],
                           [btn("Обновить", cb=Menu(screen="orders"), icon="refresh"), *back()]))


SCREENS = {"home": home, "bal": balance, "orders": orders}


# ------------------------------------------------------------------ handlers

async def cmd_home(message: Message) -> None:
    await show(message, await home(message.from_user.id))


async def cmd_balance(message: Message) -> None:
    await show(message, await balance(message.from_user.id))


async def cmd_orders(message: Message) -> None:
    await show(message, await orders(message.from_user.id))


async def on_menu(cb: CallbackQuery, callback_data: Menu) -> None:
    await show(cb.message, await SCREENS[callback_data.screen](cb.from_user.id), edit=True)
    await cb.answer()


def router() -> Router:
    r = Router(name="customer")
    r.message.register(cmd_home, CommandStart())  # /start with a login link is taken by the login router first
    r.message.register(cmd_home, Command("menu"))
    r.message.register(cmd_balance, Command("balance"))
    r.message.register(cmd_orders, Command("orders"))
    r.callback_query.register(on_menu, Menu.filter(F.screen.in_(SCREENS)))
    return r
