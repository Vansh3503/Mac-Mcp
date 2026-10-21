"""The host: AXPilot's long-running process behind ``AXPilot.app``, serving MCP over a Unix socket.

Each MCP client starts ``axpilot stdio``, a thin bridge that connects here (CONTRACTS §7). The
host owns the engine and the macOS permissions (granted once to the launcher), and serves every
bridge connection as its own MCP session:

1. **Socket**: created with mode ``0600`` in a ``0700`` folder. A leftover socket from a host
   that died is removed; a socket another live host is serving makes :func:`serve` fail with
   ``HOST_UNAVAILABLE`` (only one host per user).
2. **Peer check**: a connecting process must run as the same user (``SO_PEERCRED`` on Linux,
   ``LOCAL_PEERCRED`` on macOS; where neither is available the socket's permissions alone keep
   other users out).
3. **Hello** (:mod:`axpilot.bridge.protocol`) within :data:`HELLO_TIMEOUT_S`, naming the client
   app and the profile/naming it wants.
4. **Pairing**: with ``[safety] require_pairing`` (the default) the user approves each client app
   once; the answer is remembered in the :class:`~axpilot.host.pairing.PairingStore`.
5. **Session**: :func:`~axpilot.server.app.build_server` for that client (its profile, naming
   and name in audit records and confirmations) over the socket until either side closes.

At most ``max_clients`` sessions run at once; more get ``BUSY``. Refusals are sent as a reply
line before the connection is closed, so the bridge can show the reason.

A host started by ``AXPilot.app`` (which sets ``AXPILOT_LAUNCHER_VERSION``) stops when the launcher
dies, e.g. when it is force-quit: nothing could restart the host, but it would keep the socket
and the permissions. It notices within :data:`PARENT_POLL_S` that it has a new parent.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import logging
import os
import socket
import stat
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from anyio.abc import SocketAttribute, SocketStream, TaskStatus

from axpilot import paths
from axpilot.bridge.protocol import ClientIdentity, Hello, LineReader, Reply, parse_hello
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.host.pairing import PairingStore
from axpilot.host.session import serve_session
from axpilot.policy.confirm import Confirmer, ConfirmRequest
from axpilot.server.app import build_server
from axpilot.server.context import CONFIRM_TIMEOUT_MAX_S, ServerContext

__all__ = [
    "DEFAULT_MAX_CLIENTS",
    "HELLO_TIMEOUT_S",
    "LAUNCHER_ENV",
    "PARENT_POLL_S",
    "Host",
    "launcher_pid",
    "peer_uid",
    "prepare_socket",
    "serve",
    "socket_is_live",
    "watch_parent",
]

_log = logging.getLogger(__name__)

DEFAULT_MAX_CLIENTS = 8
HELLO_TIMEOUT_S = 10.0
"""A connection that sends no hello within this time is closed."""
LIVE_PROBE_TIMEOUT_S = 0.5
LAUNCHER_ENV = "AXPILOT_LAUNCHER_VERSION"
"""Set by ``AXPilot.app`` in the environment of the host it starts."""
PARENT_POLL_S = 1.0
"""How often a host started by ``AXPilot.app`` checks that the launcher is still its parent."""
_SO_PEERCRED_FORMAT = "3i"  # struct ucred: pid, uid, gid
_SOL_LOCAL = 0  # macOS <sys/un.h>
_LOCAL_PEERCRED = 0x001
_XUCRED_SIZE = 76  # struct xucred: version, uid, ngroups (+pad), 16 groups
_XUCRED_VERSION = 0


# =========================================================================== launcher
def launcher_pid(environ: Mapping[str, str] | None = None) -> int | None:
    """The pid of ``AXPilot.app`` if it started this process (it sets :data:`LAUNCHER_ENV`), else
    ``None``: a host started by hand keeps running when the shell that started it goes."""
    env = os.environ if environ is None else environ
    return os.getppid() if env.get(LAUNCHER_ENV) else None


async def watch_parent(
    parent_pid: int, *, interval_s: float = PARENT_POLL_S, getppid: Callable[[], int] | None = None
) -> None:
    """Return once this process's parent is no longer ``parent_pid``: the parent died and the
    process was re-parented (to launchd on macOS). A parent pid of 1 counts as gone already."""
    while True:
        current = getppid() if getppid is not None else os.getppid()
        if current != parent_pid or current == 1:
            return
        await anyio.sleep(interval_s)


# =========================================================================== peers
def peer_uid(sock: socket.socket) -> int | None:
    """The uid of the process at the other end of a Unix socket, or ``None`` when the platform
    offers no way to ask (the socket's ``0600`` permissions then keep other users out)."""
    peercred = getattr(socket, "SO_PEERCRED", None)
    try:
        if peercred is not None:
            data = sock.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize(_SO_PEERCRED_FORMAT))
            _, uid, _ = struct.unpack(_SO_PEERCRED_FORMAT, data)
            return int(uid)
        if sys.platform == "darwin":
            option = getattr(socket, "LOCAL_PEERCRED", _LOCAL_PEERCRED)
            data = sock.getsockopt(_SOL_LOCAL, option, _XUCRED_SIZE)
            version, uid = struct.unpack_from("=II", data)
            if version == _XUCRED_VERSION:
                return int(uid)
            _log.warning("Unexpected xucred version %s; cannot verify the peer's user.", version)
    except (OSError, struct.error) as exc:
        _log.warning("Cannot read the peer's credentials: %s", exc)
    return None


# =========================================================================== the socket
def socket_is_live(path: Path, *, timeout_s: float = LIVE_PROBE_TIMEOUT_S) -> bool:
    """True if something accepts connections on the Unix socket ``path``."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout_s)
    try:
        probe.connect(os.fspath(path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _secure_folder(folder: Path) -> None:
    """Create ``folder`` with mode ``0700``, or tighten one we own (never a shared one like /tmp)."""
    if not folder.exists():
        folder.mkdir(parents=True, mode=0o700)
        os.chmod(folder, 0o700)  # mkdir's mode is filtered by the umask
        return
    info = folder.stat()
    if info.st_uid == os.getuid() and not info.st_mode & stat.S_ISVTX and stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(folder, stat.S_IMODE(info.st_mode) & 0o700)


def prepare_socket(path: Path) -> None:
    """Get ``path`` ready for a new host: secure its folder, remove a stale socket.

    Raises ``HOST_UNAVAILABLE`` if another host is serving it, or if something that is not a
    socket (or belongs to another user) is in the way."""
    _secure_folder(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise AXPilotError(
            ErrorCode.HOST_UNAVAILABLE,
            f"{path} exists and is not a socket.",
            hint="Move it away, or set AXPILOT_SOCKET to another path.",
        )
    if info.st_uid != os.getuid():
        raise AXPilotError(
            ErrorCode.HOST_UNAVAILABLE,
            f"The socket {path} belongs to another user.",
            hint="Set AXPILOT_SOCKET to a path of your own.",
        )
    if socket_is_live(path):
        raise AXPilotError(
            ErrorCode.HOST_UNAVAILABLE,
            f"Another AXPilot host is already serving {path}.",
            hint="Only one host runs per user; quit the other one (or use it).",
        )
    _log.info("Removing a stale socket at %s", path)
    path.unlink(missing_ok=True)


# =========================================================================== the host
@dataclass
class _Session:
    client: ClientIdentity
    profile: str
    naming: str
    connected_at: float

    def to_dict(self, pairing: str) -> dict[str, Any]:
        return {
            "app": self.client.display,
            "bundle_id": self.client.parent_bundle_id,
            "pid": self.client.pid,
            "profile": self.profile,
            "naming": self.naming,
            "pairing": pairing,
            "connected_s": round(time.monotonic() - self.connected_at, 1),
        }


@dataclass
class Host:
    """The socket server (see the module docstring). Create it, then ``await host.serve(...)``."""

    ctx: ServerContext
    socket_path: Path
    pairing: PairingStore = field(default_factory=PairingStore)
    pairing_confirmer: Confirmer | None = None
    """Asks the user to pair a new client (default: the context's confirmer)."""
    max_clients: int = DEFAULT_MAX_CLIENTS
    hello_timeout_s: float = HELLO_TIMEOUT_S
    parent_pid: int | None = None
    """Stop serving once this is no longer the process's parent (see :func:`launcher_pid`)."""
    parent_poll_s: float = PARENT_POLL_S
    sessions: dict[int, _Session] = field(default_factory=dict)
    _ids: Iterator[int] = field(default_factory=lambda: itertools.count(1))
    _pair_lock: anyio.Lock = field(default_factory=anyio.Lock)
    _state_lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ serving
    async def serve(self, *, task_status: TaskStatus[Path] = anyio.TASK_STATUS_IGNORED) -> None:
        """Listen until cancelled, or until the parent is gone (``parent_pid``); the socket file is
        removed on the way out."""
        prepare_socket(self.socket_path)
        listener = await anyio.create_unix_listener(self.socket_path, mode=0o600)
        identity = self.socket_path.stat()
        _log.info("AXPilot host listening on %s", self.socket_path)
        try:
            async with listener, anyio.create_task_group() as tg:
                if self.parent_pid is not None:
                    tg.start_soon(self._stop_without_parent, self.parent_pid, tg.cancel_scope)
                task_status.started(self.socket_path)
                await listener.serve(self._guarded_handle)
        finally:
            with contextlib.suppress(OSError):
                current = self.socket_path.stat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    self.socket_path.unlink()

    async def _stop_without_parent(self, parent_pid: int, scope: anyio.CancelScope) -> None:
        await watch_parent(parent_pid, interval_s=self.parent_poll_s)
        _log.warning("AXPilot.app (pid %d) is gone; the host stops.", parent_pid)
        scope.cancel()

    async def _guarded_handle(self, conn: SocketStream) -> None:
        try:
            await self._handle(conn)
        except anyio.get_cancelled_exc_class():
            raise
        except Exception:  # one broken connection must never take the host down (logged, A8)
            _log.exception("An MCP connection failed unexpectedly")

    async def _handle(self, conn: SocketStream) -> None:
        async with conn:
            raw: socket.socket | None = conn.extra(SocketAttribute.raw_socket, None)  # noqa: S610 - anyio, not Django
            uid = peer_uid(raw) if raw is not None else None
            if uid is not None and uid != os.getuid():
                _log.warning("Refused a connection from uid %s", uid)
                await self._refuse(
                    conn,
                    AXPilotError(ErrorCode.BLOCKED_BY_POLICY, "Only the user running AXPilot may connect."),
                )
                return
            reader = LineReader(conn)
            line: bytes | None = None
            with anyio.move_on_after(self.hello_timeout_s):
                line = await reader.readline()
            if line is None:
                return
            try:
                hello = parse_hello(line)
            except AXPilotError as err:
                await self._refuse(conn, err)
                return
            await self._session(conn, reader, hello)

    async def _session(self, conn: SocketStream, reader: LineReader, hello: Hello) -> None:
        session_id = next(self._ids)
        try:
            with self._state_lock:
                if len(self.sessions) >= self.max_clients:
                    raise AXPilotError(
                        ErrorCode.BUSY,
                        f"AXPilot already serves {self.max_clients} clients.",
                        hint="Close an MCP client that is not in use, then try again.",
                    )
                self.sessions[session_id] = _Session(
                    client=hello.client, profile="", naming="", connected_at=time.monotonic()
                )
            await self._pair(hello.client)
            client_ctx = dataclasses.replace(
                self.ctx, mode="host", host_info=lambda: self._info_for(hello.client)
            )
            server = build_server(
                client_ctx, profile=hello.profile, naming=hello.naming, client_name=hello.client.display
            )
        except AXPilotError as err:
            with self._state_lock:
                self.sessions.pop(session_id, None)
            await self._refuse(conn, err)
            return
        scoped = server.axpilot_context
        with self._state_lock:
            self.sessions[session_id] = dataclasses.replace(
                self.sessions[session_id], profile=scoped.profile, naming=scoped.naming
            )
        _log.info("Client %s connected (profile %s)", hello.client.display, scoped.profile)
        try:
            await conn.send(Reply.accept().to_line())
            await serve_session(server, reader, conn)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError) as exc:
            _log.debug("Connection to %s lost: %r", hello.client.display, exc)
        finally:
            with self._state_lock:
                self.sessions.pop(session_id, None)
            _log.info("Client %s disconnected", hello.client.display)

    @staticmethod
    async def _refuse(conn: SocketStream, err: AXPilotError) -> None:
        with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
            await conn.send(Reply.refuse(err).to_line())

    # ------------------------------------------------------------------ pairing
    async def _pair(self, client: ClientIdentity) -> None:
        """Let ``client`` in, or raise ``USER_DENIED`` (asking the user once per client app)."""
        if not self.ctx.config.safety.require_pairing:
            return
        async with self._pair_lock:  # one question at a time
            decision = await anyio.to_thread.run_sync(self.pairing.decision, client.key)
            if decision == "always":
                return
            if decision == "denied":
                raise AXPilotError(
                    ErrorCode.USER_DENIED,
                    f"{client.display} is not allowed to use AXPilot (the user declined it earlier).",
                    hint=f"To be asked again, remove {client.key!r} from {self.pairing.path}.",
                )
            confirmer = self.pairing_confirmer or self.ctx.confirmer
            request = ConfirmRequest(
                tool="pairing",
                summary=f"Allow {client.display} to see and control this Mac through AXPilot?",
                detail=f"Client: {client.key} (bridge pid {client.pid})",
                client=client.display,
            )
            approved: bool | None = None
            timeout = min(self.ctx.confirm_timeout_s, CONFIRM_TIMEOUT_MAX_S)
            with self.ctx.control.asking_user(), anyio.move_on_after(timeout):
                approved = await confirmer.confirm(request)
            if approved is None:
                raise AXPilotError(
                    ErrorCode.USER_DENIED,
                    f"Nobody answered whether {client.display} may use AXPilot.",
                    hint="Connect again and answer AXPilot's question on the Mac.",
                )
            await anyio.to_thread.run_sync(
                lambda: self.pairing.remember(
                    client.key, "always" if approved else "denied", display=client.display
                )
            )
            if not approved:
                raise AXPilotError(
                    ErrorCode.USER_DENIED,
                    f"The user did not allow {client.display} to use AXPilot.",
                    hint=f"To be asked again, remove {client.key!r} from {self.pairing.path}.",
                )

    # ------------------------------------------------------------------ status
    def _info_for(self, client: ClientIdentity) -> Mapping[str, Any]:
        """What ``status`` reports in host mode for ``client``."""
        if not self.ctx.config.safety.require_pairing:
            pairing = "not required"
        else:
            pairing = self.pairing.decision(client.key) or "unpaired"
        with self._state_lock:
            sessions = list(self.sessions.values())
        clients = [s.to_dict(self.pairing.decision(s.client.key) or "unpaired") for s in sessions]
        return {"pairing": pairing, "clients": clients}


async def serve(
    ctx: ServerContext,
    socket_path: Path | None = None,
    *,
    pairing: PairingStore | None = None,
    pairing_confirmer: Confirmer | None = None,
    max_clients: int = DEFAULT_MAX_CLIENTS,
    parent_pid: int | None = None,
    task_status: TaskStatus[Path] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Serve MCP on the host socket until cancelled (see the module docstring).

    ``socket_path`` defaults to :func:`axpilot.paths.socket_path`; ``pairing`` to the user's
    pairing file. With ``parent_pid`` (:func:`launcher_pid`), serving also ends once that process
    is no longer the parent. Raises ``HOST_UNAVAILABLE`` if the socket cannot be taken."""
    host = Host(
        ctx=ctx,
        socket_path=socket_path or paths.socket_path(),
        pairing=pairing or PairingStore(),
        pairing_confirmer=pairing_confirmer,
        max_clients=max_clients,
        parent_pid=parent_pid,
    )
    await host.serve(task_status=task_status)
