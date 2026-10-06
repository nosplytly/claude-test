"""Прогон интерфейса на настоящем бэкенде SupplierHub в режиме браузера.

Запускает ``python -m supplierhub --browser --no-open`` во временной рабочей
папке, открывает выданный адрес с токеном в Chromium (Playwright), проходит
первый запуск (токен, правило, склад, запуск бота) и снимает скриншоты.
Завершается с кодом 1, если в консоли браузера были ошибки.

    .venv/bin/python tests/ui/live_backend.py --out /tmp/supplierhub-live
"""

from __future__ import annotations

import argparse
import queue
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.ui.screenshots import CHROMIUM  # noqa: E402

URL_RE = re.compile(r"http://127\.0\.0\.1:\d+/index\.html#token=\S+")


def start_backend(workdir: Path) -> tuple[subprocess.Popen[str], str]:
    """Запустить бэкенд и дождаться строки с адресом интерфейса."""
    process = subprocess.Popen(
        [sys.executable, "-m", "supplierhub", "--browser", "--no-open", "--workdir", str(workdir)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    seen: list[str] = []
    while True:
        try:
            line = lines.get(timeout=30)
        except queue.Empty:
            process.kill()
            raise RuntimeError("бэкенд не напечатал адрес:\n" + "".join(seen)) from None
        seen.append(line)
        found = URL_RE.search(line)
        if found:
            return process, found.group(0)


def walk(page: Page, out: Path) -> None:
    """Первый запуск глазами пользователя."""

    def shot(name: str) -> None:
        page.wait_for_timeout(500)
        page.screenshot(path=str(out / f"live-{name}.png"))

    page.wait_for_selector('.page[data-page="home"]')
    shot("home-first-run")

    page.locator('.nav__item[data-route="settings"]').click()
    page.wait_for_selector("#set-token")
    page.locator("#set-token").fill("live-test-token-0123456789")
    page.locator(".savebar").get_by_role("button", name="Сохранить").click()
    page.locator(".toast").first.wait_for()
    shot("settings-saved")

    page.locator('.nav__item[data-route="delivery"]').click()
    page.get_by_role("button", name="Новое правило").click()
    page.locator("#rule-match").fill("Ключ Steam")
    page.locator(".layer.is-open .modal").get_by_role("button", name="Создать правило").click()
    page.wait_for_selector(".rule")
    page.locator(".rule").first.get_by_role("button", name="Склад", exact=True).click()
    page.locator(".layer.is-open .drawer textarea").fill("AAAA-1111\nBBBB-2222\nCCCC-3333")
    page.locator(".layer.is-open .drawer").get_by_role("button", name="Добавить").click()
    page.wait_for_selector(".stock-item >> nth=2")
    shot("stock")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    shot("delivery")

    page.locator('.nav__item[data-route="home"]').click()
    page.locator(".botbox").get_by_role("button", name="Запустить").click()
    page.wait_for_selector(
        ".botbox__title:has-text('Ошибка'), .botbox__title:has-text('Работает')", timeout=40000
    )
    shot("home-after-start")

    for route in ("sales", "relist", "logs"):
        page.locator(f'.nav__item[data-route="{route}"]').click()
        page.wait_for_selector(f'.page[data-page="{route}"]')
        shot(route)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, default=Path(tempfile.gettempdir()) / "supplierhub-live"
    )
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="supplierhub-live-") as workdir:
        process, url = start_backend(Path(workdir))
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=CHROMIUM)
                page = browser.new_page(viewport={"width": 1280, "height": 820})
                page.on(
                    "console",
                    lambda msg: msg.type == "error" and errors.append(msg.text),
                )
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(url)
                walk(page, args.out)
                browser.close()
        finally:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()

    print(f"Скриншоты: {args.out}")
    if errors:
        print("Ошибки в браузере:", *errors, sep="\n  ", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
