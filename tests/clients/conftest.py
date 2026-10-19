"""Hermetic fixtures for the client configuration writers.

Every test gets its own ``HOME``, an empty ``PATH`` (a folder the test can drop fake programs
into) and an empty ``/Applications`` stand-in, so nothing on the machine running the tests (a
real Claude Desktop, a real ``claude`` CLI) can influence the results.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from axpilot.clients import base
from axpilot.system.runner import CommandResult, CommandRunner

FIXED_TIME = datetime(2026, 10, 19, 9, 30, 15)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    applications = tmp_path / "Applications"
    applications.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    for var in ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME", "PIPX_BIN_DIR", "XDG_CONFIG_HOME", "UV"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(base, "SYSTEM_APPLICATIONS_DIR", applications)
    monkeypatch.chdir(tmp_path)
    return home


@pytest.fixture
def bin_dir(home: Path) -> Path:
    """The only folder on ``PATH``."""
    return home.parent / "bin"


@pytest.fixture
def applications(home: Path) -> Path:
    """Stands in for ``/Applications``."""
    return home.parent / "Applications"


@pytest.fixture
def make_executable() -> Callable[[Path], Path]:
    def make(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    return make


@pytest.fixture
def clock() -> Callable[[], datetime]:
    return lambda: FIXED_TIME


@dataclass
class StubRunner(CommandRunner):
    """Records commands and answers them from ``responses`` (argv prefix → exit code, stderr)."""

    responses: dict[tuple[str, ...], tuple[int, str]] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_s: float = 30,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        max_output: int = 20_000,
    ) -> CommandResult:
        args = list(argv)
        self.calls.append(args)
        best: tuple[str, ...] | None = None
        for prefix in self.responses:
            if tuple(args[: len(prefix)]) == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        code, stderr = self.responses[best] if best is not None else (0, "")
        return CommandResult(args, code, "", stderr)


@pytest.fixture
def runner() -> StubRunner:
    return StubRunner()


@pytest.fixture
def claude_cli(bin_dir: Path, make_executable: Callable[[Path], Path]) -> Path:
    """A fake ``claude`` program on PATH (never executed: tests pass a StubRunner)."""
    return make_executable(bin_dir / "claude")
