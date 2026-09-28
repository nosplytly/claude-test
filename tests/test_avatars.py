"""Telegram avatars on the site: fetched through a real aiogram Bot over a fake Telegram connection, cached on disk.

Run:  .venv\\Scripts\\python.exe tests\\test_avatars.py
Nothing is sent to Telegram.
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.update({"ENV": "dev", "DATA_DIR": tempfile.mkdtemp(prefix="sh-ava-"), "NERVIXY_MOCK": "true",
                   "BOT_TOKEN": "", "ADMIN_TG_IDS": "", "MONITOR_ENABLED": "false"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from aiogram import Bot  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.exceptions import TelegramNetworkError  # noqa: E402
from aiogram.methods import GetFile, GetUserProfilePhotos  # noqa: E402
from aiogram.types import File, PhotoSize, UserProfilePhotos  # noqa: E402

from app import avatars, notify  # noqa: E402
from app.db import init_db  # noqa: E402

OK = 0
JPEG = b"\xff\xd8\xff\xe0fake-avatar"


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


class FakeTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.photos: dict[int, bool] = {}  # tg_id -> has a photo
        self.down: set[int] = set()  # tg_ids for which Telegram "fails"

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetUserProfilePhotos):
            if method.user_id in self.down:
                raise TelegramNetworkError(method=method, message="timeout")
            if not self.photos.get(method.user_id):
                return UserProfilePhotos(total_count=0, photos=[])
            sizes = [PhotoSize(file_id=f"{method.user_id}-{w}", file_unique_id=f"u{w}", width=w, height=w)
                     for w in (160, 320, 640)]
            return UserProfilePhotos(total_count=1, photos=[sizes])
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path=f"profile_photos/{method.file_id}.jpg")
        raise AssertionError(f"unexpected call {method!r}")

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        self.calls.append(url)
        yield JPEG

    async def close(self):
        pass


tg = FakeTelegram()
bot = Bot("123456:TEST_TOKEN_FOR_OFFLINE_CHECKS_ONLY", session=tg)


def calls_since(n):
    return tg.calls[n:]


async def main():
    await init_db()

    print("1. без бота")
    check(await avatars.get(700) is None, "бот не настроен → аватарки нет, на сайте будет буква")

    notify.set_bot(bot)
    tg.photos[700] = True

    print("2. есть фото")
    n = len(tg.calls)
    check(await avatars.get(700) == JPEG, "фото профиля скачано через бота")
    got = calls_since(n)
    check(any(isinstance(c, GetFile) and c.file_id == "700-160" for c in got), "берётся самый маленький размер (160 px)")
    check((avatars.DIR / "700.jpg").read_bytes() == JPEG, "фото сохранено в data/avatars")
    n = len(tg.calls)
    check(await avatars.get(700) == JPEG and not calls_since(n), "второй раз — из кэша, в Telegram не ходим")

    print("3. нет фото / скрыто настройками приватности")
    check(await avatars.get(701) is None, "фото нет → None")
    n = len(tg.calls)
    check(await avatars.get(701) is None and not calls_since(n), "отсутствие фото тоже кэшируется")

    print("4. Telegram недоступен")
    tg.down.add(702)
    check(await avatars.get(702) is None, "ошибка Telegram → без исключения, буква")
    n = len(tg.calls)
    check(await avatars.get(702) is None and not calls_since(n), "после ошибки не долбим Telegram каждую загрузку")

    print("5. фото удалили / поставили новое")
    old = time.time() - avatars.TTL - 5
    os.utime(avatars.DIR / "700.jpg", (old, old))
    tg.photos[700] = False
    check(await avatars.get(700) is None and not (avatars.DIR / "700.jpg").exists(), "через час: фото удалено → буква")
    os.utime(avatars.DIR / "700.none", (old, old))
    tg.photos[700] = True
    check(await avatars.get(700) == JPEG, "через час: фото снова есть → показываем")

    print("6. сайт")
    from app.main import app
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url="http://t", headers={"x-sh": "1"}) as c:
        r = await c.get("/api/me/avatar?u=1")
        check(r.status_code == 401, "без входа аватарку не отдаём")
        r = await c.post("/api/dev/login", json={"tg_id": 700, "name": "ivan"})
        url = r.json()["user"]["avatar"]
        check(url.startswith("/api/me/avatar?u="), f"/api/me отдаёт ссылку на аватарку ({url})")
        r = await c.get(url)
        check(r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == JPEG,
              "с фото → 200 image/jpeg со своего домена")
    async with httpx.AsyncClient(transport=t, base_url="http://t", headers={"x-sh": "1"}) as c:
        r = await c.post("/api/dev/login", json={"tg_id": 701, "name": "petr"})
        r = await c.get(r.json()["user"]["avatar"])
        check(r.status_code == 204 and not r.content, "без фото → 204, сайт оставляет букву")

    print(f"\nALL GOOD: {OK} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
