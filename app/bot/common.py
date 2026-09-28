"""What all parts of the bot share: a screen and how it is shown, who is an admin, labels."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple

from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Filter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import tgui
from ..config import settings
from ..tgui import btn
from .callbacks import Menu

# order status -> (emoji key, label)
STATUS = {"awaiting_payment": ("wait", "ждёт оплату"), "paid": ("card", "оплачен"), "submitting": ("wait", "пополняется"),
          "queued": ("queue", "в очереди"), "uncertain": ("wait", "проверяется"), "processing": ("wait", "пополняется"),
          "delivered": ("ok", "готово"), "rejected": ("fail", "возврат на баланс"), "expired": ("clock", "истёк"),
          "cancelled": ("cancel", "отменён")}
# ledger entry kind -> label
KIND = {"deposit": "Пополнение", "order": "Заказ", "refund": "Возврат", "adjust": "Корректировка"}
MSK = timedelta(hours=3)  # times in the bot are shown in Moscow time


class Screen(NamedTuple):
    text: str
    markup: InlineKeyboardMarkup | None = None


async def show(message: Message, screen: Screen, *, edit: bool = False) -> None:
    """Send the screen as a new message, or (edit=True) put it in place of the message whose button was pressed."""
    async def put(text: str, markup: InlineKeyboardMarkup | None) -> None:
        if edit:
            await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        else:
            await message.answer(text, reply_markup=markup, disable_web_page_preview=True)

    try:
        await tgui.with_emoji_fallback(put, screen.text, screen.markup)
    except TelegramBadRequest as err:
        if "not modified" not in str(err):  # «Обновить» when nothing changed
            raise


def is_admin(tg_id: int | None) -> bool:
    return tg_id is not None and tg_id in settings.admin_ids


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return is_admin(event.from_user.id if event.from_user else None)


def back(to: CallbackData | None = None) -> list:
    return [btn("Назад", cb=to or Menu(screen="home"), icon="back")]


def msk(dt: datetime) -> str:
    return f"{dt + MSK:%d.%m %H:%M}"
