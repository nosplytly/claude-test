"""Money helpers.

USD accounting (balances, prices, costs) is kept in integer micro-dollars (1 USD = 1_000_000)
so balance updates can be done atomically in SQL. Crypto and fiat nominal amounts are Decimals
stored as text.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal

MICRO = Decimal("1000000")


def D(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(repr(x))
    return Decimal(str(x))


def to_micro(usd: Decimal, *, up: bool = False) -> int:
    q = (D(usd) * MICRO).quantize(Decimal(1), rounding=ROUND_CEILING if up else ROUND_HALF_UP)
    return int(q)


def from_micro(micro: int | None) -> Decimal:
    return (Decimal(micro or 0) / MICRO).quantize(Decimal("0.000001"))


def usd_str(micro: int | None, places: int = 2) -> str:
    return str(from_micro(micro).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def ceil_to(x: Decimal, step: Decimal) -> Decimal:
    return (D(x) / step).to_integral_value(rounding=ROUND_CEILING) * step


def floor_to(x: Decimal, step: Decimal) -> Decimal:
    return (D(x) / step).to_integral_value(rounding=ROUND_DOWN) * step


def units_to_amount(units: int, decimals: int) -> Decimal:
    return (Decimal(units) / (Decimal(10) ** decimals)).normalize()


def amount_to_units(amount: Decimal, decimals: int) -> int:
    return int((D(amount) * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_HALF_UP))


def plain(x: Decimal) -> str:
    """Decimal -> plain string without exponent and trailing zeros."""
    s = format(D(x).normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
