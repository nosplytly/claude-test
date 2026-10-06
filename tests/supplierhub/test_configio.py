from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from playerok_bot.config import ConfigError, load_config
from supplierhub.configio import NEW_CONFIG, ConfigStore, coerce_section, config_view


def _read(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_defaults_when_file_missing(workdir: Path):
    store = ConfigStore(workdir)

    view = store.view()

    assert view == NEW_CONFIG
    assert view["telegram"]["enabled"] is False
    assert view["delivery"]["enabled"] is True
    assert view["relist"]["after_sale"] is True
    assert view["telegram"]["chat_ids"] == []
    assert not store.exists()
    assert store.validation_error() is None


def test_existing_file_uses_bot_defaults_for_missing_keys(workdir: Path):
    # Бот считает отсутствующий delivery.enabled выключенным — окно должно показать так же.
    (workdir / "config.toml").write_text('[playerok]\ntoken = "t"\n', encoding="utf-8")

    view = ConfigStore(workdir).view()

    assert view["delivery"]["enabled"] is False
    assert view["relist"]["after_sale"] is False
    assert view["playerok"]["token"] == "t"


def test_view_replaces_wrong_types_with_defaults():
    raw = {
        "playerok": {"token": 5, "deals_interval": "fast", "use_websocket": "yes"},
        "telegram": {"chat_ids": [1, "x", True, 2]},
        "relist": {"match": "not a list"},
        "delivery": "oops",
    }

    view = config_view(raw, exists=True)

    assert view["playerok"] == {
        "token": "",
        "proxy": "",
        "deals_interval": 20,
        "use_websocket": True,
    }
    assert view["telegram"]["chat_ids"] == [1, 2]
    assert view["relist"]["match"] == []
    assert view["delivery"] == {"enabled": False, "mark_sent": True}


def test_save_creates_file_without_token(workdir: Path):
    store = ConfigStore(workdir)

    store.save_settings({"telegram": {"enabled": True, "bot_token": "123:abc", "chat_ids": ["42"]}})

    raw = _read(workdir / "config.toml")
    # Заглушка токена используется только для проверки и в файл не попадает.
    assert raw["playerok"]["token"] == ""
    assert raw["telegram"]["enabled"] is True
    assert raw["telegram"]["chat_ids"] == [42]
    assert raw["bot"] == {"state_file": "state.json", "log_file": "bot.log"}
    assert raw["delivery"]["enabled"] is True
    assert (workdir / "config.toml").read_text(encoding="utf-8").startswith("# ")
    # Временные файлы проверки и записи не остаются.
    assert sorted(p.name for p in workdir.iterdir()) == ["config.toml"]


def test_roundtrip_keeps_rules_bot_and_unknown_keys(workdir: Path, config_file: Path):
    original = _read(config_file)
    original_text = config_file.read_text(encoding="utf-8")
    config_file.write_text(original_text + '\n[extra]\nnote = "keep me"\n', encoding="utf-8")
    store = ConfigStore(workdir)

    view = store.view()
    view["playerok"]["deals_interval"] = 30
    view["relist"]["match"] = ["Ключ", " ", "Ключ", "Гайд"]
    store.save_settings(view)

    raw = _read(config_file)
    assert raw["delivery"]["rules"] == original["delivery"]["rules"]
    assert raw["bot"] == original["bot"]
    assert raw["extra"] == {"note": "keep me"}
    assert raw["playerok"]["deals_interval"] == 30
    assert raw["relist"]["match"] == ["Ключ", "Гайд"]
    assert store.view()["playerok"]["deals_interval"] == 30
    # Итог читается ботом.
    config = load_config(config_file)
    assert config.playerok.deals_interval == 30
    assert [rule.match for rule in config.delivery.rules] == ["Ключ Steam", "Гайд"]


def test_partial_save_changes_only_given_keys(workdir: Path, config_file: Path):
    store = ConfigStore(workdir)

    store.save_settings({"playerok": {"proxy": " http://1.2.3.4:8080 "}})

    raw = _read(config_file)
    assert raw["playerok"] == {
        "token": "tok",
        "deals_interval": 20,
        "proxy": "http://1.2.3.4:8080",
    }
    assert "relist" not in raw


def test_multiline_strings_survive_roundtrip(workdir: Path):
    store = ConfigStore(workdir)
    message = 'Строка 1\nСтрока "2" \\n с обратной косой\n\tтаб'

    store.save_rule({"match": "X", "message": message, "use_stock": False})

    assert _read(workdir / "config.toml")["delivery"]["rules"][0]["message"] == message
    assert store.rules()[0]["message"] == message


@pytest.mark.parametrize(
    ("cfg", "fragment"),
    [
        ({"playerok": {"deals_interval": 2}}, "playerok.deals_interval"),
        ({"playerok": {"deals_interval": "abc"}}, "playerok.deals_interval"),
        ({"playerok": {"use_websocket": "yes"}}, "playerok.use_websocket"),
        ({"telegram": {"chat_ids": ["abc"]}}, "telegram.chat_ids"),
        ({"telegram": {"enabled": True, "chat_ids": [1]}}, "bot_token"),
        ({"telegram": {"enabled": True, "bot_token": "x"}}, "chat_ids"),
        ({"telegram": {"messages_interval": 1}}, "telegram.messages_interval"),
        ({"relist": {"interval_minutes": 0}}, "relist.interval_minutes"),
        ({"relist": {"match": [1, 2]}}, "relist.match"),
        ({"playerok": {"token": 123}}, "playerok.token"),
    ],
)
def test_invalid_settings_are_rejected_and_file_untouched(
    workdir: Path, config_file: Path, cfg: dict, fragment: str
):
    before = config_file.read_text(encoding="utf-8")

    with pytest.raises(ConfigError) as info:
        ConfigStore(workdir).save_settings(cfg)

    assert fragment in str(info.value)
    assert config_file.read_text(encoding="utf-8") == before


def test_coercion_of_numbers_and_chat_ids():
    parsed = coerce_section(
        "telegram",
        {"chat_ids": "123, -100500 ;77 123", "messages_interval": "12,5", "proxy": None},
    )

    assert parsed == {"chat_ids": [123, -100500, 77], "messages_interval": 12.5, "proxy": ""}
    assert coerce_section("playerok", {"deals_interval": 30.0}) == {"deals_interval": 30}


def test_syntax_error_is_reported(workdir: Path):
    (workdir / "config.toml").write_text("[playerok\n", encoding="utf-8")
    store = ConfigStore(workdir)

    with pytest.raises(ConfigError, match="синтаксиса"):
        store.view()
    raw, error = store.read_raw_safe()
    assert raw == {}
    assert "синтаксиса" in error


def test_validation_error_ignores_empty_token_and_caches(workdir: Path):
    path = workdir / "config.toml"
    path.write_text('[playerok]\ntoken = ""\n', encoding="utf-8")
    store = ConfigStore(workdir)

    assert store.validation_error() is None

    path.write_text("[playerok]\ndeals_interval = 1\n", encoding="utf-8")
    error = store.validation_error()
    assert error is not None and "deals_interval" in error
    assert store.validation_error() == error
    assert not list(workdir.glob(".config-check-*"))


def test_load_requires_file_and_token(workdir: Path, monkeypatch: pytest.MonkeyPatch):
    store = ConfigStore(workdir)
    with pytest.raises(Exception, match=r"config\.toml"):
        store.load()

    (workdir / "config.toml").write_text('[playerok]\ntoken = ""\n', encoding="utf-8")
    with pytest.raises(Exception, match="токен"):
        store.load()

    monkeypatch.setenv("PLAYEROK_TOKEN", "from-env")
    assert store.token() == "from-env"
    assert store.load().playerok.token == "from-env"


def test_bot_paths(workdir: Path):
    store = ConfigStore(workdir)
    assert store.state_file() == workdir / "state.json"
    assert store.log_file() == workdir / "bot.log"

    (workdir / "config.toml").write_text(
        '[bot]\nstate_file = "data/s.json"\nlog_file = ""\n', encoding="utf-8"
    )
    assert store.state_file() == workdir / "data" / "s.json"
    assert store.log_file() is None


def test_save_rejects_non_object(workdir: Path):
    store = ConfigStore(workdir)
    with pytest.raises(Exception, match="Некорректные"):
        store.save_settings(["nope"])
    with pytest.raises(Exception, match="Некорректные"):
        store.save_settings({"telegram": "nope"})
    assert not store.exists()
