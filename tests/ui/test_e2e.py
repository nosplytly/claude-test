"""Сквозные проверки: настоящий бэкенд SupplierHub + Chromium (Playwright).

1. Бэкенд запускается отдельным процессом (`python -m supplierhub --browser --no-open`)
   во временной рабочей папке; интерфейс открывается по напечатанному адресу с токеном.
2. Бэкенд в этом же процессе с настоящим `playerok_bot.bot.run`, у которого вместо
   площадки — фейковый аккаунт: бот «продаёт», выдаёт товар, пишет в ленту.

Тесты пропускаются, если нет Playwright или Chromium (путь — SUPPLIERHUB_CHROMIUM).
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

pytest.importorskip("playwright.sync_api")

from PlayerokAPI import User
from playwright.sync_api import Browser, Error, Page, sync_playwright

from playerok_bot import bot as bot_core
from playerok_bot.config import load_config
from playerok_bot.stock import Stock
from supplierhub.api import LOCK_NAME, Api
from supplierhub.feed import EventFeed
from supplierhub.paths import web_dir
from supplierhub.runtime import BotRuntime
from supplierhub.server import BridgeServer
from tests.conftest import FakeAccount, FakePolling, make_deal
from tests.ui.screenshots import CHROMIUM

ROOT = Path(__file__).resolve().parents[2]
URL_RE = re.compile(r"http://127\.0\.0\.1:\d+/index\.html#token=\S+")


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    if not Path(CHROMIUM).exists():
        pytest.skip(f"Chromium не найден: {CHROMIUM}")
    with sync_playwright() as pw:
        try:
            instance = pw.chromium.launch(executable_path=CHROMIUM)
        except Error as exc:
            pytest.skip(f"Chromium не запускается: {exc}")
        yield instance
        instance.close()


@contextmanager
def open_app(browser: Browser, url: str) -> Iterator[Page]:
    """Страница приложения; в конце — ни ошибок в консоли, ни упавших запросов."""
    context = browser.new_context(viewport={"width": 1280, "height": 820})
    page = context.new_page()
    problems: list[str] = []
    page.on("console", lambda msg: msg.type == "error" and problems.append(msg.text))
    page.on("pageerror", lambda exc: problems.append(str(exc)))
    page.on("requestfailed", lambda req: problems.append(f"{req.url}: {req.failure}"))
    page.on(
        "response", lambda resp: resp.status >= 400 and problems.append(f"{resp.status} {resp.url}")
    )
    page.goto(url)
    page.wait_for_selector('.page[data-page="home"]')
    try:
        yield page
    finally:
        context.close()
    assert problems == []


# --- 1. бэкенд отдельным процессом ------------------------------------------------


class Backend:
    """`python -m supplierhub --browser --no-open` в своей рабочей папке."""

    def __init__(self, workdir: Path) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("PLAYEROK_TOKEN", "TELEGRAM_BOT_TOKEN")
        }
        self.workdir = workdir
        self.output: list[str] = []
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "supplierhub",
                "--browser",
                "--no-open",
                "--workdir",
                str(workdir),
                "--port",
                "0",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        lines: queue.Queue[str] = queue.Queue()

        def pump() -> None:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.output.append(line)
                lines.put(line)

        threading.Thread(target=pump, daemon=True).start()
        while True:
            try:
                line = lines.get(timeout=30)
            except queue.Empty:
                self.stop()
                pytest.fail("бэкенд не напечатал адрес:\n" + "".join(self.output))
            found = URL_RE.search(line)
            if found:
                self.url = found.group(0)
                return

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(5)


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[Backend]:
    instance = Backend(tmp_path / "workdir")
    yield instance
    instance.stop()


def test_first_rule_with_stock(
    browser: Browser, backend: Backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_app(browser, backend.url) as page:
        # Токен моста убран из адреса и живёт в sessionStorage.
        assert "token=" not in page.url
        assert page.locator(".hero__title").inner_text() == "Бот остановлен"

        page.locator('.nav__item[data-route="delivery"]').click()
        page.get_by_role("button", name="Создать первое правило").click()
        modal = page.locator(".layer.is-open .modal")
        modal.wait_for()
        page.locator("#rule-match").fill("Ключ Steam")
        modal.get_by_role("button", name="Создать правило").click()
        page.wait_for_selector(".rule")
        assert page.locator(".rule__match").inner_text() == "«Ключ Steam»"

        page.locator(".rule").get_by_role("button", name="Склад", exact=True).click()
        drawer = page.locator(".layer.is-open .drawer")
        drawer.wait_for()
        drawer.locator("textarea").fill("KEY-1\n\nKEY-2\nKEY-3\n")
        drawer.get_by_role("button", name="Добавить").click()
        page.wait_for_selector(".stock-item >> nth=2")
        assert drawer.locator(".stock__count-value").inner_text() == "3"

        upload = tmp_path / "goods.txt"
        upload.write_bytes("\ufeffKEY-4\r\nlogin: a \\n pass: b\r\n".encode())
        drawer.locator('input[type="file"]').set_input_files(str(upload))
        page.wait_for_selector(".stock-item >> nth=4")
        assert drawer.locator(".stock__count-value").inner_text() == "5"

        drawer.locator(".stock-item").first.hover()
        drawer.locator(".stock-item").first.get_by_role("button", name="Удалить товар").click()
        page.wait_for_selector(".stock-item >> nth=4", state="detached")
        assert drawer.locator(".stock__count-value").inner_text() == "4"

        page.keyboard.press("Escape")
        page.wait_for_selector(".layer", state="detached")
        page.wait_for_function(
            "() => document.querySelector('.stockbox__count')?.textContent.startsWith('4')"
        )

        # Без токена бот не стартует — и интерфейс объясняет, что делать.
        page.locator(".botbox").get_by_role("button", name="Запустить").click()
        toast = page.locator(".toast--error")
        toast.wait_for()
        assert "токен" in toast.inner_text()

    # На диске — конфиг, который читает бот, и склад с нужными строками.
    monkeypatch.setenv("PLAYEROK_TOKEN", "e2e-token")
    config = load_config(backend.workdir / "config.toml")
    [rule] = config.delivery.rules
    assert rule.match == "Ключ Steam"
    assert "{product}" in rule.message
    assert rule.stock_file == (backend.workdir / "stock" / "klyuch-steam.txt").resolve()
    assert Stock(rule.stock_file).items() == ["KEY-2", "KEY-3", "KEY-4", "login: a \\n pass: b"]


# --- 2. настоящий бот с фейковой площадкой ------------------------------------------

CONFIG = """
[playerok]
token = "e2e-token"
use_websocket = false

[telegram]
enabled = false

[delivery]
enabled = true

[[delivery.rules]]
match = "Ключ Steam"
stock_file = "stock/keys.txt"
message = "Спасибо, {buyer}! Ключ: {product}"
low_stock_alert = 1
"""


class PlayerokStub(FakeAccount):
    """Аккаунт Playerok без сети: одна свежая оплаченная продажа."""

    created: ClassVar[list[PlayerokStub]] = []
    #: Сколько секунд «входить в аккаунт» (медленная сеть).
    login_delay: ClassVar[float] = 0.3

    def __init__(self, token: str, proxy: str | None = None) -> None:
        super().__init__()
        deal = make_deal("deal-e2e", name="Ключ Steam Hades II", price=349, buyer="kirill")
        deal.created_at = datetime.now(UTC) - timedelta(minutes=2)
        self.deals.page = [deal]
        self.deals.by_id = {deal.id: deal}
        PlayerokStub.created.append(self)

    async def __aenter__(self) -> PlayerokStub:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_me(self) -> User:
        await asyncio.sleep(self.login_delay)
        return User.from_dict(
            {"id": "me-1", "username": "NeonKeys", "balance": {"available": 18450.5}}
        )

    def polling(self, *, interval: float) -> FakePolling:
        return FakePolling([])


@pytest.fixture
def live_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Api + HTTP-мост в этом процессе; бот ходит в PlayerokStub вместо площадки."""
    workdir = tmp_path / "live"
    (workdir / "stock").mkdir(parents=True)
    (workdir / "config.toml").write_text(CONFIG, encoding="utf-8")
    (workdir / "stock" / "keys.txt").write_text("KEY-1\nKEY-2\n", encoding="utf-8")
    # Не первый запуск: иначе бот не тронул бы уже оплаченные сделки.
    (workdir / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("PLAYEROK_TOKEN", raising=False)
    monkeypatch.setattr(bot_core, "Account", PlayerokStub)
    monkeypatch.setattr(PlayerokStub, "created", [])

    feed = EventFeed()
    runtime = BotRuntime(feed, lock_path=workdir / LOCK_NAME)
    opened: list[str] = []
    api = Api(
        workdir,
        mode="browser",
        feed=feed,
        runtime=runtime,
        open_url=opened.append,
        open_path=lambda path: None,
    )
    server = BridgeServer(api, web_dir())
    server.start(poll_interval=0.05)
    try:
        yield {"url": server.url, "workdir": workdir, "opened": opened, "runtime": runtime}
    finally:
        runtime.stop()
        server.shutdown()


def test_running_bot_delivers_and_shows_sale(browser: Browser, live_app: dict[str, Any]) -> None:
    with open_app(browser, live_app["url"]) as page:
        page.locator(".botbox").get_by_role("button", name="Запустить").click()
        page.wait_for_selector(".botbox__title:has-text('Работает')", timeout=10000)
        assert "NeonKeys" in page.locator(".hero").inner_text()

        # Уведомления бота приходят в ленту простым текстом.
        delivered = page.locator(".feed-item", has_text="Товар выдан")
        delivered.wait_for(timeout=10000)
        assert "Ключ Steam Hades II" in delivered.inner_text()
        page.locator(".feed-item", has_text="Новая продажа").click()
        for _ in range(50):
            if live_app["opened"]:
                break
            page.wait_for_timeout(100)
        assert live_app["opened"] == ["https://playerok.com/deal/deal-e2e"]

        page.locator('.nav__item[data-route="sales"]').click()
        row = page.locator(".table__body .trow", has_text="Hades II")
        row.wait_for()
        assert "Оплачена" in row.inner_text()
        assert "Выдано" in row.inner_text()
        row.click()
        drawer = page.locator(".layer.is-open .drawer")
        drawer.wait_for()
        assert drawer.locator(".product__text").all_inner_texts() == ["KEY-1"]
        page.keyboard.press("Escape")

        page.locator('.nav__item[data-route="home"]').click()
        page.locator(".hero").get_by_role("button", name="Остановить").click()
        page.wait_for_selector(".botbox__title:has-text('Остановлен')", timeout=10000)

    [account] = PlayerokStub.created
    assert account.chats.sent == [("c1", "Спасибо, kirill! Ключ: KEY-1")]
    assert (live_app["workdir"] / "stock" / "keys.txt").read_text(encoding="utf-8") == "KEY-2\n"
    assert live_app["runtime"].status == "stopped"


def test_slow_login_can_be_cancelled(
    browser: Browser, live_app: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PlayerokStub, "login_delay", 60.0)
    with open_app(browser, live_app["url"]) as page:
        page.locator(".hero").get_by_role("button", name="Запустить бота").click()
        page.wait_for_selector(".botbox__title:has-text('Запуск')")
        page.locator(".hero").get_by_role("button", name="Отменить").click()
        page.wait_for_selector(".botbox__title:has-text('Остановлен')", timeout=10000)
        assert page.locator(".hero__title").inner_text() == "Бот остановлен"
    assert live_app["runtime"].status == "stopped"
