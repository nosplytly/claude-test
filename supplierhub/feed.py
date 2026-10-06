"""Лента событий и журнал для окна: кольцевые буферы с номерами записей."""

from __future__ import annotations

import html
import logging
import re
import threading
from collections import deque
from datetime import datetime
from typing import Any

__all__ = ["EventFeed", "LogBuffer", "parse_notification"]

#: Первый символ уведомления бота → вид события в ленте.
_KINDS: tuple[tuple[str, str], ...] = (
    ("🛒", "sale"),
    ("📦", "delivered"),
    ("✅", "confirmed"),
    ("↩", "refund"),
    ("⚠", "warning"),
    ("⏸", "warning"),
    ("❗", "error"),
    ("❌", "error"),
    ("🔑", "error"),
    ("💬", "message"),
    ("🔁", "relist"),
    ("ℹ", "info"),
    ("🤖", "info"),
)
_VARIATION_SELECTOR = "️"
_LINK = re.compile(r"<a\s[^>]*?href\s*=\s*\"([^\"]*)\"", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_notification(text: str) -> dict[str, Any]:
    """Telegram-HTML уведомления бота → {kind, title, text, url} простым текстом.

    url — первая ссылка `<a href>` (только https), kind — по эмодзи в начале,
    title — первая строка без эмодзи, text — остальные строки.
    """
    link = _LINK.search(text)
    url = html.unescape(link.group(1)).strip() if link else None
    if url is not None and not url.startswith("https://"):
        url = None
    plain = html.unescape(_TAG.sub("", text)).strip()
    kind = "info"
    for emoji, name in _KINDS:
        if plain.startswith(emoji):
            kind = name
            plain = plain[len(emoji) :].lstrip(_VARIATION_SELECTOR).strip()
            break
    title, _, rest = plain.partition("\n")
    return {"kind": kind, "title": title.strip(), "text": rest.strip(), "url": url}


class _Ring:
    """Потокобезопасный кольцевой буфер с растущими номерами записей."""

    def __init__(self, maxlen: int) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._last_id = 0

    def append(self, item: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._last_id += 1
            entry = {"id": self._last_id, **item}
            self._items.append(entry)
            return dict(entry)

    def since(self, after_id: int = 0) -> dict[str, Any]:
        """Записи с id > after_id, старые сначала, и последний выданный id."""
        with self._lock:
            # Номер больше нашего — окно пережило перезапуск приложения:
            # отдаём всё с начала, иначе новые записи «спрятались» бы.
            if after_id > self._last_id:
                after_id = 0
            items = [dict(item) for item in self._items if item["id"] > after_id]
            return {"items": items, "last_id": self._last_id}


class EventFeed:
    """Лента событий на главной: продажи, выдачи, ошибки (последние 300)."""

    def __init__(self, maxlen: int = 300) -> None:
        self._ring = _Ring(maxlen)

    def add(self, kind: str, title: str, text: str = "", url: str | None = None) -> dict[str, Any]:
        return self._ring.append(
            {"ts": _now_iso(), "kind": kind, "title": title, "text": text, "url": url}
        )

    def add_message(self, text_html: str) -> dict[str, Any]:
        """Добавить уведомление бота (Telegram-HTML)."""
        parsed = parse_notification(text_html)
        return self.add(parsed["kind"], parsed["title"], parsed["text"], parsed["url"])

    def since(self, after_id: int = 0) -> dict[str, Any]:
        return self._ring.since(after_id)


class LogBuffer(logging.Handler):
    """Хвост журнала для окна (последние 1000 записей)."""

    def __init__(self, maxlen: int = 1000, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self._ring = _Ring(maxlen)
        self._formatter = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + self._formatter.formatException(record.exc_info)
            self._ring.append(
                {
                    "ts": datetime.fromtimestamp(record.created)
                    .astimezone()
                    .isoformat(timespec="seconds"),
                    "level": _level_name(record.levelno),
                    "name": record.name,
                    "message": message,
                }
            )
        except Exception:
            self.handleError(record)

    def since(self, after_id: int = 0) -> dict[str, Any]:
        return self._ring.since(after_id)


def _level_name(levelno: int) -> str:
    if levelno >= logging.ERROR:
        return "ERROR"
    if levelno >= logging.WARNING:
        return "WARNING"
    if levelno >= logging.INFO:
        return "INFO"
    return "DEBUG"
