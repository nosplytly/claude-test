"""Уведомления в Telegram через Bot API."""

from __future__ import annotations

import html
import logging

import httpx

from .config import TelegramConfig

__all__ = ["Notifier", "esc", "link"]

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org"
_LIMIT = 4096


def esc(value: object) -> str:
    """Экранировать значение для parse_mode=HTML."""
    return html.escape(str(value), quote=False)


def link(url: str, text: object) -> str:
    return f'<a href="{html.escape(url)}">{esc(text)}</a>'


class Notifier:
    """Шлёт сообщения во все chat_ids. Ошибки только логируются.

    Если Telegram выключен в конфиге, сообщения просто пишутся в лог.
    """

    def __init__(self, config: TelegramConfig, *, client: httpx.AsyncClient | None = None):
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=15.0, proxy=config.proxy)

    async def send(self, text: str) -> bool:
        """Отправить HTML-сообщение. True, если дошло до всех получателей."""
        logger.info("Уведомление: %s", text.replace("\n", " | "))
        if not self._config.enabled:
            return True
        if len(text) > _LIMIT:
            text = text[: _LIMIT - 1] + "…"
        url = f"{_API}/bot{self._config.bot_token}/sendMessage"
        ok = True
        for chat_id in self._config.chat_ids:
            try:
                response = await self._client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                if response.status_code != 200:
                    ok = False
                    logger.warning(
                        "Telegram отклонил сообщение для %s: %s %s",
                        chat_id,
                        response.status_code,
                        response.text[:300],
                    )
            except httpx.HTTPError as exc:
                ok = False
                logger.warning("Не удалось отправить в Telegram (%s): %s", chat_id, exc)
        return ok

    async def aclose(self) -> None:
        await self._client.aclose()
