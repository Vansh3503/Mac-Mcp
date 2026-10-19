"""Fixtures shared across test folders."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    """A short folder for Unix sockets (AF_UNIX paths are limited to ~104 bytes, too few for
    pytest's ``tmp_path``)."""
    folder = Path(tempfile.mkdtemp(prefix="ax", dir="/tmp"))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)
