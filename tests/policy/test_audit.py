from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.audit import REDACTED_KEYS, AuditEvent, AuditLog, redact_args

FIXED = datetime(2026, 10, 19, 12, 34, 56, 789_000, tzinfo=UTC)


def event(tool: str = "type_text", ok: bool = True, **args: object) -> AuditEvent:
    return AuditEvent(
        tool=tool,
        client="Claude",
        args=dict(args),
        ok=ok,
        error_code=None if ok else "BLOCKED_BY_POLICY",
        duration_ms=12.345,
        summary="did a thing",
    )


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ----------------------------------------------------------------------------- redaction


def test_redacts_sensitive_string_values() -> None:
    args = {
        "text": "hunter2",
        "value": "secret",
        "source": "tell app",
        "script": "do shell",
        "command": "rm -rf /",
        "input": "abc",
        "js": "alert(1)",
        "ref": "e7",
        "submit": True,
    }
    assert redact_args(args, "redacted") == {
        "text": "<redacted 7 chars>",
        "value": "<redacted 6 chars>",
        "source": "<redacted 8 chars>",
        "script": "<redacted 8 chars>",
        "command": "<redacted 8 chars>",
        "input": "<redacted 3 chars>",
        "js": "<redacted 8 chars>",
        "ref": "e7",
        "submit": True,
    }


def test_redacted_keys_match_the_contract() -> None:
    assert {
        "text",
        "value",
        "source",
        "script",
        "command",
        "input",
        "values",
        "js",
        "keys",
        "shortcut",
        "labels",
        "locs",
        "image_png_base64",
    } == REDACTED_KEYS


def test_models_and_dataclasses_are_redacted_by_their_fields() -> None:
    @dataclasses.dataclass
    class Entry:
        ref: str
        value: str

    class FieldValue(BaseModel):
        ref: str
        value: str

    args = {"fields": [FieldValue(ref="e5", value="hunter2"), Entry(ref="e6", value="secret")]}
    logged = redact_args(args, "redacted")
    assert logged == {
        "fields": [
            {"ref": "e5", "value": "<redacted 7 chars>"},
            {"ref": "e6", "value": "<redacted 6 chars>"},
        ]
    }
    assert redact_args(args, "full")["fields"][0] == {"ref": "e5", "value": "hunter2"}


def test_non_string_sensitive_values_are_kept() -> None:
    assert redact_args({"value": 0.5, "text": None}, "redacted") == {"value": 0.5, "text": None}


def test_nested_dicts_and_lists_are_redacted() -> None:
    args = {
        "fields": [{"ref": "e1", "value": "alice"}, {"ref": "e2", "value": "pa55"}],
        "options": {"text": "nested", "count": 2},
    }
    assert redact_args(args, "redacted") == {
        "fields": [
            {"ref": "e1", "value": "<redacted 5 chars>"},
            {"ref": "e2", "value": "<redacted 4 chars>"},
        ],
        "options": {"text": "<redacted 6 chars>", "count": 2},
    }


def test_everything_under_a_sensitive_key_is_redacted() -> None:
    args = {"values": {"e1": "alice", "e2": ["x", "yy"]}, "modifiers": ["cmd"]}
    assert redact_args(args, "redacted") == {
        "values": {"e1": "<redacted 5 chars>", "e2": ["<redacted 1 chars>", "<redacted 2 chars>"]},
        "modifiers": ["cmd"],
    }


KEYS = st.sampled_from(
    [*sorted(REDACTED_KEYS), "Text", "VALUE", "Script", "ref", "app", "x", "fields"]
) | st.text(max_size=8)
JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(KEYS, children, max_size=4),
    max_leaves=25,
)


def _check_redacted(original: object, logged: object, sensitive: bool) -> None:
    if isinstance(original, str):
        assert logged == (f"<redacted {len(original)} chars>" if sensitive else original)
    elif isinstance(original, dict):
        assert isinstance(logged, dict)
        for key, value in original.items():
            _check_redacted(value, logged[str(key)], sensitive or key.casefold() in REDACTED_KEYS)
    elif isinstance(original, list):
        assert isinstance(logged, list)
        for value, logged_value in zip(original, logged, strict=True):
            _check_redacted(value, logged_value, sensitive)
    else:
        assert logged == original


@given(st.dictionaries(KEYS, JSON_VALUES, max_size=5))
def test_redaction_never_leaks_a_sensitive_string(args: dict[str, object]) -> None:
    """Every string under a sensitive key, at any depth and in any letter case, is redacted;
    everything else is kept as it was."""
    logged = redact_args(args, "redacted")
    _check_redacted(args, logged, sensitive=False)
    json.dumps(logged)


def test_redaction_does_not_mutate_the_input() -> None:
    args = {"fields": [{"value": "x"}]}
    redact_args(args, "redacted")
    assert args == {"fields": [{"value": "x"}]}


def test_none_mode_drops_arguments() -> None:
    assert redact_args({"text": "x", "ref": "e1"}, "none") == {}


def test_full_mode_keeps_everything_json_safe() -> None:
    class Color(Enum):
        RED = "red"

    args = {
        "text": "hello",
        "path": Path("/tmp/x"),
        "tags": ("a", "b"),
        "image": b"\x89PNG",
        "color": Color.RED,
        "ratio": float("nan"),
    }
    out = redact_args(args, "full")
    assert out == {
        "text": "hello",
        "path": "/tmp/x",
        "tags": ["a", "b"],
        "image": "<4 bytes>",
        "color": "red",
        "ratio": "nan",
    }
    json.dumps(out, allow_nan=False)


def test_binary_is_never_logged() -> None:
    assert redact_args({"image_png": b"abc"}, "redacted") == {"image_png": "<3 bytes>"}


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(AXPilotError) as info:
        redact_args({}, "partial")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(AXPilotError):
        AuditLog(None, log_args="partial")


def test_deeply_nested_arguments_are_cut_off() -> None:
    nested: dict = {}
    cursor = nested
    for _ in range(100):
        cursor["child"] = {}
        cursor = cursor["child"]
    json.dumps(redact_args(nested, "redacted"))
    json.dumps(redact_args(nested, "full"))


# ------------------------------------------------------------------------------ the file


def test_record_writes_one_json_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, clock=lambda: FIXED)
    log.record(event(text="hunter2", ref="e7"))
    assert read_lines(path) == [
        {
            "ts": "2026-10-19T12:34:56.789Z",
            "tool": "type_text",
            "client": "Claude",
            "ok": True,
            "error_code": None,
            "duration_ms": 12.3,
            "summary": "did a thing",
            "args": {"text": "<redacted 7 chars>", "ref": "e7"},
        }
    ]


def test_timestamps_are_utc(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    tokyo = timezone(timedelta(hours=9))
    log = AuditLog(path, clock=lambda: datetime(2026, 1, 1, 9, 0, tzinfo=tokyo))
    log.record(event())
    assert read_lines(path)[0]["ts"] == "2026-01-01T00:00:00.000Z"


def test_default_clock_produces_iso_utc(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record(event())
    ts = read_lines(path)[0]["ts"]
    assert ts.endswith("Z")
    parsed = datetime.fromisoformat(ts)
    assert parsed.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - parsed) < timedelta(minutes=5)


def test_appends_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record(event("click"))
    AuditLog(path).record(event("scroll"))
    assert [e["tool"] for e in read_lines(path)] == ["click", "scroll"]


def test_unicode_is_written_as_utf8(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path, log_args="full").record(event(text="café ⌘"))
    assert "café ⌘" in path.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_file_is_created_0600_in_a_private_directory(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "nested" / "audit.jsonl"
    old_umask = os.umask(0)
    try:
        AuditLog(path).record(event())
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_existing_file_permissions_are_tightened(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text("")
    path.chmod(0o644)
    AuditLog(path).record(event())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_disabled_log_records_nothing(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, enabled=False)
    log.record(event())
    assert not path.exists()
    assert log.tail() == []


def test_failed_calls_are_recorded(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(event("click", ok=False))
    entry = read_lines(path)[0]
    assert entry["ok"] is False
    assert entry["error_code"] == "BLOCKED_BY_POLICY"


def test_non_finite_duration_is_written_as_null(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    bad = event()
    bad.duration_ms = float("inf")
    log.record(bad)
    assert read_lines(path)[0]["duration_ms"] is None


# ---------------------------------------------------------------------------- I/O errors


def test_io_errors_never_raise_and_warn_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("")
    log = AuditLog(blocker / "audit.jsonl")
    log.record(event("click"))
    log.record(event("scroll"))
    err = capsys.readouterr().err
    assert err.count("cannot write the audit log") == 1
    assert [e["tool"] for e in log.tail()] == ["click", "scroll"]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="root ignores permissions")
def test_unwritable_file_does_not_raise(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text("")
    path.chmod(0o400)
    AuditLog(path).record(event())
    assert "cannot write" in capsys.readouterr().err


def test_closed_stderr_is_tolerated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Closed:
        def write(self, _: str) -> int:
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stderr", Closed())
    blocker = tmp_path / "file"
    blocker.write_text("")
    AuditLog(blocker / "audit.jsonl").record(event())


# ----------------------------------------------------------------------------------- tail


def test_tail_returns_the_last_entries_oldest_first(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(10):
        log.record(event(f"tool{i}"))
    assert [e["tool"] for e in log.tail(3)] == ["tool7", "tool8", "tool9"]
    assert len(log.tail()) == 10
    assert log.tail(0) == []


def test_tail_reads_history_from_previous_runs(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record(event("earlier"))
    assert [e["tool"] for e in AuditLog(path).tail()] == ["earlier"]


def test_tail_reads_large_files_backwards(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, log_args="full", max_bytes=None)
    for i in range(500):
        log.record(event(f"tool{i}", text="x" * 100))
    tail = log.tail(50)
    assert len(tail) == 50
    assert tail[0]["tool"] == "tool450"
    assert tail[-1]["tool"] == "tool499"


def test_tail_skips_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(event("first"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n[1, 2]\n\n")
    log.record(event("second"))
    assert [e["tool"] for e in log.tail()] == ["first", "second"]


def test_tail_of_a_missing_file(tmp_path: Path) -> None:
    assert AuditLog(tmp_path / "missing.jsonl").tail() == []


def test_memory_only_log(tmp_path: Path) -> None:
    log = AuditLog(None, memory_size=3)
    for i in range(5):
        log.record(event(f"tool{i}", text="secret"))
    tail = log.tail()
    assert [e["tool"] for e in tail] == ["tool2", "tool3", "tool4"]
    assert tail[0]["args"] == {"text": "<redacted 6 chars>"}
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------------------- rotation


def test_rotates_when_the_file_grows_too_large(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path, max_bytes=600)
    for i in range(10):
        log.record(event(f"tool{i}"))
    backup = tmp_path / "audit.jsonl.1"
    assert backup.exists()
    assert path.stat().st_size <= 600
    current = [e["tool"] for e in read_lines(path)]
    previous = [e["tool"] for e in read_lines(backup)]
    assert current[-1] == "tool9"
    assert previous + current == [f"tool{i}" for i in range(10)][-len(previous + current) :]
    assert [e["tool"] for e in log.tail(len(previous) + len(current))] == previous + current
