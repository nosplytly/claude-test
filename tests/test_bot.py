from __future__ import annotations

import json
from pathlib import Path

import pytest

from playerok_bot import bot
from playerok_bot.bot import BotError
from playerok_bot.config import Config, DeliveryConfig, PlayerokConfig, RelistConfig, TelegramConfig
from playerok_bot.lock import state_lock
from playerok_bot.telegram import Notifier

from .conftest import FlakyReplace


def _config(tmp_path: Path) -> Config:
    return Config(
        playerok=PlayerokConfig(token="tok"),
        telegram=TelegramConfig(),
        delivery=DeliveryConfig(),
        relist=RelistConfig(),
        state_file=tmp_path / "state.json",
        log_file=None,
    )


async def test_second_bot_on_the_same_state_file_is_refused(tmp_path: Path):
    # Например, `python -m playerok_bot` уже работает в этой папке, а потом открыли окно.
    other = state_lock(tmp_path / "state.json")
    assert other.acquire()
    try:
        with pytest.raises(BotError, match="уже запущен"):
            await bot.run(_config(tmp_path), notifier=Notifier(TelegramConfig()))
    finally:
        other.release()


async def test_run_flushes_pending_records_and_releases_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    flaky = FlakyReplace(monkeypatch, failing=False, only="state.json")

    async def fake_run(config, state, notifier, on_started, watch_chats):
        state.put("deliveries", "d1", status="reserved", products=["K1"])
        flaky.failing = True
        state.record("deliveries", "d1", status="delivered")
        flaky.failing = False

    monkeypatch.setattr(bot, "_run", fake_run)

    await bot.run(_config(tmp_path), notifier=Notifier(TelegramConfig()))

    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["deliveries"]["d1"]["status"] == "delivered"
    again = state_lock(tmp_path / "state.json")
    assert again.acquire()
    again.release()


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        ("forbidden", "не пускает с этого компьютера"),
        ("network", "Нет связи с Playerok"),
        ("server", "не работает"),
        ("unauthorized", "не принял токен"),
    ],
)
def test_login_errors_are_plain_russian(error: str, fragment: str):
    from PlayerokAPI.exceptions import (
        ForbiddenError,
        NetworkError,
        ServerError,
        UnauthorizedError,
    )

    url = "https://bff.playerok.com/rest-api/public/viewer"
    exc = {
        "forbidden": ForbiddenError(403, url),
        "network": NetworkError("Сетевая ошибка на GET " + url),
        "server": ServerError(502, url),
        "unauthorized": UnauthorizedError(401, url),
    }[error]

    text = bot.explain_playerok_error(exc)

    assert fragment in text
    assert "bff.playerok.com" not in text
