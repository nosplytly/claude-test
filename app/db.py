from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.util import await_only

from .config import settings

engine = create_async_engine(settings.db_url, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow,
                             connect_args={"timeout": 30} if settings.db_url.startswith("sqlite") else {})

WRITE_WAIT = 30  # seconds a transaction may queue for its turn to write before it gives up
_WRITE = re.compile(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
_writer = asyncio.Lock()
_writing: dict[int, asyncio.Task | None] = {}  # id(Connection) holding the write turn -> its task

if settings.db_url.startswith("sqlite"):

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


def _add_missing_columns(conn) -> None:
    """Tiny forward-only migration: add new nullable columns / indexes to existing tables."""
    from sqlalchemy import inspect, text

    from .models import Base

    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in have:
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(conn.dialect)}'
                conn.execute(text(ddl))
        for idx in table.indexes:
            idx.create(conn, checkfirst=True)


async def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
