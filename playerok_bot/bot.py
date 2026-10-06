"""Сборка бота: аккаунт, уведомления, фоновые задачи."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from PlayerokAPI import Account, ItemDealDirection, PlayerokError, UnauthorizedError, User

from .config import Config
from .delivery import AutoDelivery
from .relist import AutoRelist
from .stock import Stock
from .storage import State
from .telegram import Notifier, esc
from .watchers import DealWatcher, watch_messages

__all__ = ["BotError", "check", "run"]

logger = logging.getLogger(__name__)


class BotError(Exception):
    """Ошибка запуска — сообщение показывается пользователю как есть."""


async def run(config: Config) -> None:
    """Запустить бота и работать, пока не остановят."""
    state = State.load(config.state_file)
    first_run = state.fresh
    notifier = Notifier(config.telegram)
    try:
        async with Account(token=config.playerok.token, proxy=config.playerok.proxy) as account:
            me = await _login(account)
            delivery = AutoDelivery(account, config.delivery, state, notifier)
            relist = AutoRelist(account, me.id, config.relist, state, notifier, delivery.can_sell)
            watcher = DealWatcher(account, state, notifier, delivery, relist, first_run=first_run)

            tasks: list[Coroutine[Any, Any, None]] = [
                watcher.run_polling(config.playerok.deals_interval)
            ]
            if config.playerok.use_websocket:
                tasks.append(watcher.run_websocket())
            if config.telegram.enabled and config.telegram.notify_messages:
                tasks.append(
                    watch_messages(account, me.id, notifier, config.telegram.messages_interval)
                )
            if config.relist.expired:
                tasks.append(relist.run_expired())

            logger.info("Бот запущен как %s", me.username)
            await notifier.send(_startup_text(config, me))
            await asyncio.gather(*tasks)
    finally:
        await notifier.aclose()


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
            count = Stock(rule.stock_file).count()
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
    except UnauthorizedError as exc:
        raise BotError(
            "Playerok не принял токен. Возьмите свежий cookie token из браузера "
            "(DevTools → Application → Cookies → playerok.com)."
        ) from exc
    except PlayerokError as exc:
        raise BotError(f"Не удалось подключиться к Playerok: {exc}") from exc


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
        f"Баланс: {balance:.2f} ₽\n"
        f"Автовыдача: {delivery}\n"
        f"Перевыставление: {', '.join(modes) or 'выкл'}"
    )
