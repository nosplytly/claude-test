"""Сборка бота: аккаунт, уведомления, фоновые задачи."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from PlayerokAPI import Account, ItemDealDirection, User
from PlayerokAPI.exceptions import (
    AuthRequiredError,
    ForbiddenError,
    GraphQLError,
    NetworkError,
    PlayerokError,
    RateLimitError,
    RequestTimeoutError,
    ServerError,
    TokenError,
    UnauthorizedError,
)

from .config import Config
from .delivery import AutoDelivery
from .lock import state_lock
from .relist import AutoRelist
from .stock import Stock, StockError
from .storage import State
from .telegram import Notifier, esc, money
from .watchers import DealWatcher, watch_messages

__all__ = ["BAD_TOKEN", "BotError", "check", "explain_playerok_error", "run"]

#: Текст для отклонённого токена (общий для бота и проверки в окне).
BAD_TOKEN = (
    "Playerok не принял токен. Возьмите свежий cookie token из браузера "
    "(DevTools → Application → Cookies → playerok.com)."
)

logger = logging.getLogger(__name__)


class BotError(Exception):
    """Ошибка запуска — сообщение показывается пользователю как есть."""


async def run(
    config: Config,
    *,
    notifier: Notifier | None = None,
    on_started: Callable[[User, DealWatcher], None] | None = None,
    watch_chats: bool | None = None,
) -> None:
    """Запустить бота и работать, пока не остановят.

    `notifier` — свой уведомитель (например, окно приложения), он закрывается
    по завершении. `on_started` вызывается после входа в аккаунт.
    `watch_chats` включает слежение за сообщениями; по умолчанию — только
    когда их есть куда пересылать (включён Telegram).
    """
    notifier = notifier or Notifier(config.telegram)
    lock = state_lock(config.state_file)
    try:
        if not lock.acquire():
            raise BotError(
                f"Бот с файлом {config.state_file.name} уже запущен (другое окно SupplierHub "
                "или python -m playerok_bot в этой папке). Два бота выдали бы товар дважды — "
                "закройте второй."
            )
        state = State.load(config.state_file)
        try:
            await _run(config, state, notifier, on_started, watch_chats)
        finally:
            # Отложенные записи (диск был занят) — на диск перед выходом.
            if not state.flush():
                logger.error("Не удалось сохранить %s при остановке", config.state_file)
    finally:
        lock.release()
        await notifier.aclose()


async def _run(
    config: Config,
    state: State,
    notifier: Notifier,
    on_started: Callable[[User, DealWatcher], None] | None,
    watch_chats: bool | None,
) -> None:
    first_run = state.fresh
    if watch_chats is None:
        watch_chats = config.telegram.enabled
    async with Account(token=config.playerok.token, proxy=config.playerok.proxy) as account:
        me = await _login(account)
        delivery = AutoDelivery(account, config.delivery, state, notifier)
        relist = AutoRelist(
            account,
            me.id,
            config.relist,
            state,
            notifier,
            delivery.can_sell,
            auto_delivered=delivery.handles,
        )
        watcher = DealWatcher(account, state, notifier, delivery, relist, first_run=first_run)

        tasks: list[Coroutine[Any, Any, None]] = [
            watcher.run_polling(config.playerok.deals_interval)
        ]
        if config.playerok.use_websocket:
            tasks.append(watcher.run_websocket())
        if watch_chats and config.telegram.notify_messages:
            tasks.append(
                watch_messages(account, me.id, notifier, config.telegram.messages_interval)
            )
        if config.relist.expired:
            tasks.append(relist.run_expired())

        logger.info("Бот запущен как %s", me.username)
        if on_started is not None:
            on_started(me, watcher)
        await notifier.send(_startup_text(config, me))
        await asyncio.gather(*tasks)


async def check(config: Config) -> bool:
    """Проверить токен, склад и Telegram. Печатает отчёт, True — всё в порядке."""
    ok = True
    async with Account(token=config.playerok.token, proxy=config.playerok.proxy) as account:
        try:
            me = await _login(account)
        except BotError as exc:
            print(f"✗ Playerok: {exc}")
            return False
        balance = me.balance.available if me.balance else 0.0
        print(f"✓ Playerok: вошли как {me.username}, доступно {balance:.2f} ₽")
        try:
            sales = await account.deals.search(
                filter={"direction": ItemDealDirection.OUT.value}, first=5
            )
            print(f"✓ Список продаж читается, всего продаж: {sales.total_count}")
        except PlayerokError as exc:
            ok = False
            print(f"✗ Не удалось прочитать продажи: {exc}")

    if config.delivery.enabled:
        print(f"Автовыдача: {len(config.delivery.rules)} правил")
        for rule in config.delivery.rules:
            if rule.stock_file is None:
                print(f"  • «{rule.match}»: одинаковый текст для всех")
                continue
            try:
                count = Stock(rule.stock_file).count()
            except (OSError, StockError) as exc:
                ok = False
                print(f"  ✗ «{rule.match}»: {exc}")
                continue
            mark = "✓" if count >= rule.products_per_sale else "✗"
            ok = ok and count >= rule.products_per_sale
            print(f"  {mark} «{rule.match}»: {count} шт. в {rule.stock_file}")
    else:
        print("Автовыдача выключена")

    if config.telegram.enabled:
        notifier = Notifier(config.telegram)
        try:
            sent = await notifier.send("✅ Проверка связи: бот Playerok настроен верно.")
        finally:
            await notifier.aclose()
        ok = ok and sent
        print("✓ Telegram: тестовое сообщение отправлено" if sent else "✗ Telegram: см. лог выше")
    else:
        print("Telegram выключен")
    return ok


async def _login(account: Account) -> User:
    try:
        return await account.get_me()
    except PlayerokError as exc:
        logger.warning("Вход в Playerok не удался: %r", exc)
        raise BotError(explain_playerok_error(exc)) from exc
    except (ImportError, ValueError) as exc:
        # httpx отвергает неподдерживаемый адрес прокси ещё до запроса.
        raise BotError(f"Неверный прокси для Playerok: {exc}") from exc


def explain_playerok_error(exc: Exception) -> str:
    """Понятная причина, почему не удалось войти в Playerok (подробности — в журнале)."""
    if isinstance(exc, UnauthorizedError | TokenError | AuthRequiredError):
        return BAD_TOKEN
    if isinstance(exc, GraphQLError) and exc.code in ("UNAUTHENTICATED", "UNAUTHORIZED"):
        return BAD_TOKEN
    if isinstance(exc, RequestTimeoutError):
        return (
            "Playerok не ответил вовремя. Проверьте интернет, VPN или прокси и попробуйте ещё раз."
        )
    if isinstance(exc, ForbiddenError):
        return (
            "Playerok не пускает с этого компьютера (ошибка 403). Проверьте интернет, "
            "VPN или прокси в «Дополнительно» и попробуйте позже."
        )
    if isinstance(exc, RateLimitError):
        return "Playerok просит подождать: слишком много запросов. Попробуйте через минуту."
    if isinstance(exc, ServerError):
        return "Playerok сейчас не работает (ошибка на стороне площадки). Попробуйте позже."
    if isinstance(exc, NetworkError):
        return (
            "Нет связи с Playerok. Проверьте интернет, VPN или прокси в «Дополнительно» "
            "и попробуйте ещё раз."
        )
    return f"Не удалось подключиться к Playerok: {exc}"


def _startup_text(config: Config, me: User) -> str:
    balance = me.balance.available if me.balance else 0.0
    delivery = f"вкл, правил: {len(config.delivery.rules)}" if config.delivery.enabled else "выкл"
    modes = [
        name
        for name, on in (
            ("после продажи", config.relist.after_sale),
            ("истёкшие", config.relist.expired),
        )
        if on
    ]
    return (
        f"🤖 Бот запущен: <b>{esc(me.username)}</b>\n"
        f"Баланс: {money(balance)}\n"
        f"Автовыдача: {delivery}\n"
        f"Перевыставление: {', '.join(modes) or 'выкл'}"
    )
