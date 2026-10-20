"""Streamable HTTP transport (``axpilot http``), for clients that only speak HTTP.

Locked down by default, because anything that can reach the port could drive the Mac:

* binds to ``127.0.0.1``; another address is refused unless ``allow_remote`` is set *and* a TLS
  certificate and key are given (:func:`check_bind`);
* every request needs ``Authorization: Bearer <token>``; the token is random and compared in
  constant time (``401`` otherwise). :func:`get_token` keeps it in the macOS login keychain
  (moving a token from the old file there), and in a ``0600`` file in AXPilot's
  application-support folder elsewhere or when the keychain cannot be used
  (:func:`load_or_create_token`);
* the ``Host`` header must name the bound address and port and a browser ``Origin`` must be
  explicitly allowed (``403`` otherwise): this blocks DNS-rebinding and cross-site requests;
* no CORS headers are ever sent.

The MCP endpoint is ``/mcp``.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import anyio
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from axpilot import paths
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.server.app import build_server
from axpilot.server.context import ServerContext
from axpilot.system.keychain import Keychain

__all__ = [
    "DEFAULT_PORT",
    "KEYCHAIN_ACCOUNT",
    "KEYCHAIN_SERVICE",
    "LOOPBACK_HOSTS",
    "MCP_PATH",
    "AccessGuard",
    "StoredToken",
    "build_http_app",
    "check_bind",
    "get_token",
    "load_or_create_token",
    "run_http",
    "token_file",
]

_log = logging.getLogger(__name__)

DEFAULT_PORT = 7788
"""The `[server] http_port` default."""
MCP_PATH = "/mcp"
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
_TOKEN_MIN_CHARS = 32
KEYCHAIN_SERVICE = "io.github.vansh3503.axpilot.http-token"
KEYCHAIN_ACCOUNT = "axpilot"
_KEYCHAIN_SAFE = re.compile(r"[A-Za-z0-9._-]+")


# =========================================================================== token
def token_file() -> Path:
    """Where the HTTP bearer token is kept."""
    return paths.app_support_dir() / "http-token"


def load_or_create_token(path: Path | None = None) -> str:
    """The bearer token from ``path`` (default :func:`token_file`), creating a random one with
    mode ``0600`` if there is none. A readable-by-others file is tightened to ``0600`` (with a
    warning); a token that is too short is replaced. Raises ``COMMAND_FAILED`` on I/O errors."""
    target = path or token_file()
    try:
        if target.exists():
            mode = stat.S_IMODE(target.stat().st_mode)
            if mode & 0o077:
                _log.warning("The HTTP token file %s was readable by others; restricting it to 0600.", target)
                os.chmod(target, 0o600)
            token = target.read_text(encoding="utf-8").strip()
            if len(token) >= _TOKEN_MIN_CHARS:
                return token
            _log.warning("The HTTP token in %s is too short; creating a new one.", target)
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = secrets.token_urlsafe(32)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(token + "\n")
        return token
    except OSError as exc:
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED,
            f"Cannot read or create the HTTP token file {target}: {exc.strerror or exc}",
            hint="Check that AXPilot's application-support folder is writable.",
        ) from exc


@dataclass(frozen=True, slots=True)
class StoredToken:
    """The HTTP bearer token and where it is kept (for messages: never print the token itself
    unless the user asks)."""

    value: str
    location: str


def _keychain_location() -> str:
    return f"the macOS keychain (service {KEYCHAIN_SERVICE})"


def _file_token(path: Path) -> str | None:
    """A usable token from ``path`` without creating one (``None`` if there is none)."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        _log.warning("Cannot read the HTTP token file %s: %s", path, exc.strerror or exc)
        return None
    return token if len(token) >= _TOKEN_MIN_CHARS else None


def get_token(
    store: Literal["auto", "keychain", "file"] = "auto",
    *,
    path: Path | None = None,
    keychain: Keychain | None = None,
) -> StoredToken:
    """The HTTP bearer token, created on first use (``store``: ``[server] token_store``).

    With the keychain (``keychain``, or ``auto`` on macOS) it lives in the login keychain: a token
    found in the old file (``path``, default :func:`token_file`) is moved there and the file
    removed, so configured clients keep working. When the keychain cannot be used (locked, no
    ``security`` tool, a token with characters it cannot take) the ``0600`` file is used instead,
    with a warning, as it always is with ``file`` and with ``auto`` elsewhere
    (:func:`load_or_create_token`)."""
    target = path or token_file()
    if store == "file" or (store == "auto" and sys.platform != "darwin"):
        return StoredToken(load_or_create_token(target), str(target))
    chain = keychain or Keychain()
    try:
        stored = chain.read(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        if stored is not None and len(stored) >= _TOKEN_MIN_CHARS:
            _remove_leftover(target, stored)
            return StoredToken(stored, _keychain_location())
        existing = _file_token(target)
        if existing is not None and not _KEYCHAIN_SAFE.fullmatch(existing):
            _log.warning("The HTTP token in %s cannot move to the keychain; keeping the file.", target)
            return StoredToken(load_or_create_token(target), str(target))
        token = existing or secrets.token_urlsafe(32)
        chain.write(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, token, label="AXPilot-HTTP-token")
    except AXPilotError as err:
        _log.warning(
            "The macOS keychain cannot hold the HTTP token (%s); using %s instead.", err.message, target
        )
        return StoredToken(load_or_create_token(target), str(target))
    _remove_leftover(target, token)
    return StoredToken(token, _keychain_location())


def _remove_leftover(path: Path, token: str) -> None:
    """Delete the old token file once the keychain holds its token (best effort, A7). A file with
    another token is left alone, with a warning: a client may still use it."""
    leftover = _file_token(path)
    if leftover is None and not path.exists():
        return
    if leftover is not None and not hmac.compare_digest(leftover, token):
        _log.warning("%s holds an HTTP token that is no longer used (the keychain's is); delete it.", path)
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _log.warning("Could not remove the old HTTP token file %s: %s", path, exc.strerror or exc)


# =========================================================================== binding
def check_bind(
    host: str, *, allow_remote: bool = False, tls_cert: Path | None = None, tls_key: Path | None = None
) -> None:
    """Refuse a non-loopback bind unless ``allow_remote`` and a TLS certificate and key are set
    (``BLOCKED_BY_POLICY``), and TLS files that do not exist (``INVALID_ARGUMENT``)."""
    for label, file in (("certificate", tls_cert), ("key", tls_key)):
        if file is not None and not file.is_file():
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"The TLS {label} {file} does not exist.")
    if (tls_cert is None) != (tls_key is None):
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "TLS needs both a certificate and a key.")
    if host in LOOPBACK_HOSTS:
        return
    if not allow_remote or tls_cert is None:
        raise AXPilotError(
            ErrorCode.BLOCKED_BY_POLICY,
            f"Refusing to listen on {host}: other machines could control this Mac.",
            hint="Keep the default 127.0.0.1. To accept remote connections deliberately, pass "
            "--allow-remote with --tls-cert and --tls-key.",
        )


def _host_values(host: str, port: int) -> set[str]:
    names = {host} | ({"127.0.0.1", "localhost"} if host in LOOPBACK_HOSTS else set())
    values: set[str] = set()
    for name in names:
        shown = f"[{name}]" if ":" in name and not name.startswith("[") else name
        values.update({shown, f"{shown}:{port}"})
    if host in LOOPBACK_HOSTS:
        values.update({"[::1]", f"[::1]:{port}"})
    return values


# =========================================================================== the guard
class AccessGuard:
    """ASGI middleware: ``Host``/``Origin`` checks (``403``), then the bearer token (``401``)."""

    def __init__(
        self, app: ASGIApp, *, token: str, allowed_hosts: Iterable[str], allowed_origins: Iterable[str] = ()
    ) -> None:
        if len(token) < _TOKEN_MIN_CHARS:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"The HTTP token must have at least {_TOKEN_MIN_CHARS} characters.",
            )
        self.app = app
        self._token = token.encode("utf-8")
        self._hosts = frozenset(h.lower() for h in allowed_hosts)
        self._origins = frozenset(o.rstrip("/").lower() for o in allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = (headers.get("host") or "").lower()
        if host not in self._hosts:
            await self._deny(send, 403, "forbidden", "This server only answers requests addressed to itself.")
            return
        origin = headers.get("origin")
        if origin is not None and origin.rstrip("/").lower() not in self._origins:
            await self._deny(send, 403, "forbidden", "Requests from web pages are not allowed.")
            return
        if not self._authorized(headers.get("authorization")):
            await self._deny(send, 401, "unauthorized", "A valid bearer token is required.", bearer=True)
            return
        await self.app(scope, receive, send)

    def _authorized(self, header: str | None) -> bool:
        if not header:
            return False
        scheme, _, credentials = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(credentials.strip().encode("utf-8"), self._token)

    @staticmethod
    async def _deny(send: Send, status: int, error: str, message: str, *, bearer: bool = False) -> None:
        body = json.dumps({"error": error, "message": message}).encode("utf-8")
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if bearer:
            headers.append((b"www-authenticate", b'Bearer realm="axpilot"'))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


# =========================================================================== the app
def build_http_app(
    ctx: ServerContext,
    *,
    token: str,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    allowed_origins: Sequence[str] = (),
    profile: str | None = None,
    naming: str | None = None,
    client_name: str | None = None,
) -> AccessGuard:
    """The ASGI app: the MCP server's Streamable HTTP app behind :class:`AccessGuard`."""
    from mcp.server.transport_security import TransportSecuritySettings  # noqa: PLC0415 - HTTP only

    server = build_server(ctx, profile=profile, naming=naming, client_name=client_name)
    hosts = _host_values(host, port)
    # The SDK compares origins exactly; give it each one with and without the trailing slash.
    origins = {o.rstrip("/").lower() for o in allowed_origins}
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins | {f"{o}/" for o in origins}),
    )
    inner = server.streamable_http_app(streamable_http_path=MCP_PATH, transport_security=security, host=host)
    return AccessGuard(inner, token=token, allowed_hosts=hosts, allowed_origins=allowed_origins)


async def run_http(
    ctx: ServerContext,
    *,
    token: str,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    allow_remote: bool = False,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    allowed_origins: Sequence[str] = (),
    profile: str | None = None,
    naming: str | None = None,
) -> None:
    """Serve :func:`build_http_app` with uvicorn until cancelled."""
    import uvicorn  # noqa: PLC0415 - only needed for `axpilot http`

    check_bind(host, allow_remote=allow_remote, tls_cert=tls_cert, tls_key=tls_key)
    app = build_http_app(
        ctx,
        token=token,
        host=host,
        port=port,
        allowed_origins=allowed_origins,
        profile=profile,
        naming=naming,
    )
    options: dict[str, Any] = {"host": host, "port": port, "log_level": "warning"}
    if tls_cert is not None and tls_key is not None:
        options.update(ssl_certfile=str(tls_cert), ssl_keyfile=str(tls_key))
    server = uvicorn.Server(uvicorn.Config(app, **options))
    finished = anyio.Event()

    async def serve() -> None:
        # Shielded: on cancellation uvicorn is asked to exit and finishes its own shutdown (open
        # connections, the app's lifespan) instead of being torn down halfway.
        with anyio.CancelScope(shield=True):
            try:
                await server.serve()
            finally:
                finished.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(serve)
        try:
            await finished.wait()
        except anyio.get_cancelled_exc_class():
            server.should_exit = True
            raise
