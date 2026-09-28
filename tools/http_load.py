"""Load test over the internet against the live site — read-only, the way visitors browse.

    python tools\\http_load.py                                  # 60 s, 30 visitors, https://sh-control-universe.com
    python tools\\http_load.py --visitors 80 --seconds 120
    python tools\\http_load.py --burst                          # one IP hammering: the protection must answer 429
    python tools\\http_load.py --url http://localhost:8000      # the local copy

What it requests: the page, its styles and scripts, /api/config, /api/rates (every few seconds, like the open page
does) and /healthz. No logins, no orders, no Steam login checks (each costs a supplier request), no payments —
nothing that changes data or touches the supplier (rates are cached on the server for 30 minutes).

All the load comes from one IP, and nginx lets one IP make ~30 requests a second (API: 10) — above that it answers
429 by design. So the normal run keeps each visitor at a human pace; --burst checks the limiter itself.
Before --burst put your IP into F2B_WHITELIST in the server's .env and restart the service; otherwise the site
rightly takes you for an attacker and bans the IP for an hour.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict

import httpx

UA = "SupplierHub-loadtest/1.0 (+read-only)"


class Stats:
    def __init__(self):
        self.lat: dict[str, list[float]] = defaultdict(list)
        self.codes: dict[str, Counter] = defaultdict(Counter)
        self.errors: Counter = Counter()
        self.started = time.monotonic()

    def add(self, name: str, ms: float, code: int | None, err: str | None = None):
        if err:
            self.errors[err] += 1
            self.codes[name]["ERR"] += 1
            return
        self.lat[name].append(ms)
        self.codes[name][code] += 1

    def total(self) -> Counter:
        t = Counter()
        for c in self.codes.values():
            t.update(c)
        return t


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


async def get(client: httpx.AsyncClient, stats: Stats, name: str, path: str) -> httpx.Response | None:
    t = time.perf_counter()  # monotonic() ticks in 16 ms steps on Windows
    try:
        r = await client.get(path)
        stats.add(name, (time.perf_counter() - t) * 1000, r.status_code)
        return r
    except httpx.TimeoutException:
        stats.add(name, 0, None, "таймаут")
    except httpx.HTTPError as e:
        stats.add(name, 0, None, type(e).__name__)
    return None


def client_for(url: str, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=url, timeout=timeout, headers={"User-Agent": UA}, follow_redirects=False,
                             limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))


async def preflight(url: str) -> list[str]:
    """Quick sanity checks before the load: page, health, HTTPS redirect, protective headers."""
    notes, bad = [], False
    async with client_for(url, 15) as c:
        try:
            r = await c.get("/")
            ok = r.status_code == 200 and "SupplierHub" in r.text
            notes.append(f"{'✓' if ok else '✗'} главная: {r.status_code}, {len(r.content) // 1024} КБ, {r.http_version}")
            bad |= not ok
            if url.startswith("https://"):
                h = {k.lower() for k in r.headers}
                for need in ("strict-transport-security", "content-security-policy", "x-content-type-options"):
                    notes.append(f"{'✓' if need in h else '✗'} заголовок {need}")
            h = await c.get("/healthz")
            ok = h.status_code == 200 and h.json().get("ok") is True
            notes.append(f"{'✓' if ok else '✗'} /healthz: {h.status_code} {h.text[:120]}")
            bad |= not ok
        except httpx.HTTPError as e:
            return [f"✗ сайт не отвечает: {type(e).__name__}: {e}"]
    if url.startswith("https://"):
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
                r = await c.get("http://" + url.removeprefix("https://") + "/")
            ok = r.status_code in (301, 308) and r.headers.get("location", "").startswith("https://")
            notes.append(f"{'✓' if ok else '✗'} http:// → https:// ({r.status_code})")
        except httpx.HTTPError as e:
            notes.append(f"✗ http:// не отвечает ({type(e).__name__})")
    return notes


async def assets_of(url: str) -> list[str]:
    async with client_for(url, 15) as c:
        html = (await c.get("/")).text
    found = re.findall(r'(?:href|src)="(/assets/[^"]+\.(?:css|js)(?:\?[^"]*)?)"', html)
    return list(dict.fromkeys(found)) or ["/assets/app.css", "/assets/app.js"]


async def visitor(url: str, stats: Stats, deadline: float, assets: list[str], timeout: float):
    """One person: opens the site (page + files + config), then the open page refreshes rates now and then;
    after a while they leave and someone new comes (a fresh connection)."""
    await asyncio.sleep(random.uniform(0, 5))  # people don't arrive in the same millisecond
    while time.monotonic() < deadline:
        async with client_for(url, timeout) as c:
            await get(c, stats, "страница /", "/")
            await asyncio.gather(*(get(c, stats, "файлы (css/js)", a) for a in assets))
            await get(c, stats, "/api/config", "/api/config")
            stay = time.monotonic() + random.uniform(20, 45)
            while time.monotonic() < min(stay, deadline):
                await asyncio.sleep(random.uniform(3, 7))
                await get(c, stats, "/api/rates", "/api/rates")
                if random.random() < 0.15:
                    await get(c, stats, "/healthz", "/healthz")


async def burst(url: str, stats: Stats, seconds: float, workers: int):
    deadline = time.monotonic() + seconds

    async def hammer():
        async with client_for(url, 10) as c:
            while time.monotonic() < deadline:
                await get(c, stats, "/api/config (шквал)", "/api/config")

    await asyncio.gather(*(hammer() for _ in range(workers)))


def report(stats: Stats) -> tuple[int, int, int]:
    took = time.monotonic() - stats.started
    total = stats.total()
    n = sum(total.values())
    print(f"\n{'запрос':<22}{'кол-во':>8}{'ok %':>8}{'p50 мс':>9}{'p95 мс':>9}{'p99 мс':>9}{'макс':>8}")
    for name in stats.codes:
        c, xs = stats.codes[name], stats.lat[name]
        cnt = sum(c.values())
        ok = sum(v for k, v in c.items() if isinstance(k, int) and k < 400)
        print(f"{name:<22}{cnt:>8}{100 * ok / cnt:>7.1f}%{pct(xs, 50):>9.0f}{pct(xs, 95):>9.0f}{pct(xs, 99):>9.0f}"
              f"{max(xs or [0]):>8.0f}")
    server_err = sum(v for k, v in total.items() if isinstance(k, int) and k >= 500)
    limited = total.get(429, 0) + total.get(503, 0)
    banned = total.get(403, 0)
    print(f"\nвсего {n} запросов за {took:.0f} с ≈ {n / max(took, 1):.1f} в секунду")
    print("коды ответов:", ", ".join(f"{k}: {v}" for k, v in sorted(total.items(), key=lambda kv: str(kv[0]))))
    if stats.errors:
        print("ошибки соединения:", ", ".join(f"{k}: {v}" for k, v in stats.errors.items()))
    return server_err - total.get(503, 0), limited, banned


async def main():
    ap = argparse.ArgumentParser(description="Нагрузка на живой сайт через интернет (только чтение)")
    ap.add_argument("--url", default="https://sh-control-universe.com")
    ap.add_argument("--visitors", type=int, default=30, help="одновременных посетителей (по умолчанию 30)")
    ap.add_argument("--seconds", type=int, default=60, help="сколько секунд держать нагрузку (по умолчанию 60)")
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--burst", action="store_true", help="проверить защиту: 20 потоков долбят /api/config 15 секунд")
    args = ap.parse_args()
    url = args.url.rstrip("/")

    print(f"Цель: {url}\n\n1. Проверки перед нагрузкой")
    notes = await preflight(url)
    print("\n".join("   " + x for x in notes))
    if any(x.startswith("✗ сайт не отвечает") for x in notes):
        sys.exit(2)

    if args.burst:
        print("\n2. Шквал с одного IP (защита должна отвечать 429, а сайт — выжить)")
        stats = Stats()
        await burst(url, stats, 15, 20)
        _, limited, banned = report(stats)
        await asyncio.sleep(3)
        stats2 = Stats()
        async with client_for(url, 15) as c:
            r = await get(c, stats2, "после шквала", "/api/config")
        print(f"\n   {'✓' if limited else '✗'} ограничение сработало: {limited} ответов 429/503")
        print(f"   {'✗' if banned else '✓'} бан по IP: {banned} ответов 403"
              + (" — ваш IP не в F2B_WHITELIST, его забанило на час (снять: /unban IP в боте)" if banned else ""))
        print(f"   {'✓' if r is not None and r.status_code == 200 else '✗'} через 3 секунды сайт снова отвечает: "
              f"{r.status_code if r is not None else 'нет ответа'}")
        return

    print(f"\n2. Нагрузка: {args.visitors} посетителей одновременно, {args.seconds} с")
    assets = await assets_of(url)
    stats = Stats()
    deadline = time.monotonic() + args.seconds

    async def ticker():
        while time.monotonic() < deadline:
            await asyncio.sleep(10)
            t = stats.total()
            print(f"   … {int(time.monotonic() - stats.started)} с: {sum(t.values())} запросов, "
                  f"ошибок {sum(v for k, v in t.items() if k == 'ERR' or (isinstance(k, int) and k >= 500))}")

    await asyncio.gather(ticker(), *(visitor(url, stats, deadline, assets, args.timeout) for _ in range(args.visitors)))
    server_err, limited, banned = report(stats)
    n = sum(stats.total().values())
    conn_err = sum(stats.errors.values())
    pages = stats.lat.get("страница /", [])
    api = stats.lat.get("/api/config", []) + stats.lat.get("/api/rates", [])
    print("\n3. Итог")
    print(f"   {'✓' if not server_err else '✗'} ошибок сервера (5xx): {server_err}")
    print(f"   {'✓' if conn_err <= n * 0.005 else '✗'} обрывов и таймаутов: {conn_err}")
    print(f"   {'✓' if pct(pages, 95) < 1500 else '✗'} страница открывается: p95 {pct(pages, 95):.0f} мс (норма < 1500)")
    print(f"   {'✓' if pct(api, 95) < 800 else '✗'} API отвечает: p95 {pct(api, 95):.0f} мс (норма < 800)")
    if limited:
        print(f"   ! {limited} ответов 429 — с одного IP упёрлись в лимит nginx; уменьшите --visitors")
    if banned:
        print(f"   ✗ {banned} ответов 403 — IP забанен; добавьте его в F2B_WHITELIST и снимите бан (/unban IP)")
    if pages:
        print(f"   медиана страницы {statistics.median(pages):.0f} мс, API {statistics.median(api or [0]):.0f} мс")


if __name__ == "__main__":
    asyncio.run(main())
