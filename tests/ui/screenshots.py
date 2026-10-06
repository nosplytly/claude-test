"""Скриншоты интерфейса SupplierHub на фейковом API (tests/ui/mock_api.js).

Поднимает локальный HTTP-сервер для supplierhub/web, открывает страницы в Chromium
через Playwright, снимает каждую страницу и основные диалоги и завершается с кодом 1,
если в консоли браузера были ошибки или сработала проверка экранирования.

    .venv/bin/python tests/ui/screenshots.py --out /tmp/supplierhub-ui

Путь к Chromium можно задать переменной окружения SUPPLIERHUB_CHROMIUM.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

from playwright.sync_api import Browser, Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "supplierhub" / "web"
MOCK = Path(__file__).with_name("mock_api.js")
CHROMIUM = os.environ.get(
    "SUPPLIERHUB_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
)

WIDE = (1280, 820)
NARROW = (1040, 680)
HUGE = (1920, 1080)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map: ClassVar[dict[str, str]] = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".woff2": "font/woff2",
        ".svg": "image/svg+xml",
        ".js": "text/javascript",
    }

    def log_message(self, format: str, *args: object) -> None:
        """Не засорять вывод журналом запросов."""


@contextmanager
def serve(
    directory: Path, handler_class: type[http.server.SimpleHTTPRequestHandler] | None = None
) -> Iterator[str]:
    """Отдавать папку по HTTP на случайном порту 127.0.0.1."""
    handler = functools.partial(handler_class or QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


class Session:
    """Открывает страницы с фейковым API и собирает ошибки консоли."""

    def __init__(self, browser: Browser, base: str, out: Path) -> None:
        self.browser = browser
        self.base = base
        self.out = out
        self.errors: list[str] = []
        self.shots: list[Path] = []

    def open(self, route: str, *, state: str = "running", size: tuple[int, int] = WIDE) -> Page:
        context = self.browser.new_context(
            viewport={"width": size[0], "height": size[1]},
            device_scale_factor=1,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        context.add_init_script(path=str(MOCK))
        page = context.new_page()
        label = f"{route}?state={state}"
        page.on(
            "console",
            lambda msg: msg.type == "error" and self.errors.append(f"{label}: {msg.text}"),
        )
        page.on("pageerror", lambda exc: self.errors.append(f"{label}: {exc}"))
        page.goto(f"{self.base}/index.html?state={state}#/{route}")
        page.wait_for_selector(f'.page[data-page="{route}"]')
        page.evaluate("document.fonts.ready.then(() => true)")
        page.wait_for_timeout(600)
        return page

    def shot(self, page: Page, name: str) -> None:
        path = self.out / f"{name}.png"
        page.screenshot(path=str(path))
        self.shots.append(path)

    def close(self, page: Page) -> None:
        page.context.close()


def capture(session: Session) -> None:
    """Снять все экраны и состояния, нужные для визуальной проверки."""
    # Главная в разных состояниях и размерах окна.
    for state in ("running", "stopped", "empty", "error", "config"):
        page = session.open("home", state=state)
        session.shot(page, f"home-{state}")
        session.close(page)
    page = session.open("home", size=(1280, 1500))
    session.shot(page, "home-running-full")
    session.close(page)
    page = session.open("home", state="empty", size=(1280, 1200))
    session.shot(page, "home-empty-full")
    session.close(page)
    page = session.open("home", size=NARROW)
    session.shot(page, "home-1040")
    session.close(page)
    for route in ("home", "sales", "delivery"):
        page = session.open(route, size=HUGE)
        page.wait_for_timeout(300)
        session.shot(page, f"{route}-1920")
        session.close(page)
    for route in ("relist", "settings"):
        page = session.open(route, size=NARROW)
        page.wait_for_timeout(300)
        session.shot(page, f"{route}-1040")
        session.close(page)

    # Продажи: список, карточка выданной и проблемной сделки.
    page = session.open("sales")
    page.wait_for_selector(".table__body .trow")
    session.shot(page, "sales")
    page.locator(".table__body .trow", has_text="Elden Ring").click()
    page.wait_for_selector(".layer.is-open .drawer")
    page.wait_for_timeout(400)
    session.shot(page, "sales-drawer")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.locator(".table__body .trow", has_text="Hades II").click()
    page.wait_for_timeout(450)
    session.shot(page, "sales-drawer-failed")
    page.keyboard.press("Escape")
    page.get_by_role("radio", name="Требуют внимания").click()
    page.wait_for_timeout(200)
    session.shot(page, "sales-attention")
    session.close(page)
    page = session.open("sales", state="empty")
    page.wait_for_selector(".empty")
    session.shot(page, "sales-empty")
    session.close(page)
    page = session.open("sales", size=NARROW)
    page.wait_for_selector(".table__body .trow")
    session.shot(page, "sales-1040")
    session.close(page)

    # Автовыдача: правила, редактор, склад и подтверждение удаления.
    page = session.open("delivery")
    page.wait_for_selector(".rule")
    session.shot(page, "delivery")
    page.get_by_role("button", name="Новое правило").click()
    page.wait_for_selector(".layer.is-open .modal")
    page.wait_for_timeout(400)
    session.shot(page, "delivery-editor-new")
    page.locator(".layer.is-open .sheet__body").evaluate("el => el.scrollTo(0, el.scrollHeight)")
    page.wait_for_timeout(200)
    session.shot(page, "delivery-editor-new-bottom")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.locator(".rule").first.get_by_role("button", name="Редактировать").click()
    page.wait_for_selector(".layer.is-open .modal")
    page.wait_for_timeout(400)
    session.shot(page, "delivery-editor")
    page.locator("#rule-message").fill("Спасибо, {buyer}! Ключ будет позже.")
    page.get_by_role("button", name="Сохранить").click()
    page.wait_for_timeout(300)
    session.shot(page, "delivery-editor-error")
    page.keyboard.press("Escape")
    page.wait_for_selector(".layer--confirm.is-open")
    page.wait_for_timeout(300)
    session.shot(page, "delivery-editor-close-confirm")
    page.get_by_role("button", name="Закрыть").last.click()
    page.wait_for_timeout(400)
    page.locator(".rule").first.get_by_role("button", name="Склад", exact=True).click()
    page.wait_for_selector(".layer.is-open .drawer .stock-item")
    page.wait_for_timeout(400)
    session.shot(page, "delivery-stock")
    page.get_by_role("button", name="Показать").click()
    page.wait_for_timeout(150)
    session.shot(page, "delivery-stock-revealed")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.locator(".rule").nth(1).get_by_role("button", name="Удалить").click()
    page.wait_for_selector(".layer--confirm.is-open")
    page.wait_for_timeout(300)
    session.shot(page, "delivery-confirm")
    session.close(page)
    page = session.open("delivery", size=NARROW)
    page.wait_for_selector(".rule")
    session.shot(page, "delivery-1040")
    session.close(page)
    page = session.open("delivery", state="empty")
    session.shot(page, "delivery-empty")
    session.close(page)

    # Перевыставление и настройки (с панелью «Сохранить»).
    page = session.open("relist")
    page.wait_for_selector(".split")
    session.shot(page, "relist")
    page.locator("#relist-expired").check()
    page.wait_for_timeout(300)
    session.shot(page, "relist-dirty")
    session.close(page)

    page = session.open("settings")
    page.wait_for_selector(".settings-section")
    session.shot(page, "settings")
    page.locator("#set-deals-interval").fill("15")
    page.get_by_role("button", name="Проверить").click()
    page.wait_for_timeout(1200)
    session.shot(page, "settings-dirty")
    page.locator(".main").evaluate("el => el.scrollTo(0, el.scrollHeight)")
    page.wait_for_timeout(300)
    session.shot(page, "settings-bottom")
    session.close(page)
    page = session.open("settings", state="empty")
    page.wait_for_selector(".settings-section")
    session.shot(page, "settings-empty")
    session.close(page)

    page = session.open("logs")
    page.wait_for_selector(".logline")
    session.shot(page, "logs")
    session.close(page)

    capture_fixes(session)
    page = session.open("logs", size=NARROW)
    page.wait_for_selector(".logline")
    session.shot(page, "logs-1040")
    session.close(page)


def capture_fixes(session: Session) -> None:
    """Состояния после UX-ревью: проблемы бота, перезапуск, выход, склад, сделки."""
    for problem in ("auth", "network"):
        page = session.open("home", state=f"running&problem={problem}")
        session.shot(page, f"home-problem-{problem}")
        session.close(page)
    page = session.open("home", state="running&mode=browser")
    session.shot(page, "home-browser-mode")
    page.locator(".sidebar__quit").click()
    page.wait_for_selector(".layer--confirm.is-open")
    page.wait_for_timeout(300)
    session.shot(page, "quit-confirm")
    page.locator(".layer--confirm.is-open").get_by_role("button", name="Остановить и выйти").click()
    page.wait_for_selector(".splash__title")
    page.wait_for_selector(".layer", state="detached")
    session.shot(page, "quit-done")
    session.close(page)

    # Сохранили настройку, которую бот читает только при запуске: подсказка везде.
    page = session.open("settings", size=NARROW)
    page.wait_for_selector("#set-deals-interval")
    page.locator("#set-deals-interval").fill("25")
    page.locator(".savebar").get_by_role("button", name="Сохранить").click()
    page.wait_for_selector(".botbox__restart")
    page.locator(".nav__item[data-route='home']").click()
    page.wait_for_timeout(500)
    session.shot(page, "home-restart-1040")
    page.locator(".nav__item[data-route='delivery']").click()
    page.wait_for_timeout(500)
    session.shot(page, "delivery-restart-1040")
    session.close(page)

    # Панель «Сохранить» с ошибкой и тост над ней.
    page = session.open("settings", state="empty", size=NARROW)
    page.locator("#set-tg-enabled").click(force=True)
    page.locator(".savebar").get_by_role("button", name="Сохранить").click()
    page.evaluate("() => toast.error('Не удалось запустить бота', { text: 'Пример ошибки.' })")
    page.wait_for_timeout(400)
    session.shot(page, "settings-savebar-toast-1040")
    session.close(page)

    # Склад: многострочный товар, удаление с отменой, выключение склада.
    page = session.open("delivery")
    page.locator(".rule").first.get_by_role("button", name="Склад", exact=True).click()
    page.wait_for_selector(".layer.is-open .drawer .stock-item")
    drawer = page.locator(".layer.is-open .drawer")
    drawer.locator("textarea").fill("Логин: vasya777\nПароль: Qwerty123\nПочта: v@mail.ru")
    drawer.get_by_role("button", name="Добавить").click()
    page.wait_for_selector(".layer--modal.is-open .modal")
    page.wait_for_timeout(400)
    session.shot(page, "stock-multiline-dialog")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.locator(".stock-item").first.hover()
    page.locator(".stock-item").first.get_by_role("button", name="Удалить товар").click()
    page.wait_for_selector(".toast .toast__action")
    page.wait_for_timeout(300)
    session.shot(page, "stock-delete-undo")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.locator(".rule").first.get_by_role("button", name="Редактировать").click()
    page.wait_for_selector(".layer.is-open .modal")
    page.locator("#rule-stock").click(force=True)
    page.wait_for_selector(".layer--confirm.is-open")
    page.wait_for_timeout(300)
    session.shot(page, "editor-stock-off-confirm")
    session.close(page)

    # Сделки: ошибка выдачи с действиями и «правило уже есть».
    page = session.open("sales")
    page.wait_for_selector(".table__body .trow")
    page.evaluate(
        """() => window.__mock.state.sales.unshift({
          deal_id: 'late-rule-1', item: 'Ключ Steam — Portal 2', buyer: 'late_buyer', price: 199,
          status: 'PAID', created_at: new Date().toISOString(), chat_id: 'c-77',
          delivery: { status: 'no_rule', products: [], error: null, delivered_at: null },
        })"""
    )
    page.get_by_role("button", name="Обновить").click()
    page.locator(".table__body .trow", has_text="Portal 2").click()
    page.wait_for_selector(".layer.is-open .drawer")
    page.wait_for_timeout(400)
    session.shot(page, "sales-drawer-rule-available")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.locator(".table__body .trow", has_text="Hades II").click()
    page.wait_for_selector(".layer.is-open .drawer")
    page.wait_for_timeout(400)
    session.shot(page, "sales-drawer-failed-actions")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    page.locator(".table__body .trow", has_text="Буст рейтинга").click()
    page.wait_for_timeout(400)
    session.shot(page, "sales-drawer-no-rule")
    session.close(page)

    # Ссылка не открылась — показываем её.
    page = session.open("home")
    page.evaluate("() => { window.__mock.state.openFails = true; }")
    page.locator(".feed-item.is-link").first.click()
    page.wait_for_selector(".toast__url")
    page.wait_for_timeout(300)
    session.shot(page, "open-url-failed")
    session.close(page)

    page = session.open("relist", state="empty")
    page.wait_for_selector(".split")
    session.shot(page, "relist-empty")
    session.close(page)


def check_escaping(session: Session) -> None:
    """Данные с HTML-разметкой должны показываться текстом, а не выполняться."""
    for route, ready in (("home", ".feed-item"), ("sales", ".trow"), ("delivery", ".rule")):
        page = session.open(route, state="xss")
        page.wait_for_selector(ready)
        if route == "sales":
            page.locator(".table__body .trow").first.click()
            page.wait_for_timeout(300)
        injected = page.evaluate(
            "() => Boolean(window.__xss) || document.querySelectorAll('img[src=\"x\"]').length"
        )
        if injected:
            session.errors.append(f"{route}?state=xss: HTML из данных попал в разметку")
        session.close(page)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(tempfile.gettempdir()) / "supplierhub-ui",
        help="куда сохранить скриншоты",
    )
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    with serve(WEB) as base, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        session = Session(browser, base, args.out)
        try:
            capture(session)
            check_escaping(session)
        finally:
            browser.close()

    print(f"Скриншотов: {len(session.shots)} → {args.out}")
    if session.errors:
        print("Ошибки в браузере:", file=sys.stderr)
        for error in session.errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
