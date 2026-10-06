"""Межпроцессная блокировка: один бот на один файл состояния."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import BinaryIO

__all__ = ["ProcessLock", "state_lock"]


class ProcessLock:
    """Неблокирующий замок на файле.

    Два бота на одном state.json выдали бы товар дважды, поэтому второй
    бот (второе окно SupplierHub или `python -m playerok_bot` в той же папке)
    не должен запускаться. Замок снимается сам, если процесс упал.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None
        self._guard = threading.Lock()

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """Взять замок. False — его держит другой процесс (или другой объект)."""
        with self._guard:
            if self._handle is not None:
                return True
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            try:
                _lock(handle)
            except OSError:
                handle.close()
                return False
            self._handle = handle
            return True

    def release(self) -> None:
        with self._guard:
            handle, self._handle = self._handle, None
            if handle is None:
                return
            try:
                _unlock(handle)
            except OSError:
                pass
            finally:
                handle.close()


def state_lock(state_file: Path) -> ProcessLock:
    """Замок бота на файл состояния: `state.json` → `.supplierhub-state.json.lock` рядом."""
    return ProcessLock(state_file.with_name(f".supplierhub-{state_file.name}.lock"))


if os.name == "nt":
    import msvcrt

    def _lock(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
