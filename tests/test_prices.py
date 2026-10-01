"""Prices for volatile coins are confirmed by several exchanges before an invoice uses them.

Run:  .venv\\Scripts\\python.exe tests\\test_prices.py
Fake exchanges, throw-away database: nothing leaves the machine.
"""
import asyncio
import os
import sys
import tempfile
import time
from decimal import Decimal as D
from pathlib import Path

os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "dev", "DATA_DIR": tempfile.mkdtemp(prefix="sh-prices-"),
                   "NERVIXY_MOCK": "true", "BOT_TOKEN": "", "ADMIN_TG_IDS": "", "MONITOR_ENABLED": "false",
                   "WALLET_BTC": "1BitcoinEaterAddressDontSendf59kuE",
                   "WALLET_USDT_TRC20": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import prices as P  # noqa: E402
from app.prices import COINS, PriceFeed  # noqa: E402

OK = 0


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


BASE = {"BTC": D("80000"), "ETH": D("3000"), "LTC": D("90"), "TON": D("3"), "TRX": D("0.3"), "SOL": D("150")}


class Fake(PriceFeed):
    """A PriceFeed whose exchanges answer what the test says (an Exception = the exchange is down)."""

    def __init__(self):
        super().__init__()
        self.answers: dict[str, dict | Exception] = {}

    def sources(self):
        def make(name):
            async def fn():
                a = self.answers.get(name, Exception("down"))
                if isinstance(a, Exception):
                    raise a
                return dict(a)
            return fn
        return {n: make(n) for n in ("binance", "okx", "bybit", "coingecko", "kraken")}


def market(k=D(1), **over):
    return {c: (over.get(c) or BASE[c] * k) for c in COINS}


async def main():
    print("1. медиана нескольких бирж")
    f = Fake()
    f.answers = {"binance": market(D("1.001")), "okx": market(), "bybit": market(D("0.999")), "coingecko": market(D("1.003"))}
    await f.refresh()
    check(f.price("BTC") == D("80040.000") or abs(f.price("BTC") - D("80040")) < 1, f"BTC = медиана четырёх: {f.price('BTC')}")
    check(set(f.agreed["BTC"]) == {"binance", "okx", "bybit", "coingecko"}, "все четыре биржи согласны")

    print("2. выброс одной биржи не влияет")
    f.answers["binance"] = market(BTC=D("40000"))  # a glitch: half the price
    await f.refresh()
    check(abs(f.price("BTC") - D("80000")) < D("200") and "binance" not in f.agreed["BTC"],
          f"Binance отдал BTC за 40 000 — проигнорирован, цена {f.price('BTC')}")

    print("3. одной биржи на холодном старте мало")
    g = Fake()
    g.answers = {"okx": market()}
    await g.refresh()
    check(g.price("BTC") is None and "BTC" in g.problems, f"только OKX ответила, прошлой цены нет → без цены ({g.problems['BTC']})")
    check(g.snapshot() == {"USDT": "1"}, "на сайте видна только USDT")
    f.answers = {"okx": market(D("1.005"))}
    await f.refresh()
    check(f.price("BTC") is not None and "BTC" not in f.problems,
          "ответила одна биржа, но с прошлой ценой сходится (±2%) → цена обновлена")

    print("4. биржи не сходятся — платим только когда ясно")
    h = Fake()
    h.answers = {"binance": market(), "okx": market(D("1.10"))}
    await h.refresh()
    check(h.price("ETH") is None and "не сходятся" in h.problems["ETH"], "две биржи расходятся на 10% → цены нет")

    print("5. резкий скачок подтверждается вторым замером")
    f.answers = {n: market(D("1.005")) for n in ("binance", "okx", "bybit")}
    await f.refresh()
    before = f.price("SOL")
    crash = {n: market(D("1.005"), SOL=D("120")) for n in ("binance", "okx", "bybit")}
    f.answers = crash
    await f.refresh()
    check(f.price("SOL") == before and "скачок" in f.problems["SOL"], f"SOL −20% за раз → держим старую цену {before}, ждём")
    check(f.price("BTC") is not None, "остальные монеты при этом обновились как обычно")
    await f.refresh()
    check(f.price("SOL") == D("120") and "SOL" not in f.problems, "второй замер показал то же → новая цена 120 принята")
    f.answers = {n: market(D("1.005"), SOL=D("60")) for n in ("binance", "okx", "bybit")}
    await f.refresh()
    f.answers = {n: market(D("1.005"), SOL=D("121")) for n in ("binance", "okx", "bybit")}
    await f.refresh()
    check(f.price("SOL") == D("121"), "разовый «провал» до 60 так и не стал ценой")

    print("6. устаревшая цена не используется")
    f.coin_at["TON"] = time.time() - P.MAX_AGE - 5
    check(f.price("TON") is None and f.price("BTC") is not None, "цена TON старше 10 минут → нет её, у BTC есть")
    check("TON" not in f.snapshot(), "и на сайте TON без курса")

    print("7. без цены монеты счёт в ней не выставляется")
    from app import orders as O
    from app.db import init_db, session_scope
    from app.models import User
    from app.prices import price_feed
    await init_db()
    price_feed.prices, price_feed.updated_at, price_feed.coin_at = {}, 0.0, {}
    async with session_scope() as s:
        u = User(tg_id=1, username="p")
        s.add(u)
        await s.flush()
    try:
        async with session_scope() as s:
            await O.create_order(s, await s.get(User, u.id), steam_login="gooduser", amount=D("1000"), currency="RUB",
                                 method_code="btc")
        msg = "создан"
    except O.OrderError as e:
        msg = str(e)
    check("временно недоступна" in msg, f"BTC без подтверждённой цены → «{msg}»")
    async with session_scope() as s:
        o = await O.create_order(s, await s.get(User, u.id), steam_login="gooduser", amount=D("1000"), currency="RUB",
                                 method_code="usdt_trc20")
    check(o.id, "а в USDT (курс 1:1) заказ создаётся как обычно")

    print(f"\nALL GOOD: {OK} checks passed")


asyncio.run(main())
