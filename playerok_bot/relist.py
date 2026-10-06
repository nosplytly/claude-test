"""Перевыставление лотов: после продажи и по истечении срока."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from PlayerokAPI import Deal, ItemStatus

from .config import RelistConfig
from .storage import State
from .telegram import Notifier, esc

if TYPE_CHECKING:
    from PlayerokAPI import Account

__all__ = ["AutoRelist"]

logger = logging.getLogger(__name__)

DEALS = "relisted_deals"
ITEMS = "relisted_items"

#: Статусы проданного лота, который можно выставить заново.
_SOLD = frozenset({ItemStatus.SOLD, ItemStatus.EXPIRED})
#: Не трогать истёкший лот повторно раньше этого срока.
_COOLDOWN = timedelta(hours=12)
#: Сколько раз проверить лот после продажи, прежде чем решить, что он ещё в продаже.
_SALE_CHECKS = 3


class AutoRelist:
    """Публикует лот заново на бесплатном тарифе (DEFAULT).

    Платное поднятие (PREMIUM/VIP) бот не покупает, чтобы не тратить баланс.
    """

    def __init__(
        self,
        account: Account,
        me_id: str,
        config: RelistConfig,
        state: State,
        notifier: Notifier,
        can_sell: Callable[[str | None], bool],
    ) -> None:
        self._account = account
        self._me_id = me_id
        self._config = config
        self._state = state
        self._notifier = notifier
        self._can_sell = can_sell
        self._lock = asyncio.Lock()

    def skip(self, deal: Deal) -> None:
        """Не перевыставлять лот по этой продаже."""
        self._state.put(DEALS, deal.id, result="skipped")

    async def after_sale(self, deal: Deal) -> None:
        """Выставить проданный лот заново. Один раз на сделку."""
        if not self._config.after_sale or deal.item is None:
            return
        async with self._lock:
            entry = self._state.get(DEALS, deal.id) or {}
            if entry.get("result") not in (None, "waiting"):
                return
            name = deal.item.name
            if not self._config.matches(name):
                self._state.put(DEALS, deal.id, result="not_matched")
                return
            if not self._can_sell(name):
                self._state.put(DEALS, deal.id, result="no_stock")
                await self._notifier.send(
                    f"⏸ Лот «{esc(name)}» не перевыставлен: на складе не хватает товара. "
                    "Пополните файл и выставьте лот вручную."
                )
                return
            try:
                item = await self._account.items.get(item_id=deal.item.id)
            except Exception as exc:
                logger.warning("Сделка %s: не удалось получить лот: %s", deal.id, exc)
                return
            if item.status not in _SOLD:
                # Лот с запасом на несколько продаж остаётся в продаже — его не трогаем.
                # Но статус может смениться не сразу после оплаты, поэтому проверяем
                # ещё на следующих опросах.
                checks = int(entry.get("checks") or 0) + 1
                result = "still_active" if checks >= _SALE_CHECKS else "waiting"
                self._state.put(
                    DEALS, deal.id, result=result, checks=checks, status=str(item.status)
                )
                return
            ok = await self._publish(item.id, name)
            self._state.put(DEALS, deal.id, result="ok" if ok else "error", item_id=item.id)

    async def run_expired(self) -> None:
        """Периодически перевыставлять лоты с истёкшим сроком."""
        while True:
            try:
                await self.relist_expired()
            except Exception as exc:
                logger.warning("Проверка истёкших лотов не удалась: %s", exc)
            await asyncio.sleep(self._config.interval_minutes * 60)

    async def relist_expired(self) -> None:
        page = await self._account.items.search(
            user_id=self._me_id, status=ItemStatus.EXPIRED, first=50
        )
        for item in page:
            if item.status is not None and item.status != ItemStatus.EXPIRED:
                continue
            if not self._config.matches(item.name):
                continue
            if not self._can_sell(item.name):
                logger.info("Лот «%s» истёк, но товара на складе нет — пропускаю", item.name)
                continue
            async with self._lock:
                if self._recently_relisted(item.id):
                    continue
                await self._publish(item.id, item.name)

    def _recently_relisted(self, item_id: str) -> bool:
        entry = self._state.get(ITEMS, item_id)
        if not entry:
            return False
        if entry.get("replaced_by"):
            # Площадка выставила копию, а старый лот остался истёкшим — его больше не трогаем.
            return True
        try:
            at = datetime.fromisoformat(entry.get("at", ""))
        except (TypeError, ValueError):
            return False
        return datetime.now(UTC) - at < _COOLDOWN

    async def _publish(self, item_id: str, name: str | None) -> bool:
        title = esc(name or item_id)
        try:
            item = await self._account.items.publish(item_id)
        except Exception as exc:
            logger.warning("Лот %s: перевыставить не удалось: %s", item_id, exc)
            self._state.put(ITEMS, item_id, error=str(exc))
            await self._notifier.send(f"⚠️ Не удалось перевыставить лот «{title}»: {esc(exc)}")
            return False
        replaced_by = item.id if item.id and item.id != item_id else None
        self._state.put(
            ITEMS, item_id, status=str(item.status), replaced_by=replaced_by, error=None
        )
        logger.info("Лот %s перевыставлен, статус %s", item_id, item.status)
        await self._notifier.send(f"🔁 Лот «{title}» снова в продаже (статус: {esc(item.status)})")
        return True
