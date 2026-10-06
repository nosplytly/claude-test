from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from PlayerokAPI import GraphQLError, NetworkError, UnauthorizedError

from supplierhub import checks
from supplierhub.api import Api, endpoint_names

from .conftest import FakeBot, make_me

#: Все методы из контракта окна (spec: «API methods»).
CONTRACT = {
    "get_app_info",
    "get_overview",
    "get_feed",
    "get_logs",
    "start_bot",
    "stop_bot",
    "restart_bot",
    "get_sales",
    "get_config",
    "save_config",
    "list_rules",
    "save_rule",
    "delete_rule",
    "move_rule",
    "get_stock",
    "add_stock",
    "remove_stock_item",
    "clear_stock",
    "check_token",
    "test_telegram",
    "open_path",
    "open_url",
}

GARBAGE: list[Any] = [None, 0, -1, 10**12, 1.5, True, "", "x" * 50, [], {}, [1, "a"], {"a": [None]}]


def _exposed_like_pywebview(api: Api) -> set[str]:
    """Что увидит окно: pywebview отдаёт публичные методы и обходит публичные объекты."""
    names = set()
    for name in dir(api):
        if name.startswith("_"):
            continue
        attr = getattr(api, name)
        assert inspect.ismethod(attr), f"публичный не-метод {name} утёк бы в окно"
        names.add(name)
    return names


def test_exposed_methods_match_contract(api: Api):
    assert endpoint_names() == CONTRACT
    assert _exposed_like_pywebview(api) == CONTRACT


def test_app_info(api: Api, workdir: Path):
    assert api.get_app_info() == {
        "ok": True,
        "data": {
            "version": "1.0.0",
            "workdir": str(workdir),
            "config_exists": False,
            "mode": "window",
        },
    }
    api._set_mode("browser")
    assert api.get_app_info()["data"]["mode"] == "browser"


@pytest.mark.parametrize("method", sorted(CONTRACT - {"check_token", "test_telegram"}))
def test_envelope_never_raises(api: Api, config_file: Path, fake_bot: FakeBot, method: str):
    func = getattr(api, method)
    calls: list[tuple[Any, ...]] = [()]
    calls += [(value,) for value in GARBAGE]
    calls += [(a, b) for a in GARBAGE[:6] for b in GARBAGE[:6]]
    calls += [(1, 2, 3), (1, 2, 3, 4, 5)]
    for args in calls:
        result = func(*args)
        assert isinstance(result, dict), (method, args)
        assert set(result) in ({"ok", "data"}, {"ok", "error"}), (method, args, result)
        if not result["ok"]:
            assert isinstance(result["error"], str) and result["error"], (method, args)
        json.dumps(result)


def test_internal_errors_become_envelopes(api: Api, monkeypatch: pytest.MonkeyPatch):
    def boom() -> None:
        raise RuntimeError("неожиданно")

    monkeypatch.setattr(api._config, "exists", boom)

    result = api.get_app_info()

    assert result == {"ok": False, "error": "Внутренняя ошибка: неожиданно"}


def test_os_errors_become_envelopes(api: Api, monkeypatch: pytest.MonkeyPatch):
    def denied() -> None:
        raise PermissionError(13, "Отказано в доступе", "config.toml")

    monkeypatch.setattr(api._config, "rules", denied)

    result = api.list_rules()

    assert result["ok"] is False
    assert "config.toml" in result["error"] and "Отказано" in result["error"]


def test_all_successful_results_are_json(api: Api, config_file: Path):
    for method in ("get_app_info", "get_overview", "get_sales", "get_config", "list_rules"):
        result = getattr(api, method)()
        assert result["ok"] is True, result
        json.dumps(result)
    assert api.get_feed()["data"] == {"items": [], "last_id": 0}
    assert api.get_logs("5")["data"] == {"items": [], "last_id": 0}


def test_config_api(api: Api, workdir: Path):
    config = api.get_config()["data"]
    assert config["delivery"]["enabled"] is True

    config["playerok"]["token"] = "  secret  "
    config["telegram"]["chat_ids"] = ["111", 222]
    assert api.save_config(config) == {
        "ok": True,
        "data": {"saved": True, "restart_required": False},
    }
    saved = api.get_config()["data"]
    assert saved["playerok"]["token"] == "secret"
    assert saved["telegram"]["chat_ids"] == [111, 222]

    bad = api.save_config({"playerok": {"deals_interval": 1}})
    assert bad["ok"] is False and "deals_interval" in bad["error"]


# --- check_token ------------------------------------------------------------------


class FakeAccount:
    behaviour: ClassVar[Any] = "ok"
    seen: ClassVar[list[tuple[str | None, str | None]]] = []

    def __init__(self, token: str | None = None, *, proxy: str | None = None) -> None:
        FakeAccount.seen.append((token, proxy))

    async def __aenter__(self) -> FakeAccount:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get_me(self):
        if FakeAccount.behaviour == "ok":
            return make_me("checked", 42.0)
        if FakeAccount.behaviour == "slow":
            await asyncio.sleep(10)
        raise FakeAccount.behaviour


@pytest.fixture
def fake_account(monkeypatch: pytest.MonkeyPatch) -> type[FakeAccount]:
    FakeAccount.behaviour = "ok"
    FakeAccount.seen = []
    monkeypatch.setattr(checks, "Account", FakeAccount)
    return FakeAccount


def test_check_token_with_given_token(api: Api, fake_account: type[FakeAccount]):
    result = api.check_token("  tok  ")

    assert result == {"ok": True, "data": {"username": "checked", "balance": 42.0}}
    assert fake_account.seen == [("tok", None)]


def test_check_token_uses_saved_token_and_proxy(
    api: Api, workdir: Path, fake_account: type[FakeAccount]
):
    assert api.check_token()["ok"] is False  # токена нигде нет

    (workdir / "config.toml").write_text(
        '[playerok]\ntoken = "saved"\nproxy = "http://p:1"\n', encoding="utf-8"
    )
    assert api.check_token(None)["ok"] is True
    assert api.check_token("")["ok"] is True
    assert fake_account.seen == [("saved", "http://p:1")] * 2


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (UnauthorizedError(401, "https://playerok.com/graphql"), "не принял токен"),
        (
            GraphQLError([{"message": "no", "extensions": {"code": "UNAUTHENTICATED"}}]),
            "не принял токен",
        ),
        (GraphQLError([{"message": "rate"}], "viewer"), "viewer: rate"),
        (NetworkError("connection reset"), "Не удалось подключиться к Playerok: connection reset"),
    ],
)
def test_check_token_errors(api: Api, fake_account: type[FakeAccount], error, fragment):
    fake_account.behaviour = error

    result = api.check_token("tok")

    assert result["ok"] is False and fragment in result["error"]


def test_check_token_timeout(api: Api, fake_account: type[FakeAccount], monkeypatch):
    fake_account.behaviour = "slow"
    monkeypatch.setattr(checks, "TIMEOUT", 0.05)

    result = api.check_token("tok")

    assert result["ok"] is False and "не ответил" in result["error"]


def test_check_token_rejects_non_string(api: Api):
    assert api.check_token(123)["ok"] is False


# --- test_telegram -------------------------------------------------------------------


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"requests": [], "status": 200, "body": {"ok": True}}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if isinstance(state["status"], Exception):
            raise state["status"]
        return httpx.Response(state["status"], json=state["body"])

    real = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        state["kwargs"] = kwargs
        return real(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(checks.httpx, "AsyncClient", client)
    return state


def test_telegram_with_unsaved_settings(api: Api, telegram: dict[str, Any]):
    result = api.test_telegram(
        {"enabled": False, "bot_token": " 1:abc ", "chat_ids": ["5", 6], "proxy": "http://p:1"}
    )

    assert result == {"ok": True, "data": {"sent": True}}
    bodies = [json.loads(request.content) for request in telegram["requests"]]
    assert [body["chat_id"] for body in bodies] == [5, 6]
    assert bodies[0]["text"] == "✅ Проверка связи: SupplierHub настроен верно."
    assert telegram["requests"][0].url.path == "/bot1:abc/sendMessage"
    assert telegram["kwargs"]["proxy"] == "http://p:1"


def test_telegram_with_saved_settings(api: Api, workdir: Path, telegram: dict[str, Any]):
    missing = api.test_telegram()
    assert missing["ok"] is False and "@BotFather" in missing["error"]

    (workdir / "config.toml").write_text(
        '[telegram]\nbot_token = "9:z"\nchat_ids = []\n', encoding="utf-8"
    )
    no_chat = api.test_telegram()
    assert no_chat["ok"] is False and "chat id" in no_chat["error"]

    (workdir / "config.toml").write_text(
        '[telegram]\nbot_token = "9:z"\nchat_ids = [77]\n', encoding="utf-8"
    )
    assert api.test_telegram()["ok"] is True
    assert telegram["requests"][-1].url.path == "/bot9:z/sendMessage"


@pytest.mark.parametrize(
    ("status", "body", "fragment"),
    [
        (401, {"ok": False, "description": "Unauthorized"}, "@BotFather"),
        (400, {"ok": False, "description": "Bad Request: chat not found"}, "не найден"),
        (403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}, "/start"),
        (500, {"ok": False, "description": "Internal"}, "Internal"),
    ],
)
def test_telegram_errors_are_explained(api: Api, telegram, status, body, fragment):
    telegram["status"], telegram["body"] = status, body

    result = api.test_telegram({"bot_token": "1:a", "chat_ids": [1]})

    assert result["ok"] is False and fragment in result["error"]


def test_telegram_network_error_hides_token(api: Api, telegram):
    telegram["status"] = httpx.ConnectError("нет сети до https://api.telegram.org/bot1:SECRET/")

    result = api.test_telegram({"bot_token": "1:SECRET", "chat_ids": [1]})

    assert result["ok"] is False
    assert "Не удалось связаться с Telegram" in result["error"]
    assert "SECRET" not in result["error"]


def test_telegram_bad_settings(api: Api, telegram):
    assert api.test_telegram("nope")["ok"] is False
    bad_ids = api.test_telegram({"bot_token": "1:a", "chat_ids": ["abc"]})
    assert bad_ids["ok"] is False and "chat_ids" in bad_ids["error"]
    assert telegram["requests"] == []


# --- open_path / open_url --------------------------------------------------------------


def test_open_path(api: Api, workdir: Path, opened: dict[str, list[Any]]):
    assert api.open_path("workdir") == {"ok": True, "data": {}}
    assert api.open_path("stock")["ok"] is True
    assert (workdir / "stock").is_dir()

    no_log = api.open_path("log")
    assert no_log["ok"] is False and "ещё не создан" in no_log["error"]
    (workdir / "bot.log").write_text("x", encoding="utf-8")
    assert api.open_path("log")["ok"] is True

    assert api.open_path("../..")["ok"] is False
    assert api.open_path("C:\\Windows")["ok"] is False
    assert opened["paths"] == [workdir, workdir / "stock", workdir / "bot.log"]


def test_open_path_log_disabled(api: Api, workdir: Path):
    (workdir / "config.toml").write_text('[bot]\nlog_file = ""\n', encoding="utf-8")

    result = api.open_path("log")

    assert result["ok"] is False and "отключена" in result["error"]


@pytest.mark.parametrize(
    "url",
    [
        "http://playerok.com",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://",
        "https://user:pass@evil.com",
        "https://playerok.com/a b",
        "https://playerok.com/\nx",
        "HTTPS://playerok.com",
        None,
        123,
    ],
)
def test_open_url_rejects(api: Api, opened: dict[str, list[Any]], url):
    result = api.open_url(url)

    assert result["ok"] is False
    assert opened["urls"] == []


def test_open_url_accepts_https(api: Api, opened: dict[str, list[Any]]):
    url = "https://playerok.com/deal/abc?x=1#y"

    assert api.open_url(url) == {"ok": True, "data": {}}
    assert opened["urls"] == [url]


def test_telegram_ignores_unrelated_fields(api: Api, telegram):
    # Окно присылает весь раздел Telegram — лишние поля проверке не мешают.
    result = api.test_telegram(
        {
            "enabled": "?",
            "bot_token": "1:a",
            "chat_ids": [1],
            "messages_interval": "много",
            "notify_messages": None,
        }
    )

    assert result == {"ok": True, "data": {"sent": True}}
