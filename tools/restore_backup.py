"""Restore a SupplierHub database backup (the file the bot sends, or one from data\\backups).

SQLite (supplierhub-….sqlite3.gz.enc):
    .venv\\Scripts\\python.exe tools\\restore_backup.py <file> [--out C:\\restore\\supplierhub.sqlite3]
  The file is decrypted, unpacked and checked (SQLite integrity + every balance equals its ledger), then written to
  --out — never over an existing file. To put it into service:
    Stop-Service supplierhub-app
    copy the restored file over data\\supplierhub.sqlite3 (delete supplierhub.sqlite3-wal / -shm next to it)
    Start-Service supplierhub-app

PostgreSQL (supplierhub-….dump.gz.enc):
    .venv\\Scripts\\python.exe tools\\restore_backup.py <file> --into postgresql://supplierhub:PASSWORD@127.0.0.1:5432/restored
  The archive is decrypted and loaded into --into, an EMPTY database (create it first:
  pgsql\\bin\\createdb -h 127.0.0.1 -U postgres -O supplierhub restored), then the books are checked there. To put it
  into service: point DATABASE_URL in .env at that database and restart supplierhub-app. Without --into the archive
  is only decrypted and checked (pg_restore --list), and written to --out.

The password is BACKUP_PASSWORD (asked for if not given with --password or in the environment).
"""
import argparse
import asyncio
import getpass
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SH_ENV_FILE", os.devnull)  # the tool never needs the live .env

from app.backup import pg_archive_ok, pg_args, pg_tool, unpack, verify, verify_pg  # noqa: E402


def report(info: dict, what: str) -> int:
    print(f"  {what}: {info['integrity']}")
    print(f"  клиентов: {info['users']}, заказов: {info['orders']}, операций по балансу: {info['ledger']}")
    print(f"  на балансах: ${info['balances_micro'] / 1_000_000:.2f}")
    if info["mismatched"]:
        print(f"  ВНИМАНИЕ: у {len(info['mismatched'])} клиентов баланс не совпадает с журналом: {info['mismatched'][:5]}")
    print("Готово к подмене базы" if info["ok"] else "Файл восстановлен, но проверка показала проблему — смотри выше")
    return 0 if info["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Restore a SupplierHub database backup")
    ap.add_argument("backup", type=Path)
    ap.add_argument("--out", type=Path, help="where to write the restored file")
    ap.add_argument("--into", help="PostgreSQL backups: an empty database to load the backup into")
    ap.add_argument("--password", help="BACKUP_PASSWORD (default: env BACKUP_PASSWORD or a prompt)")
    a = ap.parse_args()

    data = a.backup.read_bytes()
    if a.backup.name.endswith(".enc"):
        password = a.password or os.environ.get("BACKUP_PASSWORD") or getpass.getpass("BACKUP_PASSWORD: ")
        try:
            data = unpack(data, password)
        except ValueError as err:
            print(f"Не удалось расшифровать: {err}")
            return 2
    postgres = data.startswith(b"PGDMP")
    suffix = ".dump" if postgres else ".sqlite3"
    out = a.out or a.backup.with_name(a.backup.name.split(suffix)[0] + f"-restored{suffix}")
    if out.exists():
        print(f"{out} уже существует — выберите другой --out")
        return 3
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"Восстановлено: {out}")
    if not postgres:
        return report(verify(out), "целостность SQLite")

    readable = pg_archive_ok(out)
    print(f"  архив PostgreSQL: {readable}")
    if readable != "ok":
        return 1
    if not a.into:
        print("Чтобы загрузить его в базу: тот же запуск с --into postgresql://…/пустая_база")
        return 0
    args, env, dbname = pg_args(a.into)
    r = subprocess.run([pg_tool("pg_restore"), *args, "--dbname", dbname, "--no-owner", "--no-privileges",
                        "--exit-on-error", "--single-transaction", str(out)], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print(f"pg_restore не смог загрузить архив: {(r.stderr or r.stdout).strip()[:500]}")
        return 4
    print(f"Загружено в базу {dbname}")
    return report(asyncio.run(verify_pg(a.into)), "загрузка")


if __name__ == "__main__":
    sys.exit(main())
