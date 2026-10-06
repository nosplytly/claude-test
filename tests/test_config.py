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


def test_notepad_bom_and_crlf_are_accepted(tmp_path: Path):
    # Блокнот: «UTF-8 с BOM» и переводы строк Windows.
    text = (
        '[playerok]\r\ntoken = "tok"\r\n[[delivery.rules]]\r\nmatch = "Ключ"\r\nmessage = "x"\r\n'
    )
    path = tmp_path / "config.toml"
    path.write_bytes(text.encode("utf-8-sig"))

    config = load_config(path)

    assert config.playerok.token == "tok"
    assert config.delivery.rules[0].match == "Ключ"


def test_config_not_in_utf8(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_bytes('[playerok]\ntoken = "ключ"\n'.encode("cp1251"))

    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(path)


@pytest.mark.parametrize(
    "proxy", ["http://1.2.3.4:8080", "https://user:pass@proxy.example:443", "  "]
)
def test_valid_proxies(tmp_path: Path, proxy: str):
    path = _write(
        tmp_path,
        f'[playerok]\ntoken = "t"\nproxy = "{proxy}"\n[telegram]\nproxy = "{proxy}"\n',
    )

    config = load_config(path)

    assert config.playerok.proxy == (proxy.strip() or None)
    assert config.telegram.proxy == (proxy.strip() or None)


@pytest.mark.parametrize(
    ("section", "proxy", "fragment"),
    [
        ("playerok", "1.2.3.4:8080", "должен начинаться с http://"),
        ("telegram", "1.2.3.4:8080", "должен начинаться с http://"),
        ("telegram", "ftp://1.2.3.4", "должен начинаться с http://"),
        ("playerok", "http://", "нет сервера"),
    ],
)
def test_bad_proxy_is_reported_at_load(tmp_path: Path, section: str, proxy: str, fragment: str):
    # Telegram выключен, но неверный прокси всё равно ловится сразу, а не при запуске бота.
    table = "" if section == "playerok" else f"[{section}]\n"
    path = _write(tmp_path, f'[playerok]\ntoken = "t"\n{table}proxy = "{proxy}"\n')

    with pytest.raises(ConfigError, match=fragment):
        load_config(path)


def test_socks_proxy_accepted_when_socksio_installed(tmp_path: Path):
    path = _write(tmp_path, '[playerok]\ntoken = "t"\nproxy = "socks5://1.2.3.4:1080"\n')

    assert load_config(path).playerok.proxy == "socks5://1.2.3.4:1080"


def test_socks_proxy_without_socksio_is_explained(tmp_path: Path, monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "socksio" else real(name, *a)
    )
    path = _write(
        tmp_path, '[telegram]\nproxy = "socks5://1.2.3.4:1080"\n[playerok]\ntoken = "t"\n'
    )

    with pytest.raises(ConfigError, match="SOCKS"):
        load_config(path)
