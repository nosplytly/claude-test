"""Ошибки, текст которых показывается пользователю."""

from __future__ import annotations

__all__ = ["ApiError"]


class ApiError(Exception):
    """Ошибка для окна приложения — сообщение показывается как есть."""
