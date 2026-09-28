"""Telegram profile photos for the site header.

The bot fetches the photo and we serve it from our own origin: the bot token never reaches the browser and the CSP
(img-src 'self') is kept. Cached on disk; Telegram is asked again at most once an hour per user (new or removed
photo). No photo, a photo hidden by privacy settings, no bot or a Telegram hiccup -> None, and the site keeps
showing the first letter of the name.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import notify
from .config import settings

log = logging.getLogger("sh.avatars")
DIR = settings.data_dir / "avatars"
FRESH = 3600  # seconds a cached answer (photo or "no photo") is trusted
BACKOFF = 300  # after a Telegram error, don't ask about this user again sooner
_locks: dict[int, asyncio.Lock] = {}
_failed_at: dict[int, float] = {}


def _fresh(path) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < FRESH


async def get(tg_id: int) -> bytes | None:
    """JPEG of the user's current Telegram photo, or None."""
    photo, none = DIR / f"{tg_id}.jpg", DIR / f"{tg_id}.none"
    async with _locks.setdefault(tg_id, asyncio.Lock()):  # one fetch per user even if several tabs ask at once
        if _fresh(photo):
            return photo.read_bytes()
        if _fresh(none):
            return None
        bot = notify.current_bot()
        if bot is None or time.monotonic() - _failed_at.get(tg_id, -BACKOFF) < BACKOFF:
            return photo.read_bytes() if photo.exists() else None
        try:
            photos = await asyncio.wait_for(bot.get_user_profile_photos(tg_id, limit=1), 10)
            data = None
            if photos.total_count and photos.photos:
                smallest = photos.photos[0][0]  # 160x160: plenty for a 36 px avatar even on a 3x screen
                data = (await asyncio.wait_for(bot.download(smallest.file_id), 15)).getvalue()
        except Exception as err:  # noqa: BLE001 - keep what we had, try later
            _failed_at[tg_id] = time.monotonic()
            log.warning("avatar of %s: %s", tg_id, err)
            return photo.read_bytes() if photo.exists() else None
        DIR.mkdir(parents=True, exist_ok=True)
        if not data:
            photo.unlink(missing_ok=True)
            none.touch()
            return None
        tmp = photo.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(photo)
        none.unlink(missing_ok=True)
        return data
