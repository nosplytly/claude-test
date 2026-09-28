"""Daily online backup of the SQLite database into data/backups (keeps the last 14)."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from .config import settings

log = logging.getLogger("sh.backup")
KEEP = 14


def _backup_now() -> str | None:
    if not settings.db_url.startswith("sqlite"):
        return None
    src_path = settings.data_dir / "supplierhub.sqlite3"
    if not src_path.exists():
        return None
    out_dir = settings.data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst_path = out_dir / f"supplierhub-{datetime.now(timezone.utc):%Y%m%d-%H%M}.sqlite3"
    src, dst = sqlite3.connect(src_path), sqlite3.connect(dst_path)
    try:
        src.backup(dst)  # consistent snapshot even while the app is writing
    finally:
        dst.close()
        src.close()
    for old in sorted(out_dir.glob("supplierhub-*.sqlite3"))[:-KEEP]:
        old.unlink(missing_ok=True)
    return str(dst_path)


async def run_backups() -> None:
    await asyncio.sleep(60)
    while True:
        try:
            path = await asyncio.to_thread(_backup_now)
            if path:
                log.info("database backup: %s", path)
        except Exception:
            log.exception("backup failed")
        await asyncio.sleep(24 * 3600)
