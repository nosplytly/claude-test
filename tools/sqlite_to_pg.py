"""Move the data from the SQLite file to PostgreSQL (once, when the server switches over).

    Stop-Service supplierhub-app
    .venv\\Scripts\\python.exe tools\\sqlite_to_pg.py --pg postgresql://supplierhub:PASSWORD@127.0.0.1:5432/supplierhub
    (then DATABASE_URL=… in .env, Start-Service supplierhub-app)

--sqlite defaults to data\\supplierhub.sqlite3. The PostgreSQL database must be empty: the schema is created by the
migrations, then every table is copied in one transaction — all or nothing. Rows from an older version of the app
(columns it didn't have yet, empty values in them) get the model's defaults. Afterwards: row counts are compared
table by table, every balance is checked against its ledger, and the id counters continue after the copied rows.
The SQLite file is only read, never changed.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SH_ENV_FILE", os.devnull)  # everything comes from the arguments

from sqlalchemy import Integer, func, inspect, insert, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.backup import verify_pg  # noqa: E402
from app.db import _default_of, alembic_config  # noqa: E402
from app.models import Base  # noqa: E402

BATCH = 1000


def pg_url(url: str) -> str:
    return "postgresql+asyncpg://" + url.split("://", 1)[1]


async def copy(sqlite_path: Path, url: str) -> int:
    src = create_async_engine(f"sqlite+aiosqlite:///file:{sqlite_path.as_posix()}?mode=ro&uri=true")
    dst = create_async_engine(pg_url(url))
    try:
        async with src.connect() as s:
            have = await s.run_sync(lambda c: {t: {col["name"] for col in inspect(c).get_columns(t)}
                                               for t in inspect(c).get_table_names()})
        async with dst.begin() as d:
            if await d.run_sync(lambda c: inspect(c).has_table("users")):
                if (await d.execute(text("SELECT count(*) FROM users"))).scalar_one():
                    print("В базе PostgreSQL уже есть данные — переношу только в пустую базу.")
                    return 3
            from alembic import command
            await d.run_sync(lambda c: command.upgrade(alembic_config(c), "head"))

            counts = {}
            for table in Base.metadata.sorted_tables:  # parents before children: foreign keys hold at every step
                cols = [c for c in table.columns if c.name in have.get(table.name, set())]
                defaults = {c.name: _default_of(c) for c in table.columns if not c.nullable}
                n = 0
                if cols:
                    async with src.connect() as s:
                        result = await s.stream(select(*cols).order_by(*table.primary_key.columns))
                        async for chunk in result.mappings().partitions(BATCH):
                            rows = []
                            for row in chunk:
                                r = dict(row)
                                for name, default in defaults.items():
                                    if r.get(name) is None and default is not None:
                                        r[name] = default  # a column this old database didn't have, or left empty
                                rows.append(r)
                            await d.execute(insert(table), rows)
                            n += len(rows)
                counts[table.name] = n
                pk = list(table.primary_key.columns)
                if len(pk) == 1 and isinstance(pk[0].type, Integer):  # the id counter continues after the copied ids
                    await d.execute(text(f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk[0].name}'), "
                                         f"coalesce((SELECT max({pk[0].name}) FROM {table.name}), 0) + 1, false)"))
                print(f"  {table.name:16} {n:>8} строк")

            for table in Base.metadata.sorted_tables:  # the same number of rows on both sides
                if table.name not in have:
                    continue
                async with src.connect() as s:
                    a = (await s.execute(select(func.count()).select_from(table))).scalar_one()
                b = (await d.execute(select(func.count()).select_from(table))).scalar_one()
                if a != b:
                    raise RuntimeError(f"{table.name}: в SQLite {a} строк, в PostgreSQL {b}")
            async with src.connect() as s:
                src_bal = (await s.execute(text("SELECT coalesce(sum(balance_micro), 0) FROM users"))).scalar_one()
            dst_bal = (await d.execute(text("SELECT coalesce(sum(balance_micro), 0) FROM users"))).scalar_one()
            if int(src_bal) != int(dst_bal):
                raise RuntimeError(f"сумма балансов разошлась: {src_bal} → {dst_bal}")
    finally:
        await src.dispose()
        await dst.dispose()

    info = await verify_pg(url)
    print(f"Перенесено: клиентов {info['users']}, заказов {info['orders']}, операций по балансу {info['ledger']}, "
          f"на балансах ${info['balances_micro'] / 1_000_000:.2f}")
    if info["mismatched"]:
        print(f"ВНИМАНИЕ: у {len(info['mismatched'])} клиентов баланс не совпадает с журналом "
              f"(так было и в SQLite): {info['mismatched'][:5]}")
        return 1
    print("Балансы сходятся с журналом. Теперь DATABASE_URL в .env и перезапуск supplierhub-app.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Copy the SupplierHub data from SQLite to PostgreSQL")
    ap.add_argument("--pg", required=True, help="postgresql://user:password@host:5432/database (empty)")
    ap.add_argument("--sqlite", type=Path, default=ROOT / "data" / "supplierhub.sqlite3")
    a = ap.parse_args()
    if not a.sqlite.exists():
        print(f"Нет файла {a.sqlite}")
        return 2
    try:
        return asyncio.run(copy(a.sqlite, a.pg))
    except Exception as err:  # noqa: BLE001
        print(f"Перенос не выполнен, база PostgreSQL не изменена: {type(err).__name__}: {err}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
