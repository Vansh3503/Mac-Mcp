"""The pairing store: decisions persist privately and atomically; damage never locks anyone in or out."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.host.pairing import PairingStore


def fixed_clock() -> datetime:
    return datetime(2026, 10, 19, 14, 2, 11, tzinfo=UTC)


def test_decisions_round_trip(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "support" / "paired-clients.json", clock=fixed_clock)
    assert store.decision("com.anthropic.claudefordesktop") is None
    entry = store.remember("com.anthropic.claudefordesktop", "always", display="Claude")
    assert entry.decided_at == "2026-10-19T14:02:11Z"
    store.remember("com.example.evil", "denied")
    assert store.decision("com.anthropic.claudefordesktop") == "always"
    assert store.decision("com.example.evil") == "denied"
    assert [e.client for e in store.entries()] == ["com.anthropic.claudefordesktop", "com.example.evil"]
    assert store.entries()[0].display == "Claude"
    assert PairingStore(store.path).decision("com.example.evil") == "denied"  # persisted


def test_the_file_is_private(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "paired-clients.json")
    store.remember("x", "always")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["clients"]["x"]["decision"] == "always"
    assert not list(tmp_path.glob(".*.tmp"))  # no temporary file left behind


def test_forget(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "p.json")
    store.remember("x", "denied")
    assert store.forget("x") is True
    assert store.forget("x") is False
    assert store.decision("x") is None


def test_a_damaged_file_is_set_aside(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "p.json"
    path.write_text("{not json", encoding="utf-8")
    store = PairingStore(path)
    assert store.decision("x") is None
    assert (tmp_path / "p.json.corrupt").read_text(encoding="utf-8") == "{not json"
    assert "damaged" in caplog.text
    store.remember("x", "always")
    assert store.decision("x") == "always"


@pytest.mark.parametrize(
    "content",
    [
        '{"clients": []}',
        '["x"]',
        '{"version": 1, "clients": {"x": {"decision": "maybe"}, "y": "always"}}',
    ],
)
def test_malformed_entries_are_ignored(tmp_path: Path, content: str) -> None:
    path = tmp_path / "p.json"
    path.write_text(content, encoding="utf-8")
    store = PairingStore(path)
    assert store.decision("x") is None
    assert store.decision("y") is None


def test_invalid_input_is_refused(tmp_path: Path) -> None:
    store = PairingStore(tmp_path / "p.json")
    with pytest.raises(AXPilotError) as caught:
        store.remember("x", "sometimes")  # type: ignore[arg-type]
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(AXPilotError):
        store.remember("", "always")
    with pytest.raises(AXPilotError):
        store.remember("k" * 600, "always")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_an_unwritable_folder_is_reported(tmp_path: Path) -> None:
    folder = tmp_path / "locked"
    folder.mkdir()
    os.chmod(folder, 0o500)
    try:
        with pytest.raises(AXPilotError) as caught:
            PairingStore(folder / "p.json").remember("x", "always")
        assert caught.value.code is ErrorCode.COMMAND_FAILED
    finally:
        os.chmod(folder, 0o700)


def test_an_unreadable_file_asks_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = PairingStore(tmp_path / "p.json")
    store.remember("x", "always")

    def broken(*args: object, **kwargs: object) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", broken)
    assert store.decision("x") is None
