"""Telegram avatars in the site header: served from our origin, cached, safe when Telegram misbehaves.

Run:  .venv\\Scripts\\python.exe tests\\test_avatars.py
Throw-away database; the bot is a fake object — nothing is sent to Telegram.
"""
import asyncio
import io
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="sh-ava-")
os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "dev", "DATA_DIR": TMP, "NERVIXY_MOCK": "true", "BOT_TOKEN": "",
                   "ADMIN_TG_IDS": "", "MONITOR_ENABLED": "false"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import avatars, notify  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

OK = 0
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


class FakeBot:
    """Knows who has which photo; counts how often it is asked."""

    def __init__(self):
        self.photos = {101: JPEG}  # tg_id -> bytes; 102 has no photo
        self.calls = 0
        self.fail = False

    async def get_user_profile_photos(self, user_id, limit=1):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Telegram is down (injected)")
        if user_id in self.photos:
            size = SimpleNamespace(file_id=f"file-{user_id}")
            return SimpleNamespace(total_count=1, photos=[[size]])
        return SimpleNamespace(total_count=0, photos=[])

    async def download(self, file_id):
        return io.BytesIO(self.photos[int(file_id.split("-")[1])])


async def login(c, tg_id):
    r = await c.post("/api/dev/login", json={"tg_id": tg_id, "name": f"u{tg_id}"}, headers={"X-SH": "1"})
    return r.json()["user"]


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t")


async def main():
    await init_db()
    bot = FakeBot()

    print("1. без бота и без входа")
    async with client() as c:
        check((await c.get("/api/me/avatar")).status_code == 401, "без входа аватарку не отдаём (401)")
        u = await login(c, 101)
        check(u["avatar"].startswith("/api/me/avatar?u="), f"в профиле есть ссылка на аватарку ({u['avatar']})")
        check((await c.get(u["avatar"])).status_code == 204, "бот не настроен → 204, сайт оставляет букву")

    notify.set_bot(bot)
    avatars._failed_at.clear()
    print("2. с фото и без")
    async with client() as c:
        u = await login(c, 101)
        r = await c.get(u["avatar"])
        check(r.status_code == 200 and r.content == JPEG and r.headers["content-type"] == "image/jpeg",
              "есть фото в Telegram → отдаём его с нашего домена (image/jpeg)")
        check("private" in r.headers.get("cache-control", ""), "кешируется только в браузере владельца (private)")
        calls = bot.calls
        for _ in range(5):
            await c.get(u["avatar"])
        check(bot.calls == calls, "повторные запросы берутся из кеша, Telegram не дёргаем")
    async with client() as c:
        u = await login(c, 102)
        check((await c.get(u["avatar"])).status_code == 204, "фото нет → 204 (буква имени)")
        calls = bot.calls
        await c.get(u["avatar"])
        check(bot.calls == calls, "«фото нет» тоже кешируется")

    print("3. каждый видит только своё")
    async with client() as c:
        await login(c, 102)
        r = await c.get("/api/me/avatar?u=1")  # the id in the URL is only a cache key
        check(r.status_code == 204, "подставить чужой id в ссылку бесполезно — отдаётся фото того, кто вошёл")

    print("4. смена фото и сбои Telegram")
    old = time.time() - avatars.FRESH - 10
    os.utime(avatars.DIR / "101.jpg", (old, old))
    bot.photos[101] = JPEG + b"-new"
    async with client() as c:
        u = await login(c, 101)
        check((await c.get(u["avatar"])).content == JPEG + b"-new", "через час подхватываем новое фото")
        os.utime(avatars.DIR / "101.jpg", (old, old))
        bot.fail = True
        r = await c.get(u["avatar"])
        check(r.status_code == 200 and r.content == JPEG + b"-new", "Telegram упал → показываем прежнее фото")
        calls = bot.calls
        await c.get(u["avatar"])
        check(bot.calls == calls, "после ошибки не долбим Telegram 5 минут")
    async with client() as c:
        u = await login(c, 103)
        check((await c.get(u["avatar"])).status_code == 204, "Telegram упал, а фото ещё не было → 204, сайт работает")

    print(f"\nALL GOOD: {OK} checks passed")


asyncio.run(main())
