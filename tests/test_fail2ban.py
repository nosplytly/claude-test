"""Built-in fail2ban: who gets banned, who never does, escalation, persistence, IPv6, whitelist.

Run:  .venv\\Scripts\\python.exe tests\\test_fail2ban.py
Throw-away database, mock supplier, no bot: nothing leaves the machine.
"""
import asyncio
import hashlib
import hmac
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="sh-f2b-")
os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "dev", "DATA_DIR": TMP, "NERVIXY_MOCK": "true", "BOT_TOKEN": "", "ADMIN_TG_IDS": "",
                   "MONITOR_ENABLED": "false", "NERVIXY_WEBHOOK_SECRET": "whsec_test",
                   "F2B_WHITELIST": "198.51.100.0/24, 2001:db8:ffff::/48, not-an-ip"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import fail2ban as f2b  # noqa: E402
from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

OK = 0


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


def client(ip: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 40000)), base_url="https://t")


async def main():
    await init_db()
    await f2b.load()

    print("1. сканер уязвимостей")
    async with client("203.0.113.7") as c:
        r = await c.get("/.env")
        check(r.status_code == 404 and not f2b.banned_for("203.0.113.7"), "первый запрос /.env → 404, ещё не бан")
        await c.get("/wp-login.php")
        check(f2b.banned_for("203.0.113.7") > 3500, "второй запрос (/wp-login.php) → бан на час")
        r = await c.get("/")
        check(r.status_code == 403 and "временно ограничен" in r.text and int(r.headers["retry-after"]) > 3500,
              "забаненный IP получает 403 и Retry-After даже на главной")
        r = await c.post("/api/orders", json={}, headers={"X-SH": "1"})
        check(r.status_code == 403 and "временно ограничен" in r.json()["detail"], "API сайта для него тоже закрыто")
        r = await c.get("/api/v1/me")
        check(r.status_code == 403 and r.json()["ok"] is False, "партнёрское API отвечает в своём формате {ok:false}")

    print("2. обычные посетители не страдают")
    async with client("203.0.113.20") as c:
        for _ in range(40):
            assert (await c.get("/")).status_code == 200
            await c.get("/api/config")
            await c.get("/assets/app.css")
        for _ in range(10):  # an iPhone refreshing the page: the browser asks for these by itself every time
            for auto in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/robots.txt",
                         "/.well-known/assetlinks.json"):
                await c.get(auto)
        await c.get("/api/orders/NOPE", headers={"X-SH": "1"})
        check(not f2b.banned_for("203.0.113.20") and "203.0.113.20" not in f2b._points,
              "120 обычных запросов + 50 автоматических запросов iPhone (иконки, favicon, robots) + 404 заказа → 0 очков")
    async with client("127.0.0.1") as c:
        for _ in range(10):
            await c.get("/.env")
        check(not f2b.banned_for("127.0.0.1") and (await c.get("/")).status_code == 200, "localhost не банится никогда")
    async with client("198.51.100.9") as c:
        for _ in range(10):
            await c.get("/.git/config")
        check(not f2b.banned_for("198.51.100.9"), "IP из F2B_WHITELIST не банится (кривая запись в списке пропущена)")

    print("3. подбор API-ключей и поддельные вебхуки")
    async with client("203.0.113.30") as c:
        for i in range(4):
            r = await c.get("/api/v1/me", headers={"X-API-Key": f"sh_live_guess{i}"})
        check(r.status_code == 401 and not f2b.banned_for("203.0.113.30"), "4 неверных ключа → ещё 401")
        await c.get("/api/v1/me", headers={"X-API-Key": "sh_live_guess5"})
        check(f2b.banned_for("203.0.113.30") > 0, "5-й неверный ключ → бан")
    async with client("203.0.113.31") as c:
        for _ in range(3):
            r = await c.post("/api/webhooks/nervixy", content=b'{"order_id":"1"}', headers={"X-Nervixy-Signature": "bad"})
        check(r.status_code == 403 and not f2b.banned_for("203.0.113.31"), "3 поддельные подписи → 403, ещё не бан")
        await c.post("/api/webhooks/nervixy", content=b"{}", headers={"X-Nervixy-Signature": "bad"})
        check(f2b.banned_for("203.0.113.31") > 0, "4-я поддельная подпись → бан")
    async with client("203.0.113.32") as c:
        body = b'{"event":"webhook.test"}'
        for _ in range(20):
            sig = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
            r = await c.post("/api/webhooks/nervixy", content=body, headers={"X-Nervixy-Signature": f"sha256={sig}"})
        check(r.status_code == 200 and not f2b.banned_for("203.0.113.32"), "настоящий nervixy с верной подписью не банится")
    async with client("203.0.113.33") as c:
        for _ in range(5):
            r = await c.post("/api/orders", json={})
        check(r.status_code == 403 and f2b.banned_for("203.0.113.33") > 0, "5 POST без заголовка сайта (CSRF) → бан")
    async with client("203.0.113.34") as c:
        codes = [(await c.post("/api/auth/start", headers={"X-SH": "1"})).status_code for _ in range(20)]
        check(codes.count(429) == 10 and f2b.banned_for("203.0.113.34") > 0,
              "долбит вход после лимита: 10 ответов 429 → бан")
        check((await c.post("/api/auth/start", headers={"X-SH": "1"})).status_code == 403, "дальше — 403 бана")

    print("4. IPv6 и окно подсчёта")
    async with client("2001:db8:1:2::1") as c:
        await c.get("/.env")
    async with client("2001:db8:1:2::ffff") as c:
        await c.get("/phpmyadmin/")
    check(f2b.banned_for("2001:db8:1:2::1") > 0 and f2b.banned_for("2001:db8:1:2:abcd::5") > 0,
          "IPv6: два адреса одной /64 набрали очки вместе → забанена вся /64")
    check(not f2b.banned_for("2001:db8:1:3::1"), "соседняя /64 не задета")
    check(not f2b.banned_for("2001:db8:ffff:1::1"), "IPv6 из белого списка (/48) не банится")
    async with client("::ffff:203.0.113.7") as c:
        check((await c.get("/")).status_code == 403, "IPv4 в IPv6-обёртке (::ffff:…) узнаётся как тот же IP")
    async with client("203.0.113.40") as c:
        await c.get("/.env")
        f2b._points["203.0.113.40"][0] = (time.monotonic() - 11 * 60, 5)  # the first hit is now older than the window
        await c.get("/.git/HEAD")
        check(not f2b.banned_for("203.0.113.40"), "очки старше 10 минут не считаются")

    print("5. повторные баны, ручные баны, снятие")
    key = "203.0.113.7"
    f2b._bans[key]["until"] = time.time() - 1  # the first ban ran out
    check(not f2b.banned_for(key), "бан истёк → IP снова пускают")
    async with client(key) as c:
        await c.get("/.env")
        await c.get("/.env.bak")
    check(23 * 3600 < f2b.banned_for(key) <= 24 * 3600, "повторный бан в течение недели → 24 часа")
    f2b._bans[key]["until"] = time.time() - 1
    async with client(key) as c:
        await c.get("/.env")
        await c.get("/.env.bak")
    check(6 * 86400 < f2b.banned_for(key) <= 7 * 86400, "третий → 7 дней (максимум)")
    check(f2b.ban("127.0.0.1") is None and f2b.ban("198.51.100.5") is None and f2b.ban("nonsense") is None,
          "вручную нельзя забанить localhost, белый список и мусор")
    k, sec = f2b.ban("192.0.2.10", 2)
    check(k == "192.0.2.10" and sec == 7200 and f2b.banned_for("192.0.2.10") > 7000, "/ban 192.0.2.10 2 → бан на 2 ч")
    check(f2b.unban("192.0.2.10") == "192.0.2.10" and not f2b.banned_for("192.0.2.10"), "/unban снимает бан сразу")
    check(f2b.unban("192.0.2.99") is None, "снять бан с незабаненного → «не был заблокирован»")
    check(f2b.unban("2001:db8:1:2::77") == "2001:db8:1:2::/64", "IPv6 снимается по любому адресу из /64")

    print("6. сохранение и файл для брандмауэра")
    await f2b.flush()
    listed = (Path(TMP) / "f2b-bans.txt").read_text().split()
    check(set(listed) == {k for k, _, _ in f2b.active()} and "203.0.113.30" in listed and "192.0.2.10" not in listed,
          f"f2b-bans.txt = активные баны ({len(listed)} шт.), снятые туда не попадают")
    before = {k for k, _, _ in f2b.active()}
    f2b._bans.clear()
    f2b._history.clear()
    await f2b.load()
    check({k for k, _, _ in f2b.active()} == before and f2b._history.get("203.0.113.7", {}).get("n") == 3,
          "после перезапуска баны и история повторов восстанавливаются из базы")
    f2b._bans["203.0.113.30"]["until"] = time.time() - 1
    f2b._expire()
    await f2b.flush()
    check("203.0.113.30" not in (Path(TMP) / "f2b-bans.txt").read_text(), "истёкший бан уходит из файла")

    print("7. выключатель")
    from app.config import settings
    settings.f2b_enabled = False
    async with client("203.0.113.7") as c:
        check((await c.get("/")).status_code == 200, "F2B_ENABLED=false → никого не блокирует")
    settings.f2b_enabled = True

    print(f"\nALL GOOD: {OK} checks passed")


asyncio.run(main())
