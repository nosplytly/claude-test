"""Login via Telegram bot deep link.

1. Site: POST /api/auth/start -> t.me/<bot>?start=login_<token> + a 2-digit code; browser gets a secret cookie.
2. Bot: user picks the same code among 3 buttons -> request confirmed for that Telegram user.
3. Site polls /api/auth/poll (needs the browser secret) -> session cookie. The bot also offers a one-time
   "back to site" link that logs in whichever browser opens it.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import SessionLocal
from .models import LoginRequest, User, WebSession
from .utils import sha256, token, utcnow

SESSION_COOKIE = "sh_session"
LOGIN_COOKIE = "sh_login"
SESSION_TTL = timedelta(days=30)
LOGIN_TTL = timedelta(minutes=5)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


async def start_login(s: AsyncSession, ip: str, ua: str) -> tuple[LoginRequest, str]:
    browser_secret = token(24)
    req = LoginRequest(id=token(18), browser_hash=sha256(browser_secret), link_key_hash="",
                       code=f"{secrets.randbelow(90) + 10}", ip=ip, user_agent=(ua or "")[:300],
                       expires_at=utcnow() + LOGIN_TTL)
    s.add(req)
    await s.execute(delete(LoginRequest).where(LoginRequest.expires_at < utcnow() - timedelta(days=1)))
    return req, browser_secret


def code_choices(code: str) -> list[str]:
    opts = {code}
    while len(opts) < 3:
        opts.add(f"{secrets.randbelow(90) + 10}")
    out = list(opts)
    secrets.SystemRandom().shuffle(out)
    return out


async def upsert_tg_user(s: AsyncSession, tg_id: int, username: str | None, first: str | None,
                         last: str | None, lang_code: str | None = None) -> User:
    u = (await s.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
    if not u:
        u = User(tg_id=tg_id)
        s.add(u)
    u.username, u.first_name, u.last_name, u.bot_blocked = username, first, last, False
    if lang_code:
        u.tg_lang = lang_code
    await s.flush()
    return u


WEBAPP_MAX_AGE = 24 * 3600


def verify_webapp(init_data: str, bot_token: str, max_age: int = WEBAPP_MAX_AGE) -> dict | None:
    """Telegram Mini App login: `initData` is signed with a key derived from our bot token
    (core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app). Returns the Telegram user or None."""
    import hashlib
    import hmac
    import json
    import time
    from urllib.parse import parse_qsl

    if not bot_token or not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    got = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    if not got or not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None  # an old launch link replayed
        user = json.loads(pairs.get("user", ""))
        return user if isinstance(user, dict) and int(user.get("id", 0)) > 0 else None
    except (ValueError, TypeError):
        return None


async def create_session(s: AsyncSession, user: User, ip: str, ua: str) -> str:
    raw = token(32)
    s.add(WebSession(token_hash=sha256(raw), user_id=user.id, expires_at=utcnow() + SESSION_TTL, ip=ip,
                     user_agent=(ua or "")[:300]))
    user.last_seen_at = utcnow()
    return raw


async def user_from_session(s: AsyncSession, raw: str | None) -> User | None:
    if not raw:
        return None
    ws = await s.get(WebSession, sha256(raw))
    if not ws or ws.expires_at < utcnow():
        return None
    u = await s.get(User, ws.user_id)
    return None if (not u or u.is_banned) else u


async def current_user(request: Request) -> User | None:
    """Own short session: the connection is back in the pool before the endpoint runs. An endpoint may wait on the
    supplier for seconds, and a connection held that long by every request starves the pool under load."""
    # the cookie, or the header the Mini App sends (inside Telegram Web the site runs in a cross-site iframe,
    # where browsers don't send its cookies)
    raw = request.cookies.get(SESSION_COOKIE) or request.headers.get("x-sh-session")
    if not raw:
        return None
    async with SessionLocal() as s:
        return await user_from_session(s, raw)


async def require_user(user: User | None = Depends(current_user)) -> User:
    if not user:
        raise HTTPException(401, "Войдите через Telegram")
    return user
