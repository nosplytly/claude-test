"""Admin side of the bot: panel (stats, Nervixy, work queue, last orders), /bal and /disc, buttons in admin alerts.

Everything in router() is admin-only through one router-level filter; denied_router() answers «Нет доступа»
to anyone else pressing an admin button. Admin commands from others are ignored silently.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import InvalidOperation

from aiogram import Router
from aiogram.filters import Command, CommandObject, or_f
from aiogram.types import CallbackQuery, Message

from .. import notify
from ..config import settings
from ..db import session_scope
from ..ledger import change_balance
from ..money import D, to_micro, usd_str
from ..nervixy import nervixy
from ..orders import admin_refund, admin_retry, discount_for, fmt_amount, reserved_cost_micro
from ..payments import monitor
from ..tgui import DANGER, PRIMARY, btn, e, esc, kb, line, site
from ..utils import utcnow
from . import queries as q
from .callbacks import Admin, ClaimAction, OrderAction
from .common import STATUS, IsAdmin, Screen, back, show

HOME = Admin(screen="home")


async def _supplier_account() -> dict | None:
    try:
        return await nervixy.account(max_age=60)
    except Exception:  # noqa: BLE001 - rate limit / network: the screen says "нет связи"
        return None


def _refresh(screen: str) -> list:
    return [btn("Обновить", cb=Admin(screen=screen), icon="refresh"), *back(HOME)]


# ------------------------------------------------------------------ screens

async def panel() -> Screen:
    async with session_scope() as s:
        day = await q.totals(s, utcnow() - timedelta(days=1))
        by = await q.status_counts(s, ("awaiting_payment", *q.NEED_ATTENTION, *q.IN_PROGRESS))
        claims = await q.pending_claims(s)
    acc = await _supplier_account()
    problems = sum(by.get(x, 0) for x in q.NEED_ATTENTION) + claims
    text = (f"{e('admin')} <b>Админ-панель</b>\n{line()}\n"
            f"{e('stats')} За 24 ч: <b>{day.orders}</b> заказов · оборот ${usd_str(day.turnover)} · "
            f"маржа <b>+${usd_str(day.margin)}</b>\n"
            f"{e('money')} Пополнений баланса: ${usd_str(day.deposits)}\n"
            f"{e('bank')} Nervixy: " + (f"<b>${acc['balance']}</b> (скидка {acc['discount']}%)" if acc else "нет связи") + "\n"
            f"{e('work')} В работе: {sum(by.get(x, 0) for x in q.IN_PROGRESS)} · "
            f"ждут оплату: {by.get('awaiting_payment', 0)}\n"
            + (f"{e('warn')} Требуют внимания: <b>{problems}</b>\n" if problems else f"{e('ok')} Проблем нет\n"))
    return Screen(text, kb(
        [btn("Статистика", cb=Admin(screen="stats"), style=PRIMARY, icon="stats"),
         btn("Nervixy", cb=Admin(screen="nx"), icon="bank")],
        [btn("Проблемы и очередь", cb=Admin(screen="work"), style=DANGER if problems else None,
             icon="warn" if problems else "work"),
         btn("Последние заказы", cb=Admin(screen="last"), icon="list")],
        [btn("Клиенты: баланс и скидки", cb=Admin(screen="help"), icon="user")],
        [btn("Обновить", cb=HOME, icon="refresh"), *back()],
    ))


async def stats() -> Screen:
    now = utcnow()
    periods = [("24 часа", now - timedelta(days=1)), ("7 дней", now - timedelta(days=7)),
               ("30 дней", now - timedelta(days=30)), ("Всё время", None)]
    async with session_scope() as s:
        rows = [(name, await q.totals(s, since)) for name, since in periods]
        users, balances = await q.clients(s)
    text = f"{e('stats')} <b>Статистика</b>\n{line()}\n"
    for name, t in rows:
        text += (f"<b>{name}</b>\n  {e('orders')} {t.orders} заказов · оборот ${usd_str(t.turnover)}\n"
                 f"  {e('margin')} маржа <b>+${usd_str(t.margin)}</b> · {e('money')} пополнений ${usd_str(t.deposits)}\n")
    text += f"\n{e('user')} Клиентов: {users} · на их балансах ${usd_str(balances)}"
    return Screen(text, kb(_refresh("stats")))


async def supplier() -> Screen:
    acc = await _supplier_account()
    async with session_scope() as s:
        reserved = await reserved_cost_micro(s)
    text = f"{e('bank')} <b>Nervixy</b>\n{line()}\n"
    if acc:
        free = to_micro(acc["balance"]) - reserved
        text += (f"Баланс: <b>${acc['balance']}</b>\nСкидка поставщика: {acc['discount']}%\n"
                 f"Зарезервировано под заказы: ${usd_str(reserved)}\n"
                 f"Свободно для новых заказов: <b>${usd_str(max(free, 0))}</b>\n"
                 f"Алерт при балансе ниже ${settings.nervixy_low_balance_usd}")
        if acc["balance"] < settings.nervixy_low_balance_usd:
            text += f"\n\n{e('low')} Пора пополнить (USDT внутренним переводом Bybit)."
    else:
        text += f"{e('net')} Нет связи с API: {esc(nervixy.last_error or 'лимит запросов, попробуйте через минуту')}"
    return Screen(text, kb(_refresh("nx")))


async def work() -> Screen:
    async with session_scope() as s:
        problem = await q.orders_with_status(s, q.NEED_ATTENTION)
        active = await q.orders_with_status(s, q.IN_PROGRESS)
        waiting = (await q.status_counts(s, ("awaiting_payment",))).get("awaiting_payment", 0)
        unmatched = await q.unmatched_transfers(s, utcnow() - timedelta(days=1))
        claims = await q.pending_claims(s)
    text = f"{e('work')} <b>Проблемы и очередь</b>\n{line()}\n"
    if problem:
        text += f"{e('warn')} <b>Требуют внимания</b>\n" + "".join(
            f"  {e(STATUS[o.status][0])} <code>{o.public_id}</code> · {fmt_amount(o.amount, o.currency)} · "
            f"{STATUS[o.status][1]}\n" for o in problem)
    text += (f"{e('wait')} Пополняются сейчас: {len(active)}\n" + "".join(
        f"  <code>{o.public_id}</code> · {fmt_amount(o.amount, o.currency)} → <code>{esc(o.steam_login)}</code>\n"
        for o in active))
    text += (f"{e('card')} Ждут оплату: {waiting}\n"
             f"{e('question')} Неопознанных платежей за сутки: {unmatched}\n"
             f"{e('claim')} Заявок по хэшу на проверке: {claims}")
    if not problem:
        text += f"\n\n{e('ok')} Всё идёт штатно."
    return Screen(text, kb(_refresh("work")))


async def last() -> Screen:
    async with session_scope() as s:
        rows = await q.last_orders(s)
    text = f"{e('list')} <b>Последние заказы</b>\n{line()}\n"
    for o, u in rows:
        ek, label = STATUS.get(o.status, ("question", o.status))
        margin = o.price_micro - (o.nervixy_paid_micro or o.cost_micro)
        text += (f"{e(ek)} <b>{fmt_amount(o.amount, o.currency)}</b> → <code>{esc(o.steam_login)}</code>\n"
                 f"   {esc(u.display_name)} · {label}" + (f" · маржа +${usd_str(margin)}" if o.status == "delivered" else "")
                 + "\n")
    if not rows:
        text += "Заказов пока нет."
    return Screen(text, kb(_refresh("last")))


async def clients_help() -> Screen:
    return Screen(f"{e('user')} <b>Клиенты: баланс и скидки</b>\n{line()}\n"
                  "Отправьте боту команду:\n\n"
                  "<code>/bal @ник</code> — баланс клиента\n"
                  "<code>/bal @ник +5 бонус</code> — начислить $5\n"
                  "<code>/bal @ник -2 списание</code> — списать $2\n\n"
                  "<code>/disc @ник 4.1</code> — персональная скидка партнёру\n"
                  "<code>/disc @ник reset</code> — вернуть общую скидку\n\n"
                  "<i>Вместо @ника можно Telegram ID. Клиенту приходит уведомление об изменении баланса.</i>",
                  kb(back(HOME)))


SCREENS = {"home": panel, "stats": stats, "nx": supplier, "work": work, "last": last, "help": clients_help}


# ------------------------------------------------------------------ handlers

async def cmd_panel(message: Message) -> None:
    await show(message, await panel())


async def on_screen(cb: CallbackQuery, callback_data: Admin) -> None:
    await show(cb.message, await SCREENS[callback_data.screen](), edit=True)
    await cb.answer()


async def cmd_bal(message: Message, command: CommandObject) -> None:
    """/bal <tg_id|@username> [+5.5|-3 комментарий] — show or adjust a customer's balance."""
    parts = (command.args or "").split(maxsplit=2)
    if not parts:
        return await show(message, await clients_help())
    async with session_scope() as s:
        u = await q.find_user(s, parts[0])
        if not u:
            return await show(message, Screen(f"{e('question')} Пользователь не найден"))
        if len(parts) == 1:
            return await show(message, Screen(f"{e('user')} {esc(u.display_name)} (tg {u.tg_id})\n"
                                              f"{e('money')} Баланс: <b>${usd_str(u.balance_micro)}</b>\n"
                                              f"{e('percent')} Скидка: {discount_for(u)}%"))
        try:
            delta = to_micro(D(parts[1]))
        except (InvalidOperation, ValueError):
            return await show(message, Screen("Сумма в USD, например +5 или -2.5"))
        comment = parts[2] if len(parts) > 2 else "корректировка админом"
        new = await change_balance(s, u.id, delta, "adjust", comment=comment, require_funds=delta < 0)
    if new is None:
        return await show(message, Screen(f"{e('warn')} Недостаточно средств на балансе клиента"))
    await show(message, Screen(f"{e('ok')} {esc(u.display_name)}: баланс теперь <b>${usd_str(new)}</b>"))
    await notify.notify_user(u.id, f"{e('money')} <b>Баланс изменён</b>: {'+' if delta > 0 else '−'}${usd_str(abs(delta))}\n"
                                   f"{esc(comment)}\n\nТеперь на балансе: <b>${usd_str(new)}</b>",
                             kb([btn("Пополнить Steam", url=site("/"), style=PRIMARY, icon="rocket")]))


async def cmd_disc(message: Message, command: CommandObject) -> None:
    """/disc <tg_id|@username> <процент|reset> — personal discount for a partner."""
    parts = (command.args or "").split()
    if len(parts) != 2:
        return await show(message, await clients_help())
    who, value = parts
    sd = await nervixy.supplier_discount()
    async with session_scope() as s:
        u = await q.find_user(s, who)
        if not u:
            return await show(message, Screen(f"{e('question')} Пользователь не найден"))
        if value.lower() == "reset":
            u.discount = None
            reply = f"{e('ok')} {esc(u.display_name)}: общая скидка {settings.client_discount}%"
        else:
            try:
                d = D(value.replace(",", "."))
            except InvalidOperation:
                return await show(message, Screen("Процент числом, например 4.1"))
            if d < 0 or d >= sd:
                return await show(message, Screen(f"{e('warn')} Скидка должна быть от 0 до {sd}% (скидка поставщика), "
                                                  "иначе работаем в минус"))
            u.discount = d
            reply = f"{e('ok')} {esc(u.display_name)}: персональная скидка <b>{d}%</b> (ваша маржа {sd - d}%)"
    await show(message, Screen(reply))


async def on_order_action(cb: CallbackQuery, callback_data: OrderAction) -> None:
    retry = callback_data.action == "retry"
    msg = await (admin_retry if retry else admin_refund)(callback_data.order_id)
    await cb.message.edit_reply_markup(reply_markup=None)
    await show(cb.message, Screen(f"{e('retry' if retry else 'refund')} {esc(msg)}"))
    await cb.answer()


async def on_claim_action(cb: CallbackQuery, callback_data: ClaimAction) -> None:
    msg = await monitor.approve_claim(callback_data.claim_id, callback_data.action == "ok")
    await cb.message.edit_reply_markup(reply_markup=None)
    await show(cb.message, Screen(f"{e('claim')} Заявка #{callback_data.claim_id}: {esc(msg)}"))
    await cb.answer()


async def no_access(cb: CallbackQuery) -> None:
    await cb.answer("Нет доступа", show_alert=True)


def router() -> Router:
    r = Router(name="admin")
    r.message.filter(IsAdmin())
    r.callback_query.filter(IsAdmin())
    r.message.register(cmd_panel, Command("stats", "admin"))
    r.message.register(cmd_bal, Command("bal"))
    r.message.register(cmd_disc, Command("disc"))
    r.callback_query.register(on_screen, Admin.filter())
    r.callback_query.register(on_order_action, OrderAction.filter())
    r.callback_query.register(on_claim_action, ClaimAction.filter())
    return r


def denied_router() -> Router:
    r = Router(name="admin-denied")
    r.callback_query.register(no_access, or_f(Admin.filter(), OrderAction.filter(), ClaimAction.filter()))
    return r
