"""The audit log: one JSON line per tool call.

Each line looks like::

    {"ts":"2026-10-19T12:34:56.789Z","tool":"type_text","client":"Claude","ok":true,
     "error_code":null,"duration_ms":182.4,"summary":"typed into e7",
     "args":{"ref":"e7","text":"<redacted 11 chars>"}}

``ts`` is ISO-8601 UTC. Arguments are logged according to ``[audit] log_args``:
``none`` (no arguments), ``redacted`` (the default: typed text, values, scripts and
commands are replaced by their length) or ``full``.

The file is created with mode ``0600`` (and tightened to it if it already exists),
rotated to ``<name>.1`` when it exceeds ``max_bytes``, and never makes a tool call fail:
I/O errors are reported once on stderr and the log keeps the most recent events in memory.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import os
import sys
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePath
from typing import Any

from axpilot.errors import AXPilotError, ErrorCode

__all__ = ["LOG_ARG_MODES", "REDACTED_KEYS", "AuditEvent", "AuditLog", "redact_args"]

REDACTED_KEYS: frozenset[str] = frozenset(
    {
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
    }
)
"""Argument names whose string values are redacted in ``redacted`` mode (at any depth)."""

LOG_ARG_MODES: tuple[str, ...] = ("none", "redacted", "full")

_MAX_DEPTH = 32
_FILE_MODE = 0o600
_DIR_MODE = 0o700
_READ_BLOCK = 8192


@dataclass(slots=True)
class AuditEvent:
    """One finished tool call."""

    tool: str
    client: str | None
    args: dict[str, Any]
    ok: bool
    error_code: str | None
    duration_ms: float
    summary: str | None = None


# ----------------------------------------------------------------------------- redaction


def _plain(value: Any) -> Any:
    """Pydantic models and dataclasses (tool arguments such as ``fill_form`` fields) as dicts, so
    redaction sees their keys instead of a ``str()`` that holds every value."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="python")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return value


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Convert ``value`` to plain JSON types (bytes are never logged, only their size)."""
    if depth > _MAX_DEPTH:
        return "<…>"
    value = _plain(value)
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(value)} bytes>"
    if isinstance(value, Enum):
        return _json_safe(value.value, depth + 1)
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe(v, depth + 1) for v in value]
    return str(value)


def _redact(value: Any, sensitive: bool, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return "<…>"
    value = _plain(value)
    if isinstance(value, str):
        return f"<redacted {len(value)} chars>" if sensitive else value
    if isinstance(value, Mapping):
        return {
            str(k): _redact(v, sensitive or str(k).casefold() in REDACTED_KEYS, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_redact(v, sensitive, depth + 1) for v in value]
    return _json_safe(value, depth)


def redact_args(args: Mapping[str, Any], mode: str) -> dict:
    """Return a JSON-safe copy of ``args`` for the audit log.

    * ``none``: ``{}``.
    * ``redacted``: string values under the keys in :data:`REDACTED_KEYS` (at any depth,
      including every string nested inside such a key) become ``"<redacted N chars>"``;
      everything else is kept.
    * ``full``: everything is kept.

    Binary values are always replaced by ``"<N bytes>"``. Raises ``INVALID_ARGUMENT`` for
    an unknown mode.
    """
    if mode == "none":
        return {}
    if mode == "redacted":
        return {str(k): _redact(v, str(k).casefold() in REDACTED_KEYS, 1) for k, v in args.items()}
    if mode == "full":
        return {str(k): _json_safe(v, 1) for k, v in args.items()}
    raise AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Unknown audit argument mode {mode!r}.",
        hint=f"Use one of: {', '.join(LOG_ARG_MODES)}.",
    )


# ------------------------------------------------------------------------------- the log


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_utc(ts: datetime) -> str:
    ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read_last_lines(path: Path, n: int) -> list[bytes]:
    """The last ``n`` non-empty lines of ``path``, read backwards in blocks."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        position = fh.tell()
        data = b""
        while position > 0 and data.count(b"\n") <= n:
            step = min(_READ_BLOCK, position)
            position -= step
            fh.seek(position)
            data = fh.read(step) + data
    lines = [line for line in data.split(b"\n") if line.strip()]
    return lines[-n:]


def _parse_lines(lines: list[bytes]) -> list[dict]:
    entries: list[dict] = []
    for line in lines:
        try:
            entry = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


class AuditLog:
    """Append-only JSONL audit log.

    ``path=None`` keeps events in memory only (``tail`` still works). ``enabled=False``
    records nothing. ``max_bytes`` (``None`` = unlimited) rotates the file to
    ``<name>.1`` before it would grow past that size. ``clock`` returns the current time
    (injectable for tests).
    """

    def __init__(
        self,
        path: Path | None,
        *,
        enabled: bool = True,
        log_args: str = "redacted",
        max_bytes: int | None = 10 * 1024 * 1024,
        memory_size: int = 200,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if log_args not in LOG_ARG_MODES:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unknown audit argument mode {log_args!r}.",
                hint=f"Use one of: {', '.join(LOG_ARG_MODES)}.",
            )
        self.path = Path(path) if path is not None else None
        self.enabled = enabled
        self.log_args = log_args
        self.max_bytes = max_bytes
        self._clock = clock
        self._memory: deque[dict] = deque(maxlen=max(memory_size, 1))
        self._lock = threading.Lock()
        self._warned = False

    @property
    def backup_path(self) -> Path | None:
        """Where the previous file goes on rotation (``audit.jsonl.1``)."""
        return self.path.with_name(self.path.name + ".1") if self.path is not None else None

    def entry(self, event: AuditEvent) -> dict:
        """The JSON object written for ``event`` (arguments already redacted)."""
        duration = float(event.duration_ms)
        return {
            "ts": _iso_utc(self._clock()),
            "tool": event.tool,
            "client": event.client,
            "ok": event.ok,
            "error_code": event.error_code,
            "duration_ms": round(duration, 1) if math.isfinite(duration) else None,
            "summary": event.summary,
            "args": redact_args(event.args or {}, self.log_args),
        }

    def record(self, event: AuditEvent) -> None:
        """Append one JSON line for ``event``. Never raises on I/O errors."""
        if not self.enabled:
            return
        entry = self.entry(event)
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            self._memory.append(entry)
            if self.path is not None:
                self._append(line.encode("utf-8"))

    def tail(self, n: int = 50) -> list[dict]:
        """The last ``n`` entries, oldest first (from the file, else from memory)."""
        if n <= 0:
            return []
        with self._lock:
            memory = list(self._memory)[-n:]
            if self.path is None:
                return memory
            try:
                lines = _read_last_lines(self.path, n)
                backup = self.backup_path
                if len(lines) < n and backup is not None and backup.exists():
                    lines = _read_last_lines(backup, n - len(lines)) + lines
            except FileNotFoundError:
                return memory
            except OSError as exc:
                self._warn("read", exc)
                return memory
        return _parse_lines(lines)

    # ------------------------------------------------------------------ internals
    def _append(self, data: bytes) -> None:
        assert self.path is not None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
            self._rotate_if_needed(len(data))
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(self.path, flags, _FILE_MODE)
            try:
                if os.fstat(fd).st_mode & 0o077:
                    os.fchmod(fd, _FILE_MODE)
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError as exc:
            self._warn("write", exc)

    def _rotate_if_needed(self, incoming: int) -> None:
        if self.max_bytes is None or self.path is None:
            return
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size and size + incoming > self.max_bytes:
            backup = self.backup_path
            assert backup is not None
            os.replace(self.path, backup)

    def _warn(self, action: str, exc: OSError) -> None:
        if self._warned:
            return
        self._warned = True
        with contextlib.suppress(OSError, ValueError):  # stderr may be closed
            print(
                f"axpilot: cannot {action} the audit log {self.path}: {exc} "
                "(further audit errors are not reported)",
                file=sys.stderr,
            )
