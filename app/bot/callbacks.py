"""The bot's button protocol: every callback_data put on an inline button, typed.

The wire format is unchanged ("m:home", "a:nx", "ad:retry:42", "cl:ok:7", "lc:<token>:<code>", "lx:<token>"),
so buttons in messages sent before an update keep working. A typo in a screen or action name now fails
right where the button is built instead of producing a dead button.
"""
from __future__ import annotations

from typing import Literal

from aiogram.filters.callback_data import CallbackData


class Menu(CallbackData, prefix="m"):
    """Customer screens (app.bot.customer)."""
    screen: Literal["home", "bal", "orders"]


class Admin(CallbackData, prefix="a"):
    """Admin screens (app.bot.admin)."""
    screen: Literal["home", "stats", "nx", "work", "last", "help"]


class OrderAction(CallbackData, prefix="ad"):
    """Admin decision on an order the supplier didn't confirm."""
    action: Literal["retry", "refund"]
    order_id: int


class ClaimAction(CallbackData, prefix="cl"):
    """Admin decision on a customer's "this payment is mine" claim."""
    action: Literal["ok", "no"]
    claim_id: int


class LoginCode(CallbackData, prefix="lc"):
    """One of the three codes offered when logging in on the site."""
    token: str
    code: str


class LoginCancel(CallbackData, prefix="lx"):
    """«Это не я» under a login request."""
    token: str
