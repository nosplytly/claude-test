"""Alembic environment.

The app migrates at start: app.db.init_db() hands over its own connection. On the command line (alembic.ini in the
project root) it connects by itself, to the same database (DATABASE_URL, or the SQLite file in DATA_DIR):

    .venv\\Scripts\\alembic revision --autogenerate -m "what changed"   # after editing app/models.py
    .venv\\Scripts\\alembic upgrade head                                # the app does this itself at start
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base, Clip, DecText

config = context.config
target_metadata = Base.metadata
if config.config_file_name:  # the command line; the app has its own logging
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def render_item(type_, obj, autogen_context):
    """Our column types, written into migrations as plain SQLAlchemy types, so a migration never depends on how
    app/models.py looks later."""
    if type_ == "type" and isinstance(obj, Clip):
        return f"sa.String(length={obj.impl.length})"
    if type_ == "type" and isinstance(obj, DecText):
        return 'sa.String(length=80).with_variant(sa.Numeric(), "postgresql")'
    return False


def run_with(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_item=render_item,
                      render_as_batch=connection.dialect.name == "sqlite")  # SQLite ALTERs by copying the table
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    from app.config import settings

    engine = create_async_engine(settings.db_url)
    async with engine.connect() as conn:
        await conn.run_sync(run_with)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      render_item=render_item, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    run_with(config.attributes["connection"])
else:
    asyncio.run(run_online())
