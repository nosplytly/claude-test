"""Telegram bot handlers, fed with real aiogram Update objects through a fake Telegram connection.

Run:  .venv\\Scripts\\python.exe tests\\test_bot_offline.py
Nothing is sent to Telegram: every Bot API call is captured by FakeSession.
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal as N
from pathlib import Path

os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "dev", "DATA_DIR": tempfile.mkdtemp(prefix="sh-bot-"), "NERVIXY_MOCK": "true",
                   "BOT_TOKEN": "123456:TEST_TOKEN_FOR_OFFLINE_CHECKS_ONLY", "ADMIN_TG_IDS": "999",
                   "BASE_URL": "https://sh-control-universe.com", "MONITOR_ENABLED": "false"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import DeleteMessage, EditMessageCaption, GetMe, SendMessage, SendPhoto  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, PhotoSize, Update, User  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import bot as B  # noqa: E402
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
        if self.refuse_custom_emoji and "tg-emoji" in (getattr(method, "text", None) or getattr(method, "caption", None) or ""):
            from aiogram.exceptions import TelegramBadRequest
            raise TelegramBadRequest(method=method, message="Bad Request: custom emoji entities are not allowed")
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="SH", username="sh_test_bot")
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type="private"), text=method.text)
        if isinstance(method, SendPhoto):
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id, type="private"), caption=method.caption,
                           photo=[PhotoSize(file_id="banner-file-id", file_unique_id="b", width=1280, height=640)])
        return True

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    async def close(self):
        pass


session = FakeSession()
bot = Bot("123456:TEST_TOKEN_FOR_OFFLINE_CHECKS_ONLY", session=session, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
dp.include_router(B.router)
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


async def press(uid, data, username="ivan", *, photo=False):
    """Press a button under a text message, or (photo=True) under a banner screen."""
    global _uid
    _uid += 1
    msg = Message(message_id=500 + _uid, date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"),
                  **({"caption": "…", "photo": [PhotoSize(file_id="p", file_unique_id="p", width=1, height=1)]}
                     if photo else {"text": "…"}))
    cb = CallbackQuery(id=str(_uid), from_user=tg_user(uid, username), chat_instance="ci", data=data, message=msg)
    before = len(session.calls)
    await dp.feed_update(bot, Update(update_id=_uid, callback_query=cb))
    return session.calls[before:]


def texts(calls):
    return " || ".join(getattr(c, "text", None) or getattr(c, "caption", None) or "" for c in calls)


def buttons(calls):
    out = []
    for c in calls:
        kb = getattr(c, "reply_markup", None)
        for row in (kb.inline_keyboard if kb else []):
            out += [(b.text, b.callback_data or b.url or (b.web_app.url if b.web_app else None)) for b in row]  # site links open as the Mini App
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

    print("5c. меню: картинка, эмодзи из пака, плашки")
    from app import tgui
    B._banner_id = None
    out = await send_text(555, "/start")
    home = next(c for c in out if isinstance(c, SendPhoto))
    check(not isinstance(home.photo, str) and "SupplierHub" in home.caption,
          "меню — картинка SupplierHub, текст в подписи (первый раз картинка загружается файлом)")
    buttons_ = [b for row in home.reply_markup.inline_keyboard for b in row]

    def style_of(word):
        return next((b.style for b in buttons_ if word in b.text), "missing")

    def icon_of(word):
        return next((b.icon_custom_emoji_id for b in buttons_ if word in b.text), "missing")
    check(style_of("Пополнить Steam") == "primary" and style_of("Баланс") == "success",
          f"цвета кнопок: {[(b.text, b.style) for b in buttons_]}")
    check(icon_of("Пополнить Steam") == "5319188885811539232" and icon_of("Баланс") == "5319220067274104632",
          "на кнопках эмодзи из твоего пака: «Пополнить Steam» — Launch, «Баланс» — монеты")
    check(any(b.text == "🧾 Мои заказы" and not b.icon_custom_emoji_id for b in buttons_),
          "где в паке нет подходящего эмодзи — обычный перед текстом")
    check('<tg-emoji emoji-id="5319142830877223516">' in home.caption and '<tg-emoji emoji-id="5318774481597017341">' in home.caption,
          "в подписи логотип и «привет» из твоего пака")
    check("<blockquote" not in home.caption and "\n\n" in home.caption and "Скидка" in home.caption.split("\n\n")[-1],
          "преимущества отдельным блоком, без цитаты (её цвет Telegram берёт от бота — у нас красный)")
    check("Привет, Иван" in home.caption, "меню здоровается по имени")
    bal_buttons = [b.text for b in buttons_ if "Баланс" in b.text and "$" in b.text]
    check(len(bal_buttons) == 1 and "Баланс" not in home.caption and "$" not in home.caption,
          f"баланс показан один раз — на кнопке ({bal_buttons}), в тексте не повторяется")
    check(not any("Админ-панель" in b.text for b in buttons_), "у обычного клиента нет кнопки админки")
    out = await send_text(555, "/start")
    check(next(c for c in out if isinstance(c, SendPhoto)).photo == "banner-file-id",
          "дальше картинка уходит по file_id — без повторной загрузки")
    out = await press(555, "m:bal", photo=True)
    check(any(isinstance(c, EditMessageCaption) and "Баланс: $" in c.caption and "<blockquote" not in c.caption for c in out)
          and not any(isinstance(c, (DeleteMessage, SendPhoto)) for c in out),
          "«Баланс» под картинкой → та же картинка, меняется подпись (движения на плашке)")
    out = await press(555, "m:orders", photo=True)
    check(any(isinstance(c, EditMessageCaption) and "Мои заказы" in c.caption for c in out),
          "кнопка «Мои заказы» → список заказов")
    out = await press(555, "m:home", photo=True)
    check(any(isinstance(c, EditMessageCaption) and "SupplierHub" in c.caption for c in out), "«Назад» → главное меню")
    out = await press(555, "m:home")
    check(any(isinstance(c, DeleteMessage) for c in out) and any(isinstance(c, SendPhoto) for c in out),
          "меню из текстового сообщения («Вход подтверждён» → «Меню») приходит новой картинкой вместо старого")
    out = await press(555, "a:home")
    check(any(getattr(c, "show_alert", False) and "Нет доступа" in (c.text or "") for c in out),
          "чужой нажал админ-кнопку → «Нет доступа»")
    out = await send_text(999, "/start", "boss")
    admin_home = next(c for c in out if isinstance(c, SendPhoto))
    check(any("Админ-панель" in b.text for row in admin_home.reply_markup.inline_keyboard for b in row),
          "у админа в меню есть кнопка «Админ-панель»")
    out = await press(999, "a:home", "boss", photo=True)
    check(any(isinstance(c, DeleteMessage) for c in out)
          and any(isinstance(c, SendMessage) and "Админ-панель" in c.text for c in out),
          "админка из-под картинки → обычным сообщением (там длинные списки, подпись к фото до 1024 символов)")
    for key, word in (("a:home", "Админ-панель"), ("a:stats", "маржа"), ("a:nx", "Nervixy"), ("a:work", "очередь"),
                      ("a:last", "Последние заказы"), ("a:help", "/bal")):
        out = await press(999, key, "boss")
        check(word.lower() in texts(out).lower() and "<blockquote" not in texts(out), f"админ-кнопка {key} → «{word}»")
    out = await press(999, "a:stats", "boss")
    check(texts(out).count("\n\n") >= 4, "статистика: четыре периода — четыре отдельных блока")
    from app.config import settings as _st
    from app.tgui import panel as _panel
    _st.tg_panels = True
    check(_panel("x").startswith("<blockquote>"), "TG_PANELS=true возвращает цитатные плашки (для бота с подходящим цветом)")
    _st.tg_panels = False

    print("5f. пак эмодзи")
    out = await send_text(999, "/emoji", "boss")
    check("Пак не загружен" in texts(out), "/emoji без загруженного пака → объясняет, почему")
    got = tgui.use_pack([("5318848256250260091", "✅"), ("7000000000000000001", "🧾"), ("7000000000000000002", "🦄")])
    check(sorted(got) == ["claim", "orders"] and tgui._ids["ok"] == "5318848256250260091",
          "новое эмодзи в паке с базовым 🧾 само встаёт на «Мои заказы», выбранные вручную не трогаются")
    out = await send_text(999, "/emoji", "boss")
    check("3 эмодзи" in texts(out) and "7000000000000000001</code> · claim, orders" in texts(out)
          and "не используется" in texts(out), "/emoji показывает пак: ID и где какое эмодзи стоит")
    out = await send_text(555, "/emoji")
    check(not out, "не-админ: /emoji молча игнорируется")
    tgui.load()
    tgui.pack = []

    print("5g. если Telegram не примет кастомные эмодзи в меню")
    session.refuse_custom_emoji = True
    out = await send_text(555, "/start")
    photos = [c for c in out if isinstance(c, SendPhoto)]
    retry_buttons = [b for row in photos[-1].reply_markup.inline_keyboard for b in row]
    check(len(photos) == 2 and "tg-emoji" not in photos[1].caption
          and any(b.text == "🚀 Пополнить Steam" and not b.icon_custom_emoji_id for b in retry_buttons),
          "меню уходит со стандартными эмодзи и в тексте, и на кнопках")
    session.refuse_custom_emoji = False
    tgui._enabled = True

    print("5e. баны IP из бота")
    from app import fail2ban as f2b
    out = await send_text(555, "/ban 192.0.2.50 3")
    check(not out and not f2b.banned_for("192.0.2.50"), "не-админ: /ban молча игнорируется")
    out = await send_text(999, "/ban 192.0.2.50 3", "boss")
    check("заблокирован на 3 ч" in texts(out) and f2b.banned_for("192.0.2.50") > 3 * 3600 - 60, "/ban IP 3 → бан на 3 часа")
    out = await send_text(999, "/ban 127.0.0.1", "boss")
    check("не внешний" in texts(out), "/ban localhost → отказ")
    out = await send_text(999, "/ban 192.0.2.51 abc", "boss")
    check("Часы числом" in texts(out) and not f2b.banned_for("192.0.2.51"), "/ban с мусором вместо часов → подсказка")
    out = await press(999, "a:bans", "boss")
    check("192.0.2.50" in texts(out) and "вручную" in texts(out), "экран «Баны IP» показывает бан и причину")
    out = await press(999, "a:home", "boss")
    check("Заблокировано IP: 1" in texts(out), "в админ-панели видно число банов")
    out = await send_text(999, "/unban 192.0.2.50", "boss")
    check("Бан снят" in texts(out) and not f2b.banned_for("192.0.2.50"), "/unban снимает бан")
    out = await send_text(999, "/unban 192.0.2.50", "boss")
    check("не был заблокирован" in texts(out), "повторный /unban → «не был заблокирован»")

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

    print("7. бот не повторяет одно и то же")
    from aiogram.methods import EditMessageText

    from app import orders as O
    from app.models import Order
    from app.money import to_micro
    async with session_scope() as s:
        o = Order(public_id="SHCARDTEST", user_id=u.id, steam_login="card_test", currency="RUB", amount=N("100"),
                  fx_rate=N("78.5"), nominal_micro=to_micro(N("1.27")), price_micro=1_230_000, cost_micro=1_210_000,
                  client_discount=N("3.75"), supplier_discount=N("4.5"), status="processing", method="balance",
                  nervixy_order_id="nx-card", charged_micro=1_230_000, submitted_at=utcnow() - timedelta(seconds=42))
        s.add(o)
        await s.flush()
        oid = o.id
        card = O.new_order_card(o, await s.get(DbUser, u.id))
    await O.remember_card(oid, await notify.notify_admins(card, O.admin_kb()))
    check("Пополняется у nervixy" in session.calls[-1].text, "админу пришла одна карточка заказа «Пополняется у nervixy…»")
    before = len(session.calls)
    await O.apply_status(oid, "delivered")
    new = session.calls[before:]
    to_admin = [c for c in new if getattr(c, "chat_id", None) == 999]
    check(len(to_admin) == 1 and isinstance(to_admin[0], EditMessageText) and "Выполнено" in to_admin[0].text
          and "Маржа" in to_admin[0].text,
          "заказ выполнен → та же карточка обновилась на «✅ Выполнено за N с», второго сообщения админу нет")
    check(any(isinstance(c, SendMessage) and c.chat_id == 555 and "Готово" in c.text for c in new),
          "клиенту пришло «Готово!»")
    for _ in range(2):
        before = len(session.calls)
        await notify.notify_admins("Баланс nervixy низкий", key="nx-low-test", every=3600)
        notify._throttle.clear()  # as if the service had been restarted
    sent = [c for c in session.calls if isinstance(c, SendMessage) and c.text == "Баланс nervixy низкий"]
    check(len(sent) == 1, "алерт с интервалом после перезапуска сервиса не повторяется (время хранится в базе)")

    print("8. промокоды в боте")
    out = await send_text(555, "/promo new HACK $100")
    from app.models import Promo
    async with session_scope() as s:
        hack = (await s.execute(select(Promo).where(Promo.code == "HACK"))).scalar_one_or_none()
    check(hack is None and "Такого промокода нет" in texts(out), "клиент не может создать промокод (для него это ввод кода)")
    out = await send_text(999, "/promo new BOTGIFT $3 5 7d", "boss")
    check("BOTGIFT" in texts(out) and "+$3 на баланс" in texts(out) and "5 раз" in texts(out),
          "админ: /promo new BOTGIFT $3 5 7d → создан")
    out = await send_text(999, "/promo new BOTGIFT $3", "boss")
    check("уже есть" in texts(out), "повторное создание того же кода → «уже есть»")
    out = await send_text(999, "/promo new BAD% 1%", "boss")
    check("Код:" in texts(out), "кривой код → подсказка формата")
    out = await send_text(555, "/promo botgift")
    check("+$3 на баланс" in texts(out) and "На балансе" in texts(out), "клиент: /promo botgift → $3 на баланс")
    out = await send_text(555, "/promo BOTGIFT")
    check("уже использовали" in texts(out), "второй раз → «уже использовали»")
    out = await send_text(999, "/promo", "boss")
    check("BOTGIFT" in texts(out) and "1/5" in texts(out), "админ: /promo → список, BOTGIFT использован 1 из 5")
    out = await send_text(999, "/promo off BOTGIFT", "boss")
    check("выключен" in texts(out), "админ: /promo off → выключен")

    print(f"\nALL GOOD: {OK} checks passed")


if __name__ == "__main__":
    asyncio.run(main())
