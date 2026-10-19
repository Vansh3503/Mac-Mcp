"""A real AXPilot host in-process, for tests of bridges, transports and the SDK's ``connect()``."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import anyio

from axpilot.host.daemon import Host
from axpilot.host.pairing import PairingStore
from axpilot.server.context import ServerContext

__all__ = ["running_host", "short_socket_dir"]


@contextmanager
def short_socket_dir() -> Iterator[Path]:
    """A new, short folder for Unix sockets, removed when the block ends (``AF_UNIX`` paths are
    limited to about 104 bytes, too few for pytest's ``tmp_path``)."""
    folder = Path(tempfile.mkdtemp(prefix="ax", dir="/tmp"))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@asynccontextmanager
async def running_host(ctx: ServerContext, socket_path: Path, **options: Any) -> AsyncIterator[Host]:
    """A :class:`~axpilot.host.daemon.Host` serving ``socket_path`` until the block ends.

    ``options`` go to :class:`Host` (``pairing_confirmer``, ``max_clients``, ...); pairing decisions
    are kept next to the socket unless ``pairing`` is given. Unix socket paths are limited to about
    100 bytes, so use a short folder (pytest's ``tmp_path`` is usually too long)."""
    options.setdefault("pairing", PairingStore(socket_path.parent / "pairing.json"))
    host = Host(ctx=ctx, socket_path=socket_path, **options)
    async with anyio.create_task_group() as tg:
        await tg.start(host.serve)
        try:
            yield host
        finally:
            tg.cancel_scope.cancel()
