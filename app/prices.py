"""Crypto -> USD prices from public exchange tickers (Binance, then OKX, Bybit, CoinGecko)."""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

import httpx

from .money import D

log = logging.getLogger("sh.prices")

COINS = ("BTC", "ETH", "LTC", "TON", "TRX", "SOL")
STABLE = {"USDT": Decimal(1)}
_GECKO = {"BTC": "bitcoin", "ETH": "ethereum", "LTC": "litecoin", "TON": "the-open-network", "TRX": "tron",
          "SOL": "solana"}
MAX_AGE = 600  # seconds; older prices are not used for new invoices


class PriceFeed:
    def __init__(self):
        self.prices: dict[str, Decimal] = {}
        self.updated_at = 0.0
        self.source = ""
        self._http = httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0 SupplierHub"})

    async def _binance(self) -> dict[str, Decimal]:
        syms = '["' + '","'.join(c + "USDT" for c in COINS) + '"]'
        r = await self._http.get("https://api.binance.com/api/v3/ticker/price", params={"symbols": syms})
        r.raise_for_status()
        return {x["symbol"][:-4]: D(x["price"]) for x in r.json()}

    async def _okx(self) -> dict[str, Decimal]:
        r = await self._http.get("https://www.okx.com/api/v5/market/tickers", params={"instType": "SPOT"})
        r.raise_for_status()
        want = {c + "-USDT": c for c in COINS}
        return {want[x["instId"]]: D(x["last"]) for x in r.json()["data"] if x["instId"] in want}

    async def _bybit(self) -> dict[str, Decimal]:
        r = await self._http.get("https://api.bybit.com/v5/market/tickers", params={"category": "spot"})
        r.raise_for_status()
        want = {c + "USDT": c for c in COINS}
        return {want[x["symbol"]]: D(x["lastPrice"]) for x in r.json()["result"]["list"] if x["symbol"] in want}

    async def _gecko(self) -> dict[str, Decimal]:
        r = await self._http.get("https://api.coingecko.com/api/v3/simple/price",
                                 params={"ids": ",".join(_GECKO.values()), "vs_currencies": "usd"})
        r.raise_for_status()
        data = r.json()
        return {c: D(data[g]["usd"]) for c, g in _GECKO.items() if g in data}

    async def refresh(self) -> None:
        for name, fn in (("binance", self._binance), ("okx", self._okx), ("bybit", self._bybit), ("coingecko", self._gecko)):
            try:
                got = await fn()
                got = {k: v for k, v in got.items() if v > 0}
                if all(c in got for c in COINS):
                    self.prices, self.updated_at, self.source = got, time.time(), name
                    return
                log.warning("price source %s incomplete: %s", name, sorted(got))
            except Exception as e:  # noqa: BLE001 - any source failure just falls through to the next
                log.warning("price source %s failed: %s", name, e)

    def price(self, coin: str) -> Decimal | None:
        if coin in STABLE:
            return STABLE[coin]
        if time.time() - self.updated_at > MAX_AGE:
            return None
        return self.prices.get(coin)

    def snapshot(self) -> dict[str, str]:
        out = {k: str(v) for k, v in STABLE.items()}
        if time.time() - self.updated_at <= MAX_AGE:
            out.update({k: str(v) for k, v in self.prices.items()})
        return out

    async def run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(30)


price_feed = PriceFeed()
