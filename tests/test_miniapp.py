"""Telegram Mini App: login from the signed launch data, header sessions, bot buttons, framing rules.

Run:  .venv\\Scripts\\python.exe tests\\test_miniapp.py
Throw-away database, a made-up bot token: nothing leaves the machine.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

TOKEN = "123456:TEST_TOKEN_FOR_MINI_APP_CHECKS"
os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "prod", "DATA_DIR": tempfile.mkdtemp(prefix="sh-app-"),
                   "NERVIXY_MOCK": "true", "BOT_TOKEN": TOKEN, "ADMIN_TG_IDS": "", "MONITOR_ENABLED": "false",
                   "BASE_URL": "https://sh.test"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import tgui  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402

OK = 0


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


def init_data(user: dict, *, token=TOKEN, auth_date=None, tamper=None) -> str:
    """Launch data signed exactly like Telegram does (core.telegram.org/bots/webapps)."""
    fields = {"query_id": "AAHdF6IQAAAAAN0XohDhrOrc", "user": json.dumps(user, separators=(",", ":")),
              "auth_date": str(auth_date or int(time.time())), "signature": "sig-for-third-parties"}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields.update(tamper)
    return urlencode(fields)


def client(ip="203.0.113.60"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 1)), base_url="https://sh.test")


async def main():
    await init_db()
    me = {"id": 424242, "first_name": "Ваня", "username": "vanya_tg", "language_code": "ru"}

    print("1. вход из мини-аппа")
    async with client() as c:
        r = await c.post("/api/auth/webapp", json={"init_data": init_data(me)}, headers={"X-SH": "1"})
        body = r.json()
        check(r.status_code == 200 and body["user"]["username"] == "vanya_tg" and body["token"],
              "подпись Telegram верна → вошли без кода, пользователь создан")
        check("sh_session" in r.cookies, "кука сессии выставлена (для приложений Telegram)")
    async with client() as c:  # a fresh client: no cookies, like the Telegram Web iframe
        r = await c.get("/api/me", headers={"X-SH-Session": body["token"]})
        check(r.json()["user"]["username"] == "vanya_tg", "без куки, по заголовку X-SH-Session — тоже вошли (Telegram Web)")
        r = await c.get("/api/me", headers={"X-SH-Session": "forged"})
        check(r.json()["user"] is None, "поддельный токен в заголовке → не вошли")

    print("2. подделки не проходят")
    async with client("203.0.113.61") as c:
        cases = {
            "чужой id в данных": init_data(me, tamper={"user": json.dumps({**me, "id": 1})}),
            "подписано чужим ботом": init_data(me, token="999:OTHER"),
            "данные двухдневной давности": init_data(me, auth_date=int(time.time()) - 2 * 86400),
            "без подписи": init_data(me, tamper={"hash": ""}),
            "мусор": "user=%7B%7D&hash=abc",
        }
        for name, data in cases.items():
            r = await c.post("/api/auth/webapp", json={"init_data": data}, headers={"X-SH": "1"})
            check(r.status_code == 401 and "sh_session" not in r.cookies, f"{name} → 401, сессии нет")
    async with session_scope() as s:
        n = len((await s.execute(select(User))).scalars().all())
    check(n == 1, "ни одна подделка не создала пользователя")
    async with session_scope() as s:
        (await s.execute(select(User).where(User.tg_id == 424242))).scalar_one().is_banned = True
    async with client("203.0.113.62") as c:
        r = await c.post("/api/auth/webapp", json={"init_data": init_data(me)}, headers={"X-SH": "1"})
        check(r.status_code == 403, "заблокированный клиент → 403")

    print("3. кнопки бота")
    b = tgui.btn("Пополнить Steam", url="https://sh.test/")
    check(b.web_app and b.web_app.url == "https://sh.test/" and not b.url, "ссылка на сайт → кнопка мини-аппа")
    b = tgui.btn("Пополнить баланс", url="https://sh.test/#/deposit")
    check(b.web_app and b.web_app.url.endswith("#/deposit"), "с нужной страницей (#/deposit)")
    b = tgui.btn("Вернуться на сайт", url="https://sh.test/auth/tg?t=1&k=2")
    check(b.url and not b.web_app, "ссылка входа для браузера остаётся обычной ссылкой")
    b = tgui.btn("Поддержка", url="https://t.me/splytly")
    check(b.url and not b.web_app, "чужие ссылки (t.me) — обычные")
    settings.mini_app = False
    check(tgui.btn("x", url="https://sh.test/").url == "https://sh.test/", "MINI_APP=false → снова обычные ссылки")
    settings.mini_app = True

    print("4. встраивание")
    async with client() as c:
        r = await c.get("/")
        csp = r.headers["content-security-policy"]
        check("frame-ancestors 'self' https://web.telegram.org" in csp and "x-frame-options" not in r.headers,
              "показывать сайт во фрейме может только Telegram Web (мини-апп), остальным нельзя")
        check("https://telegram.org" in csp.split("script-src")[1].split(";")[0], "скрипт SDK Telegram разрешён, прочие чужие — нет")

    print(f"\nALL GOOD: {OK} checks passed")


asyncio.run(main())
