from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from playerok_bot.config import ConfigError, load_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PLAYEROK_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_config_loads_with_env_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYEROK_TOKEN", "pk-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-token")
    path = tmp_path / "config.toml"
    shutil.copy(EXAMPLE, path)

    config = load_config(path)

    assert config.playerok.token == "pk-token"
    assert config.telegram.bot_token == "tg-token"
    assert config.telegram.chat_ids == [123456789]
    assert config.delivery.enabled and len(config.delivery.rules) == 2
    assert config.delivery.rules[0].stock_file == tmp_path / "stock" / "steam_keys.txt"
    assert config.delivery.rules[1].stock_file is None
    assert config.relist.after_sale and not config.relist.expired
    assert config.state_file == tmp_path / "state.json"
    assert config.log_file == tmp_path / "bot.log"


def test_minimal_config(tmp_path):
    config = load_config(_write(tmp_path, '[playerok]\ntoken = "t"\n'))

    assert not config.telegram.enabled
    assert not config.delivery.enabled
    assert not config.relist.enabled


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match=r"config\.example\.toml"):
        load_config(tmp_path / "config.toml")


def test_missing_token(tmp_path):
    with pytest.raises(ConfigError, match="токен Playerok"):
        load_config(_write(tmp_path, "[playerok]\n"))


def test_stock_rule_must_send_product(tmp_path):
    text = """
[playerok]
token = "t"
[[delivery.rules]]
match = "Ключ"
stock_file = "keys.txt"
message = "Спасибо!"
"""
    with pytest.raises(ConfigError, match=r"\{product\}"):
        load_config(_write(tmp_path, text))


def test_telegram_needs_chat_ids(tmp_path):
    text = '[playerok]\ntoken = "t"\n[telegram]\nenabled = true\nbot_token = "x"\n'
    with pytest.raises(ConfigError, match="chat_ids"):
        load_config(_write(tmp_path, text))


def test_wrong_type(tmp_path):
    with pytest.raises(ConfigError, match="deals_interval"):
        load_config(_write(tmp_path, '[playerok]\ntoken = "t"\ndeals_interval = "fast"\n'))


def test_syntax_error(tmp_path):
    with pytest.raises(ConfigError, match="синтаксиса"):
        load_config(_write(tmp_path, "[playerok\n"))


def test_rule_matching_is_case_insensitive_and_ordered(tmp_path):
    text = """
[playerok]
token = "t"
[[delivery.rules]]
match = "steam"
message = "first"
[[delivery.rules]]
match = "Ключ Steam"
message = "second"
"""
    config = load_config(_write(tmp_path, text))

    assert config.delivery.find_rule("Ключ STEAM Witcher").message == "first"
    assert config.delivery.find_rule("Аккаунт") is None
    assert config.delivery.find_rule(None) is None
