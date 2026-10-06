"""Запуск SupplierHub: окно pywebview, а если оно недоступно — режим браузера."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import socket
import sys
import threading
import webbrowser
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import __version__
from .api import LOCK_NAME, Api
from .configio import ConfigStore
from .feed import EventFeed, LogBuffer
from .paths import icon_path, resolve_workdir, user_data_dir, web_dir
from .runtime import BotRuntime
from .server import BridgeServer

__all__ = ["main", "run_browser", "run_window", "setup_logging"]

logger = logging.getLogger("supplierhub")

WINDOW_TITLE = "SupplierHub"
WINDOW_SIZE = (1280, 820)
WINDOW_MIN_SIZE = (1040, 680)
WINDOW_BACKGROUND = "#05070D"
#: Постоянный порт встроенного сервера окна: от адреса страницы зависит
#: localStorage (последняя открытая вкладка и т. п.).
WINDOW_HTTP_PORT = 42017
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_NOISY_LOGGERS = ("httpx", "httpcore", "websockets", "asyncio")


def main(argv: list[str] | None = None) -> int:
    _fix_std_streams()
    args = _parse_args(argv)
    try:
        workdir = resolve_workdir(args.workdir)
    except OSError as exc:
        print(f"Не удалось открыть рабочую папку {args.workdir}: {exc}", file=sys.stderr)
        return 2

    logs = LogBuffer()
    handlers = setup_logging(logs, ConfigStore(workdir).log_file(), debug=args.debug)
    logger.info("SupplierHub %s, рабочая папка: %s", __version__, workdir)
    feed = EventFeed()
    runtime = BotRuntime(feed, lock_path=workdir / LOCK_NAME)
    api = Api(workdir, feed=feed, logs=logs, runtime=runtime)
    try:
        if not args.browser:
            if run_window(api, debug=args.debug):
                return 0
            logger.warning("Окно приложения не запустилось — открываю SupplierHub в браузере")
        return run_browser(api, port=args.port, open_browser=not args.no_open)
    finally:
        runtime.stop()
        logger.info("SupplierHub закрыт")
        _remove_handlers(handlers)


def setup_logging(
    buffer: LogBuffer, log_file: Path | None, *, debug: bool = False
) -> list[logging.Handler]:
    """Корневой логгер → журнал окна, файл с ротацией и консоль. Возвращает обработчики."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    handlers: list[logging.Handler] = [buffer]
    file_error: OSError | None = None
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8", delay=True
            )
        except OSError as exc:
            file_error = exc
        else:
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.DEBUG if debug else logging.WARNING)
        console.setFormatter(formatter)
        handlers.append(console)
    for handler in handlers:
        root.addHandler(handler)
    # Сетевые библиотеки на INFO пишут каждый запрос (а httpx — с токеном Telegram в адресе).
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if file_error is not None:
        logger.warning("Не удалось открыть файл журнала %s: %s", log_file, file_error)
    return handlers


def run_window(api: Api, *, debug: bool = False) -> bool:
    """Открыть окно pywebview и ждать его закрытия.

    False — окно запустить не удалось (нет pywebview, WebView2, GTK/QT…),
    тогда вызывающий переходит в режим браузера.
    """
    index = web_dir() / "index.html"
    if not index.is_file():
        logger.error("Не найден интерфейс приложения: %s", index)
        return False
    try:
        import webview
    except Exception as exc:
        logger.warning("pywebview недоступен: %s", exc)
        return False
    if sys.platform == "win32" and not _webview2_ready():
        logger.warning(
            "Не найден Microsoft Edge WebView2 Runtime — установите его с сайта Microsoft, "
            "чтобы SupplierHub открывался в своём окне."
        )
        return False

    _set_windows_app_id()
    api._set_mode("window")
    shown = threading.Event()
    try:
        window = webview.create_window(
            WINDOW_TITLE,
            url=str(index),
            js_api=api,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=WINDOW_MIN_SIZE,
            background_color=WINDOW_BACKGROUND,
        )
        window.events.shown += lambda: shown.set()
        icon = icon_path()
        webview.start(
            debug=debug,
            private_mode=False,
            storage_path=_storage_path(),
            http_port=_port_or_random(WINDOW_HTTP_PORT),
            icon=str(icon) if icon else None,
        )
    except Exception as exc:
        if shown.is_set():
            logger.exception("Окно приложения закрылось из-за ошибки")
            return True
        logger.warning("Не удалось открыть окно: %s", exc, exc_info=debug)
        return False
    return True


def run_browser(
    api: Api,
    *,
    port: int = 0,
    open_browser: bool = True,
    stop_event: threading.Event | None = None,
) -> int:
    """Режим браузера: локальный HTTP-мост, работает до Ctrl+C / SIGTERM."""
    api._set_mode("browser")
    try:
        server = BridgeServer(api, web_dir(), port=port)
    except OSError as exc:
        logger.error("Не удалось занять порт %s: %s", port, exc)
        print(f"Не удалось занять порт {port}: {exc}", file=sys.stderr)
        return 1
    server.start()
    print(f"SupplierHub работает: {server.url}", flush=True)
    print("Чтобы закрыть SupplierHub, нажмите Ctrl+C.", flush=True)
    if open_browser:
        webbrowser.open(server.url)

    stop = stop_event or threading.Event()
    previous = _install_signal_handlers(stop)
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        _restore_signal_handlers(previous)
        server.shutdown()
    return 0


# --- мелочи -------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="supplierhub", description="SupplierHub — приложение для бота Playerok"
    )
    parser.add_argument(
        "--workdir",
        help="рабочая папка с config.toml, state.json и stock/ (по умолчанию — папка программы)",
    )
    parser.add_argument(
        "--browser", action="store_true", help="открыть в браузере вместо отдельного окна"
    )
    parser.add_argument(
        "--no-open", action="store_true", help="не открывать браузер, только напечатать адрес"
    )
    parser.add_argument(
        "--port", type=_port, default=0, help="порт режима браузера (по умолчанию любой свободный)"
    )
    parser.add_argument(
        "--debug", action="store_true", help="подробный журнал и инструменты разработчика"
    )
    return parser.parse_args(argv)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("порт должен быть числом") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("порт должен быть от 0 до 65535")
    return port


def _fix_std_streams() -> None:
    """У оконного exe нет консоли (stdout/stderr = None), а консоль Windows
    может не уметь в эмодзи — лучше «?», чем падение."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
        elif hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(errors="replace")


def _remove_handlers(handlers: list[logging.Handler]) -> None:
    root = logging.getLogger()
    for handler in handlers:
        root.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()


def _install_signal_handlers(stop: threading.Event) -> dict[int, Any]:
    previous: dict[int, Any] = {}
    if threading.current_thread() is not threading.main_thread():
        return previous
    for name in ("SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        with contextlib.suppress(OSError, ValueError):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
    return previous


def _restore_signal_handlers(previous: dict[int, Callable[..., Any] | int | None]) -> None:
    for signum, handler in previous.items():
        with contextlib.suppress(OSError, ValueError, TypeError):
            signal.signal(signum, handler)  # type: ignore[arg-type]


def _port_or_random(preferred: int) -> int:
    """Предпочтительный порт, если свободен, иначе любой свободный."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return int(sock.getsockname()[1])
    return preferred


def _storage_path() -> str | None:
    """Папка данных WebView (localStorage); None — оставить по умолчанию."""
    path = user_data_dir() / "webview"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return str(path) if os.access(path, os.W_OK) else None


def _webview2_ready() -> bool:
    """Есть ли Edge WebView2: без него pywebview откроет древний движок IE."""
    try:
        from webview.platforms import winforms
    except Exception as exc:
        logger.warning("Не удалось загрузить оконную библиотеку Windows: %s", exc)
        return False
    return getattr(winforms, "renderer", "") != "mshtml"


def _set_windows_app_id() -> None:
    """Своя иконка и группа на панели задач Windows вместо значка Python."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SupplierHub.App")  # type: ignore[attr-defined]
