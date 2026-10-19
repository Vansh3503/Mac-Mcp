from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from axpilot.testing.host import short_socket_dir


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    """A short folder for Unix sockets."""
    with short_socket_dir() as folder:
        yield folder
