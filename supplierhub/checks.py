"""Проверки из настроек: токен Playerok и связь с Telegram."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from PlayerokAPI import (
    Account,
    AuthRequiredError,
    GraphQLError,
    PlayerokError,
    UnauthorizedError,
)
from PlayerokAPI.exceptions import TokenError

from .errors import ApiError

__all__ = ["TELEGRAM_TEST_TEXT", "fetch_account", "send_telegram_test"]

#: Сколько ждать ответа площадки или Telegram при проверке.
TIMEOUT = 20.0
TELEGRAM_TEST_TEXT = "✅ Проверка связи: SupplierHub настроен верно."

_TELEGRAM_API = "https://api.telegram.org"
_BAD_TOKEN = (
    "Playerok не принял токен. Возьмите свежий cookie token из браузера "
    "(DevTools → Application → Cookies → playerok.com)."
)


async def fetch_account(token: str, proxy: str | None, *, timeout: float = TIMEOUT) -> dict:
    """Войти с токеном и вернуть {username, balance}; ApiError с понятным текстом."""
    try:
        async with asyncio.timeout(timeout):
            async with Account(token=token, proxy=proxy or None) as account:
                me = await account.get_me()
    except TimeoutError:
        raise ApiError(
            f"Playerok не ответил за {timeout:g} с. Проверьте интернет или прокси."
        ) from None
    except (UnauthorizedError, TokenError, AuthRequiredError):
        raise ApiError(_BAD_TOKEN) from None
    except GraphQLError as exc:
        if exc.code in ("UNAUTHENTICATED", "UNAUTHORIZED"):
            raise ApiError(_BAD_TOKEN) from None
        raise ApiError(f"Не удалось подключиться к Playerok: {exc}") from None
    except PlayerokError as exc:
        raise ApiError(f"Не удалось подключиться к Playerok: {exc}") from None
    except ValueError as exc:
        # httpx отвергает кривой адрес прокси ещё до запроса.
        raise ApiError(f"Не удалось подключиться к Playerok: {exc}") from None
    balance = me.balance.available if me.balance else 0.0
    return {"username": me.username, "balance": float(balance)}


async def send_telegram_test(
    bot_token: str,
    chat_ids: list[int],
    proxy: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    text: str = TELEGRAM_TEST_TEXT,
) -> None:
    """Отправить тестовое сообщение во все чаты; ApiError с причиной, если не вышло."""
    options: dict[str, Any] = {"timeout": TIMEOUT}
    if transport is not None:
        options["transport"] = transport
    elif proxy:
        options["proxy"] = proxy
    problems: list[str] = []
    try:
        async with httpx.AsyncClient(**options) as client:
            for chat_id in chat_ids:
                response = await client.post(
                    f"{_TELEGRAM_API}/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                )
                if response.status_code != 200:
                    problems.append(_explain(chat_id, response))
    except httpx.HTTPError as exc:
        detail = str(exc).replace(bot_token, "***") or type(exc).__name__
        raise ApiError(
            f"Не удалось связаться с Telegram: {detail}. Проверьте интернет или прокси."
        ) from None
    except ValueError as exc:
        raise ApiError(f"Неверный адрес прокси для Telegram: {exc}") from None
    if problems:
        raise ApiError(" ".join(dict.fromkeys(problems)))


def _explain(chat_id: int, response: httpx.Response) -> str:
    try:
        description = str(response.json().get("description") or "")
    except ValueError:
        description = ""
    lowered = description.casefold()
    if response.status_code in (401, 404):
        return "Telegram не принял токен бота — проверьте его у @BotFather."
    if "chat not found" in lowered:
        return f"Чат {chat_id} не найден: проверьте chat id и напишите своему боту /start."
    if response.status_code == 403:
        return f"Бот не может писать в чат {chat_id}: напишите ему /start или разблокируйте его."
    return f"Telegram отклонил сообщение для {chat_id}: {description or response.status_code}."
