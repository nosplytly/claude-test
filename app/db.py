"""The database: PostgreSQL (DATABASE_URL) in production, or a SQLite file in DATA_DIR (local runs, tests).

The schema is managed by Alembic (app/migrations): init_db() brings any database to the latest revision at start.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.util import await_only

from .config import settings

log = logging.getLogger("sh.db")
SQLITE = settings.db_url.startswith("sqlite")
MIGRATIONS = Path(__file__).parent / "migrations"
BASELINE = "0001"  # the revision that matches a database created before migrations existed

if SQLITE:
    engine = create_async_engine(settings.db_url, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow,
                                 connect_args={"timeout": 30})
else:
    engine = create_async_engine(
        settings.db_url, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow,
        pool_pre_ping=True, pool_recycle=1800,  # the server may drop idle connections (restart, network blip)
        connect_args={"timeout": 30, "command_timeout": 60,
                      "server_settings": {"application_name": "supplierhub", "timezone": "UTC"}})

WRITE_WAIT = 30  # seconds a transaction may queue for its turn to write before it gives up
_WRITE = re.compile(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
_writer = asyncio.Lock()
_writing: dict[int, asyncio.Task | None] = {}  # id(Connection) holding the write turn -> its task

if SQLITE:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    # SQLite lets one transaction write at a time. Left alone, every waiting writer polls the file lock (sleeping up
    # to 100 ms between tries); under load they starve each other into "database is locked". So writers queue here
    # instead: a transaction takes the turn at its first INSERT/UPDATE/DELETE and hands it on at commit/rollback —
    # first come first served, instant hand-over, and SQLite practically never sees two writers.
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _one_writer_at_a_time(conn, cursor, statement, parameters, context, executemany):
        if id(conn) in _writing or not _WRITE.match(statement):
            return
        task = asyncio.current_task()
        if task is not None and task in _writing.values():
            # the same task already holds the turn in another session: waiting would deadlock, fail loudly instead
            raise RuntimeError("second write transaction opened while the first is still open in the same task")
        await_only(asyncio.wait_for(_writer.acquire(), WRITE_WAIT))
        _writing[id(conn)] = task

    def _hand_over(conn):
        if id(conn) in _writing:
            del _writing[id(conn)]
            _writer.release()

    event.listen(engine.sync_engine, "commit", _hand_over)
    event.listen(engine.sync_engine, "rollback", _hand_over)


SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as s:
        try:
            yield s
            await s.commit()
        except BaseException:
            await s.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as s:
        yield s


def _lock_key(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


async def serialize(s: AsyncSession, name: str) -> None:
    """From here to commit, this transaction runs one at a time with every other one that called serialize() with
    the same name. For checks that read across many rows and then write — the supplier's free balance against all
    reserved orders, a unique invoice amount against all reserved ones: two transactions that both read before
    either writes would both pass. Row locks don't help there, there is no single row to lock.

    PostgreSQL: a transaction-level advisory lock, released by commit or rollback. SQLite: the transaction takes the
    write turn now (an UPDATE that changes nothing still does), and that turn is the only one there is."""
    if SQLITE:
        await s.execute(text("UPDATE kv SET value = value WHERE 1 = 0"))
    else:
        await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(name)})


# ------------------------------------------------------------------ schema: Alembic migrations
def alembic_config(connection=None):
    """For the app (it hands over its own connection) and for tools; the `alembic` command uses alembic.ini."""
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", settings.db_url.replace("%", "%%"))
    cfg.attributes["connection"] = connection
    return cfg


def _default_of(col):
    d = col.default
    if d is None:
        return None
    return d.arg(None) if d.is_callable else d.arg if d.is_scalar else None


def _add_missing_columns(conn) -> None:
    """Bring a database made before migrations to the baseline, once, before Alembic takes over. Such databases got
    new columns added as nullable, empty for old rows: a missing column is added now with its default, and the empty
    values left by earlier additions are filled with it (the code reads them as that default anyway)."""
    from sqlalchemy import literal

    from .models import Base

    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            table.create(conn)
            continue
        have = {c["name"]: c for c in insp.get_columns(table.name)}
        for col in table.columns:
            default = _default_of(col)
            if col.name not in have:
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(conn.dialect)}'
                if not col.nullable and default is not None:
                    value = literal(default, col.type).compile(dialect=conn.dialect, compile_kwargs={"literal_binds": True})
                    ddl += f" NOT NULL DEFAULT {value}"
                conn.execute(text(ddl))
            elif have[col.name]["nullable"] and not col.nullable and default is not None:
                conn.execute(table.update().where(col.is_(None)).values({col.name: default}))
        for idx in table.indexes:
            idx.create(conn, checkfirst=True)


def _migrate(conn) -> None:
    from alembic import command

    tables = set(inspect(conn).get_table_names())
    cfg = alembic_config(conn)
    if "alembic_version" not in tables and "users" in tables:
        _add_missing_columns(conn)
        command.stamp(cfg, BASELINE)
        log.info("existing database adopted by migrations at revision %s", BASELINE)
    command.upgrade(cfg, "head")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_migrate)
