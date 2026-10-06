from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from playerok_bot import fileio
from playerok_bot.storage import State, StateError

from .conftest import FlakyReplace


def test_put_is_written_before_memory_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = State.load(tmp_path / "state.json")
    state.put("deliveries", "d1", status="reserved", products=["K1"])
    FlakyReplace(monkeypatch)

    with pytest.raises(PermissionError):
        state.put("deliveries", "d1", status="delivered")

    # Память не опередила файл: по такой записи бот ничего не делал.
    assert state.get("deliveries", "d1")["status"] == "reserved"
    on_disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert on_disk["deliveries"]["d1"]["status"] == "reserved"
    assert not list(tmp_path.glob(".state-*"))


def test_short_lock_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    flaky = FlakyReplace(monkeypatch, failing=False)
    flaky.fail_next = 3
    state = State.load(tmp_path / "state.json")

    state.put("deliveries", "d1", status="reserved")

    assert flaky.calls == 4
    assert State.load(state.path).get("deliveries", "d1")["status"] == "reserved"


def test_record_survives_failed_write_and_is_flushed_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state = State.load(tmp_path / "state.json")
    state.put("deliveries", "d1", status="reserved", products=["K1"])
    flaky = FlakyReplace(monkeypatch)

    state.record("deliveries", "d1", status="delivered")

    assert state.get("deliveries", "d1")["status"] == "delivered"
    assert state.dirty and state.failing_for() >= 0 and state.last_error is not None
    assert State.load(state.path).get("deliveries", "d1")["status"] == "reserved"
    assert state.flush() is False

    flaky.failing = False
    assert state.flush() is True
    assert not state.dirty and state.failing_for() == 0
    assert State.load(state.path).get("deliveries", "d1")["status"] == "delivered"


def test_data_is_synced_before_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    order: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(fileio.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(
        fileio.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))
    )

    State.load(tmp_path / "state.json").put("deliveries", "d1", status="reserved")

    assert order == ["fsync", "replace"]


def test_busy_file_is_not_called_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")

    def busy(self: Path, *args, **kwargs) -> str:
        raise PermissionError(13, "Отказано в доступе", str(self))

    monkeypatch.setattr(Path, "read_text", busy)

    with pytest.raises(StateError) as info:
        State.load(path)

    assert "занят" in str(info.value)
    assert "удалите" not in str(info.value)


@pytest.mark.parametrize("content", [b"{broken", b"\x00" * 64, "{}".encode("utf-16")])
def test_corrupt_file_explains_what_to_do(tmp_path: Path, content: bytes):
    path = tmp_path / "state.json"
    path.write_bytes(content)

    with pytest.raises(StateError, match=r"повреждён.*удалите"):
        State.load(path)


def test_flush_probes_after_failed_put(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = State.load(tmp_path / "state.json")
    flaky = FlakyReplace(monkeypatch)
    with pytest.raises(PermissionError):
        state.put("deliveries", "d1", status="no_rule")
    assert state.failing_for() > 0 and not state.dirty

    flaky.failing = False
    assert state.flush() is True

    assert state.failing_for() == 0
