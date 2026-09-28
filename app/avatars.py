"""Telegram profile photos for the site header, fetched through the bot and cached on disk.

The photo is downloaded here and served from our own origin: the bot token never reaches the browser and the CSP
(img-src 'self') allows it. No photo, a photo hidden by the user's privacy settings or no bot -> None, and the site
shows the first letter of the name instead.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import notify
from .config import settings

log = logging.getLogger("sh.avatars")
DIR = settings.data_dir / "avatars"
TTL = 3600  # re-ask Telegram at most once an hour per user (new photo, photo removed)
RETRY = 300  # after a Telegram error don't retry this user sooner
_locks: dict[int, asyncio.Lock] = {}
_failed: dict[int, float] = {}


def _fresh(path) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < TTL


async def get(tg_id: int) -> bytes | None:
    """JPEG of the user's current Telegram photo, or None."""
    img, none = DIR / f"{tg_id}.jpg", DIR / f"{tg_id}.none"
    async with _locks.setdefault(tg_id, asyncio.Lock()):
        if _fresh(img):
            return img.read_bytes()
        if _fresh(none):
            return None
        bot = notify.current_bot()
        if bot is None or time.monotonic() - _failed.get(tg_id, -RETRY) < RETRY:
            return img.read_bytes() if img.exists() else None
        try:
            photos = await asyncio.wait_for(bot.get_user_profile_photos(tg_id, limit=1), 10)
            data = None
            if photos.photos:
                small = photos.photos[0][0]  # smallest square (160 px): plenty for a 36 px avatar on a 3x screen
                data = (await asyncio.wait_for(bot.download(small.file_id), 15)).getvalue()
        except Exception as e:  # noqa: BLE001 - Telegram hiccup: keep what we had, try again later
            _failed[tg_id] = time.monotonic()
            log.warning("avatar of %s: %s", tg_id, e)
            return img.read_bytes() if img.exists() else None
        DIR.mkdir(parents=True, exist_ok=True)
        if not data:
            img.unlink(missing_ok=True)
            none.touch()
            return None
        tmp = img.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(img)
        none.unlink(missing_ok=True)
        return data
