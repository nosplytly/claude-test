"""Где лежат рабочие файлы и ресурсы приложения."""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "default_workdir",
    "icon_path",
    "is_frozen",
    "resolve_workdir",
    "resource_dir",
    "user_data_dir",
    "web_dir",
]

_PACKAGE_DIR = Path(__file__).resolve().parent


def is_frozen() -> bool:
    """True, если приложение собрано в exe (PyInstaller и подобные)."""
    return bool(getattr(sys, "frozen", False))


def default_workdir() -> Path:
    """Папка с exe в собранном приложении, иначе текущая папка."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def resolve_workdir(value: str | os.PathLike[str] | None = None) -> Path:
    """Абсолютный путь рабочей папки (config.toml, state.json, stock/); создаёт её."""
    path = Path(value).expanduser() if value else default_workdir()
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_dir() -> Path:
    """Папка пакета с web/ и assets/ — и из исходников, и внутри exe."""
    candidates = [_PACKAGE_DIR]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "supplierhub")
    return next((path for path in candidates if (path / "web").is_dir()), _PACKAGE_DIR)


def web_dir() -> Path:
    """Статика интерфейса: index.html, app.js, app.css, assets/."""
    return resource_dir() / "web"


def icon_path() -> Path | None:
    """PNG-иконка окна (нужна только GTK/QT; на Windows иконку даёт exe)."""
    for path in (resource_dir() / "assets" / "icon.png", web_dir() / "assets" / "icon-256.png"):
        if path.is_file():
            return path
    return None


def user_data_dir() -> Path:
    """Данные окна (localStorage WebView) — отдельно от рабочей папки."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "SupplierHub"
