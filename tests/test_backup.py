"""Backups: snapshot, books check, encryption, delivery to the admins, and an actual restore with the tool.

Run:  .venv\\Scripts\\python.exe tests\\test_backup.py
Throw-away database; the "Telegram" is a recorder — nothing leaves the machine.
"""
import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="sh-bak-")
os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "dev", "DATA_DIR": TMP, "NERVIXY_MOCK": "true", "BOT_TOKEN": "",
                   "ADMIN_TG_IDS": "999", "MONITOR_ENABLED": "false", "BACKUP_PASSWORD": "correct horse battery staple"})
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import backup as B  # noqa: E402
from app import notify  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.ledger import change_balance  # noqa: E402
from app.models import User  # noqa: E402

OK = 0
FILES: list[tuple[int, bytes, str, str]] = []
TEXTS: list[str] = []


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


async def fake_document(chat_id, data, filename, caption=""):
    FILES.append((chat_id, data, filename, caption))
    return len(FILES)


async def fake_send(chat_id, text, buttons=None):
    TEXTS.append(text)
    return len(TEXTS)


notify.send_document = fake_document
notify.send = fake_send


async def main():
    await init_db()
    async with session_scope() as s:
        for k in range(5):
            u = User(tg_id=1000 + k, username=f"c{k}")
            s.add(u)
            await s.flush()
            await change_balance(s, u.id, (k + 1) * 1_500_000, "deposit", comment="test")
            await change_balance(s, u.id, -400_000, "order", comment="test")

    print("1. снимок и сверка")
    info = await B.backup_now()
    check(info["ok"] and info["integrity"] == "ok" and info["users"] == 5 and info["ledger"] == 10,
          "снимок цел, 5 клиентов, 10 операций, балансы сходятся с журналом")
    check(info["balances_micro"] == sum((k + 1) * 1_500_000 - 400_000 for k in range(5)),
          f"сумма балансов в бэкапе верна (${info['balances_micro'] / 1e6:.2f})")

    print("2. отправка админу")
    check(len(FILES) == 1 and FILES[0][0] == 999 and FILES[0][2].endswith(".sqlite3.gz.enc"),
          f"админу ушёл файл {FILES[0][2]}")
    blob, caption = FILES[0][1], FILES[0][3]
    check("Клиентов: 5" in caption and "балансы сходятся" in caption, "в подписи: клиенты, сумма, «балансы сходятся»")
    raw = Path(info["path"]).read_bytes()
    check(raw[:16] not in blob and b"SQLite format 3" not in blob, "файл зашифрован: в нём не видно базы")

    print("3. шифрование")
    try:
        B.unpack(blob, "wrong password")
        check(False, "неверный пароль должен отвергаться")
    except ValueError as err:
        check("неверный пароль" in str(err), "неверный пароль → отказ")
    bad = bytearray(blob)
    bad[len(bad) // 2] ^= 1
    try:
        B.unpack(bytes(bad), settings.backup_password)
        check(False, "испорченный файл должен отвергаться")
    except ValueError:
        check(True, "испорчен один бит → файл не принимается (AES-GCM)")
    check(B.unpack(blob, settings.backup_password) == raw, "правильный пароль → байт в байт та же база")

    print("4. восстановление инструментом")
    enc = Path(TMP) / FILES[0][2]
    enc.write_bytes(blob)
    out = Path(TMP) / "restore" / "restored.sqlite3"
    env = {**os.environ, "BACKUP_PASSWORD": settings.backup_password}
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "restore_backup.py"), str(enc), "--out", str(out)],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    check(r.returncode == 0 and out.exists() and "Готово к подмене базы" in r.stdout,
          "tools/restore_backup.py расшифровал, проверил и записал базу")
    con = sqlite3.connect(out)
    rows = con.execute("SELECT username, balance_micro FROM users ORDER BY id").fetchall()
    con.close()
    check(rows == [(f"c{k}", (k + 1) * 1_500_000 - 400_000) for k in range(5)],
          "в восстановленной базе те же клиенты и те же балансы")
    r2 = subprocess.run([sys.executable, str(ROOT / "tools" / "restore_backup.py"), str(enc), "--out", str(out)],
                        capture_output=True, text=True, encoding="utf-8", env=env)
    check(r2.returncode == 3, "поверх существующего файла инструмент не пишет")
    r3 = subprocess.run([sys.executable, str(ROOT / "tools" / "restore_backup.py"), str(enc), "--out",
                         str(out.with_name("x.sqlite3")), "--password", "nope"],
                        capture_output=True, text=True, encoding="utf-8", env=env)
    check(r3.returncode == 2 and "неверный пароль" in r3.stdout, "с неверным паролем инструмент ничего не пишет")

    print("5. если книги не сходятся")
    con = sqlite3.connect(Path(TMP) / "supplierhub.sqlite3")
    con.execute("UPDATE users SET balance_micro = balance_micro + 1 WHERE username = 'c2'")  # simulated corruption
    con.commit()
    con.close()
    TEXTS.clear()
    info = await B.backup_now()
    check(not info["ok"] and len(info["mismatched"]) == 1, "бэкап заметил клиента, у которого баланс ≠ журналу")
    check(any("проблему с базой" in t for t in TEXTS), "админу пришёл алерт «Бэкап показал проблему с базой»")
    check("ПРОБЛЕМА С БАЗОЙ" in FILES[-1][3], "и в подписи к файлу это видно сразу")

    print("6. без пароля")
    settings.backup_password = ""
    n = len(FILES)
    TEXTS.clear()
    info = await B.backup_now()
    check(len(FILES) == n and Path(info["path"]).exists(), "без BACKUP_PASSWORD бэкап остаётся на диске, в Telegram не уходит")
    check(any("BACKUP_PASSWORD" in t for t in TEXTS), "админ узнаёт, что пароль не задан")
    backups = sorted((Path(TMP) / "backups").glob("supplierhub-*.sqlite3"))
    check(len(backups) == 3, f"все снимки на диске ({len(backups)}), старые сверх {B.KEEP} удаляются")

    print(f"\nALL GOOD: {OK} checks passed")


asyncio.run(main())
