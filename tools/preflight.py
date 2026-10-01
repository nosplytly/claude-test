"""Pre-launch check of a server's configuration: everything that can't be seen in the code.

    .venv\\Scripts\\python.exe tools\\preflight.py            (on the server, in the project folder)
    .venv\\Scripts\\python.exe tools\\preflight.py --live     (+ second-opinion providers on live mainnet transfers)

Reads .env like the app does and asks the outside services read-only questions. Never prints a secret: only
whether it's set, how long it is and whether the service accepted it. Exit code 1 if anything is red.
"""
from __future__ import annotations

import asyncio
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402

rows: list[tuple[str, str, str]] = []  # (mark, what, detail)  mark: ok | warn | bad


def ok(what, detail=""):
    rows.append(("ok", what, detail))


def warn(what, detail=""):
    rows.append(("warn", what, detail))


def bad(what, detail=""):
    rows.append(("bad", what, detail))


def is_set(value: str) -> str:
    return f"задан ({len(value)} симв.)" if value else "не задан"


async def main(live: bool):
    c = httpx.AsyncClient(timeout=20, headers={"User-Agent": "SupplierHub-preflight"})

    # ---------------------------------------------------------------- basics
    if settings.env == "prod":
        ok("ENV=prod")
    else:
        bad("ENV не prod", "сайт будет в dev-режиме: вход без Telegram, тестовая оплата")
    if settings.base_url.startswith("https://"):
        ok("BASE_URL по https", settings.base_url)
    else:
        bad("BASE_URL не https", settings.base_url)
    if settings.nervixy_mock:
        bad("NERVIXY_MOCK=true", "заказы не уйдут настоящему поставщику")

    # ---------------------------------------------------------------- Nervixy
    if not settings.nervixy_api_key:
        bad("NERVIXY_API_KEY", "не задан")
    else:
        try:
            r = await c.get(settings.nervixy_base_url, params={"action": "api_me"},
                            headers={"X-API-Key": settings.nervixy_api_key, "Accept": "application/json"})
            data = r.json()
            if data.get("ok"):
                ok("Nervixy принимает ключ", f"баланс ${data.get('balance')}, скидка {data.get('discount')}%")
            else:
                bad("Nervixy отказал", f"{data.get('error')} (если про IP — добавь IP сервера в белый список в кабинете)")
        except Exception as e:  # noqa: BLE001
            bad("Nervixy не отвечает", f"{type(e).__name__}: {e}")
    if settings.nervixy_webhook_secret.startswith("whsec_"):
        ok("NERVIXY_WEBHOOK_SECRET", is_set(settings.nervixy_webhook_secret))
    elif settings.nervixy_webhook_secret:
        warn("NERVIXY_WEBHOOK_SECRET", "задан, но не похож на whsec_… из кабинета Nervixy")
    else:
        bad("NERVIXY_WEBHOOK_SECRET не задан", "статусы заказов только опросом: медленнее и тратит лимит Nervixy")

    # ---------------------------------------------------------------- Telegram
    if not settings.bot_token:
        bad("BOT_TOKEN не задан")
    else:
        try:
            me = (await c.get(f"https://api.telegram.org/bot{settings.bot_token}/getMe")).json()
            if me.get("ok"):
                ok("токен бота живой", f"@{me['result']['username']}")
            else:
                bad("Telegram не принял токен бота", str(me.get("description")))
        except Exception as e:  # noqa: BLE001
            bad("Telegram не отвечает", f"{type(e).__name__}")
    if settings.admin_ids:
        ok("ADMIN_TG_IDS", f"{len(settings.admin_ids)} админ(а)")
    else:
        bad("ADMIN_TG_IDS пуст", "алерты, бэкапы и карточки заказов некому слать")

    # ---------------------------------------------------------------- wallets
    from app.payments.methods import METHODS
    on = [m.code for m in METHODS.values() if m.enabled]
    (ok if on else bad)("кошельки", ", ".join(on) or "ни одного — оплата криптой выключена")

    # ---------------------------------------------------------------- explorers: keys really work
    if settings.trongrid_api_key:
        r = await c.get(f"{settings.trongrid_url}/v1/accounts/TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                        headers={"TRON-PRO-API-KEY": settings.trongrid_api_key})
        (ok if r.status_code == 200 else bad)("TRONGRID_API_KEY", f"TronGrid ответил {r.status_code}")
    elif any(m.startswith(("usdt_trc20", "trx")) for m in on):
        warn("TRONGRID_API_KEY не задан", "TronGrid без ключа сильно режет запросы: при потоке платежей TRON будут опаздывать")
    if settings.toncenter_api_key:
        r = await c.get(f"{settings.toncenter_url}/masterchainInfo", headers={"X-API-Key": settings.toncenter_api_key})
        (ok if r.status_code == 200 else bad)("TONCENTER_API_KEY", f"toncenter ответил {r.status_code}")
    elif any(m in ("usdt_ton", "ton") for m in on):
        warn("TONCENTER_API_KEY не задан", "без ключа toncenter даёт 1 запрос в секунду")
    if settings.tonapi_key:
        r = await c.get(f"{settings.tonapi_url}/v2/status", headers={"Authorization": f"Bearer {settings.tonapi_key}"})
        (ok if r.status_code == 200 else bad)("TONAPI_KEY", f"tonapi ответил {r.status_code}")
    elif any(m in ("usdt_ton", "ton") for m in on):
        warn("TONAPI_KEY не задан", "вторая проверка TON-платежей на бесплатном лимите tonapi (~1 запрос/с)")

    # ---------------------------------------------------------------- secrets on disk
    if settings.backup_password and len(settings.backup_password) >= 16:
        ok("BACKUP_PASSWORD", is_set(settings.backup_password))
    else:
        bad("BACKUP_PASSWORD", "не задан или короче 16 — бэкапы в Telegram не уходят (install.ps1 его создаёт)")
    env_file = ROOT / ".env"
    if env_file.exists() and sys.platform == "win32":
        acl = subprocess.run(["icacls", str(env_file)], capture_output=True, text=True, errors="replace").stdout
        if "Users" in acl or "Пользователи" in acl or "Everyone" in acl or "Все" in acl:
            warn(".env читают все пользователи сервера", "install.ps1 закрывает доступ — запусти его")
        else:
            ok(".env закрыт от других пользователей")

    # ---------------------------------------------------------------- certificate
    cert = ROOT / "certs" / "fullchain.pem"
    if cert.exists():
        try:
            info = ssl._ssl._test_decode_cert(str(cert))  # type: ignore[attr-defined]
            until = datetime.strptime(info["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            days = (until - datetime.now(timezone.utc)).days
            (ok if days > 30 else warn if days > 0 else bad)("сертификат", f"действует ещё {days} дн. (до {until:%d.%m.%Y})")
        except Exception as e:  # noqa: BLE001
            warn("сертификат не прочитался", str(e)[:100])
    else:
        warn("certs\\fullchain.pem нет", "нужен для https (кладётся на сервер вручную, в архив не входит)")

    # ---------------------------------------------------------------- the site from outside
    if settings.base_url.startswith("https://"):
        try:
            r = await c.get(f"{settings.base_url.rstrip('/')}/healthz")
            body = r.json()
            (ok if r.status_code == 200 and body.get("ok") else bad)("сайт снаружи /healthz",
                                                                       f"{r.status_code} {body.get('problems') or ''}")
        except Exception as e:  # noqa: BLE001
            warn("сайт снаружи не ответил", f"{type(e).__name__} (нормально, если ещё не запущен)")

    # ---------------------------------------------------------------- second opinion on payments
    if not settings.crosscheck_enabled:
        bad("CROSSCHECK_ENABLED=false", "платежи зачисляются по одному источнику")
    if live:
        proc = subprocess.run([sys.executable, str(ROOT / "tools" / "crosscheck_live.py")], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        tail = (proc.stdout.strip().splitlines() or ["?"])[-1]
        (ok if proc.returncode == 0 else bad)("вторые источники платежей (живые транзакции)", tail)

    marks = {"ok": "✓", "warn": "!", "bad": "✗"}
    for mark, what, detail in rows:
        print(f"  {marks[mark]} {what}" + (f" — {detail}" if detail else ""))
    reds = sum(1 for r in rows if r[0] == "bad")
    yellows = sum(1 for r in rows if r[0] == "warn")
    print(f"\n{'ГОТОВО К ЗАПУСКУ' if not reds else 'ЕСТЬ ПРОБЛЕМЫ'}: {reds} красных, {yellows} жёлтых")
    sys.exit(1 if reds else 0)


asyncio.run(main("--live" in sys.argv))
