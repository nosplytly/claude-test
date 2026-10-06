"""Межпроцессная блокировка: один бот на одну рабочую папку (см. playerok_bot.lock)."""

from __future__ import annotations

from playerok_bot.lock import ProcessLock

__all__ = ["ProcessLock"]
