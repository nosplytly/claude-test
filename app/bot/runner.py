"""Bot start-up. main.py runs run_bot() under a supervisor that restarts it after a crash."""
from __future__ import annotations

import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, CallbackQuery, ErrorEvent

from .. import notify
from ..config import settings
from . import admin, customer, login

log = logging.getLogger("sh.bot")


class _State:
    username: str | None = None  # @username of the running bot, for t.me login links


state = _State()


def build_dispatcher() -> Dispatcher:
    """A new Dispatcher with new routers every time: a router can belong to one dispatcher only, so reusing
    module-level routers made every restart after a crash fail with "Router is already attached"."""
    dp = Dispatcher()
    # order matters: login links before the plain /start; admin handlers, then «Нет доступа» for everyone else;
    # a button nobody handles (e.g. from a message sent by an older version) still gets its spinner stopped
    fallback = Router(name="fallback")
    fallback.callback_query.register(on_unknown_button)
    dp.include_routers(login.router(), customer.router(), admin.router(), admin.denied_router(), fallback)
    dp.errors.register(on_error)
    return dp


async def on_unknown_button(cb: CallbackQuery) -> None:
    await cb.answer()


async def on_error(event: ErrorEvent) -> None:
    log.error("bot update failed: %r", event.exception, exc_info=event.exception)
    if cb := event.update.callback_query:  # stop the button's spinner and say so
        with suppress(Exception):
            await cb.answer("Что-то пошло не так, попробуйте ещё раз", show_alert=True)


async def run_bot() -> None:
    if not settings.bot_token:
        log.warning("BOT_TOKEN is not set — Telegram login and notifications are disabled")
        return
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
        state.username = me.username
        notify.set_bot(bot)
        log.info("bot @%s started", state.username)
        await bot.set_my_commands([BotCommand(command="start", description="Меню")])
        await bot.delete_webhook(drop_pending_updates=False)
        await build_dispatcher().start_polling(bot, handle_signals=False)
    finally:
        await bot.session.close()
