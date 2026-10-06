"""HTTP-мост для режима браузера: статика интерфейса и POST /api/<метод>.

Слушает только 127.0.0.1. Вызовы API требуют секретный токен в заголовке
X-SH-Token (он передаётся странице во фрагменте #token=…), а запросы с чужим
Host отклоняются — защита от DNS rebinding.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .api import Api, endpoint_names

__all__ = ["BridgeServer"]

logger = logging.getLogger(__name__)

#: Предел тела запроса: загрузка склада из .txt идёт через add_stock.
MAX_BODY = 20 * 1024 * 1024

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}


class BridgeServer:
    """Локальный сервер окна для режима браузера."""

    def __init__(
        self,
        api: Api,
        web_dir: Path,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
    ) -> None:
        self.api = api
        self.web_dir = web_dir.resolve()
        self.token = token or secrets.token_urlsafe(32)
        self.methods = endpoint_names()
        self._httpd = ThreadingHTTPServer((host, port), _make_handler(self))
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        """Адрес страницы с токеном во фрагменте (фрагмент не уходит на сервер)."""
        return f"http://127.0.0.1:{self.port}/index.html#token={self.token}"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})

    def start(self, poll_interval: float = 0.25) -> None:
        """Слушать в фоновом потоке; poll_interval — как быстро сработает shutdown()."""
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": poll_interval},
            name="supplierhub-http",
            daemon=True,
        )
        self._thread.start()
        logger.info("HTTP-мост слушает 127.0.0.1:%s", self.port)

    def shutdown(self) -> None:
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(5)
        self._httpd.server_close()

    def call(self, method: str, args: list[Any]) -> dict[str, Any]:
        return getattr(self.api, method)(*args)


def _make_handler(bridge: BridgeServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "SupplierHub"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # --- GET: статика ---------------------------------------------------

        def do_GET(self) -> None:
            if not self._host_ok():
                return
            path = urlsplit(self.path).path
            target = _static_target(bridge.web_dir, path)
            if target is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Не найдено"})
                return
            try:
                body = target.read_bytes()
            except OSError:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Не найдено"})
                return
            self._send(HTTPStatus.OK, body, _content_type(target))

        # --- POST: API ------------------------------------------------------

        def do_POST(self) -> None:
            if not self._host_ok():
                return
            origin = self.headers.get("Origin")
            if origin and origin.removeprefix("http://") not in bridge.allowed_hosts:
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Нет доступа"})
                return
            token = self.headers.get("X-SH-Token", "")
            if not hmac.compare_digest(token.encode(), bridge.token.encode()):
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Нет доступа"})
                return
            path = urlsplit(self.path).path
            method = path.removeprefix("/api/") if path.startswith("/api/") else ""
            if method not in bridge.methods:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Нет такого метода"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self._send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"ok": False, "error": "Слишком большой запрос"},
                )
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, UnicodeDecodeError):
                payload = None
            args = payload.get("args", []) if isinstance(payload, dict) else None
            if not isinstance(args, list):
                self._send_json(
                    HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Некорректный запрос"}
                )
                return
            self._send_json(HTTPStatus.OK, bridge.call(method, args))

        # --- служебное -----------------------------------------------------

        def _host_ok(self) -> bool:
            if self.headers.get("Host", "") in bridge.allowed_hosts:
                return True
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Нет доступа"})
            return False

        def _send_json(self, status: HTTPStatus, data: Any) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            if status != HTTPStatus.OK:
                # Тело отклонённого запроса не прочитано — соединение не переиспользуем.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            # По умолчанию пишет в stderr, которого у оконного exe нет.
            logger.debug("HTTP %s", format % args)

    return Handler


def _static_target(root: Path, url_path: str) -> Path | None:
    """Файл из web/ по пути запроса или None (нет файла, выход за папку, каталог)."""
    path = unquote(url_path)
    if any(char in path for char in ("\x00", "\\", ":")):
        return None
    relative = path.lstrip("/") or "index.html"
    parts = relative.split("/")
    if any(part in ("..", "") or part.startswith(".") for part in parts):
        return None
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


def _content_type(path: Path) -> str:
    known = _MIME.get(path.suffix.lower())
    if known:
        return known
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"
