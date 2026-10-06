"""Открытие папок, файлов и ссылок средствами системы."""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ApiError

__all__ = ["check_https_url", "open_in_browser", "open_in_system"]


def open_in_system(path: Path) -> None:
    """Открыть папку в проводнике или файл в связанной программе."""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]  # есть только на Windows
        return
    command = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen(
        [command, str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def check_https_url(url: object) -> str:
    """Пропустить только обычные https-ссылки (без пробелов и управляющих символов)."""
    if not isinstance(url, str) or len(url) > 2048 or not url.startswith("https://"):
        raise ApiError("Можно открыть только ссылку https://")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise ApiError("Ссылка содержит недопустимые символы.")
    parts = urlsplit(url)
    if not parts.hostname or parts.username or parts.password:
        raise ApiError("Некорректная ссылка.")
    return url


def open_in_browser(url: str) -> None:
    """Открыть ссылку в браузере по умолчанию."""
    if not webbrowser.open(url):
        raise ApiError("Не удалось открыть браузер — скопируйте ссылку вручную.")
