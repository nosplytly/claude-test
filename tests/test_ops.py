"""Operations: /healthz for uptime monitors, the in-app watchdog, and the morning report.

Run:  .venv\\Scripts\\python.exe tests\\test_ops.py
Throw-away database, mock supplier, recorded "Telegram": nothing leaves the machine.
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "dev", "DATA_DIR": tempfile.mkdtemp(prefix="sh-ops-"),
                   "NERVIXY_MOCK": "true", "BOT_TOKEN": "", "ADMIN_TG_IDS": "999", "MONITOR_ENABLED": "false",
                   "REPORT_HOUR_MSK": "9"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import health, notify, report  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.ledger import change_balance  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Order, User  # noqa: E402

OK = 0
SENT: list[str] = []


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


async def fake_send(chat_id, text, buttons=None):
    SENT.append(text)
    return len(SENT)


notify.send = fake_send


def order(uid, status, finished, price, paid):
    return Order(public_id=f"SH{abs(hash((uid, status, finished))) % 10**8:08d}", user_id=uid, steam_login="x",
                 currency="RUB", amount=Decimal("100"), fx_rate=Decimal("80"), nominal_micro=1_250_000,
                 price_micro=price, cost_micro=paid, client_discount=Decimal("3.75"), supplier_discount=Decimal("4.5"),
                 status=status, nervixy_paid_micro=paid, finished_at=finished)


async def main():
    await init_db()
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t")

    print("1. /healthz для мониторинга аптайма")
    r = await c.get("/healthz")
    check(r.status_code == 200 and r.json()["ok"] is True, "сразу после старта (грейс-период) → 200 ok")
    health.STARTED -= 1000  # as if the app had been up for a while
    r = await c.get("/healthz")
    body = r.json()
    check(r.status_code == 503 and body["ok"] is False and "обработка заказов" in body["problems"]
          and "зачисление платежей" in body["problems"],
          f"циклы молчат → 503 и что именно стоит: {body['problems']}")
    for name in health._watched():  # orders, settle, webhooks (+ telegram, notify with a bot)
        health.beat(name)
    r = await c.get("/healthz")
    check(r.status_code == 200 and r.json()["problems"] == [], "циклы отметились → снова 200")
    r = await c.head("/healthz")
    check(r.status_code == 200, "HEAD /healthz тоже работает (некоторые мониторы шлют HEAD)")

    print("2. сторож внутри приложения")
    health._beats["orders"] -= 1000
    seen = await health.check_once([])
    check(seen == ["orders"] and not any("не полностью" in t for t in SENT), "одна неудачная проверка → ещё не тревога")
    seen = await health.check_once(seen)
    check(any("обработка заказов не отвечает" in t for t in SENT), "вторая подряд → админу алерт «Сервис работает не полностью»")
    n = len(SENT)
    await health.check_once(seen)
    check(len(SENT) == n, "дальше не спамит (не чаще раза в 30 минут)")

    print("3. утренний отчёт")
    now = datetime(2026, 9, 29, 7, 0)  # 10:00 МСК
    y_start = datetime(2026, 9, 27, 21, 0)  # 28.09 00:00 МСК
    async with session_scope() as s:
        a = User(tg_id=11, username="a", created_at=y_start + timedelta(hours=5))
        b = User(tg_id=12, username="b", created_at=y_start - timedelta(days=3))
        s.add_all([a, b])
        await s.flush()
        await change_balance(s, a.id, 7_000_000, "deposit", comment="t")
        s.add_all([order(a.id, "delivered", y_start + timedelta(hours=10), 2_000_000, 1_900_000),
                   order(a.id, "delivered", y_start + timedelta(hours=20), 3_000_000, 2_850_000),
                   order(b.id, "rejected", y_start + timedelta(hours=11), 1_000_000, 950_000),
                   order(b.id, "delivered", y_start - timedelta(hours=2), 9_000_000, 8_000_000)])  # the day before
    from app.models import LedgerEntry
    from sqlalchemy import update
    async with session_scope() as s:  # the deposit happened yesterday too
        await s.execute(update(LedgerEntry).values(created_at=y_start + timedelta(hours=3)))
    since, until, label = report.yesterday_msk(now)
    check((since, until, label) == (y_start, y_start + timedelta(days=1), "за 28.09"), "границы «вчера» по Москве верные")
    text = await report.report_text(since, until, label)
    check("Выполнено заказов: <b>2</b>" in text and "отклонено: 1" in text, "в отчёте 2 выполненных и 1 отклонённый (позавчерашний не считается)")
    check("Оборот: $5.00" in text and "маржа <b>+$0.25</b>" in text, "оборот $5.00 и маржа +$0.25 посчитаны по заказам дня")
    check("Пополнений баланса: 1 на $7.00" in text and "Новых клиентов: 1" in text, "пополнения и новые клиенты за день")
    check("Nervixy:" in text and "На балансах клиентов: $7.00" in text, "баланс Nervixy и долг перед клиентами")

    print("4. расписание")
    SENT.clear()
    check(not await report.maybe_send(datetime(2026, 9, 29, 5, 0)), "в 8:00 МСК ещё рано")
    check(await report.maybe_send(datetime(2026, 9, 29, 6, 5)), "в 9:05 МСК отчёт ушёл")
    check(any("Отчёт" in t and "за 28.09" in t for t in SENT), "админу пришёл «Отчёт · за 28.09»")
    check(not await report.maybe_send(datetime(2026, 9, 29, 6, 10)), "второй раз в тот же день — нет (и после перезапуска: дата в базе)")
    check(await report.maybe_send(datetime(2026, 9, 30, 6, 1)), "на следующее утро — снова")

    await c.aclose()
    print(f"\nALL GOOD: {OK} checks passed")


asyncio.run(main())
