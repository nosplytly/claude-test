"""Уведомитель, который кроме Telegram пишет события в ленту окна."""

from __future__ import annotations

import logging

import httpx

from playerok_bot.config import TelegramConfig
from playerok_bot.telegram import Notifier

from .feed import EventFeed

__all__ = ["FeedNotifier"]

logger = logging.getLogger(__name__)


class FeedNotifier(Notifier):
    """Каждое уведомление бота попадает в ленту, затем уходит как обычно."""

    def __init__(
        self,
        config: TelegramConfig,
        feed: EventFeed,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(config, client=client)
        self._feed = feed

    async def send(self, text: str) -> bool:
        try:
            self._feed.add_message(text)
        except Exception:
            logger.exception("Не удалось добавить событие в ленту")
        return await super().send(text)
