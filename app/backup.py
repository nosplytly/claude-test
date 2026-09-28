"""Database backups.

Once a day: an online snapshot of the SQLite database into data/backups (the last 14 are kept). Each snapshot is
checked right away — SQLite integrity, and every client's balance equals the sum of their ledger — then gzip'ed,
encrypted with BACKUP_PASSWORD (scrypt -> AES-256-GCM) and sent to the admins in Telegram: if the server's disk
dies, the latest copy is in the admin's chat, and nobody can read it without the password.

Restore: tools/restore_backup.py <file>. The bot command /backup makes one right now.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

log = logging.getLogger("sh.backup")
KEEP = 14
EVERY = 24 * 3600
MAGIC = b"SHBK1"
TELEGRAM_MAX = 45 * 1024 * 1024  # bots may send documents up to 50 MB


# ------------------------------------------------------------------ snapshot + check (plain sqlite3: runs in a thread)
def snapshot() -> Path | None:
    if not settings.db_url.startswith("sqlite"):
        return None
    src_path = settings.data_dir / "supplierhub.sqlite3"
    if not src_path.exists():
        return None
    out_dir = settings.data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"supplierhub-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    dst_path, n = out_dir / f"{stamp}.sqlite3", 1
    while dst_path.exists():  # two backups in the same second (/backup tapped twice) must not overwrite each other
        n += 1
        dst_path = out_dir / f"{stamp}-{n}.sqlite3"
    src, dst = sqlite3.connect(src_path), sqlite3.connect(dst_path)
    try:
        src.backup(dst)  # consistent snapshot even while the app is writing
    finally:
        dst.close()
        src.close()
    for old in sorted(out_dir.glob("supplierhub-*.sqlite3"))[:-KEEP]:
        old.unlink(missing_ok=True)
    return dst_path


def verify(path: Path) -> dict:
    """Is this database file whole and do the books balance? {"ok", "integrity", "users", "orders", "ledger",
    "balances_micro", "mismatched": [(user_id, balance, ledger_sum)]}"""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        one = lambda q: con.execute(q).fetchone()[0]  # noqa: E731
        info = {
            "integrity": integrity,
            "users": one("SELECT count(*) FROM users"),
            "orders": one("SELECT count(*) FROM orders"),
            "ledger": one("SELECT count(*) FROM ledger"),
            "balances_micro": one("SELECT coalesce(sum(balance_micro), 0) FROM users"),
            "mismatched": con.execute(
                "SELECT u.id, u.balance_micro, coalesce(sum(l.delta_micro), 0) FROM users u "
                "LEFT JOIN ledger l ON l.user_id = u.id GROUP BY u.id "
                "HAVING u.balance_micro != coalesce(sum(l.delta_micro), 0)").fetchall(),
        }
    finally:
        con.close()
    info["ok"] = info["integrity"] == "ok" and not info["mismatched"]
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
    from .tgui import e

    path = await asyncio.to_thread(snapshot)
    if path is None:
        return {"ok": False, "error": "нет базы SQLite"}
    info = await asyncio.to_thread(verify, path)
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
