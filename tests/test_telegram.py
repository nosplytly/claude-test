from __future__ import annotations

import json

import httpx

from playerok_bot.config import TelegramConfig
from playerok_bot.telegram import Notifier, link, money


def _notifier(handler, **overrides) -> Notifier:
    config = TelegramConfig(enabled=True, bot_token="123:abc", chat_ids=[1, 2], **overrides)
    return Notifier(config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_sends_html_to_every_chat():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    assert await _notifier(handler).send("<b>Привет</b>")

    assert [r.url.path for r in requests] == ["/bot123:abc/sendMessage"] * 2
    bodies = [json.loads(r.content) for r in requests]
    assert [b["chat_id"] for b in bodies] == [1, 2]
    assert bodies[0]["parse_mode"] == "HTML"
    assert bodies[0]["text"] == "<b>Привет</b>"


async def test_long_text_is_truncated():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    await _notifier(handler).send("x" * 5000)

    assert len(seen[0]) == 4096


async def test_errors_do_not_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["chat_id"] == 1:
            raise httpx.ConnectError("no route")
        return httpx.Response(403, json={"ok": False, "description": "bot was blocked"})

    assert not await _notifier(handler).send("test")


async def test_disabled_sends_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("не должно быть запросов")

    config = TelegramConfig(enabled=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await Notifier(config, client=client).send("test")


def test_link_escapes():
    assert link("https://x/?a=1&b=2", "<товар>") == (
        '<a href="https://x/?a=1&amp;b=2">&lt;товар&gt;</a>'
    )


async def test_disabled_telegram_with_bad_proxy_does_not_break_the_bot():
    # Прокси не трогаем, пока Telegram выключен: бот должен работать.
    notifier = Notifier(TelegramConfig(enabled=False, proxy="1.2.3.4:8080"))

    assert await notifier.send("test")
    await notifier.aclose()


async def test_bad_proxy_only_fails_sending():
    notifier = Notifier(
        TelegramConfig(enabled=True, bot_token="1:a", chat_ids=[1], proxy="1.2.3.4:8080")
    )

    assert not await notifier.send("test")
    await notifier.aclose()


def test_money_in_russian_format():
    assert money(18450.5) == "18 450,50 ₽"
    assert money(100) == "100 ₽"
    assert money(None) == "0 ₽"
