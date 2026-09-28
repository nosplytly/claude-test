"""Telegram bot handlers, fed with real aiogram Update objects through a fake Telegram connection.

Run:  .venv\\Scripts\\python.exe tests\\test_bot_offline.py
Nothing is sent to Telegram: every Bot API call is captured by FakeSession.
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.update({"ENV": "dev", "DATA_DIR": tempfile.mkdtemp(prefix="sh-bot-"), "NERVIXY_MOCK": "true",
                   "BOT_TOKEN": "123456:TEST_TOKEN_FOR_OFFLINE_CHECKS_ONLY", "ADMIN_TG_IDS": "999",
                   "BASE_URL": "https://sh-control-universe.com", "MONITOR_ENABLED": "false"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import GetMe, SendMessage  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.bot.runner import build_dispatcher  # noqa: E402
from app import notify  # noqa: E402
from app.auth import start_login  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import LoginRequest, User as DbUser  # noqa: E402
from app.utils import utcnow  # noqa: E402

OK = 0


def check(cond, msg):
    global OK
    if not cond:
        raise AssertionError(msg)
    OK += 1
    print("  ✓", msg)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.refuse_custom_emoji = False

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if self.refuse_custom_emoji and "tg-emoji" in (getattr(method, "text", "") or ""):
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=method, message="Bad Request: custom emoji entities are not allowed")
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="SH", username="sh_test_bot")
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type="private"), text=method.text)
        return True

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    async def close(self):
        pass


session = FakeSession()
bot = Bot("123456:TEST_TOKEN_FOR_OFFLINE_CHECKS_ONLY", session=session, default=DefaultBotProperties(parse_mode="HTML"))
dp = build_dispatcher()
_uid = 0


def tg_user(uid, username="ivan"):
    return User(id=uid, is_bot=False, first_name="Иван", username=username)


async def send_text(uid, text, username="ivan"):
    global _uid
    _uid += 1
    cmd_len = len(text.split()[0])
    msg = Message(message_id=_uid, date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"),
                  from_user=tg_user(uid, username), text=text,
                  entities=[MessageEntity(type="bot_command", offset=0, length=cmd_len)] if text.startswith("/") else None)
    before = len(session.calls)
    await dp.feed_update(bot, Update(update_id=_uid, message=msg))
    return session.calls[before:]


async def press(uid, data, username="ivan"):
    global _uid
    _uid += 1
    msg = Message(message_id=500 + _uid, date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"), text="…")
    cb = CallbackQuery(id=str(_uid), from_user=tg_user(uid, username), chat_instance="ci", data=data, message=msg)
    before = len(session.calls)
    await dp.feed_update(bot, Update(update_id=_uid, callback_query=cb))
    return session.calls[before:]


def texts(calls):
    return " || ".join(getattr(c, "text", "") or "" for c in calls)


def buttons(calls):
    out = []
    for c in calls:
        kb = getattr(c, "reply_markup", None)
        for row in (kb.inline_keyboard if kb else []):
            out += [(b.text, b.callback_data or b.url) for b in row]
    return out


async def main():
    await init_db()
    notify.set_bot(bot)
    from app.main import app

    print("1. вход по коду")
    async with session_scope() as s:
        req, secret = await start_login(s, "1.2.3.4", "Mozilla/5.0 (Windows NT 10.0) Chrome/130")
        tok, code = req.id, req.code
    out = await send_text(555, f"/start login_{tok}")
    btns = buttons(out)
    codes = [t for t, d in btns if d and d.startswith("lc:")]
    check(len(codes) == 3 and code in codes, f"бот прислал 3 кода на выбор ({', '.join(codes)}), правильный {code} среди них")
    check(any("Это не я" in t for t, _ in btns), "есть кнопка «Это не я»")
    check("Chrome · Windows" in texts(out) and "1.2.3.4" in texts(out), "в сообщении видно устройство и IP входа")

    out = await press(555, f"lc:{tok}:{code}")
    async with session_scope() as s:
        req = await s.get(LoginRequest, tok)
        u = (await s.execute(select(DbUser).where(DbUser.tg_id == 555))).scalar_one()
    check(req.status == "confirmed" and u.username == "ivan", "правильный код → вход подтверждён, пользователь @ivan создан")
    link = next((d for t, d in buttons(out) if d and "/auth/tg?" in d), None)
    check("Вход подтверждён" in texts(out) and link, "бот ответил «Вход подтверждён» и дал кнопку «Вернуться на сайт»")

    print("2. сайт подхватывает вход")
    t = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=t, base_url="https://t") as c:
        c.cookies.set("sh_login", f"{tok}.{secret}")
        r = await c.get("/api/auth/poll")
        check(r.json().get("status") == "ok" and "sh_session" in r.cookies, "вкладка, где нажали «Войти», залогинилась сама")
        me = await c.get("/api/me")
        check(me.json()["user"]["username"] == "ivan", "сессия рабочая: /api/me → @ivan")
    async with httpx.AsyncClient(transport=t, base_url="https://t") as c2:
        r = await c2.get(link.replace("https://sh-control-universe.com", ""), follow_redirects=False)
        check(r.status_code == 303 and "sh_session" in r.cookies, "кнопка из бота логинит и другой браузер (телефон)")
        r2 = await c2.get(link.replace("https://sh-control-universe.com", ""), follow_redirects=False)
        check("sh_session" not in r2.cookies, "повторно та же ссылка не срабатывает (одноразовая)")

    print("3. защита входа")
    async with session_scope() as s:
        req2, _ = await start_login(s, "5.6.7.8", "x")
        tok2, code2 = req2.id, req2.code
    await send_text(556, f"/start login_{tok2}", "petr")
    wrong = next(c for c in ("10", "11", "12") if c != code2)
    out = await press(556, f"lc:{tok2}:{wrong}", "petr")
    async with session_scope() as s:
        check((await s.get(LoginRequest, tok2)).status == "cancelled", "неверный код → вход отменён")
    check("не совпал" in texts(out), "бот объяснил: «Код не совпал»")
    async with session_scope() as s:
        req3, _ = await start_login(s, "5.6.7.8", "x")
        tok3 = req3.id
    out = await press(556, f"lx:{tok3}", "petr")
    async with session_scope() as s:
        check((await s.get(LoginRequest, tok3)).status == "cancelled", "«Это не я» → вход отменён")
    async with session_scope() as s:
        req4, _ = await start_login(s, "5.6.7.8", "x")
        req4.expires_at = utcnow() - timedelta(minutes=1)
        tok4 = req4.id
    out = await send_text(556, f"/start login_{tok4}", "petr")
    check("устарела" in texts(out), "просроченная ссылка → «устарела»")

    print("4. команды клиента")
    out = await send_text(555, "/start")
    check("Привет" in texts(out) and any("sh-control-universe.com" in (d or "") for _, d in buttons(out)),
          "/start без параметра → приветствие и кнопка «Пополнить Steam» на сайт")
    out = await send_text(555, "/balance")
    check("Баланс" in texts(out), "/balance → «" + texts(out)[:40] + "»")
    out = await send_text(555, "/orders")
    check("Заказов пока нет" in texts(out), "/orders → «" + texts(out)[:40] + "»")
    out = await send_text(777, "/balance", "stranger")
    check("не входили" in texts(out), "незнакомый пользователь → «Вы ещё не входили на сайт»")

    print("5. админ-команды")
    out = await send_text(555, "/stats")
    check(not out, "не-админ: /stats молча игнорируется")
    out = await send_text(999, "/stats", "boss")
    check("Админ-панель" in texts(out) and "Nervixy" in texts(out), "/stats у админа → админ-панель со сводкой и nervixy")
    out = await send_text(999, "/bal @ivan +5 бонус", "boss")
    async with session_scope() as s:
        u = (await s.execute(select(DbUser).where(DbUser.tg_id == 555))).scalar_one()
    check(u.balance_micro == 5_000_000, "/bal @ivan +5 → баланс $5.00")
    check(any(isinstance(c, SendMessage) and c.chat_id == 555 and "Баланс изменён" in c.text for c in out),
          "клиенту пришло уведомление об изменении баланса")
    out = await send_text(999, "/bal @ivan -50", "boss")
    check("Недостаточно" in texts(out), "/bal в минус больше баланса → отказ")
    out = await send_text(999, "/disc @ivan 4.1", "boss")
    async with session_scope() as s:
        u = (await s.execute(select(DbUser).where(DbUser.tg_id == 555))).scalar_one()
    check(str(u.discount) == "4.1", "/disc @ivan 4.1 → персональная скидка 4.1%")
    out = await send_text(999, "/disc @ivan 7", "boss")
    check("иначе работаем в минус" in texts(out), "/disc выше скидки поставщика → отказ")
    await send_text(999, "/disc @ivan reset", "boss")
    async with session_scope() as s:
        u = (await s.execute(select(DbUser).where(DbUser.tg_id == 555))).scalar_one()
    check(u.discount is None, "/disc @ivan reset → общая скидка")

    print("5b. кнопки в алертах админа")
    from app.models import Order
    from decimal import Decimal
    async with session_scope() as s:
        o = Order(public_id="SHTESTBTN1", user_id=u.id, steam_login="x", currency="RUB", amount=Decimal(100),
                  fx_rate=Decimal(80), nominal_micro=1, price_micro=1, cost_micro=1, client_discount=Decimal(3),
                  supplier_discount=Decimal(4), status="uncertain")
        s.add(o)
        await s.flush()
        oid = o.id
    await press(555, f"ad:retry:{oid}")
    async with session_scope() as s:
        check((await s.get(Order, oid)).status == "uncertain", "чужой нажал «Отправить повторно» → ничего не произошло")
    await press(999, f"ad:retry:{oid}", "boss")
    async with session_scope() as s:
        check((await s.get(Order, oid)).status == "queued", "админ нажал «Отправить повторно» → заказ снова в очереди")

    print("5c. меню на кнопках")
    from app import tgui
    out = await send_text(555, "/start")
    home = next(c for c in out if isinstance(c, SendMessage))
    styles = {b.text: b.style for row in home.reply_markup.inline_keyboard for b in row}
    icons = {b.text: b.icon_custom_emoji_id for row in home.reply_markup.inline_keyboard for b in row}
    check(styles.get("Пополнить Steam") == "primary" and styles.get("Баланс") == "success",
          f"цвета кнопок: {styles}")
    check(icons.get("Пополнить Steam") == "5319188885811539232", "на кнопке «Пополнить Steam» твоя анимация Launch")
    check('<tg-emoji emoji-id="5319142830877223516">' in home.text, "в тексте меню твой логотип (Assemble)")
    check("Привет, Иван" in home.text, "меню здоровается по имени")
    check("Админ-панель" not in str(icons), "у обычного клиента нет кнопки админки")
    out = await press(555, "m:bal")
    check(any("Баланс: $" in (getattr(c, "text", "") or "") for c in out), "кнопка «Баланс» → экран баланса")
    out = await press(555, "m:orders")
    check(any("Мои заказы" in (getattr(c, "text", "") or "") for c in out), "кнопка «Мои заказы» → список заказов")
    out = await press(555, "m:home")
    check(any("SupplierHub" in (getattr(c, "text", "") or "") for c in out), "«Назад» → главное меню")
    out = await press(555, "a:home")
    check(any(getattr(c, "show_alert", False) and "Нет доступа" in (c.text or "") for c in out),
          "чужой нажал админ-кнопку → «Нет доступа»")
    out = await send_text(999, "/start", "boss")
    admin_home = next(c for c in out if isinstance(c, SendMessage))
    check(any("Админ-панель" in b.text for row in admin_home.reply_markup.inline_keyboard for b in row),
          "у админа в меню есть кнопка «Админ-панель»")
    for key, word in (("a:home", "Админ-панель"), ("a:stats", "маржа"), ("a:nx", "Nervixy"), ("a:work", "очередь"),
                      ("a:last", "Последние заказы"), ("a:help", "/bal")):
        out = await press(999, key, "boss")
        check(any(word.lower() in (getattr(c, "text", "") or "").lower() for c in out), f"админ-кнопка {key} → «{word}»")

    print("5d. если Telegram не примет кастомные эмодзи")
    session.refuse_custom_emoji = True
    before = len(session.calls)
    await notify.notify_admins(f"{tgui.e('ok')} проверка отката")
    sent = [c for c in session.calls[before:] if isinstance(c, SendMessage)]
    check(len(sent) == 2 and "tg-emoji" in sent[0].text and "tg-emoji" not in sent[1].text and "✅" in sent[1].text,
          "первая попытка отклонена → сообщение ушло со стандартными эмодзи")
    session.refuse_custom_emoji = False
    tgui._enabled = True

    print("6. уведомления")
    await notify.notify_user(u.id, "тест уведомления")
    check(any(isinstance(c, SendMessage) and c.chat_id == 555 and c.text == "тест уведомления" for c in session.calls[-2:]),
          "notify_user доходит до клиента")
    await notify.notify_admins("тест алерта", [[("Кнопка", "cb:x:1"), ("Сайт", "url:https://sh-control-universe.com")]])
    last = session.calls[-1]
    check(isinstance(last, SendMessage) and last.chat_id == 999 and len(last.reply_markup.inline_keyboard[0]) == 2,
          "алерт админу приходит с кнопками")

    print("7. устройство бота")
    from app.bot import customer
    from app.bot.callbacks import ClaimAction, LoginCode, Menu, OrderAction
    check([Menu(screen="orders").pack(), OrderAction(action="retry", order_id=42).pack(),
           ClaimAction(action="ok", claim_id=7).pack(), LoginCode(token="abc", code="10").pack()]
          == ["m:orders", "ad:retry:42", "cl:ok:7", "lc:abc:10"],
          "формат кнопок прежний: кнопки в уже отправленных сообщениях продолжают работать")
    try:
        build_dispatcher(), build_dispatcher()
        rebuilt = True
    except RuntimeError:  # "Router is already attached"
        rebuilt = False
    check(rebuilt, "диспетчер собирается повторно (после падения бот перезапускается, а не висит)")
    out = await press(555, "zz:old-button")
    check(len(out) == 1 and out[0].__class__.__name__ == "AnswerCallbackQuery",
          "неизвестная/устаревшая кнопка → часики гаснут, без ошибки")
    broken = customer.SCREENS["bal"]

    async def boom(tg_id):
        raise RuntimeError("test failure")

    customer.SCREENS["bal"] = boom
    try:
        out = await press(555, "m:bal")
    finally:
        customer.SCREENS["bal"] = broken
    check(any(getattr(c, "show_alert", False) and "пошло не так" in (c.text or "") for c in out),
          "ошибка в обработчике → клиенту «Что-то пошло не так», кнопка не зависает")

    print(f"\nALL GOOD: {OK} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
