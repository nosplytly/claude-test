"""Автовыдача товара по оплаченным продажам."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from PlayerokAPI import Deal

from .config import DeliveryConfig, DeliveryRule
from .stock import Stock, StockError
from .storage import State, now_iso
from .telegram import Notifier, esc, link

if TYPE_CHECKING:
    from PlayerokAPI import Account

__all__ = ["AutoDelivery", "DeliveryError", "mark_manual", "render_message"]

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
#: Продавец сам выдал товар или решил вопрос с покупателем — бот сделку не трогает.
MANUAL = "manual"
FINAL = frozenset({DELIVERED, FAILED, NO_RULE, SKIPPED, MANUAL})
#: Сделки, которые продавец может закрыть вручную («Требуют внимания»).
RESOLVABLE = frozenset({FAILED, NO_STOCK, NO_RULE})

MAX_ATTEMPTS = 5

_PLACEHOLDER = re.compile(r"\{(product|buyer|item)\}")
_DEAL_URL = "https://playerok.com/deal/{}"


class DeliveryError(Exception):
    """Действие с выдачей невозможно — сообщение показывается пользователю как есть."""


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

    def handles(self, item_name: str | None) -> bool:
        """Выдаёт ли бот этот лот сам (автовыдача включена и есть правило)."""
        return self._config.enabled and self._config.find_rule(item_name) is not None

    def skip(self, deal: Deal, reason: str) -> None:
        """Пометить сделку как не требующую выдачи."""
        self._state.put(SECTION, deal.id, status=SKIPPED, reason=reason, **_meta(deal))

    async def process(self, deal: Deal) -> None:
        """Выдать товар по оплаченной продаже. Повторный вызов безопасен."""
        if not self._config.enabled:
            return
        async with self._lock:
            await self._process(deal)

    async def mark_manual(self, deal_id: str) -> dict[str, object]:
        """Продавец решил вопрос сам: сделка уходит из «Требуют внимания» навсегда."""
        async with self._lock:
            return mark_manual(self._state, deal_id)

    async def retry(self, deal: Deal) -> str:
        """Выдать заново по просьбе продавца: «нет правила» (правило уже есть) или
        «ошибка выдачи» (тот же отложенный товар). Возвращает новый статус."""
        if not self._config.enabled:
            raise DeliveryError("Автовыдача выключена — включите её в настройках.")
        async with self._lock:
            entry = self._state.get(SECTION, deal.id) or {}
            status = entry.get("status")
            if status == NO_RULE:
                if self._config.find_rule(_name(deal)) is None:
                    raise DeliveryError(
                        "Для этого лота всё ещё нет правила автовыдачи — создайте его."
                    )
                self._state.put(SECTION, deal.id, status=None, attempts=0, error=None)
            elif status == FAILED:
                self._state.put(SECTION, deal.id, status=RESERVED, attempts=0, error=None)
            else:
                raise DeliveryError("Эту сделку повторно выдать нельзя.")
            await self._process(deal)
            return str((self._state.get(SECTION, deal.id) or {}).get("status") or "")

    async def _process(self, deal: Deal) -> None:
        entry = self._state.get(SECTION, deal.id) or {}
        if entry.get("status") in FINAL:
            return

        rule = self._config.find_rule(_name(deal))
        if rule is None:
            self._state.put(SECTION, deal.id, status=NO_RULE, **_meta(deal))
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
        stock: Stock | None = None
        #: Строки, убранные со склада как уже выданные (о них — уведомление).
        dropped: list[str] = []
        used: dict[str, str] = {}
        if rule.stock_file is not None:
            stock = Stock(rule.stock_file)
            used = self._delivered_products(deal.id)
            taken, dropped = stock.take_unique(rule.products_per_sale, used.__contains__)
            if taken is None:
                if dropped:
                    await self._report_reused(rule, dropped, used)
                self._state.put(SECTION, deal.id, status=NO_STOCK, **_meta(deal))
                if status != NO_STOCK:
                    await self._notifier.send(
                        f"❗ Закончился товар в <code>{esc(rule.stock_file.name)}</code>: "
                        f"сделка {_ref(deal)} ждёт выдачи. Пополните файл — бот выдаст "
                        "товар при следующей проверке."
                    )
                return None
            products = taken
        # Сначала запоминаем выданное, потом отправляем: если бот упадёт между
        # этими шагами, после перезапуска уйдёт тот же товар, а не новый.
        # Товар уходит покупателю, только когда запись уже на диске. Между
        # take_unique и этой записью нет ни одного await: остановка бота на
        # медленном уведомлении не должна унести забранный со склада товар.
        try:
            self._state.put(SECTION, deal.id, status=RESERVED, products=products, **_meta(deal))
        except BaseException:
            if stock is not None:
                await self._return_to_stock(stock, deal, products)
            raise
        finally:
            if dropped:
                await self._report_reused(rule, dropped, used)
        return products

    async def _return_to_stock(self, stock: Stock, deal: Deal, products: list[str]) -> None:
        """Записать резерв не вышло — вернуть товар на склад, чтобы он не пропал."""
        try:
            stock.put_back(products)
        except (OSError, StockError) as exc:
            logger.error("Сделка %s: не удалось вернуть товар на склад: %s", deal.id, exc)
            await self._notifier.send(
                f"❌ Сделка {_ref(deal)}: не удалось ни сохранить выдачу, ни вернуть товар "
                f"в <code>{esc(stock.path.name)}</code> ({esc(exc)}). Верните строки в файл "
                "вручную:\n<code>" + esc("\n".join(products)) + "</code>"
            )
        else:
            logger.warning("Сделка %s: выдача не сохранена, товар возвращён на склад", deal.id)

    def _delivered_products(self, deal_id: str) -> dict[str, str]:
        """Товары, которые уже уходили покупателям: товар → сделка."""
        used: dict[str, str] = {}
        for other_id, entry in self._state.entries(SECTION):
            products = entry.get("products") if other_id != deal_id else None
            if isinstance(products, list):
                for product in products:
                    if isinstance(product, str):
                        used.setdefault(product, other_id)
        return used

    async def _report_reused(
        self, rule: DeliveryRule, dropped: list[str], used: dict[str, str]
    ) -> None:
        deals = list(dict.fromkeys(used[product] for product in dropped if product in used))
        name = rule.stock_file.name if rule.stock_file else "склада"
        logger.warning("%s: убрано %s уже выданных или повторяющихся строк", name, len(dropped))
        refs = ", ".join(link(_DEAL_URL.format(deal_id), deal_id) for deal_id in deals[:5])
        where = f" (выдавались по сделкам: {refs})" if refs else ""
        await self._notifier.send(
            f"⚠️ В <code>{esc(name)}</code> нашлись строки, которые уже выдавались или "
            f"повторяются{where}: {len(dropped)} шт. Бот убрал их со склада и не выдаёт повторно. "
            "Так бывает, если файл был открыт в Блокноте, пока бот продавал. Добавляйте товар "
            "через SupplierHub или остановите бота перед правкой файла. Убранные строки:\n<code>"
            + esc("\n".join(dropped))
            + "</code>"
        )

    async def _send(self, deal: Deal, rule: DeliveryRule, products: list[str]) -> None:
        try:
            chat_id = deal.chat_id or (await self._account.deals.get(deal.id)).chat_id
            if not chat_id:
                raise RuntimeError("у сделки нет чата")
            await self._account.chats.send(chat_id, render_message(rule.message, products, deal))
        except Exception as exc:
            await self._send_failed(deal, products, exc)
            return

        # Товар уже у покупателя: запись не должна сорвать уведомление и отметку
        # «отправлено» — если диск занят, она уйдёт в файл при следующей записи.
        self._state.record(SECTION, deal.id, status=DELIVERED, delivered_at=now_iso(), error=None)
        logger.info("Сделка %s: товар выдан", deal.id)

        text = f"📦 Товар выдан: {_ref(deal)} → {esc(_buyer(deal))}"
        if rule.stock_file is not None:
            try:
                left = Stock(rule.stock_file).count()
            except (OSError, StockError) as exc:
                logger.warning("Не удалось посчитать остаток в %s: %s", rule.stock_file, exc)
            else:
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


def mark_manual(state: State, deal_id: str) -> dict[str, object]:
    """Отметить сделку как решённую продавцом вручную (статус MANUAL)."""
    entry = state.get(SECTION, deal_id)
    status = entry.get("status") if entry else None
    if status == MANUAL:
        return dict(entry or {})
    if status not in RESOLVABLE:
        raise DeliveryError("Эта сделка не требует внимания — отмечать нечего.")
    return dict(state.put(SECTION, deal_id, status=MANUAL, handled_at=now_iso(), was=status))


def _meta(deal: Deal) -> dict[str, object]:
    """Что запомнить о сделке для истории продаж в приложении."""
    return {
        "item": _name(deal),
        "buyer": _buyer(deal),
        "price": deal.item.price if deal.item else None,
        "chat_id": deal.chat_id,
    }


def _name(deal: Deal) -> str | None:
    return deal.item.name if deal.item else None


def _buyer(deal: Deal) -> str:
    return (deal.user.username if deal.user else None) or "покупатель"


def _ref(deal: Deal) -> str:
    return link(deal.url, f"«{_name(deal) or 'товар'}»")
