"""Продажи и сводка для окна: живые сделки бота + история из state.json."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from PlayerokAPI import Deal, ItemDealDirection

from playerok_bot.fileio import read_text

__all__ = [
    "DEAL_LABELS",
    "DELIVERY_LABELS",
    "HistoryReader",
    "build_sales",
    "compute_stats",
    "local_date",
    "needs_attention",
    "sale_delivery",
]

logger = logging.getLogger(__name__)

DEAL_LABELS: dict[str, str] = {
    "PENDING": "Ожидает оплаты",
    "PAID": "Оплачена",
    "SENT": "Отправлена",
    "CONFIRMED": "Завершена",
    "ROLLED_BACK": "Возврат",
    "FAILED": "Не состоялась",
}
#: Сделка есть только в истории бота — текущий статус на площадке неизвестен.
UNKNOWN_DEAL_LABEL = "Нет данных"

DELIVERY_LABELS: dict[str, str] = {
    "delivered": "Выдано",
    "reserved": "Выдаётся",
    "no_stock": "Нет товара",
    "failed": "Ошибка выдачи",
    "no_rule": "Без автовыдачи",
    "skipped": "Пропущена",
    "manual": "Выдано вручную",
}

#: Статусы сделки, при которых продажа состоялась (деньги получены или придут).
SOLD = frozenset({"PAID", "SENT", "CONFIRMED"})
#: Выдача требует внимания продавца.
ATTENTION = frozenset({"failed", "no_stock"})
MAX_SALES = 200

_DEAL_URL = "https://playerok.com/deal/{}"
_CHAT_URL = "https://playerok.com/chats/{}"


class HistoryReader:
    """Раздел `deliveries` из state.json с кэшем по времени изменения файла.

    Бот перезаписывает файл из другого потока; если прочитать не вышло
    (файл как раз подменяется), отдаём прошлый удачный результат.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._key: tuple[int, int] | None = None
        self._data: dict[str, dict[str, Any]] = {}

    def read(self, path: Path) -> dict[str, dict[str, Any]]:
        with self._lock:
            if path != self._path:
                self._path, self._key, self._data = path, None, {}
            try:
                stat = path.stat()
            except FileNotFoundError:
                self._key, self._data = None, {}
                return {}
            except OSError:
                return self._data
            key = (stat.st_mtime_ns, stat.st_size)
            if key == self._key:
                return self._data
            try:
                raw = json.loads(read_text(path))
            except (OSError, ValueError) as exc:
                logger.debug("Не удалось прочитать %s: %s", path, exc)
                return self._data
            section = raw.get("deliveries") if isinstance(raw, dict) else None
            data = (
                {str(k): v for k, v in section.items() if isinstance(v, dict)}
                if isinstance(section, dict)
                else {}
            )
            self._key, self._data = key, data
            return data


def local_date(value: Any) -> date | None:
    """Дата в локальном часовом поясе из ISO-строки или datetime."""
    if isinstance(value, str) and value:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    # Наивное время считаем локальным: astimezone() так и делает.
    return value.astimezone().date()


def build_sales(
    recent: Iterable[Deal] | None,
    history: dict[str, dict[str, Any]],
    *,
    limit: int | None = MAX_SALES,
    has_rule: Callable[[str | None], bool] | None = None,
) -> list[dict[str, Any]]:
    """Свежие сделки из последнего опроса + история выдач без повторов, новые сверху.

    `has_rule(название)` — есть ли теперь правило для лота: у сделок «Без автовыдачи»
    окно тогда предлагает «Выдать по правилу».
    """
    sales: dict[str, dict[str, Any]] = {}
    for deal in recent or ():
        if deal.direction == ItemDealDirection.IN or not deal.id or deal.id in sales:
            continue
        sales[deal.id] = _sale_from_deal(deal, history.get(deal.id))
    for deal_id, entry in history.items():
        if deal_id not in sales:
            sales[deal_id] = _sale_from_history(deal_id, entry)
    for sale in sales.values():
        sale["attention"] = needs_attention(sale)
        delivery = sale["delivery"]
        if delivery is not None:
            delivery["rule_available"] = bool(
                delivery["status"] == "no_rule"
                and has_rule is not None
                and sale["status"] in (None, "PAID")
                and has_rule(sale["item"])
            )
    ordered = sorted(sales.values(), key=_sort_key, reverse=True)
    return ordered if limit is None else ordered[:limit]


def needs_attention(sale: dict[str, Any]) -> bool:
    """Нужно ли продавцу что-то сделать с продажей.

    Ошибка выдачи и «нет товара» — пока сделка не закрыта на площадке
    (отправлена, завершена, возврат) и продавец не отметил её «Выдано вручную».
    Продажа без правила — пока она оплачена и ждёт товара.
    """
    delivery = sale.get("delivery") or {}
    status = delivery.get("status")
    live = sale.get("status")
    if live is not None and live != "PAID":
        return False
    if status in ATTENTION:
        return True
    return status == "no_rule" and live == "PAID"


def compute_stats(
    sales: list[dict[str, Any]],
    history: dict[str, dict[str, Any]],
    *,
    stock_total: int,
    today: date | None = None,
) -> dict[str, Any]:
    """Плитки на главной. «Сегодня» — по локальной дате компьютера."""
    today = today or datetime.now().astimezone().date()
    sales_today = 0
    revenue = 0.0
    for sale in sales:
        if sale["status"] is not None:
            # Живая сделка: считаем по статусу и дате создания.
            counted = sale["status"] in SOLD and local_date(sale["created_at"]) == today
        else:
            # Только история: время последней обработки ботом.
            delivery = sale["delivery"] or {}
            counted = (
                delivery.get("status") != "skipped" and local_date(sale["created_at"]) == today
            )
        if counted:
            sales_today += 1
            revenue += sale["price"] or 0.0
    delivered_today = sum(
        1
        for entry in history.values()
        if entry.get("status") == "delivered" and local_date(entry.get("delivered_at")) == today
    )
    attention = sum(1 for sale in sales if sale.get("attention", needs_attention(sale)))
    return {
        "sales_today": sales_today,
        "revenue_today": round(revenue, 2),
        "delivered_today": delivered_today,
        "attention": attention,
        "stock_total": stock_total,
    }


def _sale_from_deal(deal: Deal, entry: dict[str, Any] | None) -> dict[str, Any]:
    entry = entry or {}
    status = deal.status.value if deal.status is not None else None
    chat_id = deal.chat_id or entry.get("chat_id")
    item_price = deal.item.price if deal.item else None
    return {
        "deal_id": deal.id,
        "url": deal.url,
        "item": (deal.item.name if deal.item else None) or _opt_str(entry.get("item")),
        "buyer": (deal.user.username if deal.user else None) or _opt_str(entry.get("buyer")),
        "price": _price(item_price if item_price is not None else entry.get("price")),
        "status": status,
        "status_label": DEAL_LABELS.get(status or "", status or UNKNOWN_DEAL_LABEL),
        "created_at": deal.created_at.isoformat() if deal.created_at else None,
        "chat_url": _CHAT_URL.format(chat_id) if chat_id else None,
        "delivery": sale_delivery(entry),
    }


def _sale_from_history(deal_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    chat_id = _opt_str(entry.get("chat_id"))
    return {
        "deal_id": deal_id,
        "url": _DEAL_URL.format(deal_id),
        "item": _opt_str(entry.get("item")),
        "buyer": _opt_str(entry.get("buyer")),
        "price": _price(entry.get("price")),
        "status": None,
        "status_label": UNKNOWN_DEAL_LABEL,
        # Время создания сделки бот не хранит — берём время её обработки.
        "created_at": _opt_str(entry.get("at")),
        "chat_url": _CHAT_URL.format(chat_id) if chat_id else None,
        "delivery": sale_delivery(entry),
    }


def sale_delivery(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Блок `delivery` продажи из записи state.json."""
    status = entry.get("status")
    if not isinstance(status, str):
        return None
    products = entry.get("products")
    return {
        "status": status,
        "label": DELIVERY_LABELS.get(status, status),
        "products": [str(item) for item in products] if isinstance(products, list) else [],
        "error": _opt_str(entry.get("error")),
        "delivered_at": _opt_str(entry.get("delivered_at")),
        "reason": _opt_str(entry.get("reason")),
        "handled_at": _opt_str(entry.get("handled_at")),
        "rule_available": False,
    }


def _sort_key(sale: dict[str, Any]) -> float:
    value = sale["created_at"]
    if not value:
        return float("-inf")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _opt_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)
