"""Общие помощники UI-тестов: Chromium, раздача supplierhub/web и страница с фейковым API.

Тесты пропускаются, если нет Playwright или браузера (путь задаётся
переменной окружения SUPPLIERHUB_CHROMIUM).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Browser, Error, Page, sync_playwright

from tests.ui.screenshots import CHROMIUM, MOCK, WEB, serve


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

    def open(
        self,
        route: str = "home",
        state: str = "running",
        *,
        mock: bool = True,
        query: str = "",
        size: tuple[int, int] = (1280, 820),
    ) -> Page:
        context = self.browser.new_context(viewport={"width": size[0], "height": size[1]})
        if mock:
            context.add_init_script(path=str(MOCK))
        page = context.new_page()
        page.on("console", lambda msg: msg.type == "error" and self.errors.append(msg.text))
        page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.pages.append(page)
        page.goto(f"{self.base_url}/index.html?state={state}{query}#/{route}")
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
