"""Регрессии по итогам UX-ревью: склад, первые шаги, перезапуск, выход, сделки.

Интерфейс в настоящем Chromium на фейковом API (tests/ui/mock_api.js), который
повторяет договорённый с бэкендом контракт (quit_app, mark_handled,
retry_delivery, bot.restart_required, bot.problem, expected у правил и склада).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Browser, Page

from tests.ui.conftest import App, mock_state
from tests.ui.screenshots import MOCK, WEB, QuietHandler, serve
from tests.ui.test_ui_smoke import BridgeHandler

MODAL = ".layer--modal.is-open .modal"
CONFIRM = ".layer--confirm.is-open"


def open_stock(page: Page, rule: int = 0) -> None:
    page.locator(".rule").nth(rule).get_by_role("button", name="Склад", exact=True).click()
    page.locator(".layer.is-open .drawer .stock-item").first.wait_for()


def stock(page: Page, rule: int = 0) -> list[str]:
    return mock_state(page, f"rules[{rule}].stock")  # type: ignore[return-value]


# --- склад: кодировка файла и многострочные товары ------------------------------------


def test_upload_cp1251_file_is_decoded_and_confirmed(app: App, tmp_path: Path) -> None:
    """Файл «ANSI» (Windows-1251) не превращается в «�»: его показывают и спрашивают."""
    goods = tmp_path / "аккаунты.txt"
    goods.write_bytes(
        "логин: Вася777 \\n пароль: Пароль123\nлогин: Петя \\n пароль: Секрет\n".encode("cp1251")
    )
    page = app.open("delivery")
    open_stock(page)
    before = len(stock(page))
    page.locator('.layer.is-open .drawer input[type="file"]').set_input_files(str(goods))
    modal = page.locator(MODAL)
    modal.wait_for()
    assert "Windows-1251" in modal.inner_text()
    assert "Вася777" in modal.locator(".choice__list").inner_text()
    modal.get_by_role("button", name="Текст читается — добавить").click()
    page.wait_for_function(f"() => window.__mock.state.rules[0].stock.length === {before + 2}")
    assert stock(page)[-2:] == [
        "логин: Вася777 \\n пароль: Пароль123",
        "логин: Петя \\n пароль: Секрет",
    ]


def test_upload_cp1251_file_can_be_cancelled(app: App, tmp_path: Path) -> None:
    goods = tmp_path / "keys.txt"
    goods.write_bytes("Ключ: ЖЖЖ-1\n".encode("cp1251"))
    page = app.open("delivery")
    open_stock(page)
    before = stock(page)
    page.locator('.layer.is-open .drawer input[type="file"]').set_input_files(str(goods))
    page.locator(MODAL).wait_for()
    page.locator(MODAL).get_by_role("button", name="Отмена").click()
    page.wait_for_selector(".layer--modal", state="detached")
    assert stock(page) == before
    assert page.locator(".layer.is-open .drawer").count() == 1


def test_upload_utf16_file_needs_no_questions(app: App, tmp_path: Path) -> None:
    """Блокнот «Юникод» — UTF-16 с BOM: однозначно, читается без вопросов."""
    goods = tmp_path / "unicode.txt"
    goods.write_bytes("КЛЮЧ-Ж1\r\nКЛЮЧ-Ж2\r\n".encode("utf-16"))
    page = app.open("delivery")
    open_stock(page)
    before = len(stock(page))
    page.locator('.layer.is-open .drawer input[type="file"]').set_input_files(str(goods))
    page.wait_for_function(f"() => window.__mock.state.rules[0].stock.length === {before + 2}")
    assert stock(page)[-2:] == ["КЛЮЧ-Ж1", "КЛЮЧ-Ж2"]
    assert page.locator(MODAL).count() == 0


def test_broken_text_is_not_added(app: App, tmp_path: Path) -> None:
    """Уже испорченный файл (UTF-8 с «�» внутри) не попадает к покупателям."""
    goods = tmp_path / "broken.txt"
    goods.write_bytes("���: 123\n".encode())
    page = app.open("delivery")
    open_stock(page)
    before = stock(page)
    page.locator('.layer.is-open .drawer input[type="file"]').set_input_files(str(goods))
    page.locator(".toast--error", has_text="испорченные символы").wait_for()
    assert stock(page) == before


def test_multiline_account_is_offered_as_one_product(app: App) -> None:
    page = app.open("delivery")
    open_stock(page)
    drawer = page.locator(".layer.is-open .drawer")
    before = len(stock(page))

    drawer.locator("textarea").fill("Логин: vasya777\nПароль: Qwerty123\nПочта: v@mail.ru\n")
    drawer.get_by_role("button", name="Добавить").click()
    modal = page.locator(MODAL)
    modal.wait_for()
    assert "Это один товар?" in modal.inner_text()
    modal.get_by_role("button", name="Объединить в один товар").click()
    page.wait_for_function(f"() => window.__mock.state.rules[0].stock.length === {before + 1}")
    assert stock(page)[-1] == "Логин: vasya777\\nПароль: Qwerty123\\nПочта: v@mail.ru"
    assert drawer.locator("textarea").input_value() == ""

    # Несколько аккаунтов подряд — по товару на каждую первую подпись.
    drawer.locator("textarea").fill("Логин: a1\nПароль: p1\nЛогин: a2\nПароль: p2")
    drawer.get_by_role("button", name="Добавить").click()
    modal.wait_for()
    modal.get_by_role("button", name="Объединить в 2 товара").click()
    page.wait_for_function(f"() => window.__mock.state.rules[0].stock.length === {before + 3}")
    assert stock(page)[-2:] == ["Логин: a1\\nПароль: p1", "Логин: a2\\nПароль: p2"]

    # Продавец может оставить строки отдельными товарами.
    drawer.locator("textarea").fill("Steam: KEY-1\nOrigin: KEY-2")
    drawer.get_by_role("button", name="Добавить").click()
    modal.wait_for()
    modal.get_by_role("button", name="Нет, 2 товара").click()
    page.wait_for_function(f"() => window.__mock.state.rules[0].stock.length === {before + 5}")
    assert stock(page)[-2:] == ["Steam: KEY-1", "Origin: KEY-2"]


@pytest.mark.parametrize(
    "text",
    [
        "AAAA-1111\n\nBBBB-2222\n",
        "login: a \\n pass: b\nlogin: c \\n pass: d",
        "vasya777:Qwerty123\npetya:Secret",
        "Ключ: AAA\nКлюч: BBB",
    ],
)
def test_ordinary_goods_are_added_without_questions(app: App, text: str) -> None:
    page = app.open("delivery")
    open_stock(page)
    before = len(stock(page))
    drawer = page.locator(".layer.is-open .drawer")
    drawer.locator("textarea").fill(text)
    drawer.get_by_role("button", name="Добавить").click()
    page.wait_for_function(f"() => window.__mock.state.rules[0].stock.length === {before + 2}")
    assert page.locator(MODAL).count() == 0


def test_deleted_stock_item_can_be_restored(app: App) -> None:
    page = app.open("delivery")
    open_stock(page)
    first = stock(page)[0]
    count = len(stock(page))
    item = page.locator(".stock-item").first
    item.hover()
    item.get_by_role("button", name="Удалить товар").click()
    toast = page.locator(".toast", has_text="Товар №1 удалён")
    toast.wait_for()
    assert first not in stock(page)
    toast.get_by_role("button", name="Отменить").click()
    page.locator(".toast", has_text="Товар возвращён").wait_for()
    assert len(stock(page)) == count
    assert stock(page)[-1] == first


# --- правила: склад при выключении и повторном создании, устаревший номер ----------------


def test_turning_stock_off_warns_about_goods_left(app: App) -> None:
    page = app.open("delivery")
    page.locator(".rule").first.get_by_role("button", name="Редактировать").click()
    page.locator(MODAL).wait_for()
    toggle = page.locator("#rule-stock")
    toggle.click(force=True)
    dialog = page.locator(CONFIRM)
    dialog.wait_for()
    assert "14 товаров" in dialog.inner_text()
    dialog.get_by_role("button", name="Оставить склад").click()
    page.wait_for_selector(".layer--confirm", state="detached")
    assert toggle.is_checked()
    assert page.locator(".editor__stock-fields").is_visible()

    toggle.click(force=True)
    page.locator(CONFIRM).get_by_role("button", name="Выключить склад").click()
    page.wait_for_selector(".layer--confirm", state="detached")
    assert not toggle.is_checked()
    assert page.locator(".editor__stock-fields").is_hidden()


def test_recreated_rule_gets_its_old_stock_back(app: App) -> None:
    page = app.open("delivery")
    genshin = page.locator(".rule", has_text="Genshin Impact")
    genshin.get_by_role("button", name="Удалить").click()
    dialog = page.locator(CONFIRM)
    dialog.wait_for()
    text = dialog.inner_text()
    assert "stock/genshin-impact.txt (2 товара)" in text
    assert "подключить к новому правилу" not in text
    dialog.get_by_role("button", name="Удалить").click()
    page.wait_for_function("() => window.__mock.state.rules.length === 3")

    page.get_by_role("button", name="Новое правило").click()
    page.locator("#rule-match").fill("Genshin Impact")
    page.locator(MODAL).get_by_role("button", name="Создать правило").click()
    toast = page.locator(".toast", has_text="Подключён склад")
    toast.wait_for()
    assert "stock/genshin-impact.txt" in toast.inner_text()
    assert "2 товара" in toast.inner_text()


def test_stale_rule_index_is_refused(app: App) -> None:
    """Правила поменяли в обход окна: склад правила №1 — уже чужой, его не трогаем."""
    page = app.open("delivery")
    page.wait_for_selector(".rule")
    # Как будто первое правило удалили в другой вкладке: под номером 0 теперь Genshin.
    page.evaluate("() => window.__mock.state.rules.splice(0, 1)")
    page.locator(".rule").first.get_by_role("button", name="Склад", exact=True).click()
    page.locator(".toast--error", has_text="Правила изменились").wait_for()
    page.wait_for_selector(".layer.is-open .drawer", state="detached")
    page.wait_for_function(
        "() => document.querySelector('.rule__match').textContent === '«Genshin Impact»'"
    )
    calls = page.evaluate("() => window.__mockCalls.filter((c) => c.method === 'get_stock')")
    assert calls[-1]["args"][1]["match"] == "Ключ Steam"
    assert calls[-1]["args"][1]["stock_file"] == "stock/klyuch-steam.txt"


def test_rule_and_stock_calls_carry_expected_rule(app: App) -> None:
    page = app.open("delivery")
    open_stock(page)
    drawer = page.locator(".layer.is-open .drawer")
    drawer.locator("textarea").fill("NEW-KEY-1")
    drawer.get_by_role("button", name="Добавить").click()
    page.locator(".toast", has_text="Добавлено").wait_for()
    page.keyboard.press("Escape")
    page.wait_for_selector(".layer", state="detached")
    page.locator(".rule").nth(1).get_by_role("button", name="Поднять правило").click()
    page.wait_for_function("() => window.__mockCalls.some((c) => c.method === 'move_rule')")
    calls = page.evaluate("() => window.__mockCalls")
    by_method = {call["method"]: call["args"] for call in calls}
    assert by_method["add_stock"][2]["match"] == "Ключ Steam"
    assert by_method["move_rule"][2]["match"] == "Genshin Impact"


# --- главная и меню: первые шаги, перезапуск, проблемы, выход -----------------------------


def test_onboarding_next_step_skips_optional_telegram(app: App) -> None:
    page = app.open("home", "empty")
    page.evaluate("() => { window.__mock.state.config.playerok.token = 'x'.repeat(40); }")
    page.wait_for_function(
        "() => document.querySelector('.step.is-next')?.textContent.includes('Правило')",
        timeout=6000,
    )
    telegram = page.locator(".step", has_text="Telegram")
    assert telegram.locator(".btn--secondary").count() == 1
    assert page.locator(".step.is-next .btn--primary", has_text="Создать правило").count() == 1


def test_restart_hint_is_global_until_restart(app: App) -> None:
    page = app.open("settings")
    page.wait_for_selector("#set-deals-interval")
    page.locator("#set-deals-interval").fill("25")
    page.locator(".savebar").get_by_role("button", name="Сохранить").click()
    page.locator(".toast--success").first.wait_for()
    restart = page.locator(".botbox__restart")
    restart.wait_for()

    for route in ("delivery", "home"):
        page.locator(f'.nav__item[data-route="{route}"]').click()
        page.wait_for_selector(f'.page[data-page="{route}"]')
        assert restart.is_visible()
    page.locator(".hero", has_text="старыми настройками").wait_for()
    page.locator('.nav__item[data-route="delivery"]').click()
    page.locator(".callout", has_text="старыми настройками").wait_for()

    restart.get_by_role("button", name="Перезапустить").click()
    page.wait_for_selector(".botbox__title:has-text('Работает')", timeout=8000)
    restart.wait_for(state="detached")
    page.wait_for_selector(".callout:has-text('старыми настройками')", state="detached")


def test_token_problem_is_not_shown_as_working(app: App) -> None:
    page = app.open("home", "running", query="&problem=auth")
    hero = page.locator(".hero")
    hero.locator(".hero__title", has_text="Playerok не принимает токен").wait_for()
    assert "Бот работает" not in hero.inner_text()
    assert page.locator(".botbox__title").inner_text() == "Не работает"
    assert page.locator(".botbox__sub").inner_text() == "Токен не принят"
    hero.get_by_role("button", name="Обновить токен").click()
    page.wait_for_selector('.page[data-page="settings"]')
    page.wait_for_function("() => document.activeElement?.id === 'set-token'")


def test_network_problem_points_to_proxy_settings(app: App) -> None:
    page = app.open("home", "running", query="&problem=network")
    hero = page.locator(".hero")
    hero.locator(".hero__title", has_text="Нет связи с Playerok").wait_for()
    assert "6 мин" in hero.inner_text()
    hero.get_by_role("button", name="Прокси и сеть").click()
    page.wait_for_selector("#settings-advanced.is-flash")


def test_running_hero_explains_window_and_balance_time(app: App) -> None:
    page = app.open("home")
    hero = page.locator(".hero")
    text = hero.inner_text()
    assert "Не закрывайте окно" in text
    assert "Работает 2 ч" in text
    assert "Аптайм" not in text
    assert "Баланс на " in text
    page.evaluate("() => { delete window.__mock.state.account.balance_at; }")
    hero.locator(".hero__meta", has_text="Баланс при запуске").wait_for()


def test_quit_in_browser_mode(app: App) -> None:
    page = app.open("home", "running", query="&mode=browser")
    assert "Вкладку можно закрыть" in page.locator(".hero").inner_text()
    page.locator(".sidebar__quit").click()
    dialog = page.locator(CONFIRM)
    dialog.wait_for()
    assert "перестанет выдавать товар" in dialog.inner_text()
    dialog.get_by_role("button", name="Остановить и выйти").click()
    page.locator(".splash__title", has_text="SupplierHub закрыт").wait_for()
    assert mock_state(page, "quit") is True
    assert page.get_by_role("button", name="Повторить").count() == 0


def test_quit_button_in_settings_only_in_browser_mode(app: App) -> None:
    page = app.open("settings")
    page.wait_for_selector(".settings-section")
    assert page.get_by_role("button", name="Выйти из SupplierHub").count() == 0
    assert page.locator(".sidebar__quit").count() == 0
    page = app.open("settings", "stopped", query="&mode=browser")
    page.wait_for_selector(".settings-section")
    page.get_by_role("button", name="Выйти из SupplierHub").click()
    page.locator(CONFIRM).get_by_role("button", name="Выйти").click()
    page.locator(".splash__title", has_text="SupplierHub закрыт").wait_for()


def test_dropped_file_does_not_replace_the_app(app: App) -> None:
    """Файл, брошенный в окно, не открывается вместо приложения (в окне pywebview такой
    странице достались бы методы приложения); перетаскивание текста работает как прежде."""
    page = app.open("delivery")
    page.wait_for_selector(".rule")
    prevented = page.evaluate(
        """() => ['dragover', 'drop'].map((type) => {
            const send = (dataTransfer) => {
                const init = { dataTransfer, bubbles: true, cancelable: true };
                const event = new DragEvent(type, init);
                document.body.dispatchEvent(event);
                return event.defaultPrevented;
            };
            const files = new DataTransfer();
            files.items.add(new File(['<script>alert(1)</script>'], 'evil.html'));
            const text = new DataTransfer();
            text.setData('text/plain', 'KEY-1');
            return [send(files), send(text)];
        })"""
    )
    assert prevented == [[True, False], [True, False]]


# --- продажи ----------------------------------------------------------------------------------


def test_failed_sale_can_be_marked_handled(app: App) -> None:
    page = app.open("sales")
    page.get_by_role("radio", name="Требуют внимания").click()
    page.locator(".table__body .trow", has_text="Hades II").click()
    drawer = page.locator(".layer.is-open .drawer")
    drawer.wait_for()
    drawer.get_by_role("button", name="Отметить: выдано вручную").click()
    page.locator(CONFIRM).get_by_role("button", name="Отметить").click()
    page.locator(".toast", has_text="выдано вручную").wait_for()
    page.wait_for_selector(".layer.is-open .drawer", state="detached")
    page.wait_for_function(
        "() => [...document.querySelectorAll('.table__body .trow')].length === 2", timeout=8000
    )
    sale = page.evaluate(
        "() => window.__mock.state.sales.find((s) => s.item.includes('Hades II')).delivery"
    )
    assert sale["status"] == "manual"


def test_failed_sale_retry_asks_first(app: App) -> None:
    page = app.open("sales")
    page.locator(".table__body .trow", has_text="Hades II").click()
    drawer = page.locator(".layer.is-open .drawer")
    drawer.get_by_role("button", name="Повторить выдачу").click()
    dialog = page.locator(CONFIRM)
    dialog.wait_for()
    assert "дважды" in dialog.inner_text()
    dialog.get_by_role("button", name="Отмена").click()
    page.wait_for_timeout(200)
    assert page.evaluate("() => window.__mockCalls.some((c) => c.method === 'retry_delivery')") is (
        False
    )


def test_no_rule_sale_with_new_rule_can_be_delivered(app: App) -> None:
    page = app.open("sales")
    page.evaluate(
        """() => window.__mock.state.sales.unshift({
          deal_id: 'late-rule-1', item: 'Ключ Steam — Portal 2', buyer: 'late_buyer', price: 199,
          status: 'PAID', created_at: new Date().toISOString(), chat_id: 'c-77',
          delivery: { status: 'no_rule', products: [], error: null, delivered_at: null },
        })"""
    )
    page.get_by_role("button", name="Обновить").click()
    page.locator(".table__body .trow", has_text="Portal 2").click()
    drawer = page.locator(".layer.is-open .drawer")
    drawer.locator(".callout", has_text="Правило для этого лота уже есть").wait_for()
    assert drawer.get_by_role("button", name="Создать правило").count() == 0
    drawer.get_by_role("button", name="Выдать по правилу").click()
    page.locator(CONFIRM).get_by_role("button", name="Выдать по правилу").click()
    page.locator(".toast", has_text="Бот выдаёт товар").wait_for()
    sale = page.evaluate(
        "() => window.__mock.state.sales.find((s) => s.deal_id === 'late-rule-1').delivery"
    )
    assert sale["status"] == "delivered"
    assert len(sale["products"]) == 1


# --- мелочи интерфейса ---------------------------------------------------------------------


def test_savebar_error_clears_and_toasts_do_not_cover_it(app: App) -> None:
    page = app.open("settings", "empty", size=(1040, 680))
    page.wait_for_selector("#set-tg-enabled")
    page.locator("#set-tg-enabled").click(force=True)
    bar = page.locator(".savebar")
    bar.get_by_role("button", name="Сохранить").click()
    error = bar.locator(".savebar__error")
    error.wait_for()
    assert "Исправьте" in error.inner_text()
    page.locator("#set-tg-token").fill("7412345678:AAH4mockTokenForSupplierHubUi_x9")
    assert error.is_hidden()

    page.evaluate("() => toast.error('Не удалось запустить бота', { text: 'Длинное пояснение ' })")
    toast = page.locator(".toast--error")
    toast.wait_for()
    page.wait_for_timeout(300)
    toast_box = toast.bounding_box()
    bar_box = bar.locator(".savebar__inner").bounding_box()
    assert toast_box is not None and bar_box is not None
    assert toast_box["y"] + toast_box["height"] <= bar_box["y"]


def test_failed_link_is_shown_and_copyable(app: App) -> None:
    page = app.open("home")
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.evaluate("() => { window.__mock.state.openFails = true; }")
    page.locator(".feed-item.is-link").first.click()
    toast = page.locator(".toast--error")
    toast.wait_for()
    url = toast.locator(".toast__url").inner_text()
    assert url.startswith("https://playerok.com/")
    toast.get_by_role("button", name="Копировать ссылку").click()
    page.locator(".toast", has_text="Ссылка скопирована").wait_for()
    assert page.evaluate("() => navigator.clipboard.readText()") == url


def test_bridge_page_without_token_fails_fast(browser: Browser) -> None:
    """Адрес моста без #token (закладка, история) — сразу понятная ошибка, а не 15 с ожидания."""
    with serve(WEB, BridgeHandler) as url:
        context = browser.new_context()
        page = context.new_page()
        started = time.monotonic()
        page.goto(f"{url}/index.html#/home")
        page.locator(".splash__title", has_text="Ссылка устарела").wait_for(timeout=5000)
        assert time.monotonic() - started < 5
        assert "ярлыком" in page.locator(".splash__text").inner_text()
        assert page.get_by_role("button", name="Повторить").count() == 0
        context.close()


def test_window_mode_still_waits_for_late_pywebview(browser: Browser) -> None:
    """Окно pywebview отдаёт страницу своим сервером без /api и подключает мост позже."""
    errors: list[str] = []
    with serve(WEB, QuietHandler) as url:
        context = browser.new_context()
        # Мост появляется через секунду после загрузки, как в настоящем окне.
        context.add_init_script(script=f"setTimeout(() => {{ {MOCK.read_text()} }}, 1000);")
        page = context.new_page()
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"{url}/index.html#/home")
        page.wait_for_selector('.page[data-page="home"]', timeout=10000)
        context.close()
    assert errors == []


def test_relist_page_says_which_lots_are_relisted(app: App) -> None:
    """Пустой список — только лоты с правилом: уникальный товар второй раз не продаётся."""
    page = app.open("relist", "empty")
    scope = page.locator(".scope")
    scope.wait_for()
    assert "только лоты с правилом автовыдачи" in scope.inner_text()
    assert "все ваши лоты" not in page.locator(".relist").inner_text()
    page.locator("#relist-match").fill("Ключ Steam")
    page.keyboard.press("Enter")
    assert "даже если для них нет правила" in scope.inner_text()


def test_bridge_page_with_outdated_token_explains_what_to_do(browser: Browser) -> None:
    """Ключ из старой вкладки (SupplierHub перезапускали) — «Повторить» не поможет."""
    with serve(WEB, BridgeHandler) as url:
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{url}/index.html#token=outdated-token")
        page.locator(".splash__title", has_text="Ссылка устарела").wait_for(timeout=5000)
        assert "перезапускали" in page.locator(".splash__text").inner_text()
        assert page.get_by_role("button", name="Повторить").count() == 0
        context.close()


def test_network_start_error_points_to_proxy_settings(app: App) -> None:
    page = app.open("home", "error")
    page.evaluate(
        """() => { window.__mock.state.bot.error =
          'Нет связи с Playerok. Проверьте интернет, VPN или прокси в «Дополнительно».'; }"""
    )
    hero = page.locator(".hero")
    hero.get_by_role("button", name="Прокси и сеть").click()
    page.wait_for_selector("#settings-advanced.is-flash")
