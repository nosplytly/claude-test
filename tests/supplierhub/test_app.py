from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from supplierhub import api as api_module
from supplierhub import app, paths
from supplierhub.api import Api, endpoint_names
from supplierhub.feed import LogBuffer

from .conftest import FakeBot, wait_until

ROOT = Path(__file__).resolve().parents[2]
URL = re.compile(r"(http://127\.0\.0\.1:(\d+)/index\.html#token=([\w-]+))")
#: Без системного прокси: запросы идут на 127.0.0.1.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(port: int, token: str, method: str, *args: Any) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/{method}",
        data=json.dumps({"args": list(args)}).encode(),
        headers={"Content-Type": "application/json", "X-SH-Token": token},
        method="POST",
    )
    with OPENER.open(request, timeout=5) as response:
        return json.loads(response.read())


def test_cli_browser_mode_serves_and_stops(tmp_path: Path):
    workdir = tmp_path / "новая папка"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_PROXY": "127.0.0.1,localhost"}
    env.pop("PLAYEROK_TOKEN", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "supplierhub", "--browser", "--no-open", "--workdir", str(workdir)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        line = process.stdout.readline()
        match = URL.search(line)
        assert match, f"нет адреса в выводе: {line!r}"
        port, token = int(match.group(2)), match.group(3)

        with OPENER.open(f"http://127.0.0.1:{port}/assets/logo.svg", timeout=5) as response:
            assert response.headers["Content-Type"] == "image/svg+xml"
            assert b"<svg" in response.read()

        info = _post(port, token, "get_app_info")
        assert info["ok"] is True
        assert info["data"]["mode"] == "browser"
        assert Path(info["data"]["workdir"]) == workdir.resolve()
        assert _post(port, token, "start_bot")["ok"] is False  # конфига ещё нет
        assert workdir.is_dir()
    finally:
        if sys.platform == "win32":
            process.terminate()
        else:
            process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        finally:
            process.kill()
            process.stdout.close()
            process.stderr.close()
    if sys.platform != "win32":
        assert process.returncode == 0
    # Журнал пишется в bot.log рабочей папки.
    assert "SupplierHub 1.0.0" in (workdir / "bot.log").read_text(encoding="utf-8")


def test_parse_args():
    args = app._parse_args(["--browser", "--no-open", "--port", "8765", "--workdir", "x"])
    assert (args.browser, args.no_open, args.port, args.workdir, args.debug) == (
        True,
        True,
        8765,
        "x",
        False,
    )
    assert app._parse_args([]).port == 0
    for bad in (["--port", "abc"], ["--port", "70000"]):
        with pytest.raises(SystemExit):
            app._parse_args(bad)


def test_run_browser_until_stopped(api: Api, capsys: pytest.CaptureFixture[str]):
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(app.run_browser(api, open_browser=False, stop_event=stop))
    )
    thread.start()
    output: list[str] = []
    try:
        wait_until(lambda: output.append(capsys.readouterr().out) or "token=" in "".join(output))
    finally:
        stop.set()
        thread.join(5)
    assert result == [0]
    assert api.get_app_info()["data"]["mode"] == "browser"


def test_run_browser_port_busy(api: Api, capsys):
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert app.run_browser(api, port=port, open_browser=False) == 1
    assert "порт" in capsys.readouterr().err


def test_quit_app_stops_browser_mode(api: Api, workdir: Path, monkeypatch):
    monkeypatch.setattr(api_module, "QUIT_DELAY", 0.01)
    url_file = workdir / app.INSTANCE_URL
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(app.run_browser(api, open_browser=False, url_file=url_file))
    )
    thread.start()
    try:
        wait_until(url_file.exists)
        assert url_file.read_text(encoding="utf-8").startswith("http://127.0.0.1:")
        assert api.quit_app() == {"ok": True, "data": {}}
        thread.join(5)
    finally:
        api._set_quit(None)
    assert result == [0]
    assert not url_file.exists()


def test_second_launch_opens_running_instance(tmp_path: Path, monkeypatch, capsys):
    workdir = tmp_path / "work"
    workdir.mkdir()
    first = app.ProcessLock(workdir / app.INSTANCE_LOCK)
    assert first.acquire()
    url = "http://127.0.0.1:5555/index.html#token=abc"
    (workdir / app.INSTANCE_URL).write_text(url, encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(app.webbrowser, "open", opened.append)
    monkeypatch.setattr(app, "run_browser", lambda *a, **k: pytest.fail("второй сервер"))
    monkeypatch.setattr(app, "run_window", lambda *a, **k: pytest.fail("второе окно"))
    try:
        assert app.main(["--workdir", str(workdir)]) == 0
    finally:
        first.release()

    assert opened == [url]
    assert "уже работает" in capsys.readouterr().out


def test_stale_instance_address_is_ignored(tmp_path: Path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / app.INSTANCE_URL).write_text("http://127.0.0.1:1/old", encoding="utf-8")
    calls: list[Any] = []
    monkeypatch.setattr(app, "run_browser", lambda *a, **k: calls.append(k) or 0)

    assert app.main(["--workdir", str(workdir), "--browser", "--no-open"]) == 0

    assert calls and calls[0]["url_file"] == workdir / app.INSTANCE_URL
    assert not (workdir / app.INSTANCE_URL).exists()
    # Замок снят: следующий запуск снова «первый».
    lock = app.ProcessLock(workdir / app.INSTANCE_LOCK)
    assert lock.acquire()
    lock.release()


def test_log_file_outside_workdir_is_not_written(tmp_path: Path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "config.toml").write_text('[bot]\nlog_file = "../payload.bat"\n', encoding="utf-8")
    monkeypatch.setattr(app, "run_browser", lambda *a, **k: 0)

    assert app.main(["--workdir", str(workdir), "--browser", "--no-open"]) == 0

    assert not (tmp_path / "payload.bat").exists()


# --- окно pywebview ----------------------------------------------------------------


@pytest.fixture
def fake_web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setattr(app, "web_dir", lambda: web)
    monkeypatch.setattr(app, "_storage_path", lambda: str(tmp_path / "storage"))
    return web


def _fake_webview(monkeypatch: pytest.MonkeyPatch, *, fail: Exception | None = None, show=True):
    calls: dict[str, Any] = {}
    module = ModuleType("webview")

    class Events:
        def __init__(self) -> None:
            self.shown = Hook()
            self.closing = Hook()

    class Hook:
        def __init__(self) -> None:
            self.handlers: list[Any] = []

        def __iadd__(self, handler: Any) -> Hook:
            self.handlers.append(handler)
            return self

    def expose(*functions: Any) -> None:
        calls.setdefault("exposed", []).extend(functions)

    def confirm(title: str, message: str) -> bool:
        calls.setdefault("confirm", []).append(message)
        return calls.get("answer", True)

    def destroy() -> None:
        calls["destroyed"] = True

    window = SimpleNamespace(
        events=Events(), expose=expose, create_confirmation_dialog=confirm, destroy=destroy
    )
    calls["window"] = window

    def create_window(title: str, **kwargs: Any) -> Any:
        calls["create"] = {"title": title, **kwargs}
        return window

    def start(**kwargs: Any) -> None:
        calls["start"] = kwargs
        if show:
            for handler in window.events.shown.handlers:
                handler()
        if fail is not None:
            raise fail

    module.create_window = create_window  # type: ignore[attr-defined]
    module.start = start  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview", module)
    return calls


def test_run_window_opens_branded_window(api: Api, fake_web: Path, monkeypatch):
    calls = _fake_webview(monkeypatch)

    assert app.run_window(api) is True

    create = calls["create"]
    assert create["title"] == "SupplierHub"
    assert (create["width"], create["height"]) == (1280, 820)
    assert create["min_size"] == (1040, 680)
    assert create["background_color"] == "#05070D"
    # Окну отдаются только методы контракта, а не сам объект Api.
    assert create["js_api"] is app.NO_JS_API
    assert [func.__name__ for func in calls["exposed"]] == sorted(endpoint_names())
    assert all(func.__self__ is api for func in calls["exposed"])
    assert create["url"] == str(fake_web / "index.html")
    assert calls["start"]["private_mode"] is False
    assert api.get_app_info()["data"]["mode"] == "window"


def test_closing_window_with_running_bot_asks_first(
    api: Api, fake_web: Path, monkeypatch, config_file: Path, fake_bot: FakeBot
):
    calls = _fake_webview(monkeypatch)
    app.run_window(api)
    (on_closing,) = calls["window"].events.closing.handlers

    # Бот не запущен — окно закрывается без вопросов.
    assert on_closing() is True
    assert "confirm" not in calls

    assert api.start_bot()["ok"] is True
    wait_until(lambda: api.get_overview()["data"]["bot"]["status"] == "running")
    calls["answer"] = False
    assert on_closing() is False
    assert "бот остановится" in calls["confirm"][0]
    calls["answer"] = True
    assert on_closing() is True


def test_quit_app_closes_window_and_stops_bot(
    api: Api, fake_web: Path, monkeypatch, config_file: Path, fake_bot: FakeBot
):
    calls = _fake_webview(monkeypatch)
    monkeypatch.setattr(api_module, "QUIT_DELAY", 0.01)

    def start(**kwargs: Any) -> None:
        assert api.start_bot()["ok"] is True
        wait_until(lambda: api.get_overview()["data"]["bot"]["status"] == "running")
        assert api.quit_app() == {"ok": True, "data": {}}
        assert api.get_overview()["data"]["bot"]["status"] == "stopped"
        wait_until(lambda: calls.get("destroyed", False))

    sys.modules["webview"].start = start  # type: ignore[attr-defined]
    assert app.run_window(api) is True
    assert fake_bot.cancelled.is_set()


def test_run_window_falls_back_when_gui_missing(api: Api, fake_web: Path, monkeypatch):
    _fake_webview(monkeypatch, fail=RuntimeError("нет GTK"), show=False)

    assert app.run_window(api) is False


def test_run_window_error_after_shown_does_not_fall_back(api: Api, fake_web: Path, monkeypatch):
    _fake_webview(monkeypatch, fail=RuntimeError("упало потом"), show=True)

    assert app.run_window(api) is True


def test_run_window_without_pywebview(api: Api, fake_web: Path, monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", None)  # import webview → ImportError

    assert app.run_window(api) is False


def test_run_window_without_ui_files(api: Api, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(app, "web_dir", lambda: tmp_path / "missing")

    assert app.run_window(api) is False


# --- журнал и пути -----------------------------------------------------------------


def test_setup_logging_writes_buffer_and_file(tmp_path: Path):
    buffer = LogBuffer()
    log_file = tmp_path / "logs" / "bot.log"
    root = logging.getLogger()
    level = root.level
    handlers = app.setup_logging(buffer, log_file)
    try:
        logging.getLogger("supplierhub.test").info("привет из теста")
        logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/bot1:SECRET")
    finally:
        app._remove_handlers(handlers)
        root.setLevel(level)

    messages = [item["message"] for item in buffer.since()["items"]]
    assert "привет из теста" in messages
    assert not any("SECRET" in message for message in messages)
    assert "привет из теста" in log_file.read_text(encoding="utf-8")
    assert all(handler not in root.handlers for handler in handlers)


def test_workdir_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "a" / "b"
    assert paths.resolve_workdir(str(target)) == target.resolve()
    assert target.is_dir()

    monkeypatch.chdir(tmp_path)
    assert paths.resolve_workdir(None) == tmp_path.resolve()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "dist" / "SupplierHub.exe"))
    assert paths.default_workdir() == (tmp_path / "dist").resolve()


def test_resources_exist():
    assert (paths.web_dir() / "assets" / "logo.svg").is_file()
    assert paths.icon_path() is not None
    assert paths.user_data_dir().name == "SupplierHub"
