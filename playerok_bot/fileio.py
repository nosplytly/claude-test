"""Надёжная работа с файлами бота: state.json, склад, config.toml.

На Windows подменить или прочитать файл не выходит (PermissionError), пока его
коротко держит открытым другая программа: окно приложения, антивирус,
OneDrive, индексатор. Поэтому запись и чтение повторяются ещё ~2 секунды.
Перед подменой данные сбрасываются на диск (fsync): иначе после выключения
света файл мог бы остаться нужного размера, но из одних нулевых байтов.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path

__all__ = ["atomic_write_text", "read_text", "replace_with_retry"]

#: Паузы между повторами при PermissionError (в сумме около 2 с).
RETRY_DELAYS: tuple[float, ...] = (0.025, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.8)


def replace_with_retry(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """os.replace, переживающий короткую блокировку файла другой программой."""
    for delay in RETRY_DELAYS:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(src, dst)


def read_text(path: Path, encoding: str = "utf-8") -> str:
    """Прочитать текстовый файл, повторяя попытку, пока он занят."""
    for delay in RETRY_DELAYS:
        try:
            return path.read_text(encoding=encoding)
        except PermissionError:
            time.sleep(delay)
    return path.read_text(encoding=encoding)


def atomic_write_text(path: Path, text: str, *, prefix: str, newline: str | None = None) -> None:
    """Записать файл целиком: временный файл → fsync → подмена.

    При любом сбое старый файл остаётся целым, а временный удаляется.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(tmp, path)
    except BaseException:
        # Удалить временный файл — забота второстепенная: её сбой не должен
        # подменить настоящую ошибку.
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)
        raise
