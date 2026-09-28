"""Outgoing webhooks to partners (their own URL, set in the cabinet).

Body: {"event": "...", "data": {...}, "created_at": "..."}; header X-SH-Signature = hex HMAC-SHA256(body, secret).
Retries after 1, 5, 15, 60 minutes, then 3 and 6 hours.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import session_scope
from .models import User, WebhookEvent
from .utils import iso, utcnow

log = logging.getLogger("sh.webhooks")
BACKOFF = [60, 300, 900, 3600, 3 * 3600, 6 * 3600]
_http = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0), follow_redirects=False,
                          headers={"User-Agent": "SupplierHub-Webhooks/1.0", "Content-Type": "application/json"})


class BadWebhookUrl(ValueError):
    pass


async def check_url(url: str) -> str:
    """Only https URLs that resolve to public addresses (no calls into our own network)."""
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        raise BadWebhookUrl("Нужен адрес вида https://…")
    if len(url) > 500:
        raise BadWebhookUrl("Слишком длинный адрес")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise BadWebhookUrl("Домен не найден") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise BadWebhookUrl("Адрес ведёт во внутреннюю сеть")
    return url


async def queue_webhook(s: AsyncSession, user_id: int, event: str, data: dict) -> None:
    """Call inside the transaction that caused the event."""
    u = await s.get(User, user_id)
    if not u or not u.webhook_url or not u.webhook_secret:
        return
    body = json.dumps({"event": event, "data": data, "created_at": iso(utcnow())}, ensure_ascii=False)
    s.add(WebhookEvent(user_id=user_id, event=event, payload=body, status="pending", next_attempt_at=utcnow()))


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def deliver_due() -> None:
    now = utcnow()
    async with session_scope() as s:
        rows = (await s.execute(select(WebhookEvent.id).where(WebhookEvent.status == "pending",
                                                               WebhookEvent.next_attempt_at <= now)
                                .order_by(WebhookEvent.id).limit(20))).scalars().all()
    for eid in rows:
        await _deliver(eid)


async def _deliver(eid: int) -> None:
    async with session_scope() as s:
        ev = await s.get(WebhookEvent, eid)
        u = await s.get(User, ev.user_id)
        url, secret, body, event = (u.webhook_url if u else None), (u.webhook_secret if u else None), ev.payload, ev.event
    error = None
    if not url or not secret:
        error = "webhook отключён"
    else:
        try:
            await check_url(url)
            raw = body.encode()
            r = await _http.post(url, content=raw, headers={"X-SH-Signature": sign(raw, secret), "X-SH-Event": event})
            if not 200 <= r.status_code < 300:
                error = f"HTTP {r.status_code}"
        except (BadWebhookUrl, httpx.HTTPError) as e:
            error = f"{type(e).__name__}: {e}"[:300]
    async with session_scope() as s:
        ev = await s.get(WebhookEvent, eid)
        ev.attempts += 1
        if error is None:
            ev.status, ev.last_error = "sent", None
        else:
            ev.last_error = error
            if not url or ev.attempts > len(BACKOFF):
                ev.status = "failed"
            else:
                ev.next_attempt_at = utcnow() + timedelta(seconds=BACKOFF[ev.attempts - 1])
    if error:
        log.info("webhook %s to user %s failed (attempt): %s", eid, url, error)
