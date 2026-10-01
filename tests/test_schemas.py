"""Input validation (app/schemas.py): every body, id and paging parameter is checked before handler code runs.

Run:  .venv\\Scripts\\python.exe tests\\test_schemas.py
Throw-away database, mock supplier, made-up bot token: nothing leaves the machine.
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

TOKEN = "123456:TEST_TOKEN_FOR_SCHEMA_CHECKS"
WH_SECRET = "whsec_test_schema_secret"
os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "prod", "DATA_DIR": tempfile.mkdtemp(prefix="sh-schemas-"),
                   "NERVIXY_MOCK": "true", "BOT_TOKEN": TOKEN, "ADMIN_TG_IDS": "", "MONITOR_ENABLED": "false",
                   "BASE_URL": "https://sh.test", "NERVIXY_WEBHOOK_SECRET": WH_SECRET, "F2B_ENABLED": "false",
                   "WALLET_USDT_TRC20": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import schemas  # noqa: E402
from app.auth import create_session  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.nervixy import CURRENCIES, nervixy  # noqa: E402

OK = 0
H = {"X-SH": "1"}


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.70", 1)), base_url="https://sh.test")


def signed_init_data(user) -> str:
    fields = {"query_id": "AA", "user": json.dumps(user, separators=(",", ":")), "auth_date": str(int(time.time()))}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def webhook(body: bytes, *, sign=True):
    sig = hmac.new(WH_SECRET.encode(), body, hashlib.sha256).hexdigest() if sign else "0" * 64
    return {"content": body, "headers": {"X-Nervixy-Signature": f"sha256={sig}", "Content-Type": "application/json"}}


async def main():
    await init_db()
    nervixy.client.delay = 0
    calls = {"n": 0}
    real_check = nervixy.client.check_login

    async def counting_check(login):
        calls["n"] += 1
        return await real_check(login)
    nervixy.client.check_login = counting_check

    async with session_scope() as s:
        u = User(tg_id=5001, username="schema_user", first_name="S")
        s.add(u)
        await s.flush()
        tok = await create_session(s, u, "203.0.113.70", "test")
    me = {**H, "X-SH-Session": tok}

    print("1. схемы совпадают с тем, что знает сервис")
    from typing import get_args
    check(set(get_args(get_args(schemas.Currency)[0])) == set(CURRENCIES), "валюты в схеме = валюты поставщика")
    check(schemas.OrderIn(steam_login=" gaben ", amount="1000", currency="rub", method="USDT_TRC20", promo=" sale1 ")
          .model_dump() == {"steam_login": "gaben", "amount": __import__("decimal").Decimal("1000"), "currency": "RUB",
                            "method": "usdt_trc20", "promo": "SALE1"}, "пробелы убираются, регистр приводится")
    check(str(schemas.PartnerQuoteIn(amount=0.1).amount) == "0.1", "число 0.1 из JSON → ровно 0.1, без хвоста float")

    async with client() as c:
        print("2. сайт: заказ")
        good = {"steam_login": "gooduser", "amount": "1000", "currency": "RUB", "method": "usdt_trc20"}
        bad = {
            "лишнее поле (опечатка promo_code)": ({**good, "promo_code": "SALE"}, "Лишнее поле: promo_code"),
            "сумма true": ({**good, "amount": True}, "сумма — число"),
            "сумма NaN": ({**good, "amount": "NaN"}, "Некорректная сумма"),
            "сумма Infinity": ({**good, "amount": "Infinity"}, "Некорректная сумма"),
            "сумма 3 знака после запятой": ({**good, "amount": "100.123"}, "Некорректная сумма"),
            "сумма отрицательная": ({**good, "amount": "-5"}, "Некорректная сумма"),
            "сумма — объект": ({**good, "amount": {"$gt": 0}}, "Некорректная сумма"),
            "валюта EUR": ({**good, "currency": "EUR"}, "Неизвестная валюта"),
            "SQL в логине": ({**good, "steam_login": "gaben' OR 1=1--"}, "Логин Steam"),
            "HTML в логине": ({**good, "steam_login": "<img src=x onerror=alert(1)>"}, "Логин Steam"),
            "логин 65 символов": ({**good, "steam_login": "a" * 65}, "Логин Steam"),
            "логин-массив": ({**good, "steam_login": ["gaben"]}, "Логин Steam"),
            "метод ../../etc": ({**good, "method": "../../etc/passwd"}, "Выберите способ оплаты"),
            "промокод из 2 букв": ({**good, "promo": "AB"}, "Такого промокода нет"),
            "10 КБ мусора в промокоде": ({**good, "promo": "A" * 10_000}, "Такого промокода нет"),
        }
        for name, (body, words) in bad.items():
            r = await c.post("/api/orders", json=body, headers=me)
            check(r.status_code == 422 and words in r.json()["detail"], f"{name} → 422 «{r.json().get('detail')}»")
        r = await c.post("/api/orders", json={**good, "promo": "", "currency": "rub"}, headers=me)
        check(r.status_code == 200, "пустой промокод и «rub» маленькими — принимаются как обычно")

        print("3. сайт: остальное")
        before = calls["n"]
        r = await c.post("/api/steam/check", json={"login": "<script>alert(1)</script>"}, headers=H)
        check(r.status_code == 422 and calls["n"] == before, "мусорный логин отсекается до запроса к поставщику")
        r = await c.post("/api/orders/SH23456789/claim", json={"txid": "short"}, headers=me)
        check(r.status_code == 422 and "хэш" in r.json()["detail"], "короткий хэш транзакции → «не похоже на хэш»")
        for pid in ("sh-lowercase", "SH'--", "A" * 40, "..%2F..%2Fetc"):
            r = await c.get(f"/api/orders/{pid}", headers=me)
            check(r.status_code == 404, f"заказ «{pid[:20]}» — не похоже на номер → 404, до базы не доходит")
        for iid in ("0", "-1", "abc", str(2**64)):
            r = await c.get(f"/api/invoices/{iid}/qr.svg", headers=me)
            check(r.status_code == 404, f"счёт «{iid[:12]}» → 404")
        r = await c.post("/api/me/lang", json={"lang": "de"}, headers=me)
        check(r.status_code == 422, "язык не из списка → 422")
        r = await c.post("/api/deposits", json={"amount_usd": "1e400", "method": "usdt_trc20"}, headers=me)
        check(r.status_code == 422, "сумма 1e400 → 422 (не бесконечность и не переполнение)")

        print("4. сайт: настройки API")
        r = await c.post("/api/account/api-key", headers=me)
        check(r.status_code == 200, "ключ создан")
        r = await c.post("/api/account/settings", json={"allowed_ips": "1.2.3.4, bad-ip", "webhook_url": ""}, headers=me)
        check(r.status_code == 422 and r.json()["detail"] == "Некорректный IP: bad-ip", "плохой IP назван по имени")
        r = await c.post("/api/account/settings", json={"allowed_ips": ",".join(f"10.0.0.{i}" for i in range(21))},
                         headers=me)
        check(r.status_code == 422 and "20" in r.json()["detail"], "больше 20 IP → отказ")
        r = await c.post("/api/account/settings", json={"allowed_ips": " 1.2.3.4;2001:DB8::1  1.2.3.4 "}, headers=me)
        acc = (await c.get("/api/account", headers=me)).json()
        check(r.status_code == 200 and acc["api_key"]["allowed_ips"] == "1.2.3.4,2001:db8::1",
              "список IP сохранён в чистом виде: без дублей, IPv6 в одном регистре")
        await c.post("/api/account/settings", json={"allowed_ips": ""}, headers=me)
        key = (await c.post("/api/account/api-key", headers=me)).json()["key"]

        print("5. вход из Telegram и ссылка из бота")
        for name, user in {"id — строка": {"id": "abc", "first_name": "X"}, "без id": {"first_name": "X"},
                           "id отрицательный": {"id": -5, "first_name": "X"},
                           "имя — объект": {"id": 7001, "first_name": {"a": 1}}}.items():
            r = await c.post("/api/auth/webapp", json={"init_data": signed_init_data(user)}, headers=H)
            check(r.status_code == 401, f"подписано Telegram, но {name} → 401, а не падение сервера")
        r = await c.post("/api/auth/webapp", json={"init_data": signed_init_data(
            {"id": 7002, "first_name": "Ok", "is_premium": True, "allows_write_to_pm": True, "photo_url": "https://t.me/i"})},
            headers=H)
        check(r.status_code == 200, "новые поля Telegram (is_premium, photo_url…) вход не ломают")
        r = await c.get("/auth/tg", params={"t": "<script>", "k": "x"}, follow_redirects=False)
        check(r.status_code == 303 and r.headers["location"] == "/", "испорченная ссылка входа → просто на главную")

        print("6. вебхук поставщика")
        r = await c.post("/api/webhooks/nervixy", **webhook(b'{"order_id": 123, "event": "order.delivered"}', sign=False))
        check(r.status_code == 403, "без подписи → 403")
        r = await c.post("/api/webhooks/nervixy", **webhook(b'{"order_id": 123, "status": "delivered", "new_field": 1}'))
        check(r.status_code == 200, "номер заказа числом и новое поле у поставщика → принимается")
        r = await c.post("/api/webhooks/nervixy", **webhook(b'{"order_id": {"$ne": null}, "status": "delivered"}'))
        check(r.status_code == 400, "объект вместо номера заказа → 400 (даже с верной подписью)")
        r = await c.post("/api/webhooks/nervixy", **webhook(b'not json'))
        check(r.status_code == 400, "не JSON → 400")
        r = await c.post("/api/webhooks/nervixy", **webhook(b'{"order_id": null, "event": null, "status": null}'))
        check(r.status_code == 200, "пустые поля → спокойно игнорируется")

        print("7. партнёрское API")
        K = {"X-API-Key": key}
        good = {"steam_login": "gooduser", "amount": 1000, "currency": "RUB"}
        r = await c.post("/api/v1/orders", json={**good, "method": "usdt_trc20"}, headers=K)
        j = r.json()
        check(r.status_code == 400 and j["ok"] is False and "method" in j["fields"],
              "лишнее поле → 400, партнёру названо поле и причина")
        for name, body in {"сумма true": {**good, "amount": True}, "сумма 100.123": {**good, "amount": 100.123},
                           "валюта EUR": {**good, "currency": "EUR"}, "external_id с пробелом": {**good, "external_id": "a b"},
                           "логин с кавычкой": {**good, "steam_login": "x'"}}.items():
            r = await c.post("/api/v1/orders", json=body, headers=K)
            check(r.status_code == 400 and r.json()["ok"] is False, f"{name} → 400 {{ok:false}}")
        r = await c.post("/api/v1/quote", json={"amount": 1000.5, "currency": "kzt"}, headers=K)
        check(r.status_code == 200 and r.json()["amount"] == "1000.5" and r.json()["currency"] == "KZT",
              "сумма числом с дробью и валюта маленькими — как раньше")
        for q in ("limit=1000", "limit=0", "offset=-1", "limit=abc"):
            r = await c.get(f"/api/v1/orders?{q}", headers=K)
            check(r.status_code == 400 and r.json()["ok"] is False, f"/orders?{q} → 400")
        r = await c.get("/api/v1/orders?limit=100&offset=0", headers=K)
        check(r.status_code == 200, "limit=100 — можно")
        r = await c.get("/api/v1/orders/not-an-id!", headers=K)
        check(r.status_code == 404 and r.json() == {"ok": False, "error": "Не найдено"}, "битый номер заказа → 404 {ok:false}")
        r = await c.post("/api/v1/orders", content=b"{broken", headers={**K, "Content-Type": "application/json"})
        check(r.status_code == 400 and "JSON" in r.json()["error"], "битый JSON → 400 «некорректный JSON»")

        print("8. мусор на каждый вход — никогда не 500")
        junk = [None, [], [1, 2], "str", 12, 1e308, True, {"a": {"b": {"c": [None] * 50}}}, {"": ""},
                {k: "x" * 5000 for k in ("steam_login", "amount", "currency", "method", "promo", "code", "txid",
                                          "lang", "init_data", "login", "amount_usd", "allowed_ips", "webhook_url")}]
        targets = [("/api/orders", me), ("/api/steam/check", H), ("/api/promo/check", H), ("/api/promo/redeem", me),
                   ("/api/me/lang", me), ("/api/auth/webapp", H), ("/api/deposits", me), ("/api/account/settings", me),
                   ("/api/orders/SH23456789/claim", me), ("/api/deposits/DP23456789/claim", me),
                   ("/api/v1/orders", K), ("/api/v1/quote", K), ("/api/v1/deposits", K), ("/api/v1/steam/check", K)]
        worst = 0
        for path, hdr in targets:
            for j in junk:
                r = await c.post(path, json=j, headers=hdr)
                worst = max(worst, r.status_code)
                assert r.status_code < 500, (path, j, r.status_code, r.text[:200])
        check(worst < 500, f"{len(targets) * len(junk)} мусорных запросов на {len(targets)} адресов — ни одного 500")

        print("9. документация API на двух языках")
        docs = lambda r: "en" if '<html lang="en">' in r.text else "ru" if '<html lang="ru">' in r.text else "?"  # noqa: E731
        c.cookies.clear()
        r = await c.get("/api-docs")
        check(r.status_code == 200 and docs(r) == "ru" and "{{" not in r.text, "без подсказок — по-русски, шаблон заполнен")
        r = await c.get("/api-docs", headers={"Accept-Language": "en-GB,en;q=0.9"})
        check(docs(r) == "en" and r.headers["content-language"] == "en" and "https://sh.test/api/v1" in r.text,
              "английский браузер → английская версия с адресом API")
        r = await c.get("/api-docs", headers={"Cookie": "sh_lang=en", "Accept-Language": "ru"})
        check(docs(r) == "en", "сайт показан по-английски (cookie) → документация тоже, хотя браузер русский")
        r = await c.get("/api-docs?lang=ru", headers={"Cookie": "sh_lang=en"})
        check(docs(r) == "ru" and "sh_lang=ru" in r.headers.get("set-cookie", ""), "переключатель RU на странице важнее и запоминается")
        c.cookies.clear()  # the client keeps the cookie from the switch above, like a browser would
        r = await c.get("/api-docs?lang=xx", headers={"Accept-Language": "en"})
        check(r.status_code == 200 and docs(r) == "en" and "set-cookie" not in r.headers, "мусор в ?lang= игнорируется")
        r = await c.get("/assets/docs.css")
        check(r.status_code == 200 and ".docs" in r.text, "стили документации отдаются (/assets/docs.css)")

    print(f"\nALL GOOD: {OK} checks passed")


asyncio.run(main())
