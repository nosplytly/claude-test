"""Nervixy rate-limit handling, against a fake HTTP transport (no real requests).

Run:  .venv\\Scripts\\python.exe tests\\test_nervixy_limits.py
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.update({"ENV": "dev", "DATA_DIR": tempfile.mkdtemp(prefix="sh-nx-"), "NERVIXY_MOCK": "false",
                   "NERVIXY_API_KEY": "test", "NERVIXY_MIN_INTERVAL": "0.4", "BOT_TOKEN": "", "ADMIN_TG_IDS": ""})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import nervixy as N  # noqa: E402

OK = 0


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


class Fake:
    """Answers like nervixy; can be switched into '429 mode'."""

    def __init__(self):
        self.calls: list[tuple[float, str]] = []
        self.limited = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        action = request.url.params.get("action")
        self.calls.append((time.monotonic(), action))
        if self.limited:
            return httpx.Response(429, json={"error": "Слишком много попыток. Подождите 60 секунд."})
        if action == "api_me":
            return httpx.Response(200, json={"ok": True, "login": "t", "balance": 100.0, "discount": 4.5})
        if action == "api_orders":
            return httpx.Response(200, json={"ok": True, "total": 2, "orders": [
                {"id": "A", "status": "delivered"}, {"id": "B", "status": "processing"}]})
        return httpx.Response(200, json={"ok": True})


async def main():
    fake = Fake()
    client = N.NervixyClient("test", "https://nervixy.example/api.php")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))

    print("1. requests are spaced out")
    await asyncio.gather(client.me(), client.me(), client.me())
    gaps = [b[0] - a[0] for a, b in zip(fake.calls, fake.calls[1:])]
    check(all(g >= 0.39 for g in gaps), f"3 параллельных запроса ушли по очереди, паузы {[round(g, 2) for g in gaps]} с")

    print("2. after a 429 nothing is sent until the cooldown ends")
    fake.limited = True
    client.COOLDOWN = 1.5
    try:
        await client.me()
    except N.NervixyError as e:
        check(e.rate_limited, f"429 → NervixyError(rate_limited): {e.message}")
    sent = len(fake.calls)
    for _ in range(5):
        try:
            await client.me()
        except N.NervixyError as e:
            last = e.message
    check(len(fake.calls) == sent, f"5 попыток во время паузы не дошли до API ({last})")
    fake.limited = False
    await asyncio.sleep(1.6)
    r = await client.me()
    check(r["ok"] and len(fake.calls) == sent + 1, "после паузы запросы снова идут")

    print("3. cached supplier balance survives a rate-limit pause")
    w = N.Nervixy()
    w.client = client
    acc = await w.account(max_age=0)
    w.note_spent(N.D("12.5"))
    check(w._account["balance"] == N.D("87.5"), "после заказа баланс уменьшен локально: 100 → 87.5")
    fake.limited = True
    client._cooldown_until = 0
    acc = await w.account(max_age=0)
    check(acc["balance"] == N.D("87.5"), "во время 429 отдаётся кэш, а не ошибка")

    print(f"\nALL GOOD: {OK} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
