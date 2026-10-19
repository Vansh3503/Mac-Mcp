"""Which client apps may use AXPilot: the pairing store.

The first time an app (identified by its bundle id, or its path when it has none) connects
through the bridge, the host asks the user whether to allow it. The answer is remembered in
``paired-clients.json`` in AXPilot's application-support folder (mode ``0600``)::

    {"version": 1,
     "clients": {"com.anthropic.claudefordesktop":
                    {"decision": "always", "display": "Claude", "decided_at": "2026-10-19T14:02:11Z"}}}

``always`` lets the client in without asking again; ``denied`` refuses it without asking.
Writes are atomic (a temporary file and ``os.replace``). A corrupt or unreadable file is
logged, kept aside as ``paired-clients.json.corrupt`` and treated as empty, so the user is
asked again rather than locked out or let in by accident.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from axpilot import paths
from axpilot.errors import AXPilotError, ErrorCode

__all__ = ["DECISIONS", "Decision", "PairingEntry", "PairingStore"]

_log = logging.getLogger(__name__)

Decision = Literal["always", "denied"]
DECISIONS: tuple[Decision, ...] = ("always", "denied")
_VERSION = 1
_MAX_KEY = 512


@dataclass(frozen=True, slots=True)
class PairingEntry:
    """One remembered decision."""

    client: str
    decision: Decision
    display: str | None = None
    decided_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "decision": self.decision,
            "display": self.display,
            "decided_at": self.decided_at,
        }


def _now() -> datetime:
    return datetime.now(UTC)


class PairingStore:
    """Thread-safe access to the remembered pairing decisions (see the module docstring)."""

    def __init__(self, path: Path | None = None, *, clock: Callable[[], datetime] = _now) -> None:
        self.path = path if path is not None else paths.pairing_file()
        self._clock = clock
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ reading
    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            _log.warning("Cannot read the pairing file %s: %s; clients will be asked again.", self.path, exc)
            return {}
        try:
            data = json.loads(raw)
            clients = data["clients"] if isinstance(data, dict) else None
            if not isinstance(clients, dict):
                raise ValueError("no clients object")
        except (ValueError, KeyError, TypeError) as exc:
            self._set_aside(exc)
            return {}
        return {
            str(key): value
            for key, value in clients.items()
            if isinstance(value, dict) and value.get("decision") in DECISIONS
        }

    def _set_aside(self, exc: Exception) -> None:
        corrupt = self.path.with_name(self.path.name + ".corrupt")
        _log.warning(
            "The pairing file %s is damaged (%s); it was moved to %s and every client will be asked again.",
            self.path,
            exc,
            corrupt.name,
        )
        try:
            os.replace(self.path, corrupt)
        except OSError as move_error:
            _log.warning("Could not move the damaged pairing file aside: %s", move_error)

    def decision(self, client: str) -> Decision | None:
        """The remembered decision for ``client``, or ``None`` if the user was never asked."""
        with self._lock:
            entry = self._read().get(client)
        return entry["decision"] if entry is not None else None

    def entries(self) -> list[PairingEntry]:
        """Every remembered decision, sorted by client."""
        with self._lock:
            clients = self._read()
        return [
            PairingEntry(
                client=key,
                decision=value["decision"],
                display=value.get("display") if isinstance(value.get("display"), str) else None,
                decided_at=value.get("decided_at") if isinstance(value.get("decided_at"), str) else None,
            )
            for key, value in sorted(clients.items())
        ]

    # ------------------------------------------------------------------ writing
    def remember(self, client: str, decision: Decision, *, display: str | None = None) -> PairingEntry:
        """Store ``decision`` for ``client`` (replacing an earlier one)."""
        if decision not in DECISIONS:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"Unknown pairing decision {decision!r}.")
        if not client or len(client) > _MAX_KEY:
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "A pairing client key must be 1-512 characters.")
        entry = PairingEntry(
            client=client,
            decision=decision,
            display=display,
            decided_at=self._clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        with self._lock:
            clients = self._read()
            clients[client] = {"decision": decision, "display": display, "decided_at": entry.decided_at}
            self._write(clients)
        return entry

    def forget(self, client: str) -> bool:
        """Remove ``client``'s decision (the user is asked again next time). True if there was one."""
        with self._lock:
            clients = self._read()
            if clients.pop(client, None) is None:
                return False
            self._write(clients)
        return True

    def _write(self, clients: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps({"version": _VERSION, "clients": clients}, indent=2, sort_keys=True) + "\n"
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise AXPilotError(
                ErrorCode.COMMAND_FAILED,
                f"Could not save the pairing decision to {self.path}: {exc.strerror or exc}",
                hint="Check that AXPilot's application-support folder is writable.",
            ) from exc
