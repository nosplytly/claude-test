"""Database backups.

Once a day: an online snapshot of the database into data/backups (the last 14 are kept) — PostgreSQL: pg_dump
(custom format); SQLite: a copy of the file. Each snapshot is checked right away — the copy is readable, and every
client's balance equals the sum of their ledger (PostgreSQL: counted inside the very snapshot pg_dump saves) — then
gzip'ed, encrypted with BACKUP_PASSWORD (scrypt -> AES-256-GCM) and sent to the admins in Telegram: if the server's
disk dies, the latest copy is in the admin's chat, and nobody can read it without the password.

Restore: tools/restore_backup.py <file>. The bot command /backup makes one right now.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT, settings

log = logging.getLogger("sh.backup")
KEEP = 14
EVERY = 24 * 3600
MAGIC = b"SHBK1"
TELEGRAM_MAX = 45 * 1024 * 1024  # bots may send documents up to 50 MB


# the books check, the same SQL for both databases
COUNTS = {"users": "SELECT count(*) FROM users", "orders": "SELECT count(*) FROM orders",
          "ledger": "SELECT count(*) FROM ledger", "balances_micro": "SELECT coalesce(sum(balance_micro), 0) FROM users"}
MISMATCHED = ("SELECT u.id, u.balance_micro, coalesce(sum(l.delta_micro), 0) FROM users u "
              "LEFT JOIN ledger l ON l.user_id = u.id GROUP BY u.id, u.balance_micro "
              "HAVING u.balance_micro != coalesce(sum(l.delta_micro), 0)")


def _new_path(suffix: str) -> Path:
    out_dir = settings.data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"supplierhub-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    path, n = out_dir / f"{stamp}{suffix}", 1
    while path.exists():  # two backups in the same second (/backup tapped twice) must not overwrite each other
        n += 1
        path = out_dir / f"{stamp}-{n}{suffix}"
    return path


def _rotate(suffix: str) -> None:
    for old in sorted((settings.data_dir / "backups").glob(f"supplierhub-*{suffix}"))[:-KEEP]:
        old.unlink(missing_ok=True)


# ------------------------------------------------------------------ SQLite (plain sqlite3: runs in a thread)
def snapshot() -> Path | None:
    if not settings.db_url.startswith("sqlite"):
        return None
    src_path = settings.data_dir / "supplierhub.sqlite3"
    if not src_path.exists():
        return None
    dst_path = _new_path(".sqlite3")
    src, dst = sqlite3.connect(src_path), sqlite3.connect(dst_path)
    try:
        src.backup(dst)  # consistent snapshot even while the app is writing
    finally:
        dst.close()
        src.close()
    _rotate(".sqlite3")
    return dst_path


def verify(path: Path) -> dict:
    """Is this database file whole and do the books balance? {"ok", "integrity", "users", "orders", "ledger",
    "balances_micro", "mismatched": [(user_id, balance, ledger_sum)]}"""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        info = {"integrity": con.execute("PRAGMA integrity_check").fetchone()[0],
                **{k: con.execute(q).fetchone()[0] for k, q in COUNTS.items()},
                "mismatched": con.execute(MISMATCHED).fetchall()}
    finally:
        con.close()
    info["ok"] = info["integrity"] == "ok" and not info["mismatched"]
    return info


# ------------------------------------------------------------------ PostgreSQL (pg_dump / pg_restore)
def pg_tool(name: str) -> str:
    """pg_dump / pg_restore: PG_BIN from .env, the copy install.ps1 puts next to the app (pgsql\\bin), or PATH."""
    for folder in filter(None, (settings.pg_bin, str(ROOT / "pgsql" / "bin"))):
        for exe in (f"{name}.exe", name):
            if (Path(folder) / exe).is_file():
                return str(Path(folder) / exe)
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"{name} не найден: укажи в .env PG_BIN — папку bin PostgreSQL")
    return found


def pg_args(url: str) -> tuple[list[str], dict, str]:
    """Connection arguments for the PostgreSQL tools: (args, env with the password, database name)."""
    from sqlalchemy.engine import make_url

    u = make_url(url)
    args = ["-h", u.host or "127.0.0.1", "-p", str(u.port or 5432), "-U", u.username or "postgres"]
    return args, {**os.environ, "PGPASSWORD": u.password or "", "PGCONNECT_TIMEOUT": "15"}, u.database or "postgres"


def pg_archive_ok(path: Path) -> str:
    """'ok' if pg_restore can read the archive and it holds our tables' data, else what went wrong."""
    r = subprocess.run([pg_tool("pg_restore"), "--list", str(path)], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return (r.stderr or r.stdout).strip()[:300] or f"pg_restore exit {r.returncode}"
    missing = [t for t in ("users", "orders", "ledger") if f"TABLE DATA public {t} " not in r.stdout]
    return "ok" if not missing else f"в архиве нет данных таблиц: {', '.join(missing)}"


async def pg_snapshot() -> tuple[Path, dict]:
    """pg_dump of the live database. The books are counted in a REPEATABLE READ transaction whose snapshot pg_dump
    then uses (--snapshot): the check describes exactly the data in the file, though the app keeps writing."""
    from sqlalchemy import text

    from .db import engine

    path = _new_path(".dump")
    args, env, dbname = pg_args(settings.db_url)
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="REPEATABLE READ")
        async with conn.begin():
            snap = (await conn.execute(text("SELECT pg_export_snapshot()"))).scalar_one()
            info = {k: int((await conn.execute(text(q))).scalar_one()) for k, q in COUNTS.items()}
            info["mismatched"] = [tuple(int(x) for x in row) for row in (await conn.execute(text(MISMATCHED))).all()]
            r = await asyncio.to_thread(
                subprocess.run, [pg_tool("pg_dump"), *args, "--format=custom", f"--snapshot={snap}", "--no-owner",
                                 "--no-privileges", "--file", str(path), dbname],
                capture_output=True, text=True, env=env, timeout=3600)
    if r.returncode != 0:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump: {(r.stderr or r.stdout).strip()[:300]}")
    info["integrity"] = await asyncio.to_thread(pg_archive_ok, path)
    info["ok"] = info["integrity"] == "ok" and not info["mismatched"]
    _rotate(".dump")
    return path, info


async def verify_pg(url: str) -> dict:
    """The books check on a PostgreSQL database (a restored copy): same keys as verify()."""
    import asyncpg

    conn = await asyncpg.connect("postgresql://" + url.split("://", 1)[1])
    try:
        info = {k: int(await conn.fetchval(q)) for k, q in COUNTS.items()}
        info["mismatched"] = [tuple(int(x) for x in row) for row in await conn.fetch(MISMATCHED)]
    finally:
        await conn.close()
    info["integrity"] = "ok"
    info["ok"] = not info["mismatched"]
    return info


# ------------------------------------------------------------------ encryption
def _key(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def encrypt(data: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(_key(password, salt)).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, password: str) -> bytes:
    """Raises ValueError on a wrong password or a damaged file (AES-GCM checks every byte)."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not blob.startswith(MAGIC):
        raise ValueError("это не файл бэкапа SupplierHub")
    n = len(MAGIC)
    salt, nonce, body = blob[n:n + 16], blob[n + 16:n + 28], blob[n + 28:]
    try:
        return AESGCM(_key(password, salt)).decrypt(nonce, body, MAGIC)
    except InvalidTag as err:
        raise ValueError("неверный пароль или файл повреждён") from err


def pack(path: Path, password: str) -> bytes:
    return encrypt(gzip.compress(path.read_bytes(), compresslevel=6), password)


def unpack(blob: bytes, password: str) -> bytes:
    return gzip.decompress(decrypt(blob, password))


# ------------------------------------------------------------------ the job
def _caption(info: dict, name: str, size: int) -> str:
    from .money import usd_str
    from .tgui import e

    state = f"{e('ok')} база цела, балансы сходятся" if info["ok"] else f"{e('alarm')} <b>ПРОБЛЕМА С БАЗОЙ</b>"
    return (f"{e('shield')} <b>Бэкап базы</b> · {name}\n{state}\n"
            f"Клиентов: {info['users']} · заказов: {info['orders']} · операций: {info['ledger']}\n"
            f"На балансах клиентов: ${usd_str(info['balances_micro'])} · файл {size // 1024} КБ\n"
            f"<i>Зашифрован паролем BACKUP_PASSWORD. Восстановить: tools/restore_backup.py</i>")


async def backup_now(*, send: bool = True) -> dict:
    """Snapshot, check, and (with a password) send it to the admins. Returns what happened."""
    from . import notify
    from .kv import kv_set
    from .notify import esc
    from .tgui import e

    if settings.db_url.startswith("sqlite"):
        path = await asyncio.to_thread(snapshot)
        if path is None:
            return {"ok": False, "error": "нет базы SQLite"}
        info = await asyncio.to_thread(verify, path)
    else:
        try:
            path, info = await pg_snapshot()
        except Exception as err:  # noqa: BLE001  (pg_dump missing or failing: the admins must hear of it)
            log.exception("PostgreSQL backup failed")
            await notify.notify_admins(f"{e('alarm')} <b>Бэкап базы не сделан</b>\n<code>{esc(str(err)[:300])}</code>",
                                       key="backup-failed", every=6 * 3600)
            return {"ok": False, "error": str(err)}
    info.update(path=str(path), sent=0)
    log.info("database backup %s: %s", path.name, {k: v for k, v in info.items() if k != "mismatched"})
    if not info["ok"]:
        await notify.notify_admins(
            f"{e('alarm')} <b>Бэкап показал проблему с базой</b>\n"
            f"Проверка целостности: <code>{info['integrity']}</code>\n"
            f"Клиентов, у кого баланс не сходится с журналом: {len(info['mismatched'])}\n"
            f"Файл: <code>{path.name}</code>", key="backup-bad", every=6 * 3600)
    await kv_set("backup:last", time.time())
    if not send:
        return info
    password = settings.backup_password
    if not password:
        await notify.notify_admins(f"{e('warn')} Бэкап базы сделан только на диске сервера: в .env не задан "
                                   "BACKUP_PASSWORD, поэтому в Telegram он не отправлен. Запусти install.ps1 — "
                                   "он создаст пароль.", key="backup-nopass", every=7 * 24 * 3600)
        return info
    blob = await asyncio.to_thread(pack, path, password)
    if len(blob) > TELEGRAM_MAX:
        await notify.notify_admins(f"{e('warn')} Бэкап {len(blob) // 1048576} МБ — больше лимита Telegram, "
                                   "лежит только на диске сервера.", key="backup-big", every=7 * 24 * 3600)
        return info
    name = path.name + ".gz.enc"
    caption = _caption(info, path.stem.removeprefix("supplierhub-"), len(blob))
    for aid in settings.admin_ids:
        if await notify.send_document(aid, blob, name, caption):
            info["sent"] += 1
    return info


async def run_backups() -> None:
    from .kv import kv_get

    await asyncio.sleep(60)
    while True:
        try:
            last = float(await kv_get("backup:last", 0) or 0)
            if time.time() - last >= EVERY:  # restarts don't cause extra backups: the schedule lives in the DB
                await backup_now()
        except Exception:
            log.exception("backup failed")
        await asyncio.sleep(3600)
