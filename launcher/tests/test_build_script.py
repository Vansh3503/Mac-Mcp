"""Tests for launcher/build.sh with the Apple tools stubbed out, so they run on any OS.

They check the script's own logic: preflight checks, the compile and lipo invocations, signing
flags, verification of the result, and the packaging outputs. Whether the real tools accept those
flags is checked by running build.sh on a Mac (CI's launcher.yml and the macOS-only
test_launcher.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

LAUNCHER_DIR = Path(__file__).resolve().parents[1]
STUBS = Path(__file__).resolve().parent / "apple_tool_stubs.py"
TOOLS = ("uname", "xcrun", "codesign", "plutil", "PlistBuddy", "ditto", "sw_vers", "xattr", "arch", "shasum")

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


class BuildEnv:
    """A copy of launcher/ plus stubbed Apple tools, in a temporary directory."""

    def __init__(self, root: Path) -> None:
        self.src = root / "launcher"
        self.out = root / "out"
        self.bin = root / "stub-bin"
        self.calls_file = root / "calls.jsonl"
        self.src.mkdir()
        for name in ("main.swift", "Info.plist", "build.sh"):
            shutil.copy2(LAUNCHER_DIR / name, self.src / name)
        self.bin.mkdir()
        for tool in TOOLS:
            wrapper = self.bin / tool
            command = shlex.join([sys.executable, str(STUBS), tool])
            wrapper.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)

    @property
    def app(self) -> Path:
        return self.out / "AXPilot.app"

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        full_env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "PLISTBUDDY": str(self.bin / "PlistBuddy"),
            "AXPILOT_LAUNCHER_OUT": str(self.out),
            "STUB_LOG": str(self.calls_file),
            **env,
        }
        return subprocess.run(
            ["bash", str(self.src / "build.sh")],
            env=full_env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def calls(self, tool: str) -> list[list[str]]:
        lines = self.calls_file.read_text(encoding="utf-8").splitlines()
        return [entry["args"] for entry in map(json.loads, lines) if entry["tool"] == tool]

    def set_plist_version(self, version: str) -> None:
        path = self.src / "Info.plist"
        with path.open("rb") as handle:
            info = plistlib.load(handle)
        info["CFBundleShortVersionString"] = version
        with path.open("wb") as handle:
            plistlib.dump(info, handle)


@pytest.fixture
def build(tmp_path: Path) -> BuildEnv:
    return BuildEnv(tmp_path)


def test_build_produces_signed_bundle_and_release_files(build: BuildEnv) -> None:
    result = build.run()
    assert result.returncode == 0, result.stderr

    info_plist = build.app / "Contents" / "Info.plist"
    binary = build.app / "Contents" / "MacOS" / "AXPilot"
    assert info_plist.read_bytes() == (build.src / "Info.plist").read_bytes()
    assert os.access(binary, os.X_OK)
    assert (build.app / "Contents" / "_CodeSignature" / "CodeResources").exists()
    assert not (build.out / "obj").exists()

    with zipfile.ZipFile(build.out / "AXPilot.app.zip") as archive:
        names = set(archive.namelist())
    assert {"AXPilot.app/Contents/Info.plist", "AXPilot.app/Contents/MacOS/AXPilot"} <= names

    sums = dict(reversed(line.split("  ", 1)) for line in (build.out / "SHA256SUMS").read_text().splitlines())
    bundle_files = sorted(str(p.relative_to(build.out)) for p in build.app.rglob("*") if p.is_file())
    assert sorted(sums) == sorted([*bundle_files, "AXPilot.app.zip"])
    for name, digest in sums.items():
        assert hashlib.sha256((build.out / name).read_bytes()).hexdigest() == digest

    buildinfo = dict(
        line.split("=", 1) for line in (build.out / "BUILDINFO.txt").read_text().splitlines() if "=" in line
    )
    assert buildinfo["launcher_version"] == "1.0.0"
    assert buildinfo["bundle_id"] == "io.github.vansh3503.axpilot"
    assert buildinfo["cdhash_arm64"] == "a" * 40
    assert buildinfo["cdhash_x86_64"] == "b" * 40
    assert buildinfo["git_commit"] == "unknown"  # the temporary copy is not a git checkout

    assert f"cdhash (arm64): {'a' * 40}" in result.stdout
    assert f"cdhash (x86_64): {'b' * 40}" in result.stdout
    assert "designated requirement: cdhash H" in result.stdout


def test_compiles_both_architectures_with_the_documented_flags(build: BuildEnv) -> None:
    assert build.run().returncode == 0
    compiles = [args for args in build.calls("xcrun") if args[0] == "swiftc" and "-o" in args]
    targets = sorted(args[args.index("-target") + 1] for args in compiles)
    assert targets == ["arm64-apple-macos14", "x86_64-apple-macos14"]
    for args in compiles:
        assert "-O" in args
        assert args[args.index("-module-name") + 1] == "AXPilot"
        assert args[-1] == "main.swift"  # relative: no absolute build path in the binary
    lipo = next(args for args in build.calls("xcrun") if args[:2] == ["lipo", "-create"])
    assert lipo[2:4] == ["-output", str(build.app / "Contents" / "MacOS" / "AXPilot")]
    assert [Path(slice_).name for slice_ in lipo[4:]] == ["AXPilot-arm64", "AXPilot-x86_64"]


def test_signs_ad_hoc_without_hardened_runtime_or_timestamp(build: BuildEnv) -> None:
    assert build.run().returncode == 0
    (sign,) = [args for args in build.calls("codesign") if "--sign" in args]
    assert sign[:4] == ["--force", "--sign", "-", "--timestamp=none"]
    assert not any(arg.startswith("--options") or arg == "-o" for arg in sign)
    assert any(args[:3] == ["--verify", "--strict", "--verbose=2"] for args in build.calls("codesign"))
    assert build.calls("xattr") == [["-cr", str(build.app)]]


def test_smoke_test_runs_the_x86_64_slice_under_rosetta(build: BuildEnv) -> None:
    assert build.run().returncode == 0
    assert any(args[0] == "-x86_64" for args in build.calls("arch"))


def test_rebuild_replaces_the_previous_output(build: BuildEnv) -> None:
    assert build.run().returncode == 0
    stale = build.app / "Contents" / "Resources" / "stale.txt"
    stale.parent.mkdir()
    stale.write_text("left over", encoding="utf-8")
    assert build.run().returncode == 0
    assert not stale.exists()


def test_refuses_to_build_off_macos(build: BuildEnv) -> None:
    result = build.run(STUB_UNAME_S="Linux")
    assert result.returncode == 1
    assert "can only be built on macOS" in result.stderr
    assert not build.out.exists()


def test_version_mismatch_between_swift_and_plist_is_fatal(build: BuildEnv) -> None:
    build.set_plist_version("9.9.9")
    result = build.run()
    assert result.returncode == 1
    assert "version mismatch" in result.stderr


def test_missing_architecture_slice_is_fatal(build: BuildEnv) -> None:
    result = build.run(STUB_ARCHS="arm64")
    assert result.returncode == 1
    assert "no x86_64 slice" in result.stderr


def test_wrong_version_output_is_fatal(build: BuildEnv) -> None:
    result = build.run(STUB_VERSION_OUTPUT="AXPilot launcher 0.0.0")
    assert result.returncode == 1
    assert "unexpected --version output" in result.stderr


def test_stub_host_records_its_start(tmp_path: Path) -> None:
    """The stub host used by test_launcher.py works on its own (so failures there are real)."""
    record = tmp_path / "events.jsonl"
    stub = Path(__file__).resolve().parent / "stub_host.py"
    result = subprocess.run(
        [sys.executable, str(stub), "--record", str(record), "--exit-sequence", "3,0", "--say", "HI"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 3
    assert "HI on stdout" in result.stdout
    assert "HI on stderr" in result.stderr
    again = [sys.executable, str(stub), "--record", str(record), "--exit-sequence", "3,0"]
    second = subprocess.run(again, timeout=30, check=False)
    assert second.returncode == 0
    starts = [json.loads(line) for line in record.read_text().splitlines()]
    assert [s["index"] for s in starts] == [0, 1]
    assert starts[0]["ppid"] == os.getpid()
    assert starts[0]["signals"]["SIGTERM"] == "default"
    assert {0, 1, 2} <= set(starts[0]["fds"])
