from __future__ import annotations

import os
import stat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from axpilot.clients import fileops
from axpilot.clients.fileops import (
    DEFAULT_FILE_MODE,
    atomic_write,
    backup_file,
    display_path,
    read_config_text,
)
from axpilot.errors import AXPilotError, ErrorCode


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_read_missing_file(home: Path) -> None:
    snap = read_config_text(home / "missing.json")
    assert snap.text is None
    assert not snap.exists
    assert snap.is_blank


def test_read_keeps_mode_bom_and_newlines(home: Path) -> None:
    path = home / "c.json"
    path.write_bytes(b'\xef\xbb\xbf{\r\n  "a": 1\r\n}\r\n')
    path.chmod(0o600)
    snap = read_config_text(path)
    assert snap.text == '{\r\n  "a": 1\r\n}\r\n'
    assert snap.bom is True
    assert snap.newline == "\r\n"
    assert snap.mode == 0o600
    assert snap.encode('{\n  "a": 2\n}\n') == b'\xef\xbb\xbf{\r\n  "a": 2\r\n}\r\n'


def test_read_directory_is_invalid_argument(home: Path) -> None:
    folder = home / "dir.json"
    folder.mkdir()
    with pytest.raises(AXPilotError) as info:
        read_config_text(folder)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_read_non_utf8_is_invalid_argument(home: Path) -> None:
    path = home / "latin1.json"
    path.write_bytes(b'{"a": "\xe9"}')
    with pytest.raises(AXPilotError) as info:
        read_config_text(path)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "UTF-8" in info.value.message


def test_read_follows_symlinks(home: Path) -> None:
    real = home / "dotfiles" / "mcp.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = home / "mcp.json"
    link.symlink_to(real)
    assert read_config_text(link).path == real


def test_backup_name_mode_and_content(home: Path, clock: Callable[[], datetime]) -> None:
    path = home / "config.json"
    path.write_text('{"secret": 1}')
    path.chmod(0o600)
    backup = backup_file(path, clock)
    assert backup.name == "config.json.bak-20261019-093015"
    assert backup.read_text() == '{"secret": 1}'
    assert mode_of(backup) == 0o600


def test_backup_never_overwrites(home: Path, clock: Callable[[], datetime]) -> None:
    path = home / "config.json"
    path.write_text("one")
    first = backup_file(path, clock)
    path.write_text("two")
    second = backup_file(path, clock)
    third = backup_file(path, clock)
    assert first.read_text() == "one"
    assert second.name == "config.json.bak-20261019-093015-1"
    assert third.name == "config.json.bak-20261019-093015-2"
    assert second.read_text() == "two"


def test_backup_of_missing_file_fails_cleanly(home: Path, clock: Callable[[], datetime]) -> None:
    with pytest.raises(AXPilotError) as info:
        backup_file(home / "missing.json", clock)
    assert info.value.code is ErrorCode.COMMAND_FAILED


def test_atomic_write_creates_parents_with_default_mode(home: Path) -> None:
    path = home / "a" / "b" / "c.json"
    atomic_write(path, b"{}\n")
    assert path.read_bytes() == b"{}\n"
    assert mode_of(path) == DEFAULT_FILE_MODE
    assert [p.name for p in path.parent.iterdir()] == ["c.json"]


def test_atomic_write_preserves_existing_mode(home: Path) -> None:
    path = home / "c.json"
    path.write_text("old")
    path.chmod(0o640)
    atomic_write(path, b"new")
    assert path.read_text() == "new"
    assert mode_of(path) == 0o640


def test_atomic_write_through_symlink_updates_target(home: Path) -> None:
    real = home / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("old")
    link = home / "settings.json"
    link.symlink_to(real)
    atomic_write(link, b"new")
    assert link.is_symlink()
    assert real.read_text() == "new"


def test_atomic_write_failure_keeps_original_and_cleans_up(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = home / "c.json"
    path.write_text("original")

    def broken_replace(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fileops.os, "replace", broken_replace)
    with pytest.raises(AXPilotError) as info:
        atomic_write(path, b"new")
    assert info.value.code is ErrorCode.COMMAND_FAILED
    assert "No space left" in info.value.message
    assert path.read_text() == "original"
    assert [p.name for p in home.iterdir()] == ["c.json"]


def test_display_path_abbreviates_home(home: Path) -> None:
    assert display_path(home / ".cursor" / "mcp.json") == "~/.cursor/mcp.json"
    assert display_path(home) == "~"
    assert display_path(Path("/etc/hosts")) == "/etc/hosts"


def test_remove_file_is_idempotent(home: Path) -> None:
    path = home / "x.yaml"
    path.write_text("a: 1\n")
    fileops.remove_file(path)
    fileops.remove_file(path)
    assert not os.path.exists(path)
