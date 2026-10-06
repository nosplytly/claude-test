from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from PlayerokAPI import Deal

from supplierhub.api import Api
from supplierhub.stats import (
    DEAL_LABELS,
    DELIVERY_LABELS,
    HistoryReader,
    build_sales,
    compute_stats,
    local_date,
)

NOW = datetime.now(UTC)
OLD = NOW - timedelta(days=2)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def deal(
    deal_id: str,
    status: str,
    *,
    created: datetime | None = NOW,
    price: float = 100,
    name: str = "Ключ Steam",
    buyer: str = "ivan",
    chat_id: str | None = "c1",
    direction: str = "OUT",
) -> Deal:
    return Deal.from_dict(
        {
            "id": deal_id,
            "status": status,
            "direction": direction,
            "createdAt": iso(created) if created else None,
            "item": {"id": f"i-{deal_id}", "name": name, "price": price},
            "user": {"id": "u", "username": buyer},
            "chat": {"id": chat_id} if chat_id else None,
        }
    )


def history() -> dict:
    return {
        "d1": {
            "status": "delivered",
            "item": "Ключ Steam",
            "buyer": "ivan",
            "price": 100,
            "chat_id": "c1",
            "products": ["KEY-1"],
            "delivered_at": iso(NOW),
            "at": iso(NOW),
        },
        "h-nostock": {"status": "no_stock", "item": "Гайд", "price": 50, "at": iso(NOW)},
        "h-failed": {
            "status": "failed",
            "item": "Ключ",
            "price": 70,
            "error": "чат недоступен",
            "attempts": 5,
            "products": ["KEY-9"],
            "at": iso(OLD),
        },
        "h-skipped": {
            "status": "skipped",
            "reason": "оплачена до первого запуска бота",
            "price": 10,
            "at": iso(NOW),
        },
        "h-old": {
            "status": "delivered",
            "price": 30,
            "delivered_at": iso(OLD),
            "at": iso(OLD),
        },
    }


def recent() -> list[Deal]:
    return [
        deal("d1", "PAID", price=200),
        deal("r-confirmed", "CONFIRMED", price=300),
        deal("r-refund", "ROLLED_BACK", price=1000),
        deal("r-old", "SENT", created=OLD, price=999),
        deal("r-pending", "PENDING", price=5),
        deal("r-purchase", "PAID", direction="IN", price=5),
    ]


def test_build_sales_merges_and_dedupes():
    sales = build_sales(recent(), history())

    ids = [sale["deal_id"] for sale in sales]
    assert len(ids) == len(set(ids))
    assert set(ids) == {
        "d1",
        "r-confirmed",
        "r-refund",
        "r-old",
        "r-pending",
        "h-nostock",
        "h-failed",
        "h-skipped",
        "h-old",
    }
    by_id = {sale["deal_id"]: sale for sale in sales}

    live = by_id["d1"]
    assert live["status"] == "PAID" and live["status_label"] == "Оплачена"
    assert live["price"] == 200.0
    assert live["url"] == "https://playerok.com/deal/d1"
    assert live["chat_url"] == "https://playerok.com/chats/c1"
    assert live["delivery"]["status"] == "delivered"
    assert live["delivery"]["label"] == "Выдано"
    assert live["delivery"]["products"] == ["KEY-1"]

    stored = by_id["h-failed"]
    assert stored["status"] is None
    assert stored["status_label"] == "Нет данных"
    assert stored["url"] == "https://playerok.com/deal/h-failed"
    assert stored["chat_url"] is None
    assert stored["delivery"]["error"] == "чат недоступен"
    assert stored["delivery"]["label"] == "Ошибка выдачи"

    assert by_id["r-refund"]["delivery"] is None
    assert by_id["h-skipped"]["delivery"]["reason"] == "оплачена до первого запуска бота"


def test_build_sales_sorted_newest_first_and_limited():
    many = {
        f"h{n}": {"status": "delivered", "at": iso(OLD + timedelta(minutes=n))} for n in range(250)
    }
    sales = build_sales(None, many)

    assert len(sales) == 200
    assert sales[0]["deal_id"] == "h249"
    stamps = [datetime.fromisoformat(sale["created_at"]) for sale in sales]
    assert stamps == sorted(stamps, reverse=True)


def test_sales_without_dates_go_last():
    sales = build_sales(
        [deal("x", "PAID", created=None)], {"y": {"status": "delivered", "at": iso(NOW)}}
    )

    assert [sale["deal_id"] for sale in sales] == ["y", "x"]


@pytest.mark.parametrize(("status", "label"), sorted(DEAL_LABELS.items()))
def test_deal_labels(status: str, label: str):
    assert build_sales([deal("x", status)], {})[0]["status_label"] == label


@pytest.mark.parametrize(("status", "label"), sorted(DELIVERY_LABELS.items()))
def test_delivery_labels(status: str, label: str):
    sale = build_sales(None, {"x": {"status": status, "at": iso(NOW)}})[0]

    assert sale["delivery"]["label"] == label


def test_compute_stats_from_live_and_history():
    hist = history()
    stats = compute_stats(build_sales(recent(), hist), hist, stock_total=7)

    # Сегодня: d1 (PAID, 200) и r-confirmed (300) из опроса + h-nostock (50) из истории.
    # Возврат, вчерашняя, неоплаченная, пропущенная и покупка не считаются.
    assert stats == {
        "sales_today": 3,
        "revenue_today": 550.0,
        "delivered_today": 1,
        "attention": 2,
        "stock_total": 7,
    }


def test_compute_stats_history_only():
    hist = history()
    stats = compute_stats(build_sales(None, hist), hist, stock_total=0)

    # Без бота — только история: d1 (100) и h-nostock (50) сегодня.
    assert stats["sales_today"] == 2
    assert stats["revenue_today"] == 150.0
    assert stats["delivered_today"] == 1
    assert stats["attention"] == 2


def test_local_date():
    assert local_date(iso(NOW)) == NOW.astimezone().date()
    assert local_date("2026-01-02T03:04:05Z") is not None
    assert local_date("мусор") is None
    assert local_date(None) is None
    assert local_date(datetime(2026, 1, 2, 12, 0)) == datetime(2026, 1, 2).date()


def test_history_reader_cache_and_corruption(tmp_path: Path):
    path = tmp_path / "state.json"
    reader = HistoryReader()
    assert reader.read(path) == {}

    path.write_text(
        json.dumps({"deliveries": {"a": {"status": "delivered"}, "b": 5}}), encoding="utf-8"
    )
    assert reader.read(path) == {"a": {"status": "delivered"}}

    # Файл как раз подменяется ботом и битый — показываем прошлое удачное чтение.
    path.write_text("{битый", encoding="utf-8")
    assert reader.read(path) == {"a": {"status": "delivered"}}
    assert HistoryReader().read(path) == {}

    path.write_text(json.dumps(["не словарь"]), encoding="utf-8")
    assert reader.read(path) == {}


# --- через Api ----------------------------------------------------------------------


def test_overview_with_fake_state_and_watcher(api: Api, workdir: Path, config_file: Path):
    (workdir / "state.json").write_text(json.dumps({"deliveries": history()}), encoding="utf-8")
    api._runtime._on_started(
        SimpleNamespace(id="me", username="seller", balance=SimpleNamespace(available=12.5)),
        SimpleNamespace(recent=recent()),
    )

    data = api.get_overview()["data"]

    assert data["stats"] == {
        "sales_today": 3,
        "revenue_today": 550.0,
        "delivered_today": 1,
        "attention": 2,
        "stock_total": 3,
    }
    assert data["account"].pop("balance_at") is not None
    assert data["account"] == {"id": "me", "username": "seller", "balance": 12.5}
    assert data["bot"] == {
        "status": "stopped",
        "error": None,
        "started_at": None,
        "uptime_sec": None,
        "restart_required": False,
        "problem": None,
        "last_poll_at": None,
    }
    assert data["low_stock"] == []
    assert data["features"] == {
        "delivery": True,
        "telegram": False,
        "relist_after_sale": False,
        "relist_expired": False,
    }
    assert data["setup"] == {"token": True, "telegram": False, "rules": True}
    assert data["config_error"] is None


def test_overview_low_stock_and_shared_files(api: Api, workdir: Path, config_file: Path):
    text = config_file.read_text(encoding="utf-8") + (
        '\n[[delivery.rules]]\nmatch = "Ещё"\nstock_file = "stock/keys.txt"\n'
        'message = "{product}"\nlow_stock_alert = 5\n'
        '\n[[delivery.rules]]\nmatch = "Пусто"\nstock_file = "stock/empty.txt"\n'
        'message = "{product}"\n'
    )
    config_file.write_text(text, encoding="utf-8")

    data = api.get_overview()["data"]

    # Один файл на два правила считается один раз.
    assert data["stats"]["stock_total"] == 3
    assert data["low_stock"] == [
        {"rule_id": 2, "match": "Ещё", "count": 3, "threshold": 5},
        {"rule_id": 3, "match": "Пусто", "count": 0, "threshold": 0},
    ]


def test_overview_reports_config_errors(api: Api, workdir: Path):
    (workdir / "config.toml").write_text("[playerok\n", encoding="utf-8")
    assert "синтаксиса" in api.get_overview()["data"]["config_error"]

    (workdir / "config.toml").write_text(
        '[telegram]\nenabled = true\nbot_token = "x"\nchat_ids = []\n', encoding="utf-8"
    )
    data = api.get_overview()["data"]
    assert "chat_ids" in data["config_error"]
    assert data["setup"] == {"token": False, "telegram": False, "rules": False}


def test_overview_setup_telegram(api: Api, workdir: Path, monkeypatch: pytest.MonkeyPatch):
    (workdir / "config.toml").write_text(
        '[telegram]\nenabled = true\nbot_token = ""\nchat_ids = [1]\n', encoding="utf-8"
    )
    assert api.get_overview()["data"]["setup"]["telegram"] is False

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:a")
    monkeypatch.setenv("PLAYEROK_TOKEN", "t")
    setup = api.get_overview()["data"]["setup"]
    assert setup["telegram"] is True and setup["token"] is True


def test_get_sales_history_only(api: Api, workdir: Path, config_file: Path):
    (workdir / "state.json").write_text(json.dumps({"deliveries": history()}), encoding="utf-8")

    data = api.get_sales()["data"]

    assert data["live"] is False
    assert {sale["deal_id"] for sale in data["items"]} == set(history())
    assert all(sale["status"] is None for sale in data["items"])
