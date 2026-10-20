"""Hermetic fixtures for ``axpilot.setup``: a private HOME and AXPilot folders, a runner that
simulates uv/venv/pip/open/codesign without running anything, and fake launcher bundles."""

from __future__ import annotations

import plistlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import axpilot
from axpilot.setup import runtime
from axpilot.system.runner import CommandResult, CommandRunner


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME and every AXPilot location at ``tmp_path``; returns the fake home."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("AXPILOT_HOME", str(tmp_path / "support"))
    monkeypatch.setenv("AXPILOT_APP_PATH", str(tmp_path / "Applications" / "AXPilot.app"))
    monkeypatch.setenv("AXPILOT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AXPILOT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AXPILOT_SOCKET", str(tmp_path / "s.sock"))
    for var in ("UV", "UV_TOOL_BIN_DIR", "XDG_BIN_HOME", "PIPX_BIN_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    # A Mac with Homebrew's uv would otherwise always "have uv".
    monkeypatch.setattr(runtime, "SYSTEM_UV_PATHS", (tmp_path / "homebrew" / "bin" / "uv",))
    monkeypatch.chdir(tmp_path)
    return home


@dataclass
class Call:
    argv: list[str]
    cwd: str | None
    timeout_s: float


def _kind(args: list[str]) -> str:
    if "-c" in args and "-I" in args:
        return "probe"
    if "venv" in args[:3]:
        return "venv"
    if "install" in args[:4]:
        return "install"
    return Path(args[0]).name


@dataclass
class SimulatedRunner(CommandRunner):
    """Pretends to be uv/venv/pip/open/codesign. ``failures`` maps a command kind (``venv``,
    ``install``, ``probe``, ``open``, ``codesign``) to ``(exit_code, stderr)``."""

    version: str = axpilot.__version__
    failures: dict[str, tuple[int, str]] = field(default_factory=dict)
    codesign_stderr: str = ""
    calls: list[Call] = field(default_factory=list)

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
        self.calls.append(Call(args, cwd, timeout_s))
        kind = _kind(args)
        if kind in self.failures:
            code, stderr = self.failures[kind]
            return CommandResult(args, code, "", stderr)
        if kind == "venv":
            root = Path(args[-1])
            (root / "bin").mkdir(parents=True, exist_ok=True)
            (root / "pyvenv.cfg").write_text("home = /usr/bin\n")
            python = root / "bin" / "python"
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)
        if kind == "probe":
            return CommandResult(args, 0, self.version, "")
        if kind == "codesign":
            return CommandResult(args, 0, "", self.codesign_stderr)
        return CommandResult(args, 0, "", "")

    def kinds(self) -> list[str]:
        return [_kind(call.argv) for call in self.calls]


@pytest.fixture
def runner() -> SimulatedRunner:
    return SimulatedRunner()


@pytest.fixture
def make_executable() -> Callable[[Path], Path]:
    def make(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    return make


@pytest.fixture
def make_app(tmp_path: Path) -> Callable[..., Path]:
    """Build a fake ``AXPilot.app`` (inside a ``_launcher``-style folder) and return its path."""

    def make(
        folder: str = "bundle",
        *,
        binary: bytes = b"\xcf\xfa\xed\xfe fake launcher v1",
        version: str = "1.0.0",
        cdhash: str | None = None,
    ) -> Path:
        app = tmp_path / folder / "AXPilot.app"
        macos = app / "Contents" / "MacOS"
        macos.mkdir(parents=True)
        exe = macos / "AXPilot"
        exe.write_bytes(binary)
        exe.chmod(0o755)
        info = {"CFBundleIdentifier": axpilot.BUNDLE_ID, "CFBundleShortVersionString": version}
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
        signature = app / "Contents" / "_CodeSignature"
        signature.mkdir()
        (signature / "CodeResources").write_bytes(b"<plist>seal</plist>")
        (signature / "CodeResources").chmod(0o644)
        if cdhash is not None:
            (app.parent / "CDHASH").write_text(cdhash)
        return app

    return make
