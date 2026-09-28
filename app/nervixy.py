"""Nervixy B2B API (Steam top-ups). Docs: https://nervixy.digital/docs-api"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import Decimal
from typing import Any

import httpx

from .config import settings
from .money import D

log = logging.getLogger("sh.nervixy")

CURRENCIES = ("RUB", "KZT", "UAH", "USD")


class NervixyError(Exception):
    """Nervixy answered and refused (the request definitely had no effect)."""

    def __init__(self, message: str, status: int | None = None, data: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.data = data or {}

    @property
    def insufficient_funds(self) -> bool:
        return "required" in self.data and "balance" in self.data

    @property
    def rate_limited(self) -> bool:
        return self.status == 429


class NervixyTransportError(Exception):
    """No usable answer (timeout, connection error, 5xx). Outcome of a write is UNKNOWN."""


class NervixyClient:
    """Nervixy rate-limits hard ("Слишком много попыток. Подождите 60 секунд"), and every request made during
    the ban seems to extend it. So requests go out one at a time with a pause between them, and after a 429
    we stay silent for the whole cooldown instead of retrying."""

    COOLDOWN = 65.0

    def __init__(self, api_key: str, base_url: str):
        self.base_url = base_url
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"X-API-Key": api_key, "User-Agent": "SupplierHub/1.0", "Accept": "application/json"},
        )
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cooldown_until = 0.0
        self.min_interval = settings.nervixy_min_interval

    async def _call(self, action: str, payload: dict | None = None, *, get: bool = False) -> dict:
        async with self._lock:
            left = self._cooldown_until - time.monotonic()
            if left > 0:
                raise NervixyError(f"лимит запросов nervixy, пауза ещё {int(left) + 1} с", 429)
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                if get:
                    r = await self._http.get(self.base_url, params={"action": action})
                else:
                    r = await self._http.post(self.base_url, params={"action": action}, json=payload or {})
            except httpx.HTTPError as e:
                raise NervixyTransportError(f"{type(e).__name__}: {e}") from e
            finally:
                self._last = time.monotonic()
            if r.status_code == 429:
                self._cooldown_until = time.monotonic() + self.COOLDOWN
                log.warning("nervixy rate limit hit on %s, pausing all requests for %ss", action, int(self.COOLDOWN))
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code >= 500 and r.status_code != 502:
            raise NervixyTransportError(f"HTTP {r.status_code}: {(r.text or '')[:200]}")
        if not isinstance(data, dict):
            raise NervixyError(f"HTTP {r.status_code}: {(r.text or '')[:200]}", r.status_code)
        if not data.get("ok"):
            raise NervixyError(str(data.get("error") or f"HTTP {r.status_code}"), r.status_code, data)
        return data

    async def me(self) -> dict:
        return await self._call("api_me")

    async def rates(self) -> dict:
        return await self._call("api_rates", get=True)

    async def status(self, order_id: str) -> dict:
        return await self._call("api_status", {"order_id": order_id})

    async def check_login(self, steam_login: str) -> dict:
        return await self._call("api_check_login", {"steam_login": steam_login})

    async def order(self, *, amount: Decimal, currency: str, steam_login: str, partner_id: str) -> dict:
        return await self._call(
            "api_order",
            {"amount": float(amount), "currency": currency, "steam_login": steam_login, "partner_id": partner_id},
        )

    async def orders(self, limit: int = 50, offset: int = 0) -> dict:
        return await self._call("api_orders", {"limit": limit, "offset": offset})

    async def payments(self, limit: int = 50, offset: int = 0) -> dict:
        return await self._call("api_payments", {"limit": limit, "offset": offset})


class MockNervixyClient:
    """Local stand-in used when NERVIXY_MOCK=true. Logins containing 'bad' are invalid,
    'reject' get rejected after processing."""

    RATES = {"KZT": 471.45016, "RUB": 78.57377, "UAH": 44.68123}

    def __init__(self):
        self.balance = Decimal("250.00")
        self.discount = Decimal("4.5")
        self.delay = 8.0  # seconds an order stays "processing"
        self._orders: dict[str, dict] = {}

    async def me(self) -> dict:
        return {"ok": True, "login": "mock", "balance": float(self.balance), "discount": float(self.discount)}

    async def rates(self) -> dict:
        return {"ok": True, "base": "USD", "rates": dict(self.RATES), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    async def check_login(self, steam_login: str) -> dict:
        await asyncio.sleep(0.4)
        valid = "bad" not in steam_login.lower()
        return {"ok": True, "steam_login": steam_login, "valid": valid,
                "status_message": "Steam account validated" if valid else "Account not found"}

    async def order(self, *, amount: Decimal, currency: str, steam_login: str, partner_id: str) -> dict:
        rate = Decimal(1) if currency == "USD" else D(self.RATES[currency])
        usd = (D(amount) / rate).quantize(Decimal("0.01"))
        paid = (usd * (1 - self.discount / 100)).quantize(Decimal("0.01"))
        if paid > self.balance:
            raise NervixyError("Недостаточно средств", 400, {"ok": False, "balance": float(self.balance), "required": float(paid)})
        self.balance -= paid
        oid = uuid.uuid4().hex[:16]
        self._orders[oid] = {"t": time.time(), "reject": "reject" in steam_login.lower(), "steam_login": steam_login,
                             "amount": usd, "paid": paid, "currency": currency, "original_amount": D(amount),
                             "partner_id": partner_id}
        return {"ok": True, "order_id": oid, "currency": currency, "amount": float(usd), "paid": float(paid),
                "original_amount": float(amount), "steam_login": steam_login, "partner_id": partner_id}

    def _status_of(self, o: dict) -> str:
        st = "processing" if time.time() - o["t"] < self.delay else ("rejected" if o["reject"] else "delivered")
        if st == "rejected" and not o.get("refunded"):
            o["refunded"] = True
            self.balance += o["paid"]
        return st

    async def status(self, order_id: str) -> dict:
        o = self._orders.get(order_id)
        if not o:
            raise NervixyError("Заказ не найден", 404)
        return {"ok": True, "order": order_id, "status": self._status_of(o)}

    async def orders(self, limit: int = 50, offset: int = 0) -> dict:
        items = [{"id": k, "steam_login": v["steam_login"], "original_amount": str(v["original_amount"]),
                  "currency": v["currency"], "status": self._status_of(v), "paid": str(v["paid"]),
                  "partner_id": v["partner_id"]} for k, v in self._orders.items()]
        return {"ok": True, "total": len(items), "orders": items[::-1][offset: offset + limit]}

    async def payments(self, limit: int = 50, offset: int = 0) -> dict:
        return {"ok": True, "total": 0, "payments": []}


class Nervixy:
    """Client + caches (rates, account, login checks)."""

    def __init__(self):
        self.client: Any = MockNervixyClient() if settings.nervixy_mock else NervixyClient(
            settings.nervixy_api_key, settings.nervixy_base_url)
        self._rates: dict[str, Decimal] | None = None
        self._rates_at = 0.0
        self._account: dict | None = None
        self._account_at = 0.0
        self._logins: dict[str, tuple[float, bool, str]] = {}
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    async def rates(self, max_age: float = 1800) -> dict[str, Decimal]:
        """Currency units per 1 USD. Raises if we have nothing reasonably fresh."""
        if self._rates and time.time() - self._rates_at < max_age:
            return self._rates
        async with self._lock:
            if self._rates and time.time() - self._rates_at < max_age:
                return self._rates
            try:
                data = await self.client.rates()
                rates = {k.upper(): D(v) for k, v in (data.get("rates") or {}).items()}
                rates["USD"] = Decimal(1)
                if not all(c in rates and rates[c] > 0 for c in CURRENCIES):
                    raise NervixyError(f"unexpected rates payload: {data}")
                self._rates, self._rates_at, self.last_error = rates, time.time(), None
            except (NervixyError, NervixyTransportError) as e:
                self.last_error = str(e)
                log.warning("rates refresh failed: %s", e)
                if not self._rates or time.time() - self._rates_at > 3 * 3600:
                    raise
        return self._rates

    async def account(self, max_age: float = 120) -> dict:
        """{'balance': Decimal USD, 'discount': Decimal %, 'login': str}. During a rate-limit pause a snapshot
        up to 15 minutes old is still returned (our own spending is subtracted locally, see note_spent)."""
        if self._account and time.time() - self._account_at < max_age:
            return self._account
        try:
            data = await self.client.me()
        except (NervixyError, NervixyTransportError) as e:
            if self._account and time.time() - self._account_at < 900 and (
                    isinstance(e, NervixyTransportError) or e.rate_limited):
                log.info("using cached nervixy account (%s)", e)
                return self._account
            raise
        self._account = {"balance": D(data.get("balance", 0)), "discount": D(data.get("discount", 0)),
                         "login": data.get("login")}
        self._account_at = time.time()
        return self._account

    def note_spent(self, usd: Decimal) -> None:
        """Keep the cached supplier balance honest without asking the API again after every order."""
        if self._account:
            self._account["balance"] = self._account["balance"] - D(usd)

    async def supplier_discount(self) -> Decimal:
        try:
            d = (await self.account(max_age=3600))["discount"]
            if d > 0:
                return d
        except (NervixyError, NervixyTransportError) as e:
            log.warning("could not read supplier discount: %s", e)
        return settings.nervixy_discount_fallback

    async def check_login(self, login: str) -> tuple[bool, str]:
        key = login.lower()
        hit = self._logins.get(key)
        if hit and time.time() < hit[0]:
            return hit[1], hit[2]
        data = await self.client.check_login(login)
        valid = bool(data.get("valid"))
        msg = str(data.get("status_message") or "")
        self._logins[key] = (time.time() + (1800 if valid else 300), valid, msg)
        if len(self._logins) > 5000:
            self._logins.clear()
        return valid, msg


nervixy = Nervixy()
