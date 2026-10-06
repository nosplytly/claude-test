from __future__ import annotations

import logging
from pathlib import Path

import pytest

from playerok_bot.bot import BotError
from playerok_bot.config import (
    Config,
    DeliveryConfig,
    PlayerokConfig,
    RelistConfig,
    TelegramConfig,
)
from playerok_bot.storage import StateError
from supplierhub.api import Api
from supplierhub.errors import ApiError
from supplierhub.feed import EventFeed
from supplierhub.lock import ProcessLock
from supplierhub.runtime import BotRuntime

from .conftest import FakeBot, wait_until
from .test_stats import deal


def make_config(tmp_path: Path) -> Config:
    return Config(
        playerok=PlayerokConfig(token="tok"),
        telegram=TelegramConfig(),
        delivery=DeliveryConfig(),
        relist=RelistConfig(),
        state_file=tmp_path / "state.json",
        log_file=None,
    )


@pytest.fixture
def runtime(tmp_path: Path):
    instance = BotRuntime(EventFeed(), lock_path=tmp_path / ".lock")
    yield instance
    instance.stop(timeout=5)


def test_start_running_stop(runtime: BotRuntime, fake_bot: FakeBot, tmp_path: Path):
    assert runtime.snapshot()["status"] == "stopped"

    assert runtime.start(make_config(tmp_path)) == "starting"
    wait_until(lambda: runtime.status == "running")

    snap = runtime.snapshot()
    assert snap["error"] is None
    assert snap["started_at"] is not None and snap["uptime_sec"] >= 0
    assert runtime.account() == {"id": "me-1", "username": "seller", "balance": 1234.5}
    assert runtime.watcher is fake_bot.watcher
    assert fake_bot.calls[0]["watch_chats"] is True
    # Уведомления бота попадают в ленту.
    assert runtime._feed.since()["items"][0]["title"] == "Бот запущен: seller"

    assert runtime.stop() == "stopped"
    assert fake_bot.cancelled.is_set()
    snap = runtime.snapshot()
    assert snap == {"status": "stopped", "error": None, "started_at": None, "uptime_sec": None}
    # Аккаунт и наблюдатель остаются известны после остановки.
    assert runtime.account()["username"] == "seller"
    assert runtime.watcher is fake_bot.watcher
    assert fake_bot.notifiers[0]._client.is_closed


def test_second_start_is_rejected(runtime: BotRuntime, fake_bot: FakeBot, tmp_path: Path):
    runtime.start(make_config(tmp_path))

    with pytest.raises(ApiError, match="уже запущен"):
        runtime.start(make_config(tmp_path))

    wait_until(lambda: runtime.status == "running")
    with pytest.raises(ApiError, match="уже запущен"):
        runtime.start(make_config(tmp_path))
    assert len(fake_bot.calls) == 1


def test_bot_error_sets_error_status(runtime: BotRuntime, fake_bot: FakeBot, tmp_path: Path):
    fake_bot.fail_with = BotError("Playerok не принял токен.")

    runtime.start(make_config(tmp_path))
    wait_until(lambda: runtime.status == "error")

    assert runtime.snapshot()["error"] == "Playerok не принял токен."
    assert runtime.account() is None
    # Уведомитель закрыт, даже если run() упал до входа.
    assert fake_bot.notifiers[0]._client.is_closed
    assert runtime.stop() == "error"


def test_state_error(runtime: BotRuntime, fake_bot: FakeBot, tmp_path: Path):
    fake_bot.fail_with = StateError("state.json повреждён")

    runtime.start(make_config(tmp_path))
    wait_until(lambda: runtime.status == "error")

    assert runtime.snapshot()["error"] == "state.json повреждён"


def test_unexpected_error_is_logged_with_traceback(
    runtime: BotRuntime, fake_bot: FakeBot, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    fake_bot.fail_with = RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="supplierhub.runtime"):
        runtime.start(make_config(tmp_path))
        wait_until(lambda: runtime.status == "error")

    assert runtime.snapshot()["error"] == "Неожиданная ошибка: boom"
    record = next(r for r in caplog.records if r.name == "supplierhub.runtime")
    assert record.exc_info is not None


def test_restart_after_error(runtime: BotRuntime, fake_bot: FakeBot, tmp_path: Path):
    fake_bot.fail_with = BotError("нет сети")
    runtime.start(make_config(tmp_path))
    wait_until(lambda: runtime.status == "error")

    fake_bot.fail_with = None
    runtime.start(make_config(tmp_path))
    wait_until(lambda: runtime.status == "running")
    assert runtime.snapshot()["error"] is None


def test_stop_immediately_after_start(runtime: BotRuntime, fake_bot: FakeBot, tmp_path: Path):
    runtime.start(make_config(tmp_path))

    assert runtime.stop() == "stopped"
    assert runtime.status == "stopped"


def test_bot_finishing_by_itself(runtime: BotRuntime, fake_bot: FakeBot, tmp_path: Path):
    runtime.start(make_config(tmp_path))
    wait_until(lambda: runtime.status == "running")

    fake_bot.release.set()
    wait_until(lambda: runtime.status == "stopped")


def test_slow_stop_reports_stopping(runtime: BotRuntime, fake_bot: FakeBot, tmp_path: Path):
    fake_bot.stubborn = 0.5
    runtime.start(make_config(tmp_path))
    wait_until(lambda: runtime.status == "running")

    assert runtime.stop(timeout=0.05) == "stopping"
    with pytest.raises(ApiError, match="останавливается"):
        runtime.start(make_config(tmp_path))

    wait_until(lambda: runtime.status == "stopped")


def test_restart(runtime: BotRuntime, fake_bot: FakeBot, tmp_path: Path):
    runtime.start(make_config(tmp_path))
    wait_until(lambda: runtime.status == "running")

    assert runtime.restart(make_config(tmp_path)) == "starting"
    wait_until(lambda: runtime.status == "running")
    assert len(fake_bot.calls) == 2


def test_process_lock_blocks_second_bot(fake_bot: FakeBot, tmp_path: Path):
    first = BotRuntime(EventFeed(), lock_path=tmp_path / ".lock")
    second = BotRuntime(EventFeed(), lock_path=tmp_path / ".lock")
    try:
        first.start(make_config(tmp_path))
        with pytest.raises(ApiError, match="другом окне"):
            second.start(make_config(tmp_path))
        assert second.status == "stopped"

        first.stop()
        second.start(make_config(tmp_path))
        wait_until(lambda: second.status == "running")
    finally:
        first.stop()
        second.stop()


def test_process_lock(tmp_path: Path):
    one, two = ProcessLock(tmp_path / "x.lock"), ProcessLock(tmp_path / "x.lock")

    assert one.acquire() and one.held
    assert one.acquire()  # повторно — без ошибки
    assert not two.acquire()
    one.release()
    one.release()
    assert two.acquire()
    two.release()


# --- через Api ---------------------------------------------------------------------


def test_api_start_stop_and_live_sales(api: Api, fake_bot: FakeBot, config_file: Path):
    fake_bot.watcher.recent = [deal("live-1", "PAID", price=150)]

    started = api.start_bot()
    assert started == {"ok": True, "data": {"status": "starting"}}
    wait_until(lambda: api.get_overview()["data"]["bot"]["status"] == "running")

    assert api.start_bot()["ok"] is False
    sales = api.get_sales()["data"]
    assert sales["live"] is True
    assert sales["items"][0]["deal_id"] == "live-1"
    overview = api.get_overview()["data"]
    assert overview["stats"]["sales_today"] == 1
    assert overview["account"]["username"] == "seller"
    feed = api.get_feed(0)["data"]
    assert feed["items"][0]["kind"] == "info"
    assert api.get_feed(feed["last_id"])["data"]["items"] == []

    assert api.save_config({"playerok": {"deals_interval": 25}})["data"] == {
        "saved": True,
        "restart_required": True,
    }
    # Бот получил конфиг из файла.
    assert fake_bot.calls[0]["config"].playerok.token == "tok"

    assert api.restart_bot()["data"] == {"status": "starting"}
    wait_until(lambda: api.get_overview()["data"]["bot"]["status"] == "running")
    assert fake_bot.calls[1]["config"].playerok.deals_interval == 25

    assert api.stop_bot() == {"ok": True, "data": {"status": "stopped"}}
    assert api.get_sales()["data"]["live"] is False
    assert api.stop_bot()["data"] == {"status": "stopped"}


def test_api_start_errors(api: Api, fake_bot: FakeBot, workdir: Path):
    missing = api.start_bot()
    assert missing["ok"] is False and "config.toml" in missing["error"]

    (workdir / "config.toml").write_text('[playerok]\ntoken = ""\n', encoding="utf-8")
    no_token = api.start_bot()
    assert no_token["ok"] is False and "токен" in no_token["error"]

    (workdir / "config.toml").write_text(
        '[playerok]\ntoken = "t"\ndeals_interval = 1\n', encoding="utf-8"
    )
    invalid = api.start_bot()
    assert invalid["ok"] is False and "deals_interval" in invalid["error"]
    assert api.restart_bot()["ok"] is False
    assert fake_bot.calls == []


def test_api_error_state_is_visible(api: Api, fake_bot: FakeBot, config_file: Path):
    fake_bot.fail_with = BotError("Не удалось подключиться к Playerok: 403")

    api.start_bot()
    wait_until(lambda: api.get_overview()["data"]["bot"]["status"] == "error")

    assert api.get_overview()["data"]["bot"]["error"] == "Не удалось подключиться к Playerok: 403"
