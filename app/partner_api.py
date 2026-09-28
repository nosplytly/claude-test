"""Public partner API: /api/v1/*, authenticated with the X-API-Key header.

Partners top up their site balance with crypto (deposits) and spend it on Steam top-ups.
All responses: {"ok": true, ...} or {"ok": false, "error": "..."}.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .auth import client_ip
from .config import settings
from .db import session_scope
from .deposits import create_deposit, deposit_view
from .models import ApiKey, Invoice, LedgerEntry, Order, User
from .money import D, plain, to_micro, usd_str
from .nervixy import CURRENCIES, NervixyError, NervixyTransportError, nervixy
from .orders import InsufficientBalance, OrderError, create_order, discount_for, limits, public_order
from .payments.methods import enabled_methods
from .ratelimit import hit
from .utils import iso, sha256, utcnow

router = APIRouter(prefix="/api/v1")
EXT_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")


class ApiError(Exception):
    def __init__(self, status: int, error: str, **extra):
        self.status, self.error, self.extra = status, error, extra


def api_error_response(e: ApiError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": e.error, **e.extra}, status_code=e.status)


async def api_user(request: Request) -> User:
    key = request.headers.get("x-api-key") or ""
    if not key and request.headers.get("authorization", "").lower().startswith("bearer "):
        key = request.headers["authorization"][7:]
    key = key.strip()
    if not key:
        raise ApiError(401, "Нужен заголовок X-API-Key")
    hit(f"apikey:{sha256(key)[:20]}", 120, 60)
    ip = client_ip(request)
    async with session_scope() as s:
        k = (await s.execute(select(ApiKey).where(ApiKey.key_hash == sha256(key), ApiKey.revoked.is_(False)))).scalar_one_or_none()
        if not k:
            raise ApiError(401, "Неверный API-ключ")
        if k.allowed_ips and ip not in {x.strip() for x in k.allowed_ips.split(",") if x.strip()}:
            raise ApiError(403, f"IP {ip} не в белом списке ключа")
        k.last_used_at, k.last_ip = utcnow(), ip
        u = await s.get(User, k.user_id)
        if not u or u.is_banned:
            raise ApiError(403, "Аккаунт заблокирован")
        return u


def _dec(x, name: str) -> Decimal:
    try:
        v = D(x)
    except (InvalidOperation, ValueError, TypeError) as e:
        raise ApiError(400, f"Некорректное поле {name}") from e
    if not v.is_finite() or v <= 0:
        raise ApiError(400, f"Некорректное поле {name}")
    return v


# ------------------------------------------------------------------ account & reference data

@router.get("/me")
async def me(u: User = Depends(api_user)):
    return {"ok": True, "user_id": u.id, "telegram_id": u.tg_id, "username": u.username,
            "balance_usd": usd_str(u.balance_micro, 6), "discount": plain(discount_for(u))}


@router.get("/rates")
async def rates(u: User = Depends(api_user)):
    try:
        r = await nervixy.rates()
        lim = {c: dict(zip(("min", "max"), map(plain, await limits(c)))) for c in CURRENCIES}
    except (NervixyError, NervixyTransportError) as e:
        raise ApiError(503, "Курсы временно недоступны") from e
    return {"ok": True, "base": "USD", "rates": {k: str(v) for k, v in r.items()}, "discount": plain(discount_for(u)),
            "limits": lim}


@router.get("/methods")
async def methods(u: User = Depends(api_user)):
    return {"ok": True, "methods": [{"code": m.code, "coin": m.coin, "network": m.network,
                                     "network_title": m.network_title, "confirmations": m.confirmations}
                                    for m in enabled_methods()],
            "deposit_min_usd": plain(settings.min_deposit_usd), "deposit_max_usd": plain(settings.max_deposit_usd)}


class LoginIn(BaseModel):
    steam_login: str = Field(min_length=1, max_length=64)


@router.post("/steam/check")
async def steam_check(body: LoginIn, u: User = Depends(api_user)):
    try:
        valid, _ = await nervixy.check_login(body.steam_login.strip())
    except NervixyError as e:
        if e.status == 400:
            return {"ok": True, "steam_login": body.steam_login, "valid": False}
        raise ApiError(503, "Проверка временно недоступна") from e
    except NervixyTransportError as e:
        raise ApiError(503, "Проверка временно недоступна") from e
    return {"ok": True, "steam_login": body.steam_login, "valid": valid}


class QuoteIn(BaseModel):
    amount: str | float | int
    currency: str = "RUB"


@router.post("/quote")
async def quote(body: QuoteIn, u: User = Depends(api_user)):
    amount = _dec(body.amount, "amount").quantize(Decimal("0.01"))
    cur = body.currency.upper()
    if cur not in CURRENCIES:
        raise ApiError(400, "currency: RUB, KZT, UAH или USD")
    try:
        r = await nervixy.rates()
        lo, hi = await limits(cur)
    except (NervixyError, NervixyTransportError) as e:
        raise ApiError(503, "Курсы временно недоступны") from e
    nominal = amount / r[cur]
    price = to_micro(nominal * (1 - discount_for(u) / 100), up=True)
    return {"ok": True, "amount": plain(amount), "currency": cur, "nominal_usd": usd_str(to_micro(nominal), 6),
            "price_usd": usd_str(price, 6), "discount": plain(discount_for(u)), "min": plain(lo), "max": plain(hi),
            "within_limits": lo <= amount <= hi}


# ------------------------------------------------------------------ orders

class OrderIn(BaseModel):
    steam_login: str = Field(min_length=1, max_length=64)
    amount: str | float | int
    currency: str = "RUB"
    external_id: str | None = Field(default=None, max_length=64)


@router.post("/orders")
async def create(body: OrderIn, u: User = Depends(api_user)):
    hit(f"apiord:{u.id}", 60, 60)
    ext = (body.external_id or "").strip() or None
    if ext and not EXT_ID_RE.match(ext):
        raise ApiError(400, "external_id: до 64 символов A-Z a-z 0-9 _ . : -")
    if ext:
        async with session_scope() as s:
            dup = (await s.execute(select(Order).where(Order.user_id == u.id, Order.external_id == ext))).scalar_one_or_none()
            if dup:
                return {"ok": True, "duplicate": True, "order": public_order(dup)}
    amount = _dec(body.amount, "amount")
    try:
        async with session_scope() as s:  # `u` is detached: create_order touches `s` only after supplier calls
            o = await create_order(s, u, steam_login=body.steam_login, amount=amount, currency=body.currency,
                                   method_code=None, source="api", external_id=ext, balance_only=True)
            view = public_order(o)
    except InsufficientBalance as e:
        raise ApiError(402, "Недостаточно средств на балансе", balance_usd=usd_str(e.balance_micro, 6),
                       required_usd=usd_str(e.required_micro, 6)) from e
    except OrderError as e:
        raise ApiError(400, str(e)) from e
    except IntegrityError:  # the same external_id raced in from a parallel request
        if not ext:
            raise
        return {"ok": True, "duplicate": True, "order": public_order(await _find_order(u, external_id=ext))}
    return {"ok": True, "duplicate": False, "order": view}


async def _find_order(u: User, **where) -> Order:
    async with session_scope() as s:
        q = select(Order).where(Order.user_id == u.id)
        for k, v in where.items():
            q = q.where(getattr(Order, k) == v)
        o = (await s.execute(q)).scalar_one_or_none()
    if not o:
        raise ApiError(404, "Заказ не найден")
    return o


@router.get("/orders/{order_id}")
async def get_order(order_id: str, u: User = Depends(api_user)):
    return {"ok": True, "order": public_order(await _find_order(u, public_id=order_id))}


@router.get("/orders/external/{external_id}")
async def get_order_ext(external_id: str, u: User = Depends(api_user)):
    return {"ok": True, "order": public_order(await _find_order(u, external_id=external_id))}


@router.get("/orders")
async def list_orders(limit: int = 50, offset: int = 0, u: User = Depends(api_user)):
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    async with session_scope() as s:
        total = (await s.execute(select(func.count()).select_from(Order).where(Order.user_id == u.id))).scalar_one()
        rows = (await s.execute(select(Order).where(Order.user_id == u.id).order_by(Order.id.desc())
                                .limit(limit).offset(offset))).scalars().all()
    return {"ok": True, "total": total, "limit": limit, "offset": offset, "orders": [public_order(o) for o in rows]}


# ------------------------------------------------------------------ balance

class DepositIn(BaseModel):
    amount_usd: str | float | int
    method: str = Field(max_length=24)


def _public_deposit(v: dict) -> dict:
    v = dict(v)
    for k in ("invoice_id", "stage", "qr"):
        v.pop(k, None)
    return v


@router.post("/deposits")
async def new_deposit(body: DepositIn, u: User = Depends(api_user)):
    hit(f"apidep:{u.id}", 20, 600)
    try:
        async with session_scope() as s:
            user = await s.get(User, u.id)
            inv = await create_deposit(s, user, amount_usd=_dec(body.amount_usd, "amount_usd"), method_code=body.method)
            await s.flush()
            view = await deposit_view(s, inv)
    except OrderError as e:
        raise ApiError(400, str(e)) from e
    return {"ok": True, "deposit": _public_deposit(view)}


@router.get("/deposits/{deposit_id}")
async def get_deposit(deposit_id: str, u: User = Depends(api_user)):
    async with session_scope() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.public_id == deposit_id, Invoice.user_id == u.id))).scalar_one_or_none()
        if not inv:
            raise ApiError(404, "Пополнение не найдено")
        return {"ok": True, "deposit": _public_deposit(await deposit_view(s, inv))}


@router.get("/balance/history")
async def balance_history(limit: int = 50, offset: int = 0, u: User = Depends(api_user)):
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    async with session_scope() as s:
        total = (await s.execute(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.user_id == u.id))).scalar_one()
        rows = (await s.execute(select(LedgerEntry, Order.public_id).outerjoin(Order, LedgerEntry.order_id == Order.id)
                                .where(LedgerEntry.user_id == u.id).order_by(LedgerEntry.id.desc())
                                .limit(limit).offset(offset))).all()
    return {"ok": True, "total": total, "limit": limit, "offset": offset, "items": [
        {"id": e.id, "kind": e.kind, "amount_usd": usd_str(e.delta_micro, 6), "balance_usd": usd_str(e.balance_after_micro, 6),
         "order_id": pid, "comment": e.comment, "created_at": iso(e.created_at)} for e, pid in rows]}

