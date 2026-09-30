from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import logging
import re
from decimal import Decimal
from urllib.parse import quote

import segno
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import avatars, events, i18n, waitlist
from . import bot as bot_mod
from .auth import verify_webapp
from .auth import (LOGIN_COOKIE, LOGIN_TTL, SESSION_COOKIE, SESSION_TTL, client_ip, create_session, current_user,
                   require_user, start_login, upsert_tg_user)
from .config import settings
from .db import get_db, session_scope
from .models import Claim, Invoice, LoginRequest, Order, Transfer, User, WebSession
from .deposits import create_deposit, deposit_view, display_address
from .models import ApiKey, LedgerEntry
from .money import plain, usd_str
from .nervixy import CURRENCIES, NervixyError, NervixyTransportError, nervixy
from . import promos
from .orders import (CUR_SIGN, OrderError, apply_status, check_status, create_order, discount_for, limits,
                     move_status, order_transfers)
from .payments.invoices import effective_price
from .utils import token
from .webhooks import BadWebhookUrl, check_url
from .payments.methods import METHODS, enabled_methods
from .prices import price_feed
from .ratelimit import hit
from .schemas import (ApiSettingsIn, ClaimIn, DepositId, DepositWebIn, DevLoginIn, InvoiceNo, LangIn, LoginCheckIn,
                      LoginToken, NervixyEvent, OrderId, OrderIn, PromoIn, TelegramUser, WebAppIn)
from .utils import background, iso, sha256, utcnow

log = logging.getLogger("sh.api")
router = APIRouter()

CHIPS = {"RUB": [500, 1000, 2500, 5000], "KZT": [2500, 5000, 10000, 25000], "UAH": [250, 500, 1000, 2000],
         "USD": [5, 10, 25, 50]}


def csrf(request: Request) -> None:
    """State-changing calls must carry a custom header: browsers can't send it cross-site without CORS."""
    if request.headers.get("x-sh") != "1":
        raise HTTPException(403, "bad request")


def set_cookie(resp: Response, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=settings.secure_cookies, samesite="lax", path="/")


def qr_payload(inv: Invoice) -> str:
    m = METHODS[inv.method]
    addr = display_address(m, inv.address)
    amt = plain(inv.amount)
    memo = f"&text={quote(inv.comment)}" if inv.comment else ""
    if m.code == "btc":
        return f"bitcoin:{addr}?amount={amt}"
    elif m.code == "ltc":
        return f"litecoin:{addr}?amount={amt}"
    elif m.code == "ton":
        return f"ton://transfer/{addr}?amount={inv.units}{memo}"
    elif m.code == "usdt_ton":
        return f"ton://transfer/{addr}?jetton={m.token}&amount={inv.units}{memo}"
    elif m.code == "sol":
        return f"solana:{addr}?amount={amt}"
    elif m.code == "usdt_sol":
        return f"solana:{addr}?amount={amt}&spl-token={m.token}"
    else:
        return addr  # EVM and Tron wallets scan the bare address


def method_view(m) -> dict:
    return {"code": m.code, "coin": m.coin, "network": m.network, "network_title": m.network_title,
            "stable": m.stable, "confirmations": m.confirmations, "comment": m.uses_comment, "step": plain(m.step),
            "min_usd": plain(m.min_usd)}


def user_view(u: User | None) -> dict | None:
    if not u:
        return None
    return {"name": u.display_name, "username": u.username, "first_name": u.first_name,
            "avatar": f"/api/me/avatar?u={u.id}",  # ?u= keeps the browser cache apart when accounts switch
            "balance": usd_str(u.balance_micro), "balance_micro": u.balance_micro, "discount": plain(discount_for(u))}


STAGE = {"paid": "topup", "submitting": "topup", "queued": "topup", "uncertain": "topup", "processing": "topup",
         "delivered": "done", "rejected": "failed", "expired": "expired", "cancelled": "cancelled"}


async def order_view(s: AsyncSession, o: Order, full: bool = True) -> dict:
    pay_fiat = (o.amount * (1 - o.client_discount / 100)).quantize(Decimal("0.01"))
    v = {"id": o.public_id, "status": o.status, "steam_login": o.steam_login, "currency": o.currency,
         "sign": CUR_SIGN.get(o.currency, o.currency), "amount": plain(o.amount), "pay_fiat": plain(pay_fiat),
         "price_usd": usd_str(o.price_micro), "method": o.method, "created_at": iso(o.created_at),
         "finished_at": iso(o.finished_at), "error": o.error if o.status == "rejected" else None,
         "stage": STAGE.get(o.status, "pay"), "invoice": None, "payment": None}
    if not full:
        return v
    invs = (await s.execute(select(Invoice).where(Invoice.order_id == o.id).order_by(Invoice.id.desc()))).scalars().all()
    inv = next((i for i in invs if i.status in ("pending", "detected")), invs[0] if invs else None)
    if inv:
        m = METHODS[inv.method]
        v["invoice"] = {"id": inv.id, "method": m.code, "coin": m.coin, "network": m.network,
                        "network_title": m.network_title, "address": display_address(m, inv.address),
                        "amount": plain(inv.amount), "comment": inv.comment, "status": inv.status,
                        "expires_at": iso(inv.expires_at), "usd": usd_str(inv.usd_micro),
                        "from_balance": usd_str(max(o.price_micro - inv.usd_micro, 0)) if inv == invs[-1] else "0.00",
                        "qr": f"/api/invoices/{inv.id}/qr.svg?u={inv.units}", "confirmations": m.confirmations}
    trs = await order_transfers(s, o.id)
    if trs:
        t = trs[-1]
        m = METHODS[t.method]
        v["payment"] = {"amount": plain(t.amount), "coin": m.coin, "confirmations": t.confirmations,
                        "required": m.confirmations, "final": t.final, "tx_url": m.tx_url(t.txid),
                        "credited": t.status == "credited"}
        if o.status == "awaiting_payment" and any(x.status == "matched" for x in trs):
            v["stage"] = "confirming"
    return v


# ------------------------------------------------------------------ public

@router.get("/api/config")
async def api_config(user: User | None = Depends(current_user)):
    currencies = []
    try:
        rates = await nervixy.rates()
        for c in CURRENCIES:
            lo, hi = await limits(c)
            currencies.append({"code": c, "sign": CUR_SIGN[c], "min": plain(lo), "max": plain(hi), "chips": CHIPS[c]})
        rates_out = {k: str(v) for k, v in rates.items()}
    except (NervixyError, NervixyTransportError) as e:
        log.warning("config without rates: %s", e)
        rates_out = {}
    return {"site": settings.site_name, "discount": plain(settings.client_discount),
            "markup": plain(settings.volatile_markup), "currencies": currencies, "rates": rates_out,
            "prices": price_feed.snapshot(), "methods": [method_view(m) for m in enabled_methods()],
            "bot": bot_mod.bot_username, "dev_login": settings.is_dev and not settings.bot_token,
            "dev_pay": settings.is_dev and settings.nervixy_mock,
            "support_url": settings.support_url or None, "invoice_ttl_min": settings.invoice_ttl_min,
            "deposit_min": plain(settings.min_deposit_usd), "deposit_max": plain(settings.max_deposit_usd),
            "user": user_view(user), "available": bool(rates_out),
            "waitlist": await waitlist.has(user.tg_id) if user else False}  # the plugin card: "Вы в списке" after a reload


@router.get("/api/rates")
async def api_rates():
    try:
        rates = {k: str(v) for k, v in (await nervixy.rates()).items()}
    except (NervixyError, NervixyTransportError):
        rates = {}
    return {"rates": rates, "prices": price_feed.snapshot(),
            "effective": {m.code: str(effective_price(m) or "") for m in enabled_methods()}}


@router.post("/api/steam/check", dependencies=[Depends(csrf)])
async def api_check_login(body: LoginCheckIn, request: Request):
    hit(f"chk:{client_ip(request)}", 20, 60)
    login = body.login
    try:
        valid, msg = await nervixy.check_login(login)
    except NervixyError as e:
        if e.status == 400:
            return {"valid": False, "message": "Некорректный логин"}
        raise HTTPException(503, "Проверка временно недоступна") from e
    except NervixyTransportError as e:
        raise HTTPException(503, "Проверка временно недоступна") from e
    return {"valid": valid, "message": None if valid else "Аккаунт не найден или его нельзя пополнить"}


# ------------------------------------------------------------------ auth

@router.post("/api/auth/start", dependencies=[Depends(csrf)])
async def api_auth_start(request: Request, response: Response, s: AsyncSession = Depends(get_db)):
    hit(f"auth:{client_ip(request)}", 10, 60)
    if not bot_mod.bot_username:
        raise HTTPException(503, "Вход через Telegram временно недоступен")
    req, secret = await start_login(s, client_ip(request), request.headers.get("user-agent", ""))
    await s.commit()
    set_cookie(response, LOGIN_COOKIE, f"{req.id}.{secret}", int(LOGIN_TTL.total_seconds()))
    return {"url": f"https://t.me/{bot_mod.bot_username}?start=login_{req.id}", "code": req.code,
            "expires_in": int(LOGIN_TTL.total_seconds())}


@router.get("/api/auth/poll")
async def api_auth_poll(request: Request, response: Response, s: AsyncSession = Depends(get_db)):
    raw = request.cookies.get(LOGIN_COOKIE, "")
    rid, _, secret = raw.partition(".")
    req = await s.get(LoginRequest, rid) if rid else None
    if not req or not hmac.compare_digest(req.browser_hash, sha256(secret)):
        return {"status": "none"}
    if req.status == "cancelled":
        return {"status": "cancelled"}
    if req.status == "pending":
        return {"status": "expired" if req.expires_at < utcnow() else "pending"}
    if req.status != "confirmed" or req.expires_at < utcnow() - LOGIN_TTL:
        return {"status": "expired"}
    user = await s.get(User, req.user_id)
    token = await create_session(s, user, client_ip(request), request.headers.get("user-agent", ""))
    req.status = "used"
    await s.commit()
    set_cookie(response, SESSION_COOKIE, token, int(SESSION_TTL.total_seconds()))
    response.delete_cookie(LOGIN_COOKIE, path="/")
    return {"status": "ok", "user": user_view(user)}


@router.get("/auth/tg")
async def auth_from_bot_link(t: LoginToken, k: LoginToken, request: Request, s: AsyncSession = Depends(get_db)):
    """One-time link from the bot's "back to site" button."""
    resp = RedirectResponse("/", status_code=303)
    req = await s.get(LoginRequest, t)
    if (not req or req.link_used or req.status not in ("confirmed", "used") or not req.link_key_hash
            or not hmac.compare_digest(req.link_key_hash, sha256(k)) or req.expires_at < utcnow() - LOGIN_TTL):
        return resp
    if request.cookies.get(SESSION_COOKIE):
        from .auth import user_from_session
        if await user_from_session(s, request.cookies.get(SESSION_COOKIE)):
            req.link_used = True
            await s.commit()
            return resp
    user = await s.get(User, req.user_id)
    token = await create_session(s, user, client_ip(request), request.headers.get("user-agent", ""))
    req.link_used = True
    await s.commit()
    set_cookie(resp, SESSION_COOKIE, token, int(SESSION_TTL.total_seconds()))
    return resp


@router.post("/api/auth/logout", dependencies=[Depends(csrf)])
async def api_logout(request: Request, response: Response, s: AsyncSession = Depends(get_db)):
    raw = request.cookies.get(SESSION_COOKIE) or request.headers.get("x-sh-session")
    if raw:
        ws = await s.get(WebSession, sha256(raw))
        if ws:
            await s.delete(ws)
            await s.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/api/auth/webapp", dependencies=[Depends(csrf)])
async def api_auth_webapp(body: WebAppIn, request: Request, response: Response):
    """Opened as a Telegram Mini App: log in from Telegram's signed launch data — no code to tap."""
    hit(f"webapp:{client_ip(request)}", 30, 600)
    raw_user = verify_webapp(body.init_data, settings.bot_token)
    try:  # signed by Telegram, and still typed before use: an id that isn't a number must not become a 500
        tg = TelegramUser.model_validate(raw_user) if raw_user else None
    except ValidationError:
        tg = None
    if not tg:
        raise HTTPException(401, "Не удалось подтвердить вход через Telegram — откройте приложение из бота заново")
    async with session_scope() as s:
        user = await upsert_tg_user(s, tg.id, tg.username, tg.first_name, tg.last_name, tg.language_code)
        if user.is_banned:
            raise HTTPException(403, "Аккаунт заблокирован")
        raw = await create_session(s, user, client_ip(request), request.headers.get("user-agent", ""))
        view = user_view(user)
    set_cookie(response, SESSION_COOKIE, raw, int(SESSION_TTL.total_seconds()))
    return {"user": view, "token": raw}  # the token also goes in a header where cookies can't (Telegram Web iframe)


@router.post("/api/dev/login", dependencies=[Depends(csrf)])
async def api_dev_login(body: DevLoginIn, request: Request, response: Response, s: AsyncSession = Depends(get_db)):
    if not settings.is_dev:
        raise HTTPException(404)
    user = await upsert_tg_user(s, body.tg_id, body.name, "Dev", None)
    token = await create_session(s, user, client_ip(request), request.headers.get("user-agent", ""))
    await s.commit()
    set_cookie(response, SESSION_COOKIE, token, int(SESSION_TTL.total_seconds()))
    return {"user": user_view(user)}


@router.get("/api/me")
async def api_me(user: User | None = Depends(current_user)):
    return {"user": user_view(user)}


@router.post("/api/promo/check", dependencies=[Depends(csrf)])
async def api_promo_check(body: PromoIn, request: Request, user: User | None = Depends(current_user)):
    """What a code gives. Discount codes: the total discount the order form should show (capped like the server
    will cap it). Bonus codes are only described here; /api/promo/redeem credits them."""
    hit(f"promo:{client_ip(request)}", 12, 600)  # guessing codes gets you rate-limited, then banned
    try:
        async with session_scope() as s:
            p = await promos.find(s, body.code)
            if user and await promos.uses_by(s, p.id, user.id) >= p.per_user:
                raise promos.PromoError("Вы уже использовали этот промокод")
    except promos.PromoError as e:
        raise HTTPException(400, str(e)) from e
    view = {"code": p.code, "kind": p.kind, "value": plain(p.value), "label": promos.label(p)}
    if p.kind == "discount":
        sd = await nervixy.supplier_discount()
        view["total"] = plain(promos.combined(discount_for(user), sd, p.value))
    return view


@router.post("/api/promo/redeem", dependencies=[Depends(csrf)])
async def api_promo_redeem(body: PromoIn, request: Request, user: User = Depends(require_user)):
    hit(f"promo:{client_ip(request)}", 12, 600)
    try:
        async with session_scope() as s:
            p, new = await promos.redeem_bonus(s, user.id, body.code)
    except promos.PromoError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "label": promos.label(p), "balance": usd_str(new)}


@router.get("/api/me/avatar")
async def api_avatar(user: User = Depends(require_user)):
    """The logged-in user's own Telegram photo; 204 when there is none (the site keeps the letter)."""
    data = await avatars.get(user.tg_id)
    if not data:
        return Response(status_code=204, headers={"Cache-Control": "private, max-age=600"})
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/api/me/lang", dependencies=[Depends(csrf)])
async def api_me_lang(body: LangIn, user: User = Depends(require_user)):
    """The RU/EN switch on the site: the bot talks to this customer in the same language from now on."""
    await i18n.choose(user.tg_id, body.lang)
    return {"ok": True}


@router.get("/api/waitlist")
async def api_waitlist_status(user: User | None = Depends(current_user)):
    """Right after signing in: is this customer already waiting for the plugin (maybe joined through the bot)?"""
    return {"joined": await waitlist.has(user.tg_id) if user else False}


@router.post("/api/waitlist", dependencies=[Depends(csrf)])
async def api_waitlist(user: User = Depends(require_user)):
    """"Узнать о запуске" on the FunPay / Playerok card: the signed-in customer goes on the waiting list."""
    return {"ok": True, "new": await waitlist.join(user.tg_id, user.username)}

# ------------------------------------------------------------------ orders

@router.post("/api/orders", dependencies=[Depends(csrf)])
async def api_create_order(body: OrderIn, request: Request, user: User = Depends(require_user)):
    hit(f"ord:{user.id}", 8, 60)
    try:
        async with session_scope() as s:  # `user` is detached: create_order touches `s` only after supplier calls
            o = await create_order(s, user, steam_login=body.steam_login, amount=body.amount, currency=body.currency,
                                   method_code=body.method, promo_code=body.promo)
            oid = o.id
    except OrderError as e:
        raise HTTPException(400, str(e)) from e
    async with session_scope() as s:
        return await order_view(s, await s.get(Order, oid))


@router.get("/api/orders")
async def api_orders(user: User = Depends(require_user), s: AsyncSession = Depends(get_db)):
    rows = (await s.execute(select(Order).where(Order.user_id == user.id).order_by(Order.id.desc()).limit(30))).scalars().all()
    return {"orders": [await order_view(s, o, full=False) for o in rows]}


async def _own_order(s: AsyncSession, user: User, pid: str) -> Order:
    o = (await s.execute(select(Order).where(Order.public_id == pid, Order.user_id == user.id))).scalar_one_or_none()
    if not o:
        raise HTTPException(404, "Заказ не найден")
    return o


@router.get("/api/orders/{pid}")
async def api_order(pid: OrderId, user: User = Depends(require_user), s: AsyncSession = Depends(get_db)):
    o = await _own_order(s, user, pid)
    view = await order_view(s, o)
    view["balance"] = usd_str((await s.get(User, user.id)).balance_micro)
    return view


@router.post("/api/orders/{pid}/cancel", dependencies=[Depends(csrf)])
async def api_cancel(pid: OrderId, user: User = Depends(require_user)):
    async with session_scope() as s:
        o = await _own_order(s, user, pid)
        if o.status != "awaiting_payment":
            raise HTTPException(400, "Этот заказ уже нельзя отменить")
        detected = (await s.execute(select(Invoice).where(Invoice.order_id == o.id, Invoice.status == "detected"))).first()
        if detected:
            raise HTTPException(400, "Платёж уже найден — дождитесь подтверждения")
        if not await move_status(s, o.id, ("awaiting_payment",), "cancelled"):  # a payment won the race
            raise HTTPException(400, "Этот заказ уже нельзя отменить")
        for inv in (await s.execute(select(Invoice).where(Invoice.order_id == o.id, Invoice.status == "pending"))).scalars():
            inv.status = "cancelled"  # the unique amount stays reserved: a late payment still lands on the balance
        o.status, o.finished_at = "cancelled", utcnow()
        if o.promo_id:
            await promos.give_back(s, o.id)
        return await order_view(s, o)


def _norm_txid(txid: str) -> str:
    """EVM and Tron hashes are hex: one case, so the same transaction can't be claimed twice by changing letters."""
    return txid.lower() if re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", txid) else txid


@router.post("/api/orders/{pid}/claim", dependencies=[Depends(csrf)])
async def api_claim(pid: OrderId, body: ClaimIn, user: User = Depends(require_user)):
    hit(f"claim:{user.id}", 5, 600)
    txid = _norm_txid(body.txid)
    async with session_scope() as s:
        o = await _own_order(s, user, pid)
        dup = (await s.execute(select(Claim).where(Claim.txid == txid))).scalars().first()
        if dup:
            return {"ok": True, "message": "Эта транзакция уже на проверке"}
        s.add(Claim(user_id=user.id, order_id=o.id, txid=txid, status="waiting"))
    return {"ok": True, "message": "Приняли! Найдём транзакцию в сети и зачислим после проверки — обычно это "
                                   "занимает несколько минут. Уведомление придёт в Telegram."}


@router.post("/api/deposits/{pid}/claim", dependencies=[Depends(csrf)])
async def api_deposit_claim(pid: DepositId, body: ClaimIn, user: User = Depends(require_user)):
    hit(f"claim:{user.id}", 5, 600)
    txid = _norm_txid(body.txid)
    async with session_scope() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.public_id == pid, Invoice.user_id == user.id))).scalar_one_or_none()
        if not inv:
            raise HTTPException(404, "Пополнение не найдено")
        if (await s.execute(select(Claim).where(Claim.txid == txid))).scalars().first():
            return {"ok": True, "message": "Эта транзакция уже на проверке"}
        s.add(Claim(user_id=user.id, order_id=None, txid=txid, status="waiting"))
    return {"ok": True, "message": "Приняли! Найдём транзакцию в сети и зачислим после проверки. "
                                   "Уведомление придёт в Telegram."}


@router.get("/api/invoices/{iid}/qr.svg")
async def api_qr(iid: InvoiceNo, user: User = Depends(require_user), s: AsyncSession = Depends(get_db)):
    inv = await s.get(Invoice, iid)
    if not inv or inv.user_id != user.id:
        raise HTTPException(404)
    qr = segno.make(qr_payload(inv), error="h", boost_error=False)  # "h": survives the coin badge in the centre
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=6, border=0, dark="#0B0B0C", light=None, xmldecl=False, omitsize=True)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ balance top-ups (cabinet)

@router.post("/api/deposits", dependencies=[Depends(csrf)])
async def api_create_deposit(body: DepositWebIn, user: User = Depends(require_user)):
    hit(f"dep:{user.id}", 10, 600)
    try:
        async with session_scope() as s:
            u = await s.get(User, user.id)
            inv = await create_deposit(s, u, amount_usd=body.amount_usd, method_code=body.method)
            await s.flush()
            view = await deposit_view(s, inv)
    except OrderError as e:
        raise HTTPException(400, str(e)) from e
    return view


@router.get("/api/deposits/{pid}")
async def api_deposit(pid: DepositId, user: User = Depends(require_user), s: AsyncSession = Depends(get_db)):
    inv = (await s.execute(select(Invoice).where(Invoice.public_id == pid, Invoice.user_id == user.id))).scalar_one_or_none()
    if not inv:
        raise HTTPException(404, "Пополнение не найдено")
    view = await deposit_view(s, inv)
    view["balance"] = usd_str((await s.get(User, user.id)).balance_micro)
    return view


KIND_RU = {"deposit": "Пополнение", "order": "Заказ", "refund": "Возврат", "adjust": "Корректировка", "promo": "Промокод"}


@router.get("/api/balance/history")
async def api_balance_history(user: User = Depends(require_user), s: AsyncSession = Depends(get_db)):
    rows = (await s.execute(select(LedgerEntry, Order.public_id).outerjoin(Order, LedgerEntry.order_id == Order.id)
                            .where(LedgerEntry.user_id == user.id).order_by(LedgerEntry.id.desc()).limit(40))).all()
    return {"balance": usd_str(user.balance_micro), "items": [
        {"kind": e.kind, "title": KIND_RU.get(e.kind, e.kind), "amount": usd_str(e.delta_micro),
         "comment": e.comment, "order_id": pid, "created_at": iso(e.created_at)} for e, pid in rows]}


# ------------------------------------------------------------------ partner API settings (cabinet)

def key_view(k: ApiKey | None) -> dict | None:
    if not k:
        return None
    return {"prefix": k.prefix, "created_at": iso(k.created_at), "last_used_at": iso(k.last_used_at),
            "last_ip": k.last_ip, "allowed_ips": k.allowed_ips or ""}


async def _active_key(s: AsyncSession, user_id: int) -> ApiKey | None:
    return (await s.execute(select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked.is_(False))
                            .order_by(ApiKey.id.desc()))).scalars().first()


@router.get("/api/account")
async def api_account(user: User = Depends(require_user), s: AsyncSession = Depends(get_db)):
    return {"user": user_view(user), "api_key": key_view(await _active_key(s, user.id)),
            "webhook_url": user.webhook_url or "", "webhook_set": bool(user.webhook_secret)}


@router.post("/api/account/api-key", dependencies=[Depends(csrf)])
async def api_new_key(user: User = Depends(require_user)):
    hit(f"key:{user.id}", 5, 600)
    raw = "SH-" + token(30)
    async with session_scope() as s:
        old = await _active_key(s, user.id)
        ips = old.allowed_ips if old else None
        for k in (await s.execute(select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.revoked.is_(False)))).scalars():
            k.revoked = True
        k = ApiKey(user_id=user.id, key_hash=sha256(raw), prefix=raw[:10], allowed_ips=ips)
        s.add(k)
        await s.flush()
        view = key_view(k)
    return {"key": raw, "api_key": view}


@router.delete("/api/account/api-key", dependencies=[Depends(csrf)])
async def api_revoke_key(user: User = Depends(require_user)):
    async with session_scope() as s:
        for k in (await s.execute(select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.revoked.is_(False)))).scalars():
            k.revoked = True
    return {"ok": True}


@router.post("/api/account/settings", dependencies=[Depends(csrf)])
async def api_settings(body: ApiSettingsIn, user: User = Depends(require_user)):
    url = body.webhook_url
    if url:
        try:
            url = await check_url(url)
        except BadWebhookUrl as e:
            raise HTTPException(400, f"Webhook: {e}") from e
    new_secret = None
    async with session_scope() as s:
        u = await s.get(User, user.id)
        k = await _active_key(s, user.id)
        if k:
            k.allowed_ips = body.allowed_ips or None
        u.webhook_url = url or None
        if url and not u.webhook_secret:
            new_secret = u.webhook_secret = "whsec_" + token(24)
    return {"ok": True, "webhook_secret": new_secret}


@router.post("/api/account/webhook-secret", dependencies=[Depends(csrf)])
async def api_webhook_secret(user: User = Depends(require_user)):
    async with session_scope() as s:
        u = await s.get(User, user.id)
        u.webhook_secret = "whsec_" + token(24)
        return {"webhook_secret": u.webhook_secret}


@router.post("/api/account/webhook-test", dependencies=[Depends(csrf)])
async def api_webhook_test(user: User = Depends(require_user)):
    hit(f"whtest:{user.id}", 5, 300)
    from .webhooks import queue_webhook
    async with session_scope() as s:
        u = await s.get(User, user.id)
        if not u.webhook_url:
            raise HTTPException(400, "Сначала укажите адрес вебхука")
        await queue_webhook(s, u.id, "webhook.test", {"message": "SupplierHub webhook test"})
    return {"ok": True}


# ------------------------------------------------------------------ supplier webhook

@router.post("/api/webhooks/nervixy")
async def nervixy_webhook(request: Request):
    body = await request.body()
    secret = settings.nervixy_webhook_secret
    if not secret:
        raise HTTPException(403, "webhook secret not configured")
    sig = request.headers.get("x-nervixy-signature", "").strip().removeprefix("sha256=")
    calc = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, sig.lower()):
        raise HTTPException(403, "bad signature")
    try:
        ev = NervixyEvent.model_validate_json(body)
    except ValidationError as e:
        raise HTTPException(400) from e
    nid, event = ev.order_id or "", ev.event or ""
    if event == "webhook.test" or not nid:
        return {"ok": True}
    status = (ev.status or (event.split(".", 1)[1] if event.startswith("order.") else "")).lower()
    async with session_scope() as s:
        o = (await s.execute(select(Order).where(Order.nervixy_order_id == nid))).scalar_one_or_none()
        oid = o.id if o else None
    if oid:
        if status in ("delivered", "rejected"):
            # signed with our secret, so a final status in it is authoritative: apply it right away instead of asking
            # the API again (that call can hit nervixy's rate limit and delay the customer's "Готово!" by minutes).
            # apply_status only moves a "processing" order once, so replays and repeats are harmless.
            background(apply_status(oid, status), f"webhook status {oid}")
        else:
            background(check_status(oid), f"webhook check {oid}")  # anything else is a hint: re-read the status from the API
    return {"ok": True}


# ------------------------------------------------------------------ dev helpers

@router.post("/api/dev/pay/{pid}", dependencies=[Depends(csrf)])
async def api_dev_pay(pid: OrderId, user: User = Depends(require_user)):
    """Dev/mock only: pretend the exact invoice amount arrived on-chain."""
    if not (settings.is_dev and settings.nervixy_mock):
        raise HTTPException(404)
    from .payments.chains.base import Incoming
    from .payments.monitor import ingest
    import secrets as _s
    async with session_scope() as s:
        if pid.startswith("DP"):
            inv = (await s.execute(select(Invoice).where(Invoice.public_id == pid, Invoice.user_id == user.id,
                                                         Invoice.status == "pending"))).scalar_one_or_none()
        else:
            o = await _own_order(s, user, pid)
            inv = (await s.execute(select(Invoice).where(Invoice.order_id == o.id, Invoice.status == "pending")
                                   .order_by(Invoice.id.desc()))).scalars().first()
        if not inv:
            raise HTTPException(400, "нет открытого счёта")
        m = METHODS[inv.method]
        inc = Incoming(method=m.code, txid="dev" + _s.token_hex(30), idx="0", to_address=inv.address,
                       units=int(inv.units), from_address="dev-wallet", comment=inv.comment,
                       block_number=None, block_time=utcnow(), confirmations=0, final=False)
    await ingest([inc])

    async def confirm_later():
        await asyncio.sleep(6)
        async with session_scope() as s:
            t = (await s.execute(select(Transfer).where(Transfer.txid == inc.txid))).scalar_one()
            t.final, t.confirmations = True, m.confirmations
            events.kick_after_commit(s, events.credit(m.chain))  # as the chain's poller does
    background(confirm_later(), "dev payment")
    return {"ok": True}
