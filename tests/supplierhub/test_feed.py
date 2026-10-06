from __future__ import annotations

import json
import logging

import httpx
import pytest

from playerok_bot import watchers
from playerok_bot.config import TelegramConfig
from supplierhub.feed import EventFeed, LogBuffer, parse_notification
from supplierhub.notifier import FeedNotifier

from ..conftest import make_deal

DEAL = '<a href="https://playerok.com/deal/d1">«Ключ &lt;Steam&gt; &amp; Co»</a>'


@pytest.mark.parametrize(
    ("message", "kind", "title", "text"),
    [
        (
            f"🛒 Новая продажа: {DEAL} за 100 ₽\nПокупатель: ivan",
            "sale",
            "Новая продажа: «Ключ <Steam> & Co» за 100 ₽",
            "Покупатель: ivan",
        ),
        (
            f"📦 Товар выдан: {DEAL} → ivan\n"
            "Осталось в <code>keys.txt</code>: 1 — 📉 пора пополнить",
            "delivered",
            "Товар выдан: «Ключ <Steam> & Co» → ivan",
            "Осталось в keys.txt: 1 — 📉 пора пополнить",
        ),
        (
            f"✅ Покупатель подтвердил сделку {DEAL} — +100 ₽",
            "confirmed",
            "Покупатель подтвердил сделку «Ключ <Steam> & Co» — +100 ₽",
            "",
        ),
        (
            f"↩️ Сделка {DEAL} отменена, деньги возвращены покупателю",
            "refund",
            "Сделка «Ключ <Steam> & Co» отменена, деньги возвращены покупателю",
            "",
        ),
        (
            f"⚠️ Покупатель сообщил о проблеме по сделке {DEAL}",
            "warning",
            "Покупатель сообщил о проблеме по сделке «Ключ <Steam> & Co»",
            "",
        ),
        (
            "⏸ Лот «Ключ» не перевыставлен: на складе не хватает товара.",
            "warning",
            "Лот «Ключ» не перевыставлен: на складе не хватает товара.",
            "",
        ),
        (
            f"❗ Закончился товар в <code>keys.txt</code>: сделка {DEAL} ждёт выдачи.",
            "error",
            "Закончился товар в keys.txt: сделка «Ключ <Steam> & Co» ждёт выдачи.",
            "",
        ),
        (
            f"❌ Не удалось выдать товар по сделке {DEAL}: boom.\n"
            "Товар для ручной выдачи:\n<code>KEY-1\nKEY-2</code>",
            "error",
            "Не удалось выдать товар по сделке «Ключ <Steam> & Co»: boom.",
            "Товар для ручной выдачи:\nKEY-1\nKEY-2",
        ),
        (
            "🔑 Playerok отклонил токен. Возьмите новый cookie <code>token</code>.",
            "error",
            "Playerok отклонил токен. Возьмите новый cookie token.",
            "",
        ),
        (
            "💬 <b>ivan</b>: привет &amp; &lt;b&gt;\n"
            '<a href="https://playerok.com/chats/c1">Открыть чат</a>',
            "message",
            "ivan: привет & <b>",
            "Открыть чат",
        ),
        (
            "🔁 Лот «Ключ» снова в продаже (статус: PENDING_APPROVAL)",
            "relist",
            "Лот «Ключ» снова в продаже (статус: PENDING_APPROVAL)",
            "",
        ),
        (
            f"ℹ️ Для лота {DEAL} нет правила автовыдачи — выдайте товар вручную.",
            "info",
            "Для лота «Ключ <Steam> & Co» нет правила автовыдачи — выдайте товар вручную.",
            "",
        ),
        (
            "🤖 Бот запущен: <b>seller</b>\nБаланс: 10.00 ₽\nАвтовыдача: выкл",
            "info",
            "Бот запущен: seller",
            "Баланс: 10.00 ₽\nАвтовыдача: выкл",
        ),
        ("Просто текст без эмодзи", "info", "Просто текст без эмодзи", ""),
    ],
)
def test_parse_every_kind(message: str, kind: str, title: str, text: str):
    parsed = parse_notification(message)

    assert parsed["kind"] == kind
    assert parsed["title"] == title
    assert parsed["text"] == text


def test_first_link_is_extracted_and_unescaped():
    message = (
        '🛒 <a href="https://playerok.com/deal/1?a=1&amp;b=2">первая</a> '
        '<a href="https://playerok.com/deal/2">вторая</a>'
    )

    assert parse_notification(message)["url"] == "https://playerok.com/deal/1?a=1&b=2"
    assert parse_notification("ℹ️ без ссылок")["url"] is None
    # Не-https ссылки окно всё равно не откроет — не отдаём их.
    assert parse_notification('ℹ️ <a href="javascript:alert(1)">x</a>')["url"] is None


def test_real_bot_messages():
    deal = make_deal(name="Ключ <Steam>", price=99.5, buyer="ivan & co")

    sale = parse_notification(watchers._new_sale(deal))

    assert sale == {
        "kind": "sale",
        "title": "Новая продажа: «Ключ <Steam>» за 99.50 ₽",
        "text": "Покупатель: ivan & co",
        "url": "https://playerok.com/deal/d1",
    }


def test_ring_buffer_ids_and_limit():
    feed = EventFeed(maxlen=3)
    for number in range(5):
        feed.add("info", f"#{number}")

    everything = feed.since(0)
    assert [item["title"] for item in everything["items"]] == ["#2", "#3", "#4"]
    assert [item["id"] for item in everything["items"]] == [3, 4, 5]
    assert everything["last_id"] == 5

    newer = feed.since(4)
    assert [item["id"] for item in newer["items"]] == [5]
    assert feed.since(5) == {"items": [], "last_id": 5}
    # Окно пережило перезапуск приложения и пришло с большим номером.
    assert len(feed.since(100)["items"]) == 3


def test_feed_items_shape():
    feed = EventFeed()
    item = feed.add_message('📦 Товар выдан <a href="https://playerok.com/deal/x">x</a>')

    assert set(item) == {"id", "ts", "kind", "title", "text", "url"}
    assert item["kind"] == "delivered"
    assert item["url"] == "https://playerok.com/deal/x"
    assert "T" in item["ts"]
    # Копии: правка ответа не портит буфер.
    feed.since()["items"][0]["title"] = "испорчено"
    assert feed.since()["items"][0]["title"] == "Товар выдан x"


def test_log_buffer_levels_and_traceback():
    buffer = LogBuffer(maxlen=10)
    log = logging.getLogger("supplierhub.test.feed")
    log.addHandler(buffer)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    try:
        log.debug("отладка %s", 1)
        log.info("инфо")
        log.warning("внимание")
        log.critical("критично")
        try:
            raise ValueError("сломалось")
        except ValueError:
            log.exception("ошибка")
    finally:
        log.removeHandler(buffer)

    items = buffer.since(0)["items"]
    assert [item["level"] for item in items] == ["DEBUG", "INFO", "WARNING", "ERROR", "ERROR"]
    assert items[0]["message"] == "отладка 1"
    assert items[0]["name"] == "supplierhub.test.feed"
    assert "Traceback" in items[-1]["message"] and "сломалось" in items[-1]["message"]
    assert buffer.since(items[-2]["id"])["items"] == [items[-1]]


async def test_feed_notifier_records_then_delegates():
    feed = EventFeed()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    config = TelegramConfig(enabled=True, bot_token="1:a", chat_ids=[7])
    notifier = FeedNotifier(
        config, feed, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    assert await notifier.send("🛒 Новая продажа: <b>x</b>")
    await notifier.aclose()

    assert feed.since()["items"][0]["title"] == "Новая продажа: x"
    assert json.loads(requests[0].content)["text"] == "🛒 Новая продажа: <b>x</b>"


async def test_feed_notifier_without_telegram():
    feed = EventFeed()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Telegram выключен — запросов быть не должно")

    notifier = FeedNotifier(
        TelegramConfig(enabled=False),
        feed,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    assert await notifier.send("💬 <b>ivan</b>: привет")
    await notifier.aclose()

    assert feed.since()["items"][0]["kind"] == "message"


async def test_feed_failure_does_not_block_telegram(monkeypatch: pytest.MonkeyPatch):
    feed = EventFeed()
    monkeypatch.setattr(feed, "add_message", lambda text: 1 / 0)
    notifier = FeedNotifier(TelegramConfig(enabled=False), feed)

    assert await notifier.send("ℹ️ текст")
    await notifier.aclose()
