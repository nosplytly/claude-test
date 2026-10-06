from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any

import pytest

from supplierhub.api import Api
from supplierhub.server import BridgeServer


@pytest.fixture
def web(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    (root / "assets" / "fonts").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>SupplierHub</title>", encoding="utf-8")
    (root / "app.js").write_text("console.log('ok')", encoding="utf-8")
    (root / "app.css").write_text("body{}", encoding="utf-8")
    (root / "assets" / "logo.svg").write_text("<svg/>", encoding="utf-8")
    (root / "assets" / "fonts" / "inter.woff2").write_bytes(b"wOF2")
    (root / ".hidden").write_text("секрет", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("СЕКРЕТ", encoding="utf-8")
    return root


@pytest.fixture
def bridge(api: Api, web: Path):
    server = BridgeServer(api, web, token="test-token")
    server.start(poll_interval=0.01)
    yield server
    server.shutdown()


def request(
    server: BridgeServer,
    method: str,
    path: str,
    *,
    body: Any = None,
    headers: dict[str, str] | None = None,
    host: str | None = None,
    raw: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host if host is not None else f"127.0.0.1:{server.port}")
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        for key, value in (headers or {}).items():
            connection.putheader(key, value)
        if method == "POST":
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(data)))
        connection.endheaders(data if method == "POST" else None)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def call(server: BridgeServer, method: str, *args: Any, token: str = "test-token") -> Any:
    status, _, body = request(
        server, "POST", f"/api/{method}", body={"args": list(args)}, headers={"X-SH-Token": token}
    )
    return status, json.loads(body)


def test_url_contains_token(bridge: BridgeServer):
    assert bridge.url == f"http://127.0.0.1:{bridge.port}/index.html#token=test-token"


def test_random_token_by_default(api: Api, web: Path):
    first, second = BridgeServer(api, web), BridgeServer(api, web)
    try:
        assert first.token != second.token and len(first.token) >= 32
        assert first.port != second.port
    finally:
        first.shutdown()
        second.shutdown()


@pytest.mark.parametrize(
    ("path", "content_type", "body"),
    [
        ("/", "text/html; charset=utf-8", b"<!doctype html><title>SupplierHub</title>"),
        ("/index.html", "text/html; charset=utf-8", b"<!doctype html><title>SupplierHub</title>"),
        (
            "/index.html?v=1",
            "text/html; charset=utf-8",
            b"<!doctype html><title>SupplierHub</title>",
        ),
        ("/app.js", "text/javascript; charset=utf-8", b"console.log('ok')"),
        ("/app.css", "text/css; charset=utf-8", b"body{}"),
        ("/assets/logo.svg", "image/svg+xml", b"<svg/>"),
        ("/assets/fonts/inter.woff2", "font/woff2", b"wOF2"),
    ],
)
def test_static_files(bridge: BridgeServer, path: str, content_type: str, body: bytes):
    status, headers, data = request(bridge, "GET", path)

    assert status == 200
    assert headers["Content-Type"] == content_type
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert data == body


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/assets/../../secret.txt",
        "/%2e%2e/secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/..%5csecret.txt",
        "/assets\\..\\..\\secret.txt",
        "//etc/passwd",
        "/C:/Windows/win.ini",
        "/assets",
        "/assets/",
        "/.hidden",
        "/index.html%00.js",
        "/nope.html",
    ],
)
def test_traversal_and_missing_files_are_blocked(bridge: BridgeServer, path: str):
    status, _, data = request(bridge, "GET", path)

    assert status == 404
    assert "СЕКРЕТ".encode() not in data and "секрет".encode() not in data


def test_api_round_trip(bridge: BridgeServer, workdir: Path):
    status, data = call(bridge, "get_app_info")

    assert status == 200
    assert data == {
        "ok": True,
        "data": {
            "version": "1.0.0",
            "workdir": str(workdir),
            "config_exists": False,
            "mode": "window",
        },
    }


def test_api_with_arguments_and_unicode(bridge: BridgeServer):
    status, saved = call(
        bridge, "save_rule", {"match": "Ключ", "message": "Ваш ключ: {product}", "use_stock": True}
    )
    assert status == 200 and saved["ok"] is True
    assert saved["data"]["item"]["stock_file"] == "stock/klyuch.txt"

    assert call(bridge, "add_stock", 0, "А1\nБ2")[1]["data"] == {"added": 2, "count": 2}
    assert call(bridge, "get_stock", 0)[1]["data"]["items"] == ["А1", "Б2"]
    # Ошибка метода — это 200 с ok=false, а не HTTP-ошибка.
    status, error = call(bridge, "get_stock", 9)
    assert status == 200 and error["ok"] is False


@pytest.mark.parametrize("token", ["", "wrong", "test-token "])
def test_missing_or_wrong_token(bridge: BridgeServer, token: str):
    headers = {"X-SH-Token": token} if token else {}
    status, _, body = request(
        bridge, "POST", "/api/get_app_info", body={"args": []}, headers=headers
    )

    assert status == 403
    assert json.loads(body)["ok"] is False


@pytest.mark.parametrize("host", ["evil.com", "evil.com:80", "127.0.0.1", "127.0.0.1:1", ""])
def test_bad_host_is_rejected(bridge: BridgeServer, host: str):
    status, _, _ = request(bridge, "GET", "/index.html", host=host)
    assert status == 403

    status, _, _ = request(
        bridge,
        "POST",
        "/api/get_app_info",
        body={"args": []},
        headers={"X-SH-Token": "test-token"},
        host=host,
    )
    assert status == 403


def test_localhost_host_is_allowed(bridge: BridgeServer):
    status, _, _ = request(bridge, "GET", "/index.html", host=f"localhost:{bridge.port}")

    assert status == 200


def test_foreign_origin_is_rejected(bridge: BridgeServer):
    status, _, _ = request(
        bridge,
        "POST",
        "/api/get_app_info",
        body={"args": []},
        headers={"X-SH-Token": "test-token", "Origin": "http://evil.com"},
    )
    assert status == 403

    status, _, _ = request(
        bridge,
        "POST",
        "/api/get_app_info",
        body={"args": []},
        headers={"X-SH-Token": "test-token", "Origin": f"http://127.0.0.1:{bridge.port}"},
    )
    assert status == 200


@pytest.mark.parametrize("method", ["nope", "_shutdown", "_set_mode", "__init__", "get_app_info/x"])
def test_unknown_or_private_method(bridge: BridgeServer, method: str):
    status, data = call(bridge, method)

    assert status == 404
    assert data["ok"] is False


@pytest.mark.parametrize(
    "raw",
    [
        b"{not json",
        b"[]",
        b'{"args": {"a": 1}}',
        b'{"args": "x"}',
        "\udcff".encode("utf-8", "surrogatepass"),
    ],
)
def test_bad_body(bridge: BridgeServer, raw: bytes):
    status, _, body = request(
        bridge, "POST", "/api/get_app_info", raw=raw, headers={"X-SH-Token": "test-token"}
    )

    assert status == 400
    assert json.loads(body)["ok"] is False


def test_missing_args_means_no_args(bridge: BridgeServer):
    status, _, body = request(
        bridge, "POST", "/api/get_app_info", raw=b"{}", headers={"X-SH-Token": "test-token"}
    )

    assert status == 200 and json.loads(body)["ok"] is True


def test_wrong_argument_count_is_an_envelope(bridge: BridgeServer):
    status, data = call(bridge, "get_app_info", 1, 2, 3)

    assert status == 200
    assert data == {"ok": False, "error": "Неверные аргументы для get_app_info."}


def test_too_large_body(bridge: BridgeServer):
    connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=5)
    try:
        connection.putrequest("POST", "/api/add_stock", skip_host=True)
        connection.putheader("Host", f"127.0.0.1:{bridge.port}")
        connection.putheader("X-SH-Token", "test-token")
        connection.putheader("Content-Length", str(100 * 1024 * 1024))
        connection.endheaders()
        assert connection.getresponse().status == 413
    finally:
        connection.close()


def test_get_on_api_path_is_not_an_api_call(bridge: BridgeServer):
    status, _, _ = request(bridge, "GET", "/api/get_app_info")

    assert status == 404
