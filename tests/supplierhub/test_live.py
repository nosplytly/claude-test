"""Работающий бот и окно: правила без перезапуска, состояние бота, действия с продажами."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from playerok_bot.delivery import DeliveryError
from playerok_bot.lock import state_lock
from supplierhub import runtime as runtime_module
from supplierhub.api import Api

from .conftest import FakeBot, make_me, wait_until
from .test_stats import deal


def _start(api: Api) -> None:
    assert api.start_bot()["ok"] is True
    wait_until(lambda: api.get_overview()["data"]["bot"]["status"] == "running")


def _rule(match: str, **extra: Any) -> dict[str, Any]:
    return {"match": match, "message": "Держите: {product}", "use_stock": True, **extra}


# --- правила применяются к работающему боту ---------------------------------------------


def test_new_rule_reaches_running_bot_without_restart(
    api: Api, config_file: Path, fake_bot: FakeBot
):
    _start(api)
    live = fake_bot.calls[0]["config"]
    assert [rule.match for rule in live.delivery.rules] == ["Ключ Steam", "Гайд"]

    saved = api.save_rule(_rule("Аккаунт Minecraft"))["data"]

    assert saved["restart_required"] is False
    assert [rule.match for rule in live.delivery.rules] == [
        "Ключ Steam",
        "Гайд",
        "Аккаунт Minecraft",
    ]
    assert live.delivery.rules[2].stock_file == (config_file.parent / saved["item"]["stock_file"])

    assert api.move_rule(2, -1)["data"]["restart_required"] is False
    assert [rule.match for rule in live.delivery.rules][1] == "Аккаунт Minecraft"
    assert api.delete_rule(1)["data"]["restart_required"] is False
    assert len(live.delivery.rules) == 2

    settings = api.save_config({"relist": {"after_sale": True, "match": ["Ключ"]}})["data"]
    assert settings["restart_required"] is False
    assert live.relist.after_sale and live.relist.match == ["Ключ"]
    assert api.get_overview()["data"]["bot"]["restart_required"] is False
    assert len(fake_bot.calls) == 1  # без перезапуска


def test_token_change_still_needs_restart(api: Api, config_file: Path, fake_bot: FakeBot):
    _start(api)

    saved = api.save_config({"playerok": {"token": "new-token"}})["data"]

    assert saved["restart_required"] is True
    assert api.get_overview()["data"]["bot"]["restart_required"] is True
    assert fake_bot.calls[0]["config"].playerok.token == "tok"

    assert api.restart_bot()["ok"] is True
    wait_until(lambda: len(fake_bot.calls) == 2)
    wait_until(lambda: api.get_overview()["data"]["bot"]["status"] == "running")
    assert api.get_overview()["data"]["bot"]["restart_required"] is False


def test_hand_edited_config_is_picked_up(
    api: Api, config_file: Path, fake_bot: FakeBot, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(runtime_module, "CONFIG_CHECK_INTERVAL", 0.02)
    _start(api)
    live = fake_bot.calls[0]["config"]

    text = config_file.read_text(encoding="utf-8").replace('match = "Гайд"', 'match = "Курс"')
    config_file.write_text(text, encoding="utf-8")

    wait_until(lambda: live.delivery.rules[1].match == "Курс")


# --- состояние бота: проблемы, последний опрос, баланс ------------------------------------


def test_overview_shows_bot_problem_and_last_poll(api: Api, config_file: Path, fake_bot: FakeBot):
    polled = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
    problem = {"kind": "auth", "text": "Playerok не принимает токен", "since": "x"}
    fake_bot.watcher = SimpleNamespace(recent=[], problem=problem, last_poll_at=polled)
    _start(api)

    bot = api.get_overview()["data"]["bot"]

    assert bot["status"] == "running"
    assert bot["problem"] == problem
    assert bot["last_poll_at"] == "2026-10-06T08:00:00+00:00"


def test_balance_is_refreshed(
    api: Api, config_file: Path, fake_bot: FakeBot, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(runtime_module, "CONFIG_CHECK_INTERVAL", 0.01)
    monkeypatch.setattr(runtime_module, "BALANCE_INTERVAL", 0.0)

    async def refresh_me():
        return make_me(balance=99.0)

    fake_bot.watcher = SimpleNamespace(recent=[], refresh_me=refresh_me)
    _start(api)

    wait_until(lambda: api.get_overview()["data"]["account"]["balance"] == 99.0)
    assert api.get_overview()["data"]["account"]["balance_at"]


# --- «Требуют внимания»: отметить вручную, выдать заново ---------------------------------


def _write_state(workdir: Path, deliveries: dict[str, Any]) -> None:
    (workdir / "state.json").write_text(
        json.dumps({"deliveries": deliveries}, ensure_ascii=False), encoding="utf-8"
    )


def test_mark_handled_when_bot_is_stopped(api: Api, workdir: Path, config_file: Path):
    now = datetime.now(UTC).isoformat(timespec="seconds")
    _write_state(
        workdir,
        {
            "d1": {"status": "failed", "item": "Ключ", "products": ["KEY-9"], "at": now},
            "d2": {"status": "no_stock", "item": "Ключ", "at": now},
            "d3": {"status": "delivered", "item": "Ключ", "at": now},
        },
    )
    assert api.get_overview()["data"]["stats"]["attention"] == 2

    result = api.mark_handled("d1")

    assert result["ok"] is True, result
    assert result["data"]["delivery"]["status"] == "manual"
    assert result["data"]["delivery"]["label"] == "Выдано вручную"
    assert result["data"]["delivery"]["handled_at"]
    assert api.get_overview()["data"]["stats"]["attention"] == 1
    sales = {sale["deal_id"]: sale for sale in api.get_sales()["data"]["items"]}
    assert sales["d1"]["attention"] is False and sales["d2"]["attention"] is True
    saved = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert saved["deliveries"]["d1"]["status"] == "manual"
    assert saved["deliveries"]["d1"]["products"] == ["KEY-9"]

    assert "не требует внимания" in api.mark_handled("d3")["error"]
    assert api.mark_handled("")["ok"] is False
    assert api.mark_handled(None)["ok"] is False


def test_mark_handled_refuses_while_another_bot_owns_state(
    api: Api, workdir: Path, config_file: Path
):
    _write_state(workdir, {"d1": {"status": "failed", "at": "2026-10-06T08:00:00+00:00"}})
    other = state_lock(workdir / "state.json")
    assert other.acquire()
    try:
        result = api.mark_handled("d1")
    finally:
        other.release()

    assert result["ok"] is False and "другом окне" in result["error"]
    saved = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert saved["deliveries"]["d1"]["status"] == "failed"


class ActionsWatcher(SimpleNamespace):
    """Наблюдатель работающего бота с действиями продавца."""

    async def mark_handled(self, deal_id: str) -> dict[str, Any]:
        self.marked.append(deal_id)
        return {"status": "manual", "handled_at": "2026-10-06T08:00:00+00:00"}

    async def retry_delivery(self, deal_id: str) -> str:
        if deal_id == "gone":
            raise DeliveryError("Сделка уже не в статусе «Оплачена»")
        self.retried.append(deal_id)
        return "delivered"


def test_actions_go_through_running_bot(api: Api, config_file: Path, fake_bot: FakeBot):
    assert "Запустите бота" in api.retry_delivery("d1")["error"]
    watcher = ActionsWatcher(recent=[], marked=[], retried=[])
    fake_bot.watcher = watcher
    _start(api)

    assert api.mark_handled("d1")["data"]["delivery"]["status"] == "manual"
    assert api.retry_delivery("d2") == {
        "ok": True,
        "data": {"deal_id": "d2", "status": "delivered"},
    }
    assert api.retry_delivery("gone") == {
        "ok": False,
        "error": "Сделка уже не в статусе «Оплачена»",
    }
    assert watcher.marked == ["d1"] and watcher.retried == ["d2"]
    # Файл состояния окно не трогало: его ведёт бот.
    assert not (config_file.parent / "state.json").exists()


def test_attention_and_rule_hint_follow_live_status(api: Api, workdir: Path, config_file: Path):
    now = datetime.now(UTC).isoformat(timespec="seconds")
    _write_state(
        workdir,
        {
            "fixed": {"status": "failed", "at": now},
            "refunded": {"status": "no_stock", "at": now},
            "waiting": {"status": "no_stock", "at": now},
            "norule": {"status": "no_rule", "item": "Ключ Steam Witcher", "at": now},
            "norule-old": {"status": "no_rule", "item": "Аккаунт", "at": now},
        },
    )
    api._runtime._on_started(
        make_me(),
        SimpleNamespace(
            recent=[
                deal("fixed", "CONFIRMED"),
                deal("refunded", "ROLLED_BACK"),
                deal("waiting", "PAID"),
                deal("norule", "PAID", name="Ключ Steam Witcher"),
            ]
        ),
    )

    sales = {sale["deal_id"]: sale for sale in api.get_sales()["data"]["items"]}

    # Сделку закрыли на площадке (вручную выдали или вернули деньги) — внимание не нужно.
    assert sales["fixed"]["attention"] is False
    assert sales["refunded"]["attention"] is False
    assert sales["waiting"]["attention"] is True
    # Оплаченная продажа без правила ждёт продавца; правило для неё уже есть.
    assert sales["norule"]["attention"] is True
    assert sales["norule"]["delivery"]["rule_available"] is True
    assert sales["norule-old"]["attention"] is False
    assert sales["norule-old"]["delivery"]["rule_available"] is False
    assert api.get_overview()["data"]["stats"]["attention"] == 2


def test_action_interrupted_by_stop_is_reported(api: Api, config_file: Path, fake_bot: FakeBot):
    import asyncio
    import threading

    from supplierhub.errors import ApiError

    _start(api)
    started = threading.Event()

    async def forever() -> None:
        started.set()
        await asyncio.Event().wait()

    stopper = threading.Thread(target=lambda: (started.wait(5), api.stop_bot()))
    stopper.start()
    with pytest.raises(ApiError, match="остановился"):
        api._runtime.call(forever(), timeout=10)
    stopper.join(10)
    with pytest.raises(ApiError, match="не запущен"):
        api._runtime.call(forever())
