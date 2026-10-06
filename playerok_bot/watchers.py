"""Источники событий: сделки (опрос + WebSocket) и сообщения в чатах."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from PlayerokAPI import (
    Chat,
    ChatEvent,
    ChatMessage,
    ChatType,
    Deal,
    DealEvent,
    EventType,
    GraphQLError,
    ItemDealDirection,
    ItemDealStatus,
    MessageEvent,
    UnauthorizedError,
    User,
)

from .delivery import SECTION as DELIVERIES
from .delivery import AutoDelivery, DeliveryError
from .relist import AutoRelist
from .stock import StockError
from .storage import State, StateError
from .telegram import Notifier, esc, link, money

if TYPE_CHECKING:
    from PlayerokAPI import Account

__all__ = ["DealWatcher", "SeedingIncomplete", "watch_messages"]

logger = logging.getLogger(__name__)

#: Сколько последних продаж смотреть за один опрос.
_SWEEP_SIZE = 25
#: После скольких неудачных опросов подряд считать, что связи с Playerok нет.
_NETWORK_FAILURES = 3
#: Сколько секунд state.json может не записываться, прежде чем бить тревогу.
_STATE_ALERT_AFTER = 30.0
#: Важность проблем: показывается самая серьёзная.
_PROBLEM_ORDER = ("auth", "state", "network")


class SeedingIncomplete(Exception):
    """Первый запуск: не все старые сделки записаны как «не трогать».

    О каждой сделке уже сообщено; Playerok ответил, так что это не «нет связи».
    """


class DealWatcher:
    """Следит за продажами и передаёт оплаченные в автовыдачу.

    Основной источник — опрос списка продаж раз в `interval` секунд: он не
    пропустит сделку, даже если WebSocket отвалился. WebSocket лишь ускоряет
    реакцию. Обработка сделки идемпотентна, поэтому оба источника могут
    прислать одну и ту же сделку.
    """

    def __init__(
        self,
        account: Account,
        state: State,
        notifier: Notifier,
        delivery: AutoDelivery,
        relist: AutoRelist,
        *,
        first_run: bool,
    ) -> None:
        self._account = account
        self._state = state
        self._notifier = notifier
        self._delivery = delivery
        self._relist = relist
        self._first_run = first_run
        self._known: dict[str, tuple[ItemDealStatus | None, bool]] = {}
        self._seeded = asyncio.Event()
        self._auth_alerted = False
        self._failures = 0
        self._network_alerted = False
        self._state_alerted = False
        #: Сделка → тип ошибки, о которой уже сообщили (чтобы не слать каждый опрос).
        self._deal_errors: dict[str, str] = {}
        self._problems: dict[str, dict[str, str]] = {}
        #: Последние продажи из последнего опроса, новые сверху.
        self.recent: list[Deal] = []
        #: Время последнего удачного опроса продаж (UTC).
        self.last_poll_at: datetime | None = None

    @property
    def problem(self) -> dict[str, str] | None:
        """Почему бот сейчас не обрабатывает продажи: {kind, text, since} или None."""
        # Читается из потока окна, пока цикл бота меняет словарь, — только через get.
        for kind in _PROBLEM_ORDER:
            problem = self._problems.get(kind)
            if problem is not None:
                return dict(problem)
        return None

    async def run_polling(self, interval: float) -> None:
        while True:
            await self._check_state()
            try:
                await self.sweep(seeding=not self._seeded.is_set())
                self._seeded.set()
            except SeedingIncomplete as exc:
                logger.warning("Первый запуск: не все старые сделки записаны, повторю: %s", exc)
                await self._poll_ok()
            except Exception as exc:
                await self._poll_failed(exc)
            else:
                await self._poll_ok()
            await asyncio.sleep(interval)

    async def refresh_me(self) -> User:
        """Свежие данные аккаунта (баланс)."""
        return await self._account.get_me()

    async def mark_handled(self, deal_id: str) -> dict[str, object]:
        """Продавец сам решил вопрос по сделке — бот её больше не трогает."""
        return await self._delivery.mark_manual(deal_id)

    async def retry_delivery(self, deal_id: str) -> str:
        """Выдать товар по сделке заново по просьбе продавца."""
        deal = await self._account.deals.get(deal_id)
        if deal.status != ItemDealStatus.PAID:
            raise DeliveryError(
                "Сделка уже не в статусе «Оплачена» — бот её не выдаёт. Проверьте её на Playerok."
            )
        status = await self._delivery.retry(deal)
        self._deal_errors.pop(deal.id, None)
        return status

    async def run_websocket(self) -> None:
        # Сначала опрос запоминает текущие сделки, иначе на первом запуске
        # WebSocket мог бы выдать товар по сделке, оплаченной до бота.
        await self._seeded.wait()
        try:
            listener = self._account.listener(events=[EventType.NEW_DEAL, EventType.DEAL_UPDATED])
            async for event in listener.events():
                if isinstance(event, DealEvent) and event.deal is not None:
                    try:
                        await self.handle(event.deal)
                    except Exception as exc:
                        await self._deal_failed(event.deal, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("WebSocket недоступен, работаю только на опросе: %s", exc)

    async def sweep(self, *, seeding: bool = False) -> None:
        page = await self._account.deals.search(
            filter={"direction": ItemDealDirection.OUT.value}, first=_SWEEP_SIZE
        )
        self.recent = list(page.items)
        preexisting: list[Deal] = []
        failures: list[Exception] = []
        # Площадка отдаёт новые сверху — обрабатываем от старых к новым.
        # Сбой одной сделки (битый файл склада и т. п.) не должен мешать остальным.
        for deal in reversed(page.items):
            if seeding and self._first_run and deal.status == ItemDealStatus.PAID:
                preexisting.append(deal)
            try:
                await self.handle(deal, seeding=seeding)
            except Exception as exc:
                failures.append(exc)
                await self._deal_failed(deal, exc)
            else:
                self._deal_errors.pop(deal.id, None)
        if failures and seeding and self._first_run:
            # Первый запуск: пока все старые сделки не записаны как «не трогать»,
            # WebSocket не включается — опрос повторит запоминание.
            raise SeedingIncomplete(str(failures[0])) from failures[0]
        if preexisting:
            count = len(preexisting)
            waited = _plural(
                count,
                "оплаченная сделка уже ждала",
                "оплаченные сделки уже ждали",
                "оплаченных сделок уже ждали",
            )
            links = "\n".join(f"• {_ref(deal)}" for deal in preexisting)
            await self._notifier.send(
                f"ℹ️ Первый запуск: {count} {waited} выдачи. "
                f"Бот их не трогает — выдайте вручную:\n{links}"
            )

    async def handle(self, deal: Deal, *, seeding: bool = False) -> None:
        if deal.direction != ItemDealDirection.OUT:
            return
        prev = self._known.get(deal.id)
        self._known[deal.id] = (deal.status, deal.has_problem)
        paid = deal.status == ItemDealStatus.PAID
        # Продажа ещё не подтверждена покупателем — лот после неё можно перевыставлять.
        recent_sale = deal.status in (ItemDealStatus.PAID, ItemDealStatus.SENT)

        if seeding and self._first_run:
            if paid:
                self._delivery.skip(deal, "оплачена до первого запуска бота")
            if recent_sale:
                self._relist.skip(deal)
            return
        if seeding:
            if paid and self._state.get(DELIVERIES, deal.id) is None:
                await self._notifier.send(_new_sale(deal) + "\n(оплачена, пока бот был выключен)")
        else:
            await self._notify_changes(deal, prev)

        if paid:
            await self._delivery.process(deal)
        if recent_sale:
            await self._relist.after_sale(deal)

    async def _deal_failed(self, deal: Deal, exc: Exception) -> None:
        """Сделку обработать не вышло: в лог и одно уведомление на сделку и вид ошибки."""
        kind = type(exc).__name__
        if self._deal_errors.get(deal.id) == kind:
            # Та же ошибка на следующем опросе — без трассировки и без уведомления.
            logger.warning("Сделка %s: снова не удалось обработать: %s", deal.id, exc)
            return
        logger.error("Сделка %s: сбой обработки: %s", deal.id, exc, exc_info=exc)
        self._deal_errors[deal.id] = kind
        try:
            await self._notifier.send(
                f"❗ Сделка {_ref(deal)}: не удалось обработать — {esc(_explain(exc))}. "
                "Бот повторит при следующей проверке, остальные продажи обрабатываются."
            )
        except Exception:
            logger.exception("Не удалось отправить уведомление о сбое сделки %s", deal.id)

    async def _poll_ok(self) -> None:
        self.last_poll_at = datetime.now(UTC)
        self._failures = 0
        self._auth_alerted = False
        self._problems.pop("auth", None)
        self._problems.pop("network", None)
        if self._network_alerted:
            self._network_alerted = False
            await self._notifier.send(
                "ℹ️ Связь с Playerok восстановлена, продажи снова обрабатываются."
            )

    async def _poll_failed(self, exc: Exception) -> None:
        if _is_auth_error(exc):
            self._set_problem(
                "auth",
                "Playerok не принимает токен — продажи не обрабатываются. Вставьте новый токен "
                "в настройках и перезапустите бота.",
            )
            if not self._auth_alerted:
                logger.error("Playerok отклонил токен: %s", exc)
                self._auth_alerted = True
                await self._notifier.send(
                    "🔑 Playerok отклонил токен — продажи не обрабатываются. Возьмите новый "
                    "cookie <code>token</code> из браузера, вставьте его в настройках SupplierHub "
                    "(или в config.toml) и перезапустите бота."
                )
            return
        logger.warning("Опрос сделок не удался: %s", exc)
        self._failures += 1
        if self._failures < _NETWORK_FAILURES:
            return
        self._set_problem(
            "network", "Нет связи с Playerok — продажи не обрабатываются, бот пробует снова."
        )
        if not self._network_alerted:
            self._network_alerted = True
            await self._notifier.send(
                f"⚠️ Нет связи с Playerok: {esc(_explain(exc))}. Продажи не обрабатываются, "
                "бот продолжает попытки. Проверьте интернет, VPN или прокси."
            )

    async def _check_state(self) -> None:
        """Дописать отложенные записи state.json; долго не пишется — тревога."""
        self._state.flush()
        failing = self._state.failing_for()
        if failing <= 0:
            if self._problems.pop("state", None) is not None:
                self._state_alerted = False
                await self._notifier.send("ℹ️ state.json снова сохраняется, выдача продолжается.")
            return
        if failing < _STATE_ALERT_AFTER:
            return
        self._set_problem(
            "state",
            "Не удаётся сохранить state.json — новые продажи не выдаются. Закройте программы, "
            "которые держат файл, и проверьте, что он не «только для чтения».",
        )
        if not self._state_alerted:
            self._state_alerted = True
            error = self._state.last_error
            reason = (error.strerror if error is not None else None) or error
            await self._notifier.send(
                f"❗ Не удаётся сохранить <code>{esc(self._state.path.name)}</code> уже "
                f"{int(failing)} с: {esc(reason)}. Новые продажи не выдаются, пока запись "
                "не наладится. Закройте программы, которые держат файл (антивирус, OneDrive), "
                "и снимите атрибут «только для чтения»."
            )

    def _set_problem(self, kind: str, text: str) -> None:
        if kind not in self._problems:
            since = datetime.now(UTC).isoformat(timespec="seconds")
            self._problems[kind] = {"kind": kind, "text": text, "since": since}

    async def _notify_changes(
        self, deal: Deal, prev: tuple[ItemDealStatus | None, bool] | None
    ) -> None:
        prev_status, prev_problem = prev or (None, False)
        if deal.status != prev_status:
            if deal.status == ItemDealStatus.PAID:
                await self._notifier.send(_new_sale(deal))
            elif deal.status == ItemDealStatus.CONFIRMED:
                await self._notifier.send(
                    f"✅ Покупатель подтвердил сделку {_ref(deal)} — +{_price(deal)}"
                )
            elif deal.status == ItemDealStatus.ROLLED_BACK:
                await self._notifier.send(
                    f"↩️ Сделка {_ref(deal)} отменена, деньги возвращены покупателю"
                )
        if deal.has_problem and not prev_problem:
            await self._notifier.send(f"⚠️ Покупатель сообщил о проблеме по сделке {_ref(deal)}")


async def watch_messages(account: Account, me_id: str, notifier: Notifier, interval: float) -> None:
    """Пересылать в Telegram новые сообщения покупателей."""
    chats: dict[str, Chat] = {}
    try:
        async for event in account.polling(interval=interval).events():
            # Поллинг присылает обновлённый чат прямо перед его новыми сообщениями.
            if isinstance(event, ChatEvent) and event.chat is not None:
                chats[event.chat.id] = event.chat
            elif isinstance(event, MessageEvent) and event.message is not None:
                try:
                    await _forward_message(event.message, chats, me_id, notifier)
                except Exception:
                    logger.exception("Не удалось переслать сообщение %s", event.message.id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Слежение за сообщениями остановлено")


async def _forward_message(
    message: ChatMessage, chats: dict[str, Chat], me_id: str, notifier: Notifier
) -> None:
    if message.is_system or message.author_id == me_id:
        return
    chat = chats.get(message.chat_id or "")
    if chat is not None and chat.type == ChatType.NOTIFICATIONS:
        return
    author = (message.user.username if message.user else None) or "покупатель"
    text = message.text or (
        "[картинка]" if message.images or message.image_links else "[без текста]"
    )
    footer = (
        "\n" + link(f"https://playerok.com/chats/{message.chat_id}", "Открыть чат")
        if message.chat_id
        else ""
    )
    await notifier.send(f"💬 <b>{esc(author)}</b>: {esc(text)}{footer}")


def _is_auth_error(exc: Exception) -> bool:
    if isinstance(exc, UnauthorizedError):
        return True
    return isinstance(exc, GraphQLError) and exc.code in ("UNAUTHENTICATED", "UNAUTHORIZED")


def _price(deal: Deal) -> str:
    return money(deal.item.price if deal.item else 0.0)


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Форма слова для числа: 1 сделка, 2 сделки, 5 сделок."""
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    if count % 10 == 1:
        return one
    if 2 <= count % 10 <= 4:
        return few
    return many


def _explain(exc: BaseException) -> str:
    """Понятная причина сбоя для уведомления."""
    if isinstance(exc, StockError | StateError | DeliveryError):
        return str(exc)
    if isinstance(exc, UnicodeDecodeError):
        return "файл не в кодировке UTF-8 — пересохраните его в UTF-8"
    if isinstance(exc, OSError):
        where = f" {exc.filename}" if exc.filename else ""
        return f"ошибка доступа к файлу{where}: {exc.strerror or exc}"
    return str(exc) or type(exc).__name__


def _ref(deal: Deal) -> str:
    return link(deal.url, f"«{(deal.item.name if deal.item else None) or 'товар'}»")


def _new_sale(deal: Deal) -> str:
    buyer = (deal.user.username if deal.user else None) or "—"
    return f"🛒 Новая продажа: {_ref(deal)} за {_price(deal)}\nПокупатель: {esc(buyer)}"
