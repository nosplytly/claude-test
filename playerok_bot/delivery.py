"""Автовыдача товара по оплаченным продажам."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from PlayerokAPI import Deal

from .config import DeliveryConfig, DeliveryRule
from .stock import Stock
from .storage import State, now_iso
from .telegram import Notifier, esc, link

if TYPE_CHECKING:
    from PlayerokAPI import Account

__all__ = ["AutoDelivery", "render_message"]

logger = logging.getLogger(__name__)

SECTION = "deliveries"

#: Товар взят со склада, но покупатель его ещё не получил.
RESERVED = "reserved"
#: Склад пуст — выдадим, когда файл пополнят.
NO_STOCK = "no_stock"
DELIVERED = "delivered"
#: Отправить не вышло за MAX_ATTEMPTS попыток — нужна ручная выдача.
FAILED = "failed"
NO_RULE = "no_rule"
#: Сделка оплачена до первого запуска бота — её не трогаем.
SKIPPED = "skipped"
FINAL = frozenset({DELIVERED, FAILED, NO_RULE, SKIPPED})

MAX_ATTEMPTS = 5

_PLACEHOLDER = re.compile(r"\{(product|buyer|item)\}")


def render_message(template: str, products: list[str], deal: Deal) -> str:
    """Подставить {product}, {buyer} и {item} за один проход.

    Один проход важен: фигурные скобки внутри самого товара не должны
    приниматься за новые подстановки.
    """
    values = {
        "product": "\n".join(products),
        "buyer": (deal.user.username if deal.user else None) or "покупатель",
        "item": (deal.item.name if deal.item else None) or "товар",
    }
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


class AutoDelivery:
    def __init__(
        self,
        account: Account,
        config: DeliveryConfig,
        state: State,
        notifier: Notifier,
    ) -> None:
        self._account = account
        self._config = config
        self._state = state
        self._notifier = notifier
        self._lock = asyncio.Lock()

    def can_sell(self, item_name: str | None) -> bool:
        """Хватит ли товара на складе ещё на одну продажу этого лота."""
        if not self._config.enabled:
            return True
        rule = self._config.find_rule(item_name)
        if rule is None or rule.stock_file is None:
            return True
        return Stock(rule.stock_file).count() >= rule.products_per_sale

    def skip(self, deal: Deal, reason: str) -> None:
        """Пометить сделку как не требующую выдачи."""
        self._state.put(SECTION, deal.id, status=SKIPPED, reason=reason, item=_name(deal))

    async def process(self, deal: Deal) -> None:
        """Выдать товар по оплаченной продаже. Повторный вызов безопасен."""
        if not self._config.enabled:
            return
        async with self._lock:
            await self._process(deal)

    async def _process(self, deal: Deal) -> None:
        entry = self._state.get(SECTION, deal.id) or {}
        if entry.get("status") in FINAL:
            return

        rule = self._config.find_rule(_name(deal))
        if rule is None:
            self._state.put(SECTION, deal.id, status=NO_RULE, item=_name(deal))
            await self._notifier.send(
                f"ℹ️ Для лота {_ref(deal)} нет правила автовыдачи — выдайте товар вручную."
            )
            return

        if entry.get("status") == RESERVED:
            products = list(entry.get("products") or [])
        else:
            reserved = await self._reserve(deal, rule, entry.get("status"))
            if reserved is None:
                return
            products = reserved
        await self._send(deal, rule, products)

    async def _reserve(
        self, deal: Deal, rule: DeliveryRule, status: str | None
    ) -> list[str] | None:
        products: list[str] = []
        if rule.stock_file is not None:
            taken = await Stock(rule.stock_file).take(rule.products_per_sale)
            if taken is None:
                if status != NO_STOCK:
                    await self._notifier.send(
                        f"❗ Закончился товар в <code>{esc(rule.stock_file.name)}</code>: "
                        f"сделка {_ref(deal)} ждёт выдачи. Пополните файл — бот выдаст "
                        "товар при следующей проверке."
                    )
                self._state.put(SECTION, deal.id, status=NO_STOCK, item=_name(deal))
                return None
            products = taken
        # Сначала запоминаем выданное, потом отправляем: если бот упадёт между
        # этими шагами, после перезапуска уйдёт тот же товар, а не новый.
        self._state.put(SECTION, deal.id, status=RESERVED, item=_name(deal), products=products)
        return products

    async def _send(self, deal: Deal, rule: DeliveryRule, products: list[str]) -> None:
        try:
            chat_id = deal.chat_id or (await self._account.deals.get(deal.id)).chat_id
            if not chat_id:
                raise RuntimeError("у сделки нет чата")
            await self._account.chats.send(chat_id, render_message(rule.message, products, deal))
        except Exception as exc:
            await self._send_failed(deal, products, exc)
            return

        self._state.put(SECTION, deal.id, status=DELIVERED, delivered_at=now_iso(), error=None)
        logger.info("Сделка %s: товар выдан", deal.id)

        text = f"📦 Товар выдан: {_ref(deal)} → {esc(_buyer(deal))}"
        if rule.stock_file is not None:
            left = Stock(rule.stock_file).count()
            text += f"\nОсталось в <code>{esc(rule.stock_file.name)}</code>: {left}"
            if left <= rule.low_stock_alert:
                text += " — 📉 пора пополнить"
        await self._notifier.send(text)

        if self._config.mark_sent:
            try:
                await self._account.deals.update(deal.id, {"status": "SENT"})
            except Exception as exc:
                logger.warning("Сделка %s: не удалось отметить отправку: %s", deal.id, exc)
                await self._notifier.send(
                    f"⚠️ Товар по сделке {_ref(deal)} выдан, но отметить сделку "
                    f"выполненной не вышло: {esc(exc)}. Подтвердите вручную."
                )

    async def _send_failed(self, deal: Deal, products: list[str], exc: Exception) -> None:
        entry = self._state.get(SECTION, deal.id) or {}
        attempts = int(entry.get("attempts") or 0) + 1
        logger.warning("Сделка %s: попытка выдачи %s не удалась: %s", deal.id, attempts, exc)
        if attempts >= MAX_ATTEMPTS:
            self._state.put(SECTION, deal.id, status=FAILED, attempts=attempts, error=str(exc))
            manual = (
                "\nТовар для ручной выдачи:\n<code>" + esc("\n".join(products)) + "</code>"
                if products
                else ""
            )
            await self._notifier.send(
                f"❌ Не удалось выдать товар по сделке {_ref(deal)} за {attempts} попыток: "
                f"{esc(exc)}. Выдайте вручную.{manual}"
            )
            return
        self._state.put(SECTION, deal.id, attempts=attempts, error=str(exc))
        if attempts == 1:
            await self._notifier.send(
                f"⚠️ Ошибка выдачи по сделке {_ref(deal)}: {esc(exc)}. "
                "Повторю при следующей проверке."
            )


def _name(deal: Deal) -> str | None:
    return deal.item.name if deal.item else None


def _buyer(deal: Deal) -> str:
    return (deal.user.username if deal.user else None) or "покупатель"


def _ref(deal: Deal) -> str:
    return link(deal.url, f"«{_name(deal) or 'товар'}»")
