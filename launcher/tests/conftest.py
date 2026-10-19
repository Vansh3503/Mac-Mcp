"""Fixtures for the launcher tests: build locations and a harness that runs the launcher."""

from __future__ import annotations

import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

LAUNCHER_DIR = Path(__file__).resolve().parents[1]
STUB_HOST = Path(__file__).resolve().parent / "stub_host.py"
DEFAULT_BUNDLE = LAUNCHER_DIR / "build" / "AXPilot.app"


def source_version() -> str:
    """CFBundleShortVersionString of launcher/Info.plist."""
    with (LAUNCHER_DIR / "Info.plist").open("rb") as handle:
        return plistlib.load(handle)["CFBundleShortVersionString"]


@pytest.fixture(scope="session")
def launcher_version() -> str:
    return source_version()


@pytest.fixture(scope="session")
def stub_host() -> Path:
    return STUB_HOST


@pytest.fixture(scope="session")
def bundle() -> Path:
    """The built AXPilot.app (``$AXPILOT_LAUNCHER_BUNDLE`` overrides the default location)."""
    if sys.platform != "darwin":
        pytest.skip("the launcher is a macOS app bundle")
    path = Path(os.environ.get("AXPILOT_LAUNCHER_BUNDLE", DEFAULT_BUNDLE))
    if not (path / "Contents" / "MacOS" / "AXPilot").exists():
        pytest.skip(f"no launcher at {path}; run launcher/build.sh first")
    return path


@pytest.fixture(scope="session")
def launcher_bin(bundle: Path) -> Path:
    return bundle / "Contents" / "MacOS" / "AXPilot"


@dataclass
class LauncherRun:
    """One launcher process, started directly against a runtime.json in a temporary directory."""

    binary: Path
    root: Path
    process: subprocess.Popen[bytes] | None = None
    extra_fd: int | None = None
    extra_fd_number: int | None = None  # the descriptor number the launcher inherited
    started_at: float = field(default_factory=time.monotonic)

    @property
    def manifest(self) -> Path:
        return self.root / "runtime.json"

    @property
    def record(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def log_file(self) -> Path:
        return self.root / "logs" / "launcher.log"

    def write_manifest(
        self,
        *stub_args: str,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        **overrides: Any,
    ) -> None:
        """Write runtime.json that starts the stub host with ``stub_args``."""
        manifest: dict[str, Any] = {
            "version": 1,
            "python": sys.executable,
            "args": [str(STUB_HOST), "--record", str(self.record), *stub_args],
            "env": {"STUB_MARKER": "from-manifest", **(env or {})},
            "cwd": cwd,
        }
        manifest.update(overrides)
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    def start(self, *extra_args: str, manifest_flag: bool = True) -> subprocess.Popen[bytes]:
        """Start the launcher; an extra inheritable descriptor checks that the host never sees it."""
        read_end, write_end = os.pipe()
        os.close(read_end)
        self.extra_fd = self.extra_fd_number = write_end
        args = [str(self.binary), *extra_args]
        if manifest_flag:
            args += ["--runtime", str(self.manifest)]
        env = {
            **os.environ,
            "AXPILOT_LOG_DIR": str(self.root / "logs"),
            "AXPILOT_HOME": str(self.root / "home"),
        }
        self.started_at = time.monotonic()
        self.process = subprocess.Popen(
            args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(write_end,)
        )
        return self.process

    def wait(self, timeout: float = 30.0) -> int:
        assert self.process is not None
        try:
            return self.process.wait(timeout=timeout)
        finally:
            if self.extra_fd is not None:
                os.close(self.extra_fd)
                self.extra_fd = None

    def send(self, signum: int) -> None:
        assert self.process is not None
        self.process.send_signal(signum)

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        lines = self.record.read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines]
        return [e for e in events if kind is None or e["event"] == kind]

    def log(self) -> str:
        return self.log_file.read_text(encoding="utf-8") if self.log_file.exists() else ""

    def wait_until(self, condition: Callable[[], bool], timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out; launcher log:\n{self.log()}")
            time.sleep(0.05)

    def wait_for_starts(self, count: int, timeout: float = 15.0) -> None:
        self.wait_until(lambda: len(self.events("start")) >= count, timeout)

    def stop(self) -> None:
        """Kill a launcher that is still running. Stub hosts always end on their own (their
        --sleep is bounded), so their pids are not touched: they may already be reused."""
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL)
            self.process.wait(timeout=5)
        if self.extra_fd is not None:
            os.close(self.extra_fd)
            self.extra_fd = None


@pytest.fixture
def run(launcher_bin: Path, tmp_path: Path) -> Iterator[LauncherRun]:
    harness = LauncherRun(binary=launcher_bin, root=tmp_path)
    yield harness
    harness.stop()
