from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from axpilot.testing.host import short_socket_dir


@pytest.fixture(autouse=True)
def hermetic_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``Desktop.direct()`` and ``connect()`` use the default folders: keep them in ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AXPILOT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AXPILOT_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("AXPILOT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AXPILOT_SOCKET", "/nonexistent/axpilot.sock")
    monkeypatch.setenv("AXPILOT_APP_PATH", str(tmp_path / "AXPilot.app"))


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    """A short folder for Unix sockets."""
    with short_socket_dir() as folder:
        yield folder
