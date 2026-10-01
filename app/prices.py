"""Crypto -> USD prices for invoices in volatile coins (BTC, ETH, LTC, TON, TRX, SOL), from five exchanges.

A wrong price is lost money: an invoice for 0.001 BTC that should have been 0.0012 sells Steam below cost. So a
price is never taken from a single exchange on trust:
- all sources are asked at once; per coin the MEDIAN of their answers is the candidate;
- a source more than MAX_SPREAD away from the median is ignored as an outlier (a glitch, a stale or delisted pair);
- at least MIN_AGREE sources must agree — or, if only one answers, it must agree with the last good price;
- a move of more than MAX_JUMP from the last good price is held back until the next refresh confirms it
  (a real crash shows up twice; a bad tick usually doesn't).
A coin whose price can't be confirmed simply has none: its invoices pause ("оплата временно недоступна") while
the other coins go on. Stablecoins (USDT) are 1.
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from statistics import median

import httpx

from .money import D

log = logging.getLogger("sh.prices")

COINS = ("BTC", "ETH", "LTC", "TON", "TRX", "SOL")
STABLE = {"USDT": Decimal(1)}
_GECKO = {"BTC": "bitcoin", "ETH": "ethereum", "LTC": "litecoin", "TON": "the-open-network", "TRX": "tron",
          "SOL": "solana"}
# Kraken quotes USD (not USDT): within a fraction of a percent, well inside MAX_SPREAD. It also lists TON, which
# OKX and Bybit don't — without it TON had only Binance and CoinGecko, and they were 5% apart.
_KRAKEN_ASK = ("XBTUSD", "ETHUSD", "LTCUSD", "TONUSD", "TRXUSD", "SOLUSD")
_KRAKEN = {"XXBTZUSD": "BTC", "XBTUSD": "BTC", "XETHZUSD": "ETH", "ETHUSD": "ETH", "XLTCZUSD": "LTC", "LTCUSD": "LTC",
           "TONUSD": "TON", "TRXUSD": "TRX", "SOLUSD": "SOL"}
MAX_AGE = 600  # seconds; older prices are not used for new invoices
MAX_SPREAD = Decimal("0.02")  # a source this far from the median is an outlier
MIN_AGREE = 2
MAX_JUMP = Decimal("0.10")  # a bigger move since the last good price waits for one more refresh
REFRESH = 30


class PriceFeed:
    def __init__(self):
        self.prices: dict[str, Decimal] = {}
        self.updated_at = 0.0  # when the last refresh accepted anything (also the default for every coin)
        self.coin_at: dict[str, float] = {}  # when each coin's price was last confirmed
        self.agreed: dict[str, list[str]] = {}  # which sources backed it
        self.held: dict[str, Decimal] = {}  # a big jump waiting for confirmation
        self.problems: dict[str, str] = {}  # coin -> why it has no fresh price now
        self.source = ""
        self._http = httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0 SupplierHub"})

    # ------------------------------------------------------------------ sources
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

    async def _kraken(self) -> dict[str, Decimal]:
        r = await self._http.get("https://api.kraken.com/0/public/Ticker",
                                 params={"pair": ",".join(_KRAKEN_ASK)})
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"kraken: {data['error']}")
        return {_KRAKEN[k]: D(v["c"][0]) for k, v in data["result"].items() if k in _KRAKEN}

    def sources(self) -> dict:
        return {"binance": self._binance, "okx": self._okx, "bybit": self._bybit, "coingecko": self._gecko,
                "kraken": self._kraken}

    async def fetch_all(self) -> dict[str, dict[str, Decimal]]:
        """{source: {coin: price}} from every source that answered."""
        names = list(self.sources())
        results = await asyncio.gather(*(fn() for fn in self.sources().values()), return_exceptions=True)
        out = {}
        for name, res in zip(names, results, strict=True):
            if isinstance(res, BaseException):
                log.warning("price source %s failed: %s", name, res)
                continue
            out[name] = {c: v for c, v in res.items() if c in COINS and v.is_finite() and v > 0}
        return out

    # ------------------------------------------------------------------ deciding
    def decide(self, coin: str, quotes: dict[str, Decimal]) -> tuple[Decimal | None, list[str], str]:
        """(price to use or None, sources that agree, why not) for one coin from this refresh's quotes."""
        if not quotes:
            return None, [], "ни одна биржа не ответила"
        mid = Decimal(str(median(quotes.values())))
        agree = sorted(s for s, v in quotes.items() if abs(v - mid) <= mid * MAX_SPREAD)
        last = self.prices.get(coin)
        if len(agree) < MIN_AGREE:
            if len(quotes) == 1 and last and abs(mid - last) <= last * MAX_SPREAD:
                return mid, agree, ""  # the only exchange that answered says what we already knew
            spread = {s: str(v) for s, v in quotes.items()}
            if len(quotes) == 1:
                return None, agree, f"ответила только одна биржа: {spread}"
            return None, agree, f"биржи не сходятся: {spread}"
        good = Decimal(str(median([quotes[s] for s in agree])))
        if last and abs(good - last) > last * MAX_JUMP:
            held = self.held.get(coin)
            if held is None or abs(good - held) > held * MAX_SPREAD:
                self.held[coin] = good  # wait for the next refresh to say the same
                return None, agree, f"скачок {last} → {good}, ждём подтверждения"
        self.held.pop(coin, None)
        return good, agree, ""

    async def refresh(self) -> None:
        got = await self.fetch_all()
        now, accepted = time.time(), []
        for coin in COINS:
            quotes = {s: q[coin] for s, q in got.items() if coin in q}
            price, agree, why = self.decide(coin, quotes)
            if price is None:
                self.problems[coin] = why
                log.warning("price %s not confirmed: %s", coin, why)
                continue
            self.prices[coin], self.coin_at[coin], self.agreed[coin] = price, now, agree
            self.problems.pop(coin, None)
            accepted.append(coin)
        if accepted:
            self.updated_at = now
            self.source = "median:" + ",".join(sorted(got))

    # ------------------------------------------------------------------ using
    def _fresh(self, coin: str) -> bool:
        return time.time() - self.coin_at.get(coin, self.updated_at) <= MAX_AGE

    def price(self, coin: str) -> Decimal | None:
        if coin in STABLE:
            return STABLE[coin]
        return self.prices.get(coin) if self._fresh(coin) else None

    def snapshot(self) -> dict[str, str]:
        out = {k: str(v) for k, v in STABLE.items()}
        out.update({k: str(v) for k, v in self.prices.items() if self._fresh(k)})
        return out

    async def run(self) -> None:
        from .notify import notify_admins
        from .tgui import e
        from .utils import background

        since: dict[str, float] = {}
        while True:
            await self.refresh()
            for coin in COINS:
                if coin in self.problems:
                    since.setdefault(coin, time.time())
                    if time.time() - since[coin] > MAX_AGE:  # the old price is too old by now: payments in it paused
                        background(notify_admins(
                            f"{e('warn')} <b>Нет надёжной цены {coin}</b> уже {int((time.time() - since[coin]) // 60)} мин\n"
                            f"Оплата в {coin} приостановлена, остальные монеты работают.\n<i>{self.problems[coin]}</i>",
                            key=f"price-{coin}", every=3600))
                else:
                    since.pop(coin, None)
            await asyncio.sleep(REFRESH)


price_feed = PriceFeed()
