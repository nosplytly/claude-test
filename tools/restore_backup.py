"""Restore a SupplierHub database backup (the file the bot sends, or one from data\\backups).

    .venv\\Scripts\\python.exe tools\\restore_backup.py supplierhub-20260929-060000.sqlite3.gz.enc
    .venv\\Scripts\\python.exe tools\\restore_backup.py <file> --out C:\\restore\\supplierhub.sqlite3

The password is BACKUP_PASSWORD (asked for if not given with --password or in the environment). The file is
decrypted, unpacked and checked (SQLite integrity + every balance equals its ledger), then written to --out —
never over an existing file. To put it into service:
    Stop-Service supplierhub-app
    copy the restored file over data\\supplierhub.sqlite3 (delete supplierhub.sqlite3-wal / -shm next to it)
    Start-Service supplierhub-app
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SH_ENV_FILE", os.devnull)  # the tool never needs the live .env

from app.backup import unpack, verify  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Restore a SupplierHub database backup")
    ap.add_argument("backup", type=Path)
    ap.add_argument("--out", type=Path, help="where to write the restored database")
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
    out = a.out or a.backup.with_name(a.backup.name.split(".sqlite3")[0] + "-restored.sqlite3")
    if out.exists():
        print(f"{out} уже существует — выберите другой --out")
        return 3
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    info = verify(out)
    print(f"Восстановлено: {out}")
    print(f"  целостность SQLite: {info['integrity']}")
    print(f"  клиентов: {info['users']}, заказов: {info['orders']}, операций по балансу: {info['ledger']}")
    print(f"  на балансах: ${info['balances_micro'] / 1_000_000:.2f}")
    if info["mismatched"]:
        print(f"  ВНИМАНИЕ: у {len(info['mismatched'])} клиентов баланс не совпадает с журналом: {info['mismatched'][:5]}")
    print("Готово к подмене базы" if info["ok"] else "Файл восстановлен, но проверка показала проблему — смотри выше")
    return 0 if info["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
