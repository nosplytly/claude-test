"""Site login: the site opens t.me/<bot>?start=login_<token> and shows a 2-digit code; the user presses the same
code among three buttons here (app/auth.py has the site half)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from ..auth import code_choices, upsert_tg_user
from ..db import session_scope
from ..models import LoginRequest
from ..tgui import DANGER, SUCCESS, btn, e, esc, kb, site
from ..utils import sha256, token, utcnow
from .callbacks import LoginCancel, LoginCode, Menu
from .common import Screen, show

PREFIX = "login_"


def short_ua(ua: str | None) -> str:
    ua = ua or ""
    os_ = next((n for k, n in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("Windows", "Windows"),
                               ("Mac OS", "macOS"), ("Linux", "Linux")) if k in ua), "")
    br = next((n for k, n in (("YaBrowser", "Яндекс Браузер"), ("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox", "Firefox"),
                              ("Chrome", "Chrome"), ("Safari", "Safari")) if k in ua), "браузер")
    return f"{br}{' · ' + os_ if os_ else ''}"


async def _pending(s, tok: str) -> LoginRequest | None:
    req = await s.get(LoginRequest, tok)
    return req if req and req.status == "pending" and req.expires_at >= utcnow() else None


def _expired() -> Screen:
    return Screen(f"{e('clock')} Ссылка для входа устарела. Нажмите «Войти» на сайте ещё раз.")


async def on_login_link(message: Message, command: CommandObject) -> None:
    tok = command.args.removeprefix(PREFIX)
    async with session_scope() as s:
        req = await _pending(s, tok)
        if req:
            choices, ua, ip = code_choices(req.code), req.user_agent, req.ip
    if not req:
        return await show(message, _expired())
    # all code buttons look the same on purpose: the colour must not hint the right one
    markup = kb([btn(c, cb=LoginCode(token=tok, code=c)) for c in choices],
                [btn("Это не я", cb=LoginCancel(token=tok), style=DANGER, icon="cancel")])
    await show(message, Screen(f"{e('lock')} <b>Вход на сайт</b>\n\nНажмите код, который сейчас показан на сайте.\n\n"
                               f"<i>{esc(short_ua(ua))} · IP {esc(ip)}</i>\n"
                               "Если вы не входили — нажмите «Это не я».", markup))


async def on_code(cb: CallbackQuery, callback_data: LoginCode) -> None:
    tok, link_key = callback_data.token, token(18)
    async with session_scope() as s:
        req = await _pending(s, tok)
        if req and callback_data.code == req.code:
            u = cb.from_user
            user = await upsert_tg_user(s, u.id, u.username, u.first_name, u.last_name)
            req.status, req.user_id, req.link_key_hash = "confirmed", user.id, sha256(link_key)
        elif req:
            req.status = "cancelled"
    if not req:
        await show(cb.message, _expired(), edit=True)
        return await cb.answer()
    if req.status == "cancelled":
        await show(cb.message, Screen(f"{e('fail')} Код не совпал — вход отменён. Попробуйте ещё раз на сайте."), edit=True)
        return await cb.answer()
    await show(cb.message, Screen(
        f"{e('ok')} <b>Вход подтверждён</b>\n\nВозвращайтесь на сайт — вы уже вошли.",
        kb([btn("Вернуться на сайт", url=site(f"/auth/tg?t={tok}&k={link_key}"), style=SUCCESS, icon="rocket")],
           [btn("Меню", cb=Menu(screen="home"), icon="logo")])), edit=True)
    await cb.answer("Готово")


async def on_cancel(cb: CallbackQuery, callback_data: LoginCancel) -> None:
    async with session_scope() as s:
        req = await s.get(LoginRequest, callback_data.token)
        if req and req.status == "pending":
            req.status = "cancelled"
    await show(cb.message, Screen(f"{e('shield')} Вход отменён. Никто не получил доступ к вашему аккаунту."), edit=True)
    await cb.answer()


def router() -> Router:
    r = Router(name="login")
    r.message.register(on_login_link, CommandStart(deep_link=True, magic=F.args.startswith(PREFIX)))
    r.callback_query.register(on_code, LoginCode.filter())
    r.callback_query.register(on_cancel, LoginCancel.filter())
    return r
