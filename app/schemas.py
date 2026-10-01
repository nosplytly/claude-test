"""Everything a client sends, checked by pydantic before any handler code runs.

Shared by the site API (/api/*) and the partner API (/api/v1/*):
- an unknown field is an error, not silently dropped (a typo like "promo_code" must not quietly give a full-price
  order, and nobody can slip in fields the handler never expected);
- strings are trimmed and must match their exact format: Steam login, tx hash, ids, promo code, method code;
- money is a Decimal from a JSON string or number — never a float inside the code — finite, above zero, at most
  2 decimals (no silent rounding of amounts); true/false is not a number;
- closed lists (currency, language) accept nothing else.
Data from other services (the supplier's webhook, Telegram's signed user) is typed here too, but extra fields in it
are allowed: a new field on their side must not break payments or logins.
"""
from __future__ import annotations

import ipaddress
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import Path, Query
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, field_validator

from .i18n import Problem, t

STEAM_LOGIN = r"^[A-Za-z0-9_.\-]{3,64}$"
PUBLIC_ID = r"^[A-Z0-9]{4,32}$"  # SH… orders, DP… deposits (app.utils.public_id)
EXTERNAL_ID = r"^[A-Za-z0-9_.:\-]{1,64}$"
TXID = r"^[A-Za-z0-9+/=_\-]{20,128}$"
PROMO = r"^[A-Z0-9_\-]{3,32}$"
METHOD = r"^[a-z0-9_]{2,24}$"
LOGIN_TOKEN = r"^[A-Za-z0-9_\-]{8,64}$"  # one-time bot login link (app.utils.token)


class In(BaseModel):
    """A request body: no unknown fields, strings trimmed."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Foreign(BaseModel):
    """Data another service sends us: typed, but their new fields are fine."""
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


def _money_in(v):
    if isinstance(v, bool):
        raise Invalid("err.amount_bool")
    if isinstance(v, float):
        return repr(v)  # 0.1 stays 0.1, not 0.1000000000000000055511151231257827
    return v


def _upper(v):  # before the pattern check: pydantic matches the pattern first and changes case only after
    return v.strip().upper() if isinstance(v, str) else v


def _lower(v):
    return v.strip().lower() if isinstance(v, str) else v


def _blank_is_none(v):
    return None if isinstance(v, str) and not v.strip() else v


SteamLogin = Annotated[str, StringConstraints(strip_whitespace=True, pattern=STEAM_LOGIN)]
Money = Annotated[Decimal, BeforeValidator(_money_in),
                  Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)]
Currency = Annotated[Literal["RUB", "KZT", "UAH", "USD"], BeforeValidator(_upper)]
MethodCode = Annotated[str, BeforeValidator(_lower), StringConstraints(pattern=METHOD)]
PromoCode = Annotated[str, BeforeValidator(_upper), StringConstraints(pattern=PROMO)]
TxId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=TXID)]
ExternalId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=EXTERNAL_ID)]

# path and query parameters
OrderId = Annotated[str, Path(pattern=PUBLIC_ID)]
DepositId = Annotated[str, Path(pattern=PUBLIC_ID)]
ExternalIdPath = Annotated[str, Path(pattern=EXTERNAL_ID)]
InvoiceNo = Annotated[int, Path(ge=1, le=2**53)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=1_000_000)]
LoginToken = Annotated[str, Query(pattern=LOGIN_TOKEN)]


# ------------------------------------------------------------------ site API (/api/*)

class LoginCheckIn(In):
    login: SteamLogin


class DevLoginIn(In):
    tg_id: int = Field(default=1000001, ge=1, le=10**13)
    name: str = Field(default="dev", min_length=1, max_length=64)


class WebAppIn(In):
    init_data: str = Field(min_length=10, max_length=4096)


class PromoIn(In):
    code: PromoCode


class LangIn(In):
    lang: Literal["ru", "en"]


class OrderIn(In):
    steam_login: SteamLogin
    amount: Money
    currency: Currency
    method: Annotated[MethodCode | None, BeforeValidator(_blank_is_none)] = None  # None: paid from the balance
    promo: Annotated[PromoCode | None, BeforeValidator(_blank_is_none)] = None


class ClaimIn(In):
    txid: TxId


class DepositWebIn(In):
    amount_usd: Money
    method: MethodCode


class ApiSettingsIn(In):
    allowed_ips: str = Field(default="", max_length=500)  # "1.2.3.4, 2001:db8::1" → "1.2.3.4,2001:db8::1"
    webhook_url: str = Field(default="", max_length=500)  # scheme, DNS and private ranges: app.webhooks.check_url

    @field_validator("allowed_ips")
    @classmethod
    def _ips(cls, v: str) -> str:
        items = [x.strip() for x in v.replace(";", ",").replace(" ", ",").split(",") if x.strip()]
        if len(items) > 20:
            raise Invalid("err.too_many_ips")
        out = []
        for x in items:
            try:
                out.append(str(ipaddress.ip_address(x)))
            except ValueError:
                raise Invalid("err.bad_ip", ip=x) from None
        return ",".join(dict.fromkeys(out))


# ------------------------------------------------------------------ partner API (/api/v1/*)

class PartnerLoginIn(In):
    steam_login: SteamLogin


class PartnerQuoteIn(In):
    amount: Money
    currency: Currency = "RUB"


class PartnerOrderIn(In):
    steam_login: SteamLogin
    amount: Money
    currency: Currency = "RUB"
    external_id: Annotated[ExternalId | None, BeforeValidator(_blank_is_none)] = None


class PartnerDepositIn(In):
    amount_usd: Money
    method: MethodCode


# ------------------------------------------------------------------ from other services

class TelegramUser(Foreign):
    """The user inside a Mini App's launch data — read only after its HMAC signature has been checked."""
    id: int = Field(ge=1, le=10**13)
    username: Annotated[str, StringConstraints(max_length=64)] | None = None
    first_name: Annotated[str, StringConstraints(max_length=256)] | None = None
    last_name: Annotated[str, StringConstraints(max_length=256)] | None = None
    language_code: Annotated[str, StringConstraints(max_length=35)] | None = None


def _text_id(v):
    return str(v) if isinstance(v, int) and not isinstance(v, bool) else v


Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]


class NervixyEvent(Foreign):
    """The supplier's webhook body — parsed only after its HMAC signature has been checked."""
    order_id: Annotated[Short | None, BeforeValidator(_text_id)] = None
    event: Short | None = None
    status: Short | None = None


# friendly words for the site when a field doesn't pass (the partner API gets the field names and reasons instead)
FIELD_MESSAGES = {
    "login": "err.login_format", "steam_login": "err.login_format",
    "amount": "err.amount", "amount_usd": "err.amount",
    "currency": "err.currency", "method": "err.method",
    "code": "promo.not_found", "promo": "promo.not_found",
    "txid": "err.txid", "init_data": "err.webapp",
    "allowed_ips": "err.ips_too_long", "webhook_url": "err.webhook_url_long",
}


class Invalid(Problem, ValueError):
    """A validator's own words (a key), shown to the customer in their language."""


def site_message(errors: list[dict]) -> str:
    """One line for the customer, in the request's language: our own words from a validator, else the field's,
    else a general one."""
    for e in errors:
        own = (e.get("ctx") or {}).get("error")
        if isinstance(own, Problem):
            return own.text()
        if e.get("type") == "extra_forbidden":
            return t("err.extra_field", field=e["loc"][-1])
    for e in errors:
        loc = [x for x in e.get("loc", []) if isinstance(x, str) and x not in ("body", "query", "path")]
        if loc and loc[-1] in FIELD_MESSAGES:
            return t(FIELD_MESSAGES[loc[-1]])
    return t("err.check_input")