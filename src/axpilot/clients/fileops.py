"""Careful file handling for configuration files AXPilot edits on the user's behalf.

* :func:`read_config_text` reads a file once and remembers what a faithful rewrite needs: its
  real location (symlinks are followed, so dotfile managers keep working), permission bits,
  byte-order mark and line endings.
* :func:`backup_file` copies a file to ``<name>.bak-YYYYmmdd-HHMMSS`` next to it before any change.
  The backup is created exclusively (an existing backup is never overwritten) and gets the
  original's permission bits from the start, so a private file never has a readable copy.
* :func:`atomic_write` writes through a temporary file in the same directory and ``os.replace``,
  so readers see either the old or the new file, never half of one.

Every failure is reported as an :class:`~axpilot.errors.AXPilotError` naming the file.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from axpilot.errors import AXPilotError, ErrorCode

__all__ = [
    "DEFAULT_FILE_MODE",
    "ConfigText",
    "atomic_write",
    "atomic_write_text",
    "backup_file",
    "display_path",
    "read_config_text",
    "remove_file",
]

DEFAULT_FILE_MODE = 0o644
"""Permission bits for a configuration file AXPilot creates (what an editor would create)."""

_BOM = "﻿"


def display_path(path: Path) -> str:
    """``path`` with the home directory abbreviated to ``~`` (for messages)."""
    home = Path.home()
    try:
        rel = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if str(rel) == "." else f"~/{rel}"


def _io_error(action: str, path: Path, exc: OSError) -> AXPilotError:
    reason = exc.strerror or str(exc)
    return AXPilotError(
        ErrorCode.COMMAND_FAILED,
        f"Could not {action} {display_path(path)}: {reason}.",
        hint="Check that the file and its folder exist and that you own them.",
        details={"path": str(path), "errno": exc.errno},
    )


@dataclass(frozen=True, slots=True)
class ConfigText:
    """The contents of a configuration file and how to write it back faithfully."""

    path: Path
    """Where the content lives (symlinks resolved): reads, backups and writes use this path."""
    text: str | None
    """Decoded text without a byte-order mark, or ``None`` if the file does not exist."""
    mode: int | None = None
    """Permission bits of the existing file."""
    bom: bool = False
    newline: str = "\n"

    @property
    def exists(self) -> bool:
        return self.text is not None

    @property
    def is_blank(self) -> bool:
        """True if the file is missing, empty or only whitespace."""
        return self.text is None or not self.text.strip()

    def encode(self, text: str) -> bytes:
        """Encode new content with this file's line endings and byte-order mark."""
        normalized = text.replace("\r\n", "\n")
        if self.newline == "\r\n":
            normalized = normalized.replace("\n", "\r\n")
        return ((_BOM if self.bom else "") + normalized).encode("utf-8")


def _real_path(path: Path) -> Path:
    return path.resolve() if path.is_symlink() else path


def read_config_text(path: Path) -> ConfigText:
    """Read ``path`` as UTF-8. A missing file gives ``ConfigText(text=None)``.

    Raises ``INVALID_ARGUMENT`` if the path is a directory or the bytes are not UTF-8, and
    ``COMMAND_FAILED`` for other operating-system errors (such as permission denied).
    """
    real = _real_path(path)
    try:
        data = real.read_bytes()
        mode = stat.S_IMODE(real.stat().st_mode)
    except FileNotFoundError:
        return ConfigText(real, None)
    except IsADirectoryError as exc:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{display_path(path)} is a directory, not a configuration file.",
            hint="Move the directory away, then try again.",
        ) from exc
    except OSError as exc:
        raise _io_error("read", path, exc) from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{display_path(path)} is not UTF-8 text (byte {exc.start}).",
            hint="Fix or move the file, then try again.",
        ) from exc
    bom = text.startswith(_BOM)
    if bom:
        text = text[1:]
    newline = "\r\n" if "\r\n" in text else "\n"
    return ConfigText(real, text, mode, bom, newline)


def _backup_name(path: Path, stamp: str, attempt: int) -> Path:
    suffix = f".bak-{stamp}" if attempt == 0 else f".bak-{stamp}-{attempt}"
    return path.with_name(path.name + suffix)


def backup_file(path: Path, clock: Callable[[], datetime] = datetime.now) -> Path:
    """Copy ``path`` to ``<name>.bak-YYYYmmdd-HHMMSS`` beside it and return the backup's path.

    ``clock`` supplies the timestamp (injectable for tests). If a backup with that name exists,
    ``-1``, ``-2``, … is appended. Permission bits and timestamps are preserved.
    """
    stamp = clock().strftime("%Y%m%d-%H%M%S")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        attempt = 0
        while True:
            candidate = _backup_name(path, stamp, attempt)
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode & 0o777)
            except FileExistsError:
                attempt += 1
                continue
            break
        try:
            with os.fdopen(fd, "wb") as dst, path.open("rb") as src:
                shutil.copyfileobj(src, dst)
            shutil.copystat(path, candidate)
        except BaseException:
            with contextlib.suppress(OSError):
                candidate.unlink()
            raise
    except OSError as exc:
        raise _io_error("back up", path, exc) from exc
    return candidate


def _fsync_dir(directory: Path) -> None:
    """Persist a rename on filesystems that need it (best effort; not every OS allows it)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        # Directories cannot be fsync'ed everywhere (some network filesystems refuse); the
        # rename itself already happened, so this only narrows the crash window.
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Replace ``path`` with ``data`` atomically, creating parent directories as needed.

    The file keeps ``mode`` if given, otherwise the existing file's permission bits, otherwise
    :data:`DEFAULT_FILE_MODE`. A symlink at ``path`` is followed, so its target is updated.
    """
    target = _real_path(path)
    tmp: str | None = None
    try:
        if mode is None:
            try:
                mode = stat.S_IMODE(target.stat().st_mode)
            except FileNotFoundError:
                mode = DEFAULT_FILE_MODE
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
        tmp = None
    except OSError as exc:
        raise _io_error("write", path, exc) from exc
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    _fsync_dir(target.parent)


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """:func:`atomic_write` for UTF-8 text."""
    atomic_write(path, text.encode("utf-8"), mode=mode)


def remove_file(path: Path) -> None:
    """Delete ``path`` (a missing file is fine)."""
    try:
        _real_path(path).unlink(missing_ok=True)
    except OSError as exc:
        raise _io_error("remove", path, exc) from exc
