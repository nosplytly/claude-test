"""Проверки интерфейса SupplierHub в настоящем Chromium на фейковом API.

Тесты пропускаются, если нет Playwright или браузера (путь задаётся
переменной окружения SUPPLIERHUB_CHROMIUM).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Browser, Error, Page, sync_playwright

from tests.ui.screenshots import CHROMIUM, MOCK, WEB, QuietHandler, serve

ROUTES = ("home", "sales", "delivery", "relist", "settings", "logs")


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        try:
            if Path(CHROMIUM).exists():
                instance = pw.chromium.launch(executable_path=CHROMIUM)
            else:
                instance = pw.chromium.launch()
        except Error as exc:
            pytest.skip(f"Chromium недоступен: {exc}")
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    with serve(WEB) as url:
        yield url


class App:
    """Страница приложения с фейковым API и сбором ошибок консоли."""

    def __init__(self, browser: Browser, base_url: str) -> None:
        self.browser = browser
        self.base_url = base_url
        self.errors: list[str] = []
        self.pages: list[Page] = []

    def open(self, route: str = "home", state: str = "running", *, mock: bool = True) -> Page:
        context = self.browser.new_context(viewport={"width": 1280, "height": 820})
        if mock:
            context.add_init_script(path=str(MOCK))
        page = context.new_page()
        page.on("console", lambda msg: msg.type == "error" and self.errors.append(msg.text))
        page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.pages.append(page)
        page.goto(f"{self.base_url}/index.html?state={state}#/{route}")
        page.wait_for_selector(f'.page[data-page="{route}"]')
        return page

    def close(self) -> None:
        for page in self.pages:
            page.context.close()


@pytest.fixture
def app(browser: Browser, base_url: str) -> Iterator[App]:
    instance = App(browser, base_url)
    yield instance
    instance.close()
    assert instance.errors == []


def mock_state(page: Page, expression: str) -> object:
    return page.evaluate(f"() => window.__mock.state.{expression}")


@pytest.mark.parametrize("state", ["running", "empty", "error"])
def test_every_page_renders(app: App, state: str) -> None:
    page = app.open("home", state)
    for route in ROUTES:
        page.locator(f'.nav__item[data-route="{route}"]').click()
        page.wait_for_selector(f'.page[data-page="{route}"]')
        page.wait_for_timeout(150)
    assert page.locator(".nav__item.is-active").get_attribute("data-route") == "logs"


@pytest.mark.parametrize("route", ["home", "sales", "delivery"])
def test_html_in_data_is_shown_as_text(app: App, route: str) -> None:
    page = app.open(route, "xss")
    page.wait_for_selector(".feed-item, .trow, .rule")
    if route == "sales":
        page.locator(".table__body .trow").first.click()
        page.wait_for_selector(".layer.is-open .drawer")
    page.wait_for_timeout(300)
    assert page.evaluate("() => window.__xss === undefined")
    assert page.locator('img[src="x"]').count() == 0
    assert page.get_by_text("<img src=x", exact=False).count() > 0


def test_start_and_stop_bot(app: App) -> None:
    page = app.open("home", "stopped")
    page.locator(".botbox").get_by_role("button", name="Запустить").click()
    page.wait_for_selector(".botbox__title:has-text('Работает')", timeout=6000)
    assert page.locator(".hero__title").inner_text() == "Бот работает"
    page.locator(".hero").get_by_role("button", name="Остановить").click()
    page.wait_for_selector(".botbox__title:has-text('Остановлен')", timeout=6000)


def test_start_without_config_explains_what_to_do(app: App) -> None:
    page = app.open("home", "empty")
    page.locator(".botbox").get_by_role("button", name="Запустить").click()
    toast = page.locator(".toast--error")
    toast.wait_for()
    assert "config.toml" in toast.inner_text()
    toast.get_by_role("button", name="Настройки").click()
    page.wait_for_selector('.page[data-page="settings"]')


def test_settings_save_and_validation(app: App) -> None:
    page = app.open("settings")
    page.wait_for_selector("#set-deals-interval")
    assert page.locator(".savebar").is_hidden()

    page.locator("#set-deals-interval").fill("2")
    page.locator(".savebar").get_by_role("button", name="Сохранить").click()
    page.wait_for_selector(".field.has-error")
    assert mock_state(page, "config.playerok.deals_interval") == 20

    page.locator("#set-deals-interval").fill("15")
    page.locator(".savebar").get_by_role("button", name="Сохранить").click()
    page.locator(".toast--success").wait_for()
    assert mock_state(page, "config.playerok.deals_interval") == 15
    # Бот работает — тост предлагает перезапуск.
    assert page.locator(".toast__action", has_text="Перезапустить").count() == 1
    page.wait_for_selector(".savebar", state="hidden")


def test_leaving_dirty_page_asks_for_confirmation(app: App) -> None:
    page = app.open("relist")
    page.locator("#relist-expired").check()
    page.locator('.nav__item[data-route="home"]').click()
    dialog = page.locator(".layer--confirm.is-open")
    dialog.wait_for()
    dialog.get_by_role("button", name="Остаться").click()
    page.wait_for_timeout(300)
    assert page.locator('.page[data-page="relist"]').count() == 1
    page.locator('.nav__item[data-route="home"]').click()
    page.locator(".layer--confirm.is-open").get_by_role("button", name="Уйти").click()
    page.wait_for_selector('.page[data-page="home"]')


def test_create_rule_and_manage_stock(app: App) -> None:
    page = app.open("delivery", "empty")
    page.get_by_role("button", name="Создать первое правило").click()
    modal = page.locator(".layer.is-open .modal")
    modal.wait_for()
    page.locator("#rule-match").fill("Ключ Steam")
    modal.get_by_role("button", name="Создать правило").click()
    page.wait_for_selector(".rule")
    assert mock_state(page, "rules[0].stock_file") == "stock/ключ-steam.txt"

    page.locator(".rule").get_by_role("button", name="Склад", exact=True).click()
    drawer = page.locator(".layer.is-open .drawer")
    drawer.wait_for()
    drawer.locator("textarea").fill("AAAA-BBBB\n\nCCCC-DDDD\n")
    drawer.get_by_role("button", name="Добавить").click()
    page.wait_for_selector(".stock-item >> nth=1")
    assert drawer.locator(".stock__count-value").inner_text() == "2"
    assert drawer.locator(".stock-item__value").first.inner_text().startswith("•")

    drawer.locator(".stock-item").first.hover()
    drawer.locator(".stock-item").first.get_by_role("button", name="Удалить товар").click()
    page.wait_for_function("() => window.__mock.state.rules[0].stock.length === 1")

    drawer.get_by_role("button", name="Очистить").click()
    page.locator(".layer--confirm.is-open").get_by_role("button", name="Очистить").click()
    page.wait_for_function("() => window.__mock.state.rules[0].stock.length === 0")
    page.keyboard.press("Escape")
    page.wait_for_selector(".layer", state="detached")


def test_rule_editor_requires_product_placeholder(app: App) -> None:
    page = app.open("delivery")
    page.locator(".rule").first.get_by_role("button", name="Редактировать").click()
    page.locator("#rule-message").fill("Спасибо!")
    page.locator(".layer.is-open .modal").get_by_role("button", name="Сохранить").click()
    error = page.locator(".field.has-error .field__error")
    error.wait_for()
    assert "{product}" in error.inner_text()
    page.get_by_role("button", name="{product} товар со склада").click()
    assert "{product}" in page.locator("#rule-message").input_value()


def test_sales_filters_and_drawer(app: App) -> None:
    page = app.open("sales")
    page.wait_for_selector(".table__body .trow")
    total = page.locator(".table__body .trow").count()
    page.get_by_role("radio", name="Требуют внимания").click()
    assert page.locator(".table__body .trow").count() == 2 < total
    page.locator(".table__body .trow", has_text="Hades II").click()
    drawer = page.locator(".layer.is-open .drawer")
    drawer.wait_for()
    assert "502 Bad Gateway" in drawer.inner_text()
    drawer.get_by_role("button", name="Открыть сделку").click()
    page.wait_for_function("() => window.__mock.state.opened.length === 1")
    assert str(mock_state(page, "opened[0]")).startswith("https://playerok.com/deal/")


class BridgeHandler(QuietHandler):
    """Минимальный HTTP-мост: проверяет токен и отвечает заготовками."""

    token = "test-token-0123456789"
    calls: ClassVar[list[tuple[str, str | None]]] = []
    responses: ClassVar[dict[str, object]] = {
        "get_app_info": {
            "version": "1.0.0",
            "workdir": "/tmp/sh",
            "config_exists": False,
            "mode": "browser",
        },
        "get_overview": {
            "bot": {"status": "stopped", "error": None, "started_at": None, "uptime_sec": None},
            "account": None,
            "stats": {
                "sales_today": 0,
                "revenue_today": 0,
                "delivered_today": 0,
                "attention": 0,
                "stock_total": 0,
            },
            "low_stock": [],
            "features": {
                "delivery": True,
                "telegram": False,
                "relist_after_sale": True,
                "relist_expired": False,
            },
            "setup": {"token": False, "telegram": False, "rules": False},
            "config_error": None,
        },
        "get_feed": {"items": [], "last_id": 0},
    }

    def do_POST(self) -> None:
        method = self.path.removeprefix("/api/")
        sent = self.headers.get("X-SH-Token")
        self.calls.append((method, sent))
        length = int(self.headers.get("Content-Length") or 0)
        json.loads(self.rfile.read(length) or b"{}")
        if sent != self.token:
            self.send_response(403)
            self.end_headers()
            return
        data = self.responses.get(method)
        envelope = {"ok": True, "data": data} if data is not None else {"ok": False, "error": "нет"}
        body = json.dumps(envelope).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_browser_mode_uses_token_from_address(browser: Browser) -> None:
    BridgeHandler.calls.clear()
    errors: list[str] = []
    with serve(WEB, BridgeHandler) as url:
        context = browser.new_context()
        page = context.new_page()
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{url}/index.html#token={BridgeHandler.token}")
        page.wait_for_selector('.page[data-page="home"]')
        assert "token" not in page.evaluate("() => location.href")
        assert (
            page.evaluate("() => sessionStorage.getItem('supplierhub.token')")
            == BridgeHandler.token
        )
        # После перезагрузки токен берётся из sessionStorage.
        page.reload()
        page.wait_for_selector('.page[data-page="home"]')
        context.close()
    assert errors == []
    assert BridgeHandler.calls
    assert all(token == BridgeHandler.token for _, token in BridgeHandler.calls)
    assert {"get_app_info", "get_overview", "get_feed"} <= {
        method for method, _ in BridgeHandler.calls
    }


def test_opens_from_file_url(browser: Browser) -> None:
    """По file:// всё работает; Chromium лишь не грузит шрифты (CORS) — будет Segoe UI."""
    errors: list[str] = []
    context = browser.new_context()
    context.add_init_script(path=str(MOCK))
    page = context.new_page()
    page.on(
        "console",
        lambda msg: msg.type == "error" and errors.append(f"{msg.text} {msg.location.get('url')}"),
    )
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto((WEB / "index.html").as_uri() + "#/home")
    page.wait_for_selector(".hero")
    page.locator('.nav__item[data-route="delivery"]').click()
    page.wait_for_selector(".rule")
    context.close()
    assert [e for e in errors if "wght-normal.woff2" not in e] == []


def test_rule_change_while_running_suggests_restart(app: App) -> None:
    page = app.open("delivery")
    page.locator(".rule").nth(1).get_by_role("button", name="Поднять правило").click()
    hint = page.locator(".callout", has_text="старыми правилами")
    hint.wait_for()
    assert page.locator(".rule__match").first.inner_text() == "«Genshin Impact»"
    hint.get_by_role("button", name="Перезапустить").click()
    hint.wait_for(state="detached", timeout=8000)
