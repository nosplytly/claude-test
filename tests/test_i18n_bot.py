"""The bot in English for customers with an English Telegram (admins stay Russian), and the plugin waiting list.

Run:  .venv\\Scripts\\python.exe tests\\test_i18n_bot.py
Real handlers and the real money flow (mock supplier, simulated transfers) through a fake Telegram connection:
every message a customer would get is checked for leftover Russian. Nothing leaves the machine.
"""
import asyncio
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

TOKEN = "123456:TEST_TOKEN_FOR_I18N_CHECKS_ONLY"
ADDR_EVM = "0x000000000000000000000000000000000000dEaD"
os.environ.update({"SH_ENV_FILE": os.devnull, "ENV": "dev", "DATA_DIR": tempfile.mkdtemp(prefix="sh-i18n-"),
                   "NERVIXY_MOCK": "true", "BOT_TOKEN": TOKEN, "ADMIN_TG_IDS": "999", "MONITOR_ENABLED": "false",
                   "BASE_URL": "https://sh.test", "SUPPORT_URL": "https://t.me/sh_support",
                   "WALLET_USDT_TRC20": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb", "WALLET_USDT_BEP20": ADDR_EVM,
                   "WALLET_USDT_ERC20": ADDR_EVM})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage, SendPhoto  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, PhotoSize, Update, User  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import bot as B  # noqa: E402
from app import i18n, notify  # noqa: E402
from app import orders as O  # noqa: E402
from app.auth import create_session, start_login  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.deposits import create_deposit  # noqa: E402
from app.models import Claim, Invoice, LoginRequest, Order, Transfer, User as DbUser, Waitlist  # noqa: E402
from app.nervixy import nervixy  # noqa: E402
from app.payments import monitor as M  # noqa: E402
from app.payments.chains.base import Incoming  # noqa: E402
from app.payments.methods import METHODS  # noqa: E402
from app.prices import price_feed  # noqa: E402
from app.utils import utcnow  # noqa: E402

OK = 0
CYR = re.compile(r"[А-Яа-яЁё]+")
EN_ID, RU_ID, ADMIN = 777, 555, 999


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

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        now = datetime.now(timezone.utc)
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="SH", username="sh_test_bot")
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls), date=now, chat=Chat(id=method.chat_id, type="private"), text=method.text)
        if isinstance(method, SendPhoto):
            return Message(message_id=len(self.calls), date=now, chat=Chat(id=method.chat_id, type="private"),
                           caption=method.caption, photo=[PhotoSize(file_id="banner", file_unique_id="b", width=1, height=1)])
        return True

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    async def close(self):
        pass


session = FakeSession()
session.middleware(i18n.outgoing)  # the same wiring as run_bot()
bot = Bot(TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
dp.update.outer_middleware(i18n.TrackLanguage())
dp.include_router(B.router)
_n = 0
LANG_CODE = {EN_ID: "en", RU_ID: "ru"}


def tg_user(uid):
    return User(id=uid, is_bot=False, first_name="John" if uid == EN_ID else "Ivan", username=f"user{uid}",
                language_code=LANG_CODE.get(uid))


async def send_text(uid, text):
    global _n
    _n += 1
    cmd = len(text.split()[0])
    msg = Message(message_id=_n, date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"), from_user=tg_user(uid),
                  text=text, entities=[MessageEntity(type="bot_command", offset=0, length=cmd)] if text.startswith("/") else None)
    before = len(session.calls)
    await dp.feed_update(bot, Update(update_id=_n, message=msg))
    return session.calls[before:]


async def press(uid, data, *, photo=False):
    global _n
    _n += 1
    msg = Message(message_id=500 + _n, date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"),
                  **({"caption": "…", "photo": [PhotoSize(file_id="p", file_unique_id="p", width=1, height=1)]}
                     if photo else {"text": "…"}))
    cb = CallbackQuery(id=str(_n), from_user=tg_user(uid), chat_instance="ci", data=data, message=msg)
    before = len(session.calls)
    await dp.feed_update(bot, Update(update_id=_n, callback_query=cb))
    return session.calls[before:]


async def side_jobs():
    """Money workers leave Telegram to others: the customer's news goes through the outbox, the admin's card is
    posted/edited in the background. Let both finish, as the running service would a moment later."""
    from app import outbox, utils
    while utils._background:
        await asyncio.gather(*list(utils._background), return_exceptions=True)
    await outbox.flush()


def visible(calls):
    """Everything a person reads in these calls: texts, captions, pop-ups, button labels."""
    out = []
    for c in calls:
        for f in ("text", "caption"):
            v = getattr(c, f, None)
            if isinstance(v, str):
                out.append(re.sub(r"<[^>]+>", "", v))
        kb = getattr(c, "reply_markup", None)
        for row in getattr(kb, "inline_keyboard", None) or []:
            out += [b.text for b in row]
    return " | ".join(out)


def links(calls):
    out = []
    for c in calls:
        kb = getattr(c, "reply_markup", None)
        for row in getattr(kb, "inline_keyboard", None) or []:
            out += [b.url or (b.web_app.url if b.web_app else None) for b in row]
    return [x for x in out if x]


async def to_chat(chat_id, since):
    await side_jobs()
    return [c for c in session.calls[since:] if getattr(c, "chat_id", None) == chat_id]


def english(calls, what):
    text = visible(calls)
    left = CYR.findall(text)
    check(text and not left, f"{what}: по-английски{'' if not left else ' — остался русский: ' + ', '.join(left)}")
    return text


_tx = 0


async def pay(method: str, units: int, *, when=None) -> str:
    global _tx
    _tx += 1
    txid = f"dev{_tx:060d}"
    m = METHODS[method]
    await M.ingest([Incoming(method=method, txid=txid, idx="0", to_address=m.address, units=units, from_address="sender",
                             block_time=when or utcnow(), confirmations=m.confirmations, final=True)])
    await M.settle({})
    return txid


async def new_order(uid, method, login="gooduser", amount="1000"):
    async with session_scope() as s:
        u = await s.get(DbUser, uid)
        return await O.create_order(s, u, steam_login=login, amount=Decimal(amount), currency="RUB", method_code=method)


async def invoice_of(order_id):
    async with session_scope() as s:
        return (await s.execute(select(Invoice).where(Invoice.order_id == order_id).order_by(Invoice.id.desc()))).scalars().first()


async def drive(order_id):
    for _ in range(10):
        await O.process_once()
        async with session_scope() as s:
            o = await s.get(Order, order_id)
        if o.status == "processing":
            await O.check_status(order_id)
        async with session_scope() as s:
            o = await s.get(Order, order_id)
        if o.status in ("delivered", "rejected"):
            return o
    return o


async def main():
    await init_db()
    notify.set_bot(bot)
    nervixy.client.delay = 0
    price_feed.prices = {"BTC": Decimal("80000"), "ETH": Decimal("3000"), "TRX": Decimal("0.3")}
    price_feed.updated_at = time.time()

    print("1. язык по Telegram")
    check(i18n.lang_of("en") == "en" and i18n.lang_of("de") == "en" and i18n.lang_of("pt-br") == "en",
          "английский, немецкий, португальский Telegram → английский")
    check(i18n.lang_of("ru") == "ru" and i18n.lang_of("uk") == "ru" and i18n.lang_of("kk") == "ru" and i18n.lang_of(None) == "ru",
          "русский, украинский, казахский и «не сказал» → русский")
    out = await send_text(EN_ID, "/start")
    english(out, "главное меню для нового клиента с английским Telegram")
    check(all("lang=en" in u for u in links(out) if u.startswith("https://sh.test")),
          "кнопки на сайт открывают его на английском (?lang=en)")
    out = await send_text(RU_ID, "/start")
    check("Привет" in visible(out) or "Пополнить Steam" in visible(out), "клиенту с русским Telegram — по-русски")
    check(all("lang=ru" in u for u in links(out) if u.startswith("https://sh.test")), "…и сайт тоже откроется по-русски")
    out = await send_text(ADMIN, "/start")
    check("Админ-панель" in visible(out), "админу — по-русски, даже с английским Telegram")

    print("2. вход на сайт")
    async with session_scope() as s:
        req, _ = await start_login(s, "1.2.3.4", "Mozilla/5.0 (Windows NT 10.0) YaBrowser/24 Chrome/130")
        tok, code = req.id, req.code
    out = await send_text(EN_ID, f"/start login_{tok}")
    english(out, "подтверждение входа (браузер, IP, «Это не я»)")
    out = await press(EN_ID, f"lc:{tok}:{code}")
    txt = english(out, "«вход подтверждён» и всплывашка «Готово»")
    check("Sign-in confirmed" in txt and any(isinstance(c, AnswerCallbackQuery) and c.text == "Done" for c in out),
          "…с правильными словами")
    check(any("/auth/tg?" in u and "lang=" not in u for u in links(out)), "одноразовая ссылка входа не тронута")
    async with session_scope() as s:
        en = (await s.execute(select(DbUser).where(DbUser.tg_id == EN_ID))).scalar_one()
        req2, _ = await start_login(s, "5.6.7.8", "Mozilla/5.0 (iPhone) Safari/605")
        tok2 = req2.id
    check(en.tg_lang == "en" and en.lang is None, "язык Telegram записан в профиль клиента (для уведомлений)")
    await send_text(EN_ID, f"/start login_{tok2}")
    english(await press(EN_ID, f"lx:{tok2}"), "«вход отменён»")

    print("3. уведомления о деньгах и заказах")
    async with session_scope() as s:
        ru = DbUser(tg_id=RU_ID, username="user555", first_name="Ivan", tg_lang="ru")
        s.add(ru)
    i18n._chats.clear()  # like after a restart: languages come from the database
    mark = len(session.calls)
    o = await new_order(en.id, "usdt_trc20")
    await pay("usdt_trc20", int((await invoice_of(o.id)).units))
    english(await to_chat(EN_ID, mark), "«оплата получена, пополняем Steam»")
    mark = len(session.calls)
    check((await drive(o.id)).status == "delivered", "заказ выполнен")
    txt = english(await to_chat(EN_ID, mark), "«готово, Steam пополнен»")
    check("topped up with" in txt and "Enjoy" in txt, "…текст осмысленный: " + txt[:90])
    check(CYR.search(visible(await to_chat(ADMIN, mark)) or "Р"), "админу про тот же заказ — по-русски")

    mark = len(session.calls)
    r = await new_order(en.id, "usdt_trc20", login="rejectme")
    await pay("usdt_trc20", int((await invoice_of(r.id)).units))
    check((await drive(r.id)).status == "rejected", "поставщик отклонил заказ")
    english(await to_chat(EN_ID, mark), "«поставщик отклонил, деньги на балансе»")

    mark = len(session.calls)
    p = await new_order(en.id, "usdt_trc20", amount="5000")  # more than the balance: an invoice is needed
    pi = await invoice_of(p.id)
    await pay("usdt_trc20", int(Decimal(pi.units) * Decimal("0.97")))
    english(await to_chat(EN_ID, mark), "«не хватает для заказа, доплатите»")

    mark = len(session.calls)
    lo = await new_order(en.id, "usdt_bep20", amount="8000")
    li = await invoice_of(lo.id)
    async with session_scope() as s:
        x = await s.get(Invoice, li.id)
        x.expires_at, x.created_at = utcnow() - timedelta(hours=1), utcnow() - timedelta(hours=2)
    await O.expire_stale()
    await pay("usdt_bep20", int(li.units))
    english(await to_chat(EN_ID, mark), "«платёж пришёл после истечения счёта — на баланс»")

    mark = len(session.calls)
    async with session_scope() as s:
        d = await create_deposit(s, await s.get(DbUser, en.id), amount_usd=Decimal("10"), method_code="usdt_trc20")
        units = int(d.units)
    await pay("usdt_trc20", units)
    english(await to_chat(EN_ID, mark), "«баланс пополнен»")

    mark = len(session.calls)
    q = await new_order(en.id, None, amount="500")  # paid from the balance
    async with session_scope() as s:
        (await s.get(Order, q.id)).status = "queued"
    await O.admin_refund(q.id)
    english(await to_chat(EN_ID, mark), "«заказ отменён, деньги на балансе» (админ вернул)")

    async with session_scope() as s:  # a second English customer with an empty balance: the claim needs an invoice
        en2 = DbUser(tg_id=EN_ID + 1, username="user778", first_name="Kate", tg_lang="en-GB")
        s.add(en2)
    mark = len(session.calls)
    co = await new_order(en2.id, "usdt_erc20")
    txid = await pay("usdt_erc20", int(Decimal((await invoice_of(co.id)).units) * Decimal("0.5")))
    async with session_scope() as s:
        s.add(Claim(user_id=en2.id, order_id=co.id, txid=txid, status="waiting"))
    await M.process_claims()
    async with session_scope() as s:
        cl = (await s.execute(select(Claim).where(Claim.txid == txid))).scalar_one()
    await M.approve_claim(cl.id, True)
    english(await to_chat(EN_ID + 1, mark), "«платёж по заявке подтверждён»")
    mark = len(session.calls)
    co2 = await new_order(en2.id, "usdt_erc20")
    txid2 = await pay("usdt_erc20", int(Decimal((await invoice_of(co2.id)).units) * Decimal("0.4")))
    async with session_scope() as s:
        s.add(Claim(user_id=en2.id, order_id=co2.id, txid=txid2, status="waiting"))
    await M.process_claims()
    async with session_scope() as s:
        cl2 = (await s.execute(select(Claim).where(Claim.txid == txid2))).scalar_one()
    await M.approve_claim(cl2.id, False)
    english(await to_chat(EN_ID + 1, mark), "«заявка отклонена»")

    print("4. экраны бота после покупок")
    english(await press(EN_ID, "m:bal", photo=True), "баланс и история (пополнение, заказ, возврат)")
    english(await press(EN_ID, "m:orders", photo=True), "мои заказы со статусами")
    english(await press(EN_ID, "m:home", photo=True), "главное меню со скидкой и балансом")

    print("5. промокоды")
    english(await send_text(EN_ID, "/promo"), "подсказка /promo")
    english(await send_text(EN_ID, "/promo NOPE"), "«такого промокода нет»")
    admin_out = await send_text(ADMIN, "/promo new GIFT2 $2 5")
    check("GIFT2" in visible(admin_out) and CYR.search(visible(admin_out)), "админ создал промокод (ему — по-русски)")
    english(await send_text(EN_ID, "/promo GIFT2"), "бонус по промокоду зачислен")
    english(await send_text(EN_ID, "/promo GIFT2"), "«вы уже использовали этот промокод»")

    print("6. выбор языка вручную")
    out = await send_text(RU_ID, "/language")
    check("English" in visible(out), "/language предлагает Русский / English")
    out = await press(RU_ID, "lang:en", photo=True)
    english(out, "выбрал English → сразу меню на английском")
    async with session_scope() as s:
        check((await s.execute(select(DbUser.lang).where(DbUser.tg_id == RU_ID))).scalar_one() == "en",
              "выбор сохранён в профиле (русский Telegram больше не важен)")
    i18n._chats.clear()
    await notify.notify_user(ru.id, f"{B.e('ok')} <b>Готово!</b>")
    check(visible(session.calls[-1:]) == "✅ Done!", "уведомления после перезапуска — тоже на выбранном языке")
    await press(RU_ID, "lang:ru", photo=True)
    check("Пополнить Steam" in visible(await send_text(RU_ID, "/start")), "вернул русский → снова по-русски")

    print("7. переключатель языка на сайте")
    async with session_scope() as s:
        tok = await create_session(s, await s.get(DbUser, ru.id), "203.0.113.9", "test")
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 1)), base_url="https://sh.test") as c:
        h = {"X-SH": "1", "X-SH-Session": tok}
        r = await c.post("/api/me/lang", json={"lang": "en"}, headers=h)
        check(r.status_code == 200, "сайт: переключил на EN")
        r = await c.post("/api/me/lang", json={"lang": "de"}, headers=h)
        check(r.status_code == 422, "неизвестный язык не принимается")
        r = await c.post("/api/me/lang", json={"lang": "en"}, headers={"X-SH": "1"})
        check(r.status_code == 401, "без входа — нельзя")
    english(await send_text(RU_ID, "/start"), "бот заговорил по-английски вслед за сайтом")
    await press(RU_ID, "lang:ru", photo=True)

    print("8. лист ожидания плагина FunPay / Playerok")
    out = await send_text(EN_ID, "/start plugin")
    txt = english(out, "кнопка «Узнать о запуске» → бот записал")
    check("on the list" in txt, "…и сказал, что записал")
    check("already" in visible(await send_text(EN_ID, "/start plugin")), "второй раз — «вы уже в списке», без дубля")
    check("Вы в списке" in visible(await send_text(RU_ID, "/start plugin")), "русскому клиенту — по-русски")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 1)), base_url="https://sh.test") as c:
        r = await c.post("/api/waitlist", headers={"X-SH": "1", "X-SH-Session": tok})
        check(r.status_code == 200 and r.json()["new"] is False, "с сайта (уже в списке через бота) — не дублируется")
        r = await c.post("/api/waitlist", headers={"X-SH": "1"})
        check(r.status_code == 401, "с сайта без входа — только через бота")
        r = await c.get("/api/waitlist", headers={"X-SH-Session": tok})
        check(r.json() == {"joined": True}, "после перезагрузки сайт знает: «Вы в списке» (записался через бота)")
        r = await c.get("/api/config", headers={"X-SH-Session": tok})
        check(r.json()["waitlist"] is True, "…и отдаёт это сразу с настройками страницы — без мигания кнопки")
        r = await c.get("/api/waitlist")
        check(r.json() == {"joined": False}, "без входа — «не в списке», без ошибки")
        async with session_scope() as s:
            tok2 = await create_session(s, await s.get(DbUser, en2.id), "203.0.113.9", "test")
        r = await c.get("/api/config", headers={"X-SH-Session": tok2})
        check(r.json()["waitlist"] is False, "тот, кто не записывался, видит «Узнать о запуске»")
    async with session_scope() as s:
        check(len((await s.execute(select(Waitlist))).scalars().all()) == 2, "в списке ровно 2 человека")
    check("Ждут плагин" in visible(await send_text(ADMIN, "/waitlist")), "админ видит список (/waitlist)")
    check(not await send_text(EN_ID, "/waitlist send hi"), "клиенту команда рассылки недоступна")
    mark = len(session.calls)
    out = await send_text(ADMIN, "/waitlist send Плагин вышел!")
    got = [c.chat_id for c in session.calls[mark:] if isinstance(c, SendMessage) and "Плагин вышел" in (c.text or "")]
    check(sorted(got) == [RU_ID, EN_ID], "рассылка ушла обоим, по одному разу")
    check("Доставлено 2 из 2" in visible(out), "админу — отчёт «доставлено 2 из 2»")
    mark = len(session.calls)
    out = await send_text(ADMIN, "/waitlist send Ещё раз")
    check(not [c for c in session.calls[mark:] if getattr(c, "chat_id", None) in (EN_ID, RU_ID)] and "0 из 0" in visible(out),
          "повторная рассылка не спамит тем, кто уже получил")

    print("9. надёжность")
    check(i18n.to_en("Заказов пока нет.") == "No orders yet." and i18n.to_en("Заказ SH-1") == "Order SH-1",
          "«Заказ» не откусывает кусок от «Заказов»")
    check(i18n.to_en("gaben · 500 ₽") == "gaben · 500 ₽", "текст без русского не меняется")

    async def boom(*a, **k):
        raise RuntimeError("db down")
    saved, i18n._chats = i18n._chats, {}
    real = i18n.lang_for_chat
    i18n.lang_for_chat = boom
    try:
        mid = await notify.send(EN_ID, "Готово")
    finally:
        i18n.lang_for_chat, i18n._chats = real, saved
    check(mid is not None, "если перевод сломался — сообщение всё равно уходит (по-русски)")

    print(f"\nALL GOOD: {OK} checks passed")


asyncio.run(main())
