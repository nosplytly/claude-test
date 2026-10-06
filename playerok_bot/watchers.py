"""Источники событий: сделки (опрос + WebSocket) и сообщения в чатах."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from PlayerokAPI import (
    Chat,
    ChatEvent,
    ChatMessage,
    ChatType,
    Deal,
    DealEvent,
    EventType,
    GraphQLError,
    ItemDealDirection,
    ItemDealStatus,
    MessageEvent,
    UnauthorizedError,
)

from .delivery import SECTION as DELIVERIES
from .delivery import AutoDelivery
from .relist import AutoRelist
from .storage import State
from .telegram import Notifier, esc, link

if TYPE_CHECKING:
    from PlayerokAPI import Account

__all__ = ["DealWatcher", "watch_messages"]

logger = logging.getLogger(__name__)

#: Сколько последних продаж смотреть за один опрос.
_SWEEP_SIZE = 25


class DealWatcher:
    """Следит за продажами и передаёт оплаченные в автовыдачу.

    Основной источник — опрос списка продаж раз в `interval` секунд: он не
    пропустит сделку, даже если WebSocket отвалился. WebSocket лишь ускоряет
    реакцию. Обработка сделки идемпотентна, поэтому оба источника могут
    прислать одну и ту же сделку.
    """

    def __init__(
        self,
        account: Account,
        state: State,
        notifier: Notifier,
        delivery: AutoDelivery,
        relist: AutoRelist,
        *,
        first_run: bool,
    ) -> None:
        self._account = account
        self._state = state
        self._notifier = notifier
        self._delivery = delivery
        self._relist = relist
        self._first_run = first_run
        self._known: dict[str, tuple[ItemDealStatus | None, bool]] = {}
        self._seeded = asyncio.Event()
        self._auth_alerted = False
        #: Последние продажи из последнего опроса, новые сверху.
        self.recent: list[Deal] = []

    async def run_polling(self, interval: float) -> None:
        while True:
            try:
                await self.sweep(seeding=not self._seeded.is_set())
                self._seeded.set()
                self._auth_alerted = False
            except Exception as exc:
                if not _is_auth_error(exc):
                    logger.warning("Опрос сделок не удался: %s", exc)
                elif not self._auth_alerted:
                    logger.error("Playerok отклонил токен: %s", exc)
                    self._auth_alerted = True
                    await self._notifier.send(
                        "🔑 Playerok отклонил токен. Возьмите новый cookie <code>token</code> "
                        "из браузера, впишите в config.toml и перезапустите бота."
                    )
            await asyncio.sleep(interval)

    async def run_websocket(self) -> None:
        # Сначала опрос запоминает текущие сделки, иначе на первом запуске
        # WebSocket мог бы выдать товар по сделке, оплаченной до бота.
        await self._seeded.wait()
        try:
            listener = self._account.listener(events=[EventType.NEW_DEAL, EventType.DEAL_UPDATED])
            async for event in listener.events():
                if isinstance(event, DealEvent) and event.deal is not None:
                    try:
                        await self.handle(event.deal)
                    except Exception:
                        logger.exception("Сбой обработки сделки %s", event.deal.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("WebSocket недоступен, работаю только на опросе: %s", exc)

    async def sweep(self, *, seeding: bool = False) -> None:
        page = await self._account.deals.search(
            filter={"direction": ItemDealDirection.OUT.value}, first=_SWEEP_SIZE
        )
        preexisting: list[Deal] = []
        # Площадка отдаёт новые сверху — обрабатываем от старых к новым.
        for deal in reversed(page.items):
            if seeding and self._first_run and deal.status == ItemDealStatus.PAID:
                preexisting.append(deal)
            await self.handle(deal, seeding=seeding)
        self.recent = list(page.items)
        if preexisting:
            links = "\n".join(f"• {_ref(deal)}" for deal in preexisting)
            await self._notifier.send(
                f"ℹ️ Первый запуск: {len(preexisting)} оплаченных сделок уже ждали выдачи. "
                f"Бот их не трогает — выдайте вручную:\n{links}"
            )

    async def handle(self, deal: Deal, *, seeding: bool = False) -> None:
        if deal.direction != ItemDealDirection.OUT:
            return
        prev = self._known.get(deal.id)
        self._known[deal.id] = (deal.status, deal.has_problem)
        paid = deal.status == ItemDealStatus.PAID
        # Продажа ещё не подтверждена покупателем — лот после неё можно перевыставлять.
        recent_sale = deal.status in (ItemDealStatus.PAID, ItemDealStatus.SENT)

        if seeding and self._first_run:
            if paid:
                self._delivery.skip(deal, "оплачена до первого запуска бота")
            if recent_sale:
                self._relist.skip(deal)
            return
        if seeding:
            if paid and self._state.get(DELIVERIES, deal.id) is None:
                await self._notifier.send(_new_sale(deal) + "\n(оплачена, пока бот был выключен)")
        else:
            await self._notify_changes(deal, prev)

        if paid:
            await self._delivery.process(deal)
        if recent_sale:
            await self._relist.after_sale(deal)

    async def _notify_changes(
        self, deal: Deal, prev: tuple[ItemDealStatus | None, bool] | None
    ) -> None:
        prev_status, prev_problem = prev or (None, False)
        if deal.status != prev_status:
            if deal.status == ItemDealStatus.PAID:
                await self._notifier.send(_new_sale(deal))
            elif deal.status == ItemDealStatus.CONFIRMED:
                await self._notifier.send(
                    f"✅ Покупатель подтвердил сделку {_ref(deal)} — +{_price(deal)}"
                )
            elif deal.status == ItemDealStatus.ROLLED_BACK:
                await self._notifier.send(
                    f"↩️ Сделка {_ref(deal)} отменена, деньги возвращены покупателю"
                )
        if deal.has_problem and not prev_problem:
            await self._notifier.send(f"⚠️ Покупатель сообщил о проблеме по сделке {_ref(deal)}")


async def watch_messages(account: Account, me_id: str, notifier: Notifier, interval: float) -> None:
    """Пересылать в Telegram новые сообщения покупателей."""
    chats: dict[str, Chat] = {}
    try:
        async for event in account.polling(interval=interval).events():
            # Поллинг присылает обновлённый чат прямо перед его новыми сообщениями.
            if isinstance(event, ChatEvent) and event.chat is not None:
                chats[event.chat.id] = event.chat
            elif isinstance(event, MessageEvent) and event.message is not None:
                try:
                    await _forward_message(event.message, chats, me_id, notifier)
                except Exception:
                    logger.exception("Не удалось переслать сообщение %s", event.message.id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Слежение за сообщениями остановлено")


async def _forward_message(
    message: ChatMessage, chats: dict[str, Chat], me_id: str, notifier: Notifier
) -> None:
    if message.is_system or message.author_id == me_id:
        return
    chat = chats.get(message.chat_id or "")
    if chat is not None and chat.type == ChatType.NOTIFICATIONS:
        return
    author = (message.user.username if message.user else None) or "покупатель"
    text = message.text or (
        "[картинка]" if message.images or message.image_links else "[без текста]"
    )
    footer = (
        "\n" + link(f"https://playerok.com/chats/{message.chat_id}", "Открыть чат")
        if message.chat_id
        else ""
    )
    await notifier.send(f"💬 <b>{esc(author)}</b>: {esc(text)}{footer}")


def _is_auth_error(exc: Exception) -> bool:
    if isinstance(exc, UnauthorizedError):
        return True
    return isinstance(exc, GraphQLError) and exc.code in ("UNAUTHENTICATED", "UNAUTHORIZED")


def _price(deal: Deal) -> str:
    value = deal.item.price if deal.item else 0.0
    return f"{value:.2f}".removesuffix(".00") + " ₽"


def _ref(deal: Deal) -> str:
    return link(deal.url, f"«{(deal.item.name if deal.item else None) or 'товар'}»")


def _new_sale(deal: Deal) -> str:
    buyer = (deal.user.username if deal.user else None) or "—"
    return f"🛒 Новая продажа: {_ref(deal)} за {_price(deal)}\nПокупатель: {esc(buyer)}"
