"""Built-in fail2ban.

An IP that keeps misbehaving gets banned for a while: vulnerability scanners (/.env, /wp-login.php, *.php ...),
API-key guessing on /api/v1, forged webhook signatures, API calls without the site's CSRF header, and clients that
keep hammering after being rate limited. Every misdeed adds points in a sliding window; F2B_MAX_POINTS within
F2B_WINDOW_MIN bans the IP. A repeat ban within a week lasts 24x longer: 1 h -> 24 h -> 7 days (default F2B_BAN_MIN).

The ban check is the first thing every request meets (middleware in main.py), so a banned IP costs a dict lookup.
Bans live in the DB (they survive restarts) and are mirrored to data/f2b-bans.txt: on the Windows server the guard
task (deploy/windows/guard.ps1) copies that list into a firewall rule, so a banned IP can't even open a connection.
Loopback and F2B_WHITELIST addresses are never banned. IPv6 clients are tracked per /64 (one subscriber's range).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from collections import deque
from functools import lru_cache

from fastapi.responses import JSONResponse, PlainTextResponse

from .config import settings

log = logging.getLogger("sh.f2b")

KV_KEY = "f2b:bans"
WEEK = 7 * 86400
BANS_FILE = settings.data_dir / "f2b-bans.txt"

# paths nobody but a vulnerability scanner asks this site for
SCANNER = re.compile(
    r"(?i)(/\.(env|git|svn|hg|aws|ssh|docker|vscode|idea|htaccess|htpasswd|ds_store)"
    r"|wp-(admin|login|content|includes|config|json)|xmlrpc|wordpress|phpmyadmin|/pma\b|/myadmin|/adminer"
    r"|/cgi-bin|/boaform|/actuator|/vendor/|/owa/|/ecp/|/autodiscover|/server-status|/solr|/jenkins"
    r"|/manager/html|/hnap1|/goform|/_ignition|/telescope|/setup\.cgi|/eval-stdin"
    r"|\.(php\d?|phtml|asp|aspx|jsp|cgi|pl|sql|bak|old|orig|swp|ini|conf|cfg|yml|yaml|env|log|tar|gz|tgz|rar|7z|zip)$)")

# what browsers and phones ask for on their own (iOS home-screen icons, favicon, robots): a 404 there means nothing
BENIGN = re.compile(r"(?i)^/(favicon\.ico|apple-touch-icon[\w.-]*\.png|robots\.txt|sitemap\.xml|ads\.txt|"
                    r"manifest\.json|site\.webmanifest|browserconfig\.xml|\.well-known/.*)$")

MESSAGE = "Доступ с вашего IP временно ограничен из-за подозрительной активности. Попробуйте позже."

_bans: dict[str, dict] = {}  # key -> {"until": epoch, "reason": str}
_history: dict[str, dict] = {}  # key -> {"n": bans so far, "last": epoch of the last ban}
_points: dict[str, deque] = {}  # key -> deque[(monotonic, points)]
_dirty = False


@lru_cache(maxsize=4)
def _whitelist(raw: str) -> tuple:
    nets = []
    for part in re.split(r"[,\s]+", raw.strip()):
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                log.warning("F2B_WHITELIST: '%s' is not an IP or subnet — skipped", part)
    return tuple(nets)


def key_of(ip: str | None) -> str | None:
    """Ban key for a client address, or None when it must never be banned (loopback, whitelist, garbage)."""
    try:
        a = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return None
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.is_loopback or a.is_unspecified or any(a.version == n.version and a in n
                                                for n in _whitelist(settings.f2b_whitelist)):
        return None
    return str(ipaddress.ip_network(f"{a}/64", strict=False)) if a.version == 6 else str(a)


def classify(method: str, path: str, status: int) -> tuple[int, str] | None:
    """Points and reason for one finished request, None if it's nothing suspicious."""
    if status < 400 or BENIGN.match(path):
        return None
    if SCANNER.search(path):
        return 5, "сканер уязвимостей"
    if path.startswith("/api/v1/") or path == "/api/v1":
        if status == 401:
            return 2, "подбор API-ключа"
        if status == 429:
            return 1, "превышение лимитов API"
        return None
    if path.startswith("/api/webhooks/") and status == 403:
        return 3, "поддельная подпись вебхука"
    if path.startswith("/api/") and status == 403:
        return 2, "запросы к API в обход сайта"
    if status == 429:
        return 1, "слишком много запросов"
    if status in (404, 405) and not path.startswith("/api/"):
        return 1, "перебор адресов"
    return None


def banned_for(ip: str | None) -> int:
    """Seconds left of the ban on this client, 0 if it isn't banned."""
    if not _bans or not settings.f2b_enabled:
        return 0
    key = key_of(ip)
    b = _bans.get(key) if key else None
    if not b:
        return 0
    left = int(b["until"] - time.time())
    if left <= 0:
        _expire()
        return 0
    return left


def observe(ip: str | None, method: str, path: str, status: int) -> None:
    """Called for every finished request: count points, ban when the limit is reached."""
    if not settings.f2b_enabled:
        return
    hit = classify(method, path, status)
    if not hit:
        return
    key = key_of(ip)
    if not key or key in _bans:
        return
    points, reason = hit
    now = time.monotonic()
    q = _points.setdefault(key, deque())
    window = settings.f2b_window_min * 60
    while q and now - q[0][0] > window:
        q.popleft()
    q.append((now, points))
    if len(_points) > 20_000:  # a botnet spraying single requests: forget the oldest trickle
        _points.clear()
    if sum(p for _, p in q) >= settings.f2b_max_points:
        _points.pop(key, None)
        seconds = _ban(key, reason)
        log.warning("banned %s for %s min: %s (%s %s -> %s)", key, seconds // 60, reason, method, path[:120], status)
        if reason not in ("сканер уязвимостей", "перебор адресов"):  # scanners are routine noise, these are not
            _alert(key, seconds, reason)


def _ban(key: str, reason: str, seconds: int | None = None) -> int:
    global _dirty
    now = time.time()
    h = _history.get(key)
    n = h["n"] if h and now - h["last"] < WEEK else 0
    if seconds is None:
        seconds = min(settings.f2b_ban_min * 60 * 24 ** n, WEEK)
    _bans[key] = {"until": now + seconds, "reason": reason}
    _history[key] = {"n": n + 1, "last": now}
    _dirty = True
    return seconds


def ban(ip: str, hours: float | None = None, reason: str = "вручную") -> tuple[str, int] | None:
    """Manual ban (admin). Returns (key, seconds) or None for loopback / whitelisted / invalid addresses."""
    key = key_of(ip)
    if not key:
        return None
    _points.pop(key, None)
    return key, _ban(key, reason, int(hours * 3600) if hours else None)


def unban(ip: str) -> str | None:
    """Lift a ban (also forgets the escalation history). Returns the key that was unbanned."""
    global _dirty
    key = key_of(ip) or (ip or "").strip()
    if key not in _bans and key not in _history:
        return None
    _bans.pop(key, None)
    _history.pop(key, None)
    _points.pop(key, None)
    _dirty = True
    return key


def active() -> list[tuple[str, int, str]]:
    """[(key, seconds left, reason)] — longest first."""
    now = time.time()
    rows = [(k, int(b["until"] - now), b["reason"]) for k, b in _bans.items() if b["until"] > now]
    return sorted(rows, key=lambda r: -r[1])


def blocked_response(path: str, left: int):
    headers = {"Retry-After": str(left)}
    if path.startswith("/api/v1"):
        return JSONResponse({"ok": False, "error": MESSAGE}, status_code=403, headers=headers)
    if path.startswith("/api/"):
        return JSONResponse({"detail": MESSAGE}, status_code=403, headers=headers)
    return PlainTextResponse(MESSAGE, status_code=403, headers=headers)


def _expire() -> None:
    global _dirty
    now = time.time()
    for k in [k for k, b in _bans.items() if b["until"] <= now]:
        del _bans[k]
        _dirty = True
    for k in [k for k, h in _history.items() if now - h["last"] > WEEK and k not in _bans]:
        del _history[k]
        _dirty = True


def _alert(key: str, seconds: int, reason: str) -> None:
    from . import notify
    from .tgui import e

    hours = seconds / 3600
    text = (f"{e('lock')} <b>IP заблокирован</b>: <code>{key}</code>\n"
            f"Причина: {reason} · на {hours:g} ч\nСнять бан: <code>/unban {key}</code>")
    try:
        asyncio.get_running_loop().create_task(notify.notify_admins(text, key=f"f2b:{reason}", every=600))
    except RuntimeError:
        pass


async def load() -> None:
    from .kv import kv_get

    global _bans, _history
    data = await kv_get(KV_KEY, {}) or {}
    _bans = {k: v for k, v in (data.get("bans") or {}).items() if isinstance(v, dict) and "until" in v}
    _history = {k: v for k, v in (data.get("history") or {}).items() if isinstance(v, dict) and "last" in v}
    _expire()
    await flush(force=True)
    if _bans:
        log.info("fail2ban: %d active bans restored", len(_bans))


async def flush(force: bool = False) -> None:
    """Persist bans to the DB and rewrite data/f2b-bans.txt (read by the Windows firewall guard)."""
    from .kv import kv_set

    global _dirty
    if not (_dirty or force):
        return
    _dirty = False
    await kv_set(KV_KEY, {"bans": _bans, "history": _history})
    tmp = BANS_FILE.with_suffix(".tmp")
    tmp.write_text("".join(f"{k}\n" for k, _, _ in active()), encoding="ascii")
    tmp.replace(BANS_FILE)


async def run() -> None:
    while True:
        _expire()
        await flush()
        await asyncio.sleep(5)
