"""The database layer: migrations, exact decimals, long free text, serialize().

Run:  .venv\\Scripts\\python.exe tests\\test_db.py                        (a throw-away SQLite file)
      set DATABASE_URL=postgresql://user:pw@127.0.0.1:5432/empty_db  first to check PostgreSQL (the database is wiped)
"""
import asyncio
import os
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

os.environ.update({"SH_ENV_FILE": os.devnull, "DATA_DIR": tempfile.mkdtemp(prefix="sh-db-"), "NERVIXY_MOCK": "true",
                   "BOT_TOKEN": "", "MONITOR_ENABLED": "false", "F2B_ENABLED": "false"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic import command  # noqa: E402
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import inspect, select, text  # noqa: E402

from app import db  # noqa: E402
from app.models import Base, LedgerEntry, Order, Transfer, User  # noqa: E402
from app.utils import utcnow  # noqa: E402

OK = 0
KIND = "PostgreSQL" if not db.SQLITE else "SQLite"


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


def head() -> str:
    return ScriptDirectory.from_config(db.alembic_config()).get_current_head()


async def wipe():
    """An empty database: every table gone (ours and alembic_version)."""
    async with db.engine.begin() as conn:
        def drop(c):
            for name in reversed(inspect(c).get_table_names()):
                c.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE' if not db.SQLITE else f'DROP TABLE IF EXISTS "{name}"'))
        await conn.run_sync(drop)


async def state():
    async with db.engine.connect() as conn:
        def look(c):
            ctx = MigrationContext.configure(c, opts={"render_item": None})
            diff = compare_metadata(MigrationContext.configure(c), Base.metadata)
            return ctx.get_current_revision(), set(inspect(c).get_table_names()), diff
        return await conn.run_sync(look)


async def main():
    print(f"база: {KIND}")
    await wipe()

    print("1. новая база создаётся миграциями")
    await db.init_db()
    rev, tables, diff = await state()
    check(rev == head() == "0001", f"ревизия = последняя ({rev})")
    check({t.name for t in Base.metadata.sorted_tables} <= tables, f"все {len(Base.metadata.sorted_tables)} таблиц на месте")
    check(not diff, f"схема в базе = модели (autogenerate не видит разницы){': ' + str(diff) if diff else ''}")
    await db.init_db()
    check((await state())[0] == head(), "повторный запуск ничего не ломает")

    print("2. откат и накат")
    async with db.engine.begin() as conn:
        await conn.run_sync(lambda c: command.downgrade(db.alembic_config(c), "base"))
    rev, tables, _ = await state()
    check(rev is None and "users" not in tables, "downgrade base убирает все таблицы")
    await db.init_db()
    check((await state())[0] == head(), "upgrade head возвращает схему")

    print("3. база, созданная до миграций, подхватывается")
    await wipe()
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # how the app made databases until now
        await conn.execute(text("ALTER TABLE transfers DROP COLUMN xcheck_alerted"))  # one column "from a newer version"
        await conn.execute(text("INSERT INTO users (tg_id, balance_micro, is_banned, bot_blocked, created_at, updated_at) "
                                "VALUES (777, 5000000, false, false, :t, :t)"), {"t": utcnow()})
    await db.init_db()
    rev, tables, diff = await state()
    check(rev == head(), "старая база получила ревизию")
    check(not diff, f"недостающая колонка добавлена, схема совпадает с моделями{': ' + str(diff) if diff else ''}")
    async with db.session_scope() as s:
        u = (await s.execute(select(User).where(User.tg_id == 777))).scalar_one()
    check(u.balance_micro == 5_000_000, "данные сохранились (баланс клиента на месте)")

    print("4. точные суммы и длинный текст")
    await wipe()
    await db.init_db()
    long_comment = "x" * 1000
    async with db.session_scope() as s:
        u = User(tg_id=1, username="u" * 100, first_name="Имя" * 100)
        s.add(u)
        await s.flush()
        o = Order(public_id="SHTEST0001", user_id=u.id, steam_login="gaben", currency="RUB", amount=Decimal("1000.50"),
                  fx_rate=Decimal("83.889920000"), nominal_micro=1, price_micro=1, cost_micro=1,
                  client_discount=Decimal("3.75"), supplier_discount=Decimal("4.5"))
        tr = Transfer(uid="ton:abc:0", method="ton", txid="abc", to_address="EQ", units="123456789012345678901234567890",
                      amount=Decimal("1234.123456789012345678"), comment=long_comment)
        s.add_all([o, tr])
        await s.flush()
        s.add(LedgerEntry(user_id=u.id, delta_micro=1, balance_after_micro=1, kind="adjust", comment="к" * 900))
    async with db.session_scope() as s:
        o = (await s.execute(select(Order))).scalar_one()
        tr = (await s.execute(select(Transfer))).scalar_one()
        u = (await s.execute(select(User))).scalar_one()
        le = (await s.execute(select(LedgerEntry))).scalar_one()
    check(o.amount == Decimal("1000.5") and str(o.amount) == "1000.5" and str(o.fx_rate) == "83.88992",
          "десятичные возвращаются точно и в одном виде в обеих базах (1000.5, 83.88992)")
    check(tr.amount == Decimal("1234.123456789012345678") and tr.units == "123456789012345678901234567890",
          "18 знаков после точки и числа больше int64 (wei) без потерь")
    check(len(tr.comment) == 256 and len(u.username) == 64 and len(u.first_name) == 128 and len(le.comment) == 300,
          "длинный внешний текст обрезается по колонке, запись не падает (комментарий TON, имя, комментарий админа)")
    if not db.SQLITE:
        async with db.engine.connect() as conn:
            t = (await conn.execute(text("SELECT data_type FROM information_schema.columns "
                                         "WHERE table_name = 'orders' AND column_name = 'amount'"))).scalar_one()
        check(t == "numeric", "в PostgreSQL суммы — настоящий numeric, не текст")

    print("5. serialize(): проверки «по всем строкам» идут по одной")
    order: list[str] = []

    async def worker(name: str, hold: float):
        async with db.session_scope() as s:
            await db.serialize(s, "test-lock")
            order.append(f"{name}+")
            await asyncio.sleep(hold)
            order.append(f"{name}-")
            await s.execute(text("SELECT 1"))

    t0 = time.monotonic()
    first = asyncio.create_task(worker("a", 0.4))
    await asyncio.sleep(0.05)
    await asyncio.gather(first, worker("b", 0.0))
    check(order == ["a+", "a-", "b+", "b-"] and time.monotonic() - t0 >= 0.4, f"второй ждёт конца первого ({order})")
    if not db.SQLITE:
        order.clear()

        async def other(name):
            async with db.session_scope() as s:
                await db.serialize(s, f"other-{name}")
                order.append(f"{name}+")
                await asyncio.sleep(0.2)
                order.append(f"{name}-")
        await asyncio.gather(other("x"), other("y"))
        check(order[:2] == ["x+", "y+"], "разные имена друг друга не ждут (PostgreSQL)")

    if not db.SQLITE:
        print("6. перенос данных SQLite → PostgreSQL (tools/sqlite_to_pg.py)")
        import sqlite3
        import subprocess
        from sqlalchemy import create_engine
        src = Path(os.environ["DATA_DIR"]) / "old.sqlite3"
        eng = create_engine(f"sqlite:///{src.as_posix()}")
        Base.metadata.create_all(eng)
        eng.dispose()
        con = sqlite3.connect(src)
        now = "2026-09-30 12:00:00.000000"
        con.execute("ALTER TABLE transfers DROP COLUMN xcheck_alerted")  # an older version of the app
        for uid, bal in ((1, 7_000_000), (2, 0), (5, 1_250_000)):
            con.execute("INSERT INTO users (id, tg_id, username, balance_micro, is_banned, bot_blocked, created_at, "
                        "updated_at) VALUES (?, ?, ?, ?, 0, 0, ?, ?)", (uid, 100 + uid, f"u{uid}", bal, now, now))
            if bal:
                con.execute("INSERT INTO ledger (user_id, delta_micro, balance_after_micro, kind, comment, created_at) "
                            "VALUES (?, ?, ?, 'deposit', ?, ?)", (uid, bal, bal, "к" * 600, now))  # longer than the column
        con.execute("INSERT INTO orders (id, public_id, user_id, steam_login, currency, amount, fx_rate, nominal_micro, "
                    "price_micro, cost_micro, client_discount, supplier_discount, status, charged_micro, attempts, "
                    "admin_alerted, created_at, updated_at) VALUES (40, 'SHOLD00001', 1, 'gaben', 'RUB', '1000.5', "
                    "'83.88992', 1, 1, 1, '3.75', '4.5', 'delivered', 1, 0, 0, ?, ?)", (now, now))
        con.execute("INSERT INTO transfers (id, uid, method, txid, to_address, units, amount, comment, confirmations, final, "
                    "status, admin_alerted, xcheck_ok, xcheck_tries, created_at, updated_at) VALUES (9, 'ton:t:0', 'ton', "
                    "'t', 'EQ', '1500000000', '1.5', ?, 1, 1, 'credited', 0, 1, 0, ?, ?)", ("m" * 900, now, now))
        con.commit()
        con.close()
        await wipe()
        await db.engine.dispose()
        tool = [sys.executable, str(Path(__file__).resolve().parent.parent / "tools" / "sqlite_to_pg.py"),
                "--sqlite", str(src), "--pg", os.environ["DATABASE_URL"]]
        r = subprocess.run(tool, capture_output=True, text=True, encoding="utf-8")
        check(r.returncode == 0 and "Балансы сходятся" in r.stdout, f"перенос прошёл{'' if r.returncode == 0 else ': ' + r.stdout + r.stderr}")
        async with db.session_scope() as s:
            users = (await s.execute(select(User.id, User.balance_micro).order_by(User.id))).all()
            o = (await s.execute(select(Order))).scalar_one()
            tr = (await s.execute(select(Transfer))).scalar_one()
            le = (await s.execute(select(LedgerEntry).order_by(LedgerEntry.id))).scalars().first()
        check([tuple(u) for u in users] == [(1, 7_000_000), (2, 0), (5, 1_250_000)], "клиенты и балансы те же, id сохранены")
        check(o.id == 40 and o.amount == Decimal("1000.5") and o.fx_rate == Decimal("83.88992"), "заказ и точные суммы перенесены")
        check(tr.xcheck_alerted is False and len(tr.comment) == 256 and len(le.comment) == 300,
              "колонка, которой в старой базе не было, получила значение по умолчанию; длинный текст обрезан")
        async with db.session_scope() as s:
            s.add(User(tg_id=999))
            await s.flush()
            new_id = (await s.execute(select(User.id).where(User.tg_id == 999))).scalar_one()
        check(new_id == 6, f"новые записи получают id после перенесённых ({new_id})")
        r2 = subprocess.run(tool, capture_output=True, text=True, encoding="utf-8")
        check(r2.returncode == 3, "в непустую базу второй раз не переносит")

    await db.engine.dispose()
    print(f"\nALL GOOD: {OK} checks passed")


asyncio.run(main())
