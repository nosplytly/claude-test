from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from playerok_bot.config import ConfigError
from supplierhub.api import Api
from supplierhub.configio import ConfigStore, slugify
from supplierhub.errors import ApiError


def _rules_in_file(workdir: Path) -> list[dict]:
    return tomllib.loads((workdir / "config.toml").read_text(encoding="utf-8"))["delivery"]["rules"]


def _stock_rule(match: str = "Ключ Steam", **extra) -> dict:
    return {
        "match": match,
        "message": "Ключ: {product}",
        "use_stock": True,
        "stock_file": None,
        "products_per_sale": 1,
        "low_stock_alert": 0,
        **extra,
    }


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("Ключ Steam", "klyuch-steam"),
        ("ЁЖИК — щука!", "ezhik-schuka"),
        ("  Witcher 3: Wild Hunt  ", "witcher-3-wild-hunt"),
        ("物品", "tovar"),
        ("а" * 80, "a" * 40),
    ],
)
def test_slugify(text: str, slug: str):
    assert slugify(text) == slug


def test_list_rules(workdir: Path, config_file: Path):
    rules = ConfigStore(workdir).rules()

    assert rules == [
        {
            "id": 0,
            "match": "Ключ Steam",
            "message": "Ключ: {product}",
            "stock_file": "stock/keys.txt",
            "products_per_sale": 1,
            "low_stock_alert": 2,
            "stock_count": 3,
        },
        {
            "id": 1,
            "match": "Гайд",
            "message": "Ссылка: https://example.com",
            "stock_file": None,
            "products_per_sale": 1,
            "low_stock_alert": 0,
            "stock_count": None,
        },
    ]


def test_new_stock_rule_reuses_unused_slug_file(workdir: Path):
    store = ConfigStore(workdir)
    (workdir / "stock").mkdir()
    # Файл остался от удалённого правила — новое правило подключает его, товар не «теряется».
    (workdir / "stock" / "klyuch-steam.txt").write_text("OLD\n", encoding="utf-8")

    first, reused = store.save_rule_ex(_stock_rule())
    second, reused_second = store.save_rule_ex(_stock_rule("КЛЮЧ steam"))

    assert first["stock_file"] == "stock/klyuch-steam.txt"
    assert first["stock_count"] == 1 and reused is True
    # Файл уже занят первым правилом — второму достаётся новый пустой.
    assert second["stock_file"] == "stock/klyuch-steam-2.txt"
    assert second["stock_count"] == 0 and reused_second is False
    assert (workdir / "stock" / "klyuch-steam.txt").read_text(encoding="utf-8") == "OLD\n"
    assert [rule["id"] for rule in store.rules()] == [0, 1]


def test_stock_toggle_off_and_on_keeps_goods(api: Api, workdir: Path):
    created = api.save_rule(_stock_rule())["data"]["item"]
    api.add_stock(0, "KEY-1\nKEY-2")

    api.save_rule({**_stock_rule(), "use_stock": False, "message": "Скоро"}, 0)
    again = api.save_rule(_stock_rule(), 0)["data"]

    assert again["item"]["stock_file"] == created["stock_file"]
    assert again["item"]["stock_count"] == 2
    assert again["reused_stock"] is True


def test_deleted_rule_stock_is_reconnected_on_recreate(api: Api, workdir: Path):
    api.save_rule(_stock_rule())
    api.add_stock(0, "KEY-1\nKEY-2")
    api.delete_rule(0)

    recreated = api.save_rule(_stock_rule())["data"]

    assert recreated["item"]["stock_file"] == "stock/klyuch-steam.txt"
    assert recreated["item"]["stock_count"] == 2


def test_rule_without_stock(workdir: Path):
    store = ConfigStore(workdir)

    item = store.save_rule(
        {"match": "Гайд", "message": "Ссылка", "use_stock": False, "stock_file": "stock/x.txt"}
    )

    assert item["stock_file"] is None and item["stock_count"] is None
    assert "stock_file" not in _rules_in_file(workdir)[0]
    assert not (workdir / "stock").exists()


def test_edit_keeps_extra_keys_and_existing_stock_file(workdir: Path, config_file: Path):
    text = config_file.read_text(encoding="utf-8").replace(
        "low_stock_alert = 2", 'low_stock_alert = 2\ncomment = "моё"'
    )
    config_file.write_text(text, encoding="utf-8")
    store = ConfigStore(workdir)

    item = store.save_rule(
        {"match": "Ключ Steam PRO", "message": "Ваш ключ: {product}", "use_stock": True},
        0,
    )

    assert item["stock_file"] == "stock/keys.txt"
    assert item["stock_count"] == 3
    saved = _rules_in_file(workdir)[0]
    assert saved["comment"] == "моё"
    assert saved["match"] == "Ключ Steam PRO"
    assert list(saved)[:3] == ["match", "stock_file", "message"]


def test_disabling_stock_removes_path_but_keeps_file(workdir: Path, config_file: Path):
    store = ConfigStore(workdir)

    store.save_rule({"match": "Ключ Steam", "message": "Скоро", "use_stock": False}, 0)

    assert "stock_file" not in _rules_in_file(workdir)[0]
    assert (workdir / "stock" / "keys.txt").exists()


@pytest.mark.parametrize(
    "bad",
    [
        "../evil.txt",
        "..\\evil.txt",
        "stock/../config.toml",
        "stock/../../outside.txt",
        "/etc/passwd",
        "\\\\server\\share\\x.txt",
        "C:\\Windows\\win.ini",
        "C:/keys.txt",
        "other/keys.txt",
        "stock/",
        "stock/sub/",
        "stock/a\x00b.txt",
        "stock/keys.txt:stream",
        "stock/keys. ",
    ],
)
def test_stock_path_outside_stock_dir_is_rejected(workdir: Path, bad: str):
    store = ConfigStore(workdir)

    with pytest.raises(ApiError):
        store.save_rule(_stock_rule(stock_file=bad))

    assert not store.exists()
    assert not (workdir.parent / "evil.txt").exists()


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("keys.txt", "stock/keys.txt"),
        ("stock/keys.txt", "stock/keys.txt"),
        ("stock\\sub\\keys.txt", "stock/sub/keys.txt"),
        ("./stock//keys.txt", "stock/keys.txt"),
        ("Stock/keys.txt", "stock/keys.txt"),
    ],
)
def test_stock_path_inside_stock_dir_is_normalized(workdir: Path, given: str, stored: str):
    item = ConfigStore(workdir).save_rule(_stock_rule(stock_file=given))

    assert item["stock_file"] == stored
    assert (workdir / stored).is_file()


@pytest.mark.skipif(os.name == "nt", reason="симлинки на Windows требуют прав")
def test_symlink_escaping_stock_dir_is_rejected(workdir: Path, tmp_path: Path):
    (workdir / "stock").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workdir / "stock" / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ApiError):
        ConfigStore(workdir).save_rule(_stock_rule(stock_file="stock/link/keys.txt"))


def test_manual_path_outside_stock_is_refused(workdir: Path, opened: dict):
    # Путь вне stock/ вписан в config.toml вручную (например, чужой «готовый» конфиг):
    # окно такой файл не читает, не меняет, а бот с таким конфигом не запускается.
    (workdir / "victim.txt").write_text("secret\n", encoding="utf-8")
    (workdir / "config.toml").write_text(
        '[playerok]\ntoken = "tok"\n[delivery]\nenabled = true\n'
        '[[delivery.rules]]\nmatch = "A"\nstock_file = "victim.txt"\nmessage = "{product}"\n'
        '[[delivery.rules]]\nmatch = "B"\nstock_file = "/etc/hostname"\nmessage = "{product}"\n'
        '[[delivery.rules]]\nmatch = "C"\nstock_file = "stock/../config.toml"\n'
        'message = "{product}"\n',
        encoding="utf-8",
    )
    store = ConfigStore(workdir)
    api = Api(workdir, open_path=opened["paths"].append)
    try:
        for rule_id in (0, 1, 2):
            for result in (
                api.get_stock(rule_id),
                api.add_stock(rule_id, "INJECTED"),
                api.clear_stock(rule_id),
                api.remove_stock_item(rule_id, 0, "secret"),
            ):
                assert result["ok"] is False and "папке stock" in result["error"]
            assert api.list_rules()["data"]["items"][rule_id]["stock_count"] is None
        assert (workdir / "victim.txt").read_text(encoding="utf-8") == "secret\n"

        error = api.get_overview()["data"]["config_error"]
        assert error is not None and "папке stock" in error
        assert "папке stock" in api.start_bot()["error"]

        with pytest.raises(ConfigError, match="папке stock"):
            store.save_rule(_stock_rule("A", stock_file="victim.txt"), 0)
        with pytest.raises(ApiError):
            store.save_rule(_stock_rule("A", stock_file="keys/b.txt"), 0)
        assert not (workdir / "keys").exists()  # чужие папки не создаём

        # Правило можно удалить, а исправленный путь — сохранить.
        assert api.delete_rule(2)["ok"] is True
        assert api.delete_rule(1)["ok"] is True
        fixed = api.save_rule(_stock_rule("A", stock_file="stock/a.txt"), 0)
        assert fixed["ok"] is True, fixed
        assert api.get_overview()["data"]["config_error"] is None
    finally:
        api._shutdown()


def test_stale_rule_view_is_refused(api: Api, config_file: Path):
    rules = api.list_rules()["data"]["items"]
    keys, guide = rules
    # В другой вкладке правила поменяли местами — номер 0 теперь у «Гайд».
    assert api.move_rule(0, 1, keys)["ok"] is True

    stale = "Правила изменились — обновите страницу."
    assert api.clear_stock(0, keys) == {"ok": False, "error": stale}
    assert api.add_stock(0, "X", keys)["error"] == stale
    assert api.get_stock(0, keys)["error"] == stale
    assert api.remove_stock_item(0, 0, "K1", keys)["error"] == stale
    assert api.delete_rule(0, keys)["error"] == stale
    assert api.move_rule(0, 1, keys)["error"] == stale
    assert api.save_rule(_stock_rule(), 0, keys)["error"] == stale
    assert len(api.list_rules()["data"]["items"]) == 2

    # С правильным правилом (или только match) всё работает.
    assert api.get_stock(1, keys)["data"]["count"] == 3
    assert api.add_stock(1, "K4", {"match": "Ключ Steam"})["data"]["count"] == 4
    assert api.delete_rule(0, guide)["ok"] is True
    assert api.get_stock(0, "Ключ Steam")["ok"] is True


@pytest.mark.parametrize(
    ("rule", "fragment"),
    [
        ({"match": " ", "message": "x"}, "названии лота"),
        ({"match": "A", "message": "  "}, "сообщение"),
        ({"match": "A", "message": "без товара", "use_stock": True}, "{product}"),
        ({"match": "A", "message": "x", "products_per_sale": 0}, "не меньше 1"),
        ({"match": "A", "message": "x", "products_per_sale": "два"}, "целое"),
        ({"match": "A", "message": "x", "low_stock_alert": -1}, "не меньше 0"),
        ({"match": "A", "message": "x", "use_stock": "да"}, "use_stock"),
        ({"match": 5, "message": "x"}, "текст"),
        ("not a dict", "Некорректные"),
    ],
)
def test_invalid_rules(workdir: Path, rule, fragment: str):
    with pytest.raises(ApiError) as info:
        ConfigStore(workdir).save_rule(rule)

    assert fragment in str(info.value)


def test_rule_is_validated_with_whole_config(workdir: Path, config_file: Path):
    # Конфиг уже сломан в другом месте — правило не сохраняется, файл цел.
    config_file.write_text(
        config_file.read_text(encoding="utf-8").replace(
            "deals_interval = 20", "deals_interval = 1"
        ),
        encoding="utf-8",
    )
    before = config_file.read_text(encoding="utf-8")

    with pytest.raises(Exception, match="deals_interval"):
        ConfigStore(workdir).save_rule({"match": "B", "message": "x"})

    assert config_file.read_text(encoding="utf-8") == before


def test_numeric_strings_are_accepted(workdir: Path):
    item = ConfigStore(workdir).save_rule(_stock_rule(products_per_sale="2", low_stock_alert=3.0))

    assert item["products_per_sale"] == 2
    assert item["low_stock_alert"] == 3


def test_delete_rule_keeps_stock_file(workdir: Path, config_file: Path):
    store = ConfigStore(workdir)

    store.delete_rule(0)

    assert [rule["match"] for rule in store.rules()] == ["Гайд"]
    assert (workdir / "stock" / "keys.txt").exists()
    with pytest.raises(ApiError):
        store.delete_rule(5)


def test_move_rule(workdir: Path, config_file: Path):
    store = ConfigStore(workdir)

    items = store.move_rule(1, -1)
    assert [rule["match"] for rule in items] == ["Гайд", "Ключ Steam"]
    assert [rule["id"] for rule in items] == [0, 1]

    # На краях — без изменений.
    assert [r["match"] for r in store.move_rule(0, -1)] == ["Гайд", "Ключ Steam"]
    assert [r["match"] for r in store.move_rule(1, 1)] == ["Гайд", "Ключ Steam"]
    assert [r["match"] for r in store.move_rule(0, 1)] == ["Ключ Steam", "Гайд"]

    for bad in (0, 2, True, "up"):
        with pytest.raises(ApiError):
            store.move_rule(0, bad)
    with pytest.raises(ApiError):
        store.move_rule(7, 1)


@pytest.mark.parametrize("rule_id", [-1, 2, "0", None, True, 0.5])
def test_bad_rule_ids(workdir: Path, config_file: Path, rule_id):
    with pytest.raises(ApiError, match="не найдено"):
        ConfigStore(workdir).stock_for(rule_id)


# --- склад через Api ------------------------------------------------------------


def test_stock_api(api: Api, config_file: Path):
    stock = api.get_stock(0)
    assert stock == {
        "ok": True,
        "data": {"rule_id": 0, "file": "stock/keys.txt", "items": ["K1", "K2", "K3"], "count": 3},
    }

    added = api.add_stock(0, "N1\r\n\n  N2  \nmulti\\nline\n")
    assert added == {"ok": True, "data": {"added": 3, "count": 6}}
    assert api.get_stock(0)["data"]["items"][-1] == "multi\\nline"

    # Значение не совпало (бот успел выдать товар) — ничего не удаляем.
    assert api.remove_stock_item(0, 0, "K2")["data"] == {"removed": False, "count": 6}
    assert api.remove_stock_item(0, 0, "K1")["data"] == {"removed": True, "count": 5}
    assert api.remove_stock_item(0, 99, "K1")["data"] == {"removed": False, "count": 5}

    assert api.clear_stock(0)["data"] == {"removed": 5}
    assert api.get_stock(0)["data"]["count"] == 0
    assert (config_file.parent / "stock" / "keys.txt").read_text(encoding="utf-8") == ""


def test_stock_api_errors(api: Api, config_file: Path):
    no_stock = api.get_stock(1)
    assert no_stock["ok"] is False and "нет склада" in no_stock["error"]
    assert api.add_stock(0, 123)["ok"] is False
    assert api.add_stock(9, "x")["ok"] is False
    assert api.remove_stock_item(0, "first", "K1")["ok"] is False
    assert api.remove_stock_item(0, 0, 5)["ok"] is False
    assert api.clear_stock(None)["ok"] is False


def test_stock_file_not_utf8(api: Api, config_file: Path):
    (config_file.parent / "stock" / "keys.txt").write_bytes("Ключ".encode("cp1251"))

    result = api.get_stock(0)

    assert result["ok"] is False and "UTF-8" in result["error"]
    assert api.list_rules()["data"]["items"][0]["stock_count"] is None


def test_rules_api_roundtrip(api: Api, workdir: Path):
    created = api.save_rule(_stock_rule())
    assert created["ok"] is True
    assert created["data"]["item"]["stock_file"] == "stock/klyuch-steam.txt"
    assert created["data"]["restart_required"] is False

    edited = api.save_rule({**_stock_rule(), "low_stock_alert": 5}, 0)
    assert edited["data"]["item"]["low_stock_alert"] == 5
    assert api.list_rules()["data"]["items"][0]["low_stock_alert"] == 5

    assert api.delete_rule(0)["data"]["deleted"] is True
    assert api.list_rules() == {"ok": True, "data": {"items": []}}
    assert api.move_rule(0, 1)["ok"] is False
