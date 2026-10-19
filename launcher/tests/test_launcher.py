"""Behaviour tests for the built launcher (macOS only; run ``launcher/build.sh`` first).

They run ``AXPilot.app/Contents/MacOS/AXPilot --runtime <tmp>/runtime.json`` directly, with the
stub host (``stub_host.py``) in place of ``python -m axpilot.host``, and check the contract in
launcher/README.md. TCC attribution itself is spike S1's job: run directly, the launcher's parent
is pytest, not launchd.
"""

from __future__ import annotations

import itertools
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from conftest import LauncherRun

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the launcher is a macOS app bundle")


def gaps(starts: list[dict]) -> list[float]:
    return [later["time"] - earlier["time"] for earlier, later in itertools.pairwise(starts)]


# --- command line --------------------------------------------------------------------------------


def test_version_matches_info_plist(launcher_bin: Path, bundle: Path, launcher_version: str) -> None:
    out = subprocess.run([launcher_bin, "--version"], capture_output=True, text=True, check=True, timeout=10)
    with (bundle / "Contents" / "Info.plist").open("rb") as handle:
        built_version = plistlib.load(handle)["CFBundleShortVersionString"]
    assert out.stdout.strip() == f"AXPilot launcher {launcher_version}"
    assert built_version == launcher_version


def test_runtime_flag_without_a_value_is_a_usage_error(launcher_bin: Path) -> None:
    result = subprocess.run(
        [launcher_bin, "--runtime"], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 64
    assert "usage" in result.stderr


def test_unknown_arguments_are_ignored(run: LauncherRun) -> None:
    run.write_manifest()
    run.start("-psn_0_12345", "--bogus")
    assert run.wait() == 0
    assert len(run.events("start")) == 1


def test_default_manifest_location_honours_axpilot_home(run: LauncherRun) -> None:
    run.write_manifest()
    (run.root / "home").mkdir()
    shutil.copy(run.manifest, run.root / "home" / "runtime.json")
    run.start(manifest_flag=False)
    assert run.wait() == 0
    assert len(run.events("start")) == 1


# --- runtime.json validation ---------------------------------------------------------------------


def test_missing_manifest_exits_78(run: LauncherRun) -> None:
    run.start()
    assert run.wait() == 78
    assert "error:" in run.log()
    assert run.events() == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"version": 2}, id="newer-version"),
        pytest.param({"python": "python3"}, id="relative-python"),
        pytest.param({"python": "/nonexistent/python3"}, id="missing-python"),
        pytest.param({"python": "/etc/hosts"}, id="python-not-executable"),
        pytest.param({"cwd": "relative/dir"}, id="relative-cwd"),
        pytest.param({"env": {"BAD=KEY": "x"}}, id="env-key-with-equals"),
        pytest.param({"args": "not-a-list"}, id="args-wrong-type"),
    ],
)
def test_invalid_manifest_exits_78(run: LauncherRun, overrides: dict) -> None:
    run.write_manifest(**overrides)
    run.start()
    assert run.wait() == 78
    assert "error:" in run.log()
    assert run.events() == []


def test_manifest_without_optional_keys_is_accepted(run: LauncherRun, stub_host: Path) -> None:
    manifest = {"version": 1, "python": sys.executable, "args": [str(stub_host), "--record", str(run.record)]}
    run.manifest.write_text(json.dumps(manifest), encoding="utf-8")
    run.start()
    assert run.wait() == 0


# --- the host process ----------------------------------------------------------------------------


def test_host_process_contract(run: LauncherRun, bundle: Path, launcher_version: str) -> None:
    run.write_manifest("--say", "MARKER-42")
    process = run.start()
    assert run.wait() == 0
    (start,) = run.events("start")
    assert start["ppid"] == process.pid  # spawned by the launcher, not exec'd in its place
    assert start["pgid"] == start["pid"]  # its own process group
    assert start["stdin_is_devnull"]
    assert run.extra_fd_number not in start["fds"]  # the launcher inherited it; the host must not
    assert start["fds"] == [0, 1, 2]
    assert start["signals"]["SIGTERM"] == "default"
    assert start["signals"]["SIGHUP"] == "default"
    assert start["signals"]["SIGQUIT"] == "default"
    assert start["signals"]["SIGINT"] == "python-default"  # Python only installs it over SIG_DFL
    assert start["blocked_signals"] == []
    env = start["env"]
    assert env["STUB_MARKER"] == "from-manifest"
    assert env["AXPILOT_LAUNCHER_VERSION"] == launcher_version
    assert Path(env["AXPILOT_LAUNCHER_BUNDLE"]).resolve() == bundle.resolve()
    log = run.log()
    assert "MARKER-42 on stdout" in log
    assert "MARKER-42 on stderr" in log
    assert f"started host pid {start['pid']}" in log
    assert "not launchd" in log  # we started it from pytest, and it says so


def test_cwd_from_manifest(run: LauncherRun, tmp_path: Path) -> None:
    workdir = tmp_path / "work dir"
    workdir.mkdir()
    run.write_manifest(cwd=str(workdir))
    run.start()
    assert run.wait() == 0
    (start,) = run.events("start")
    assert Path(start["cwd"]).resolve() == workdir.resolve()


def test_log_is_rotated_at_startup(run: LauncherRun) -> None:
    run.log_file.parent.mkdir(parents=True)
    run.log_file.write_bytes(b"old\n" * 1_300_000)  # 5.2 MB
    run.write_manifest()
    run.start()
    assert run.wait() == 0
    rotated = run.log_file.with_name("launcher.log.1")
    assert rotated.stat().st_size == 5_200_000
    assert run.log_file.stat().st_size < 10_000


# --- exit statuses and restarts ------------------------------------------------------------------


def test_restart_after_a_failure_then_clean_exit(run: LauncherRun) -> None:
    run.write_manifest("--exit-sequence", "3,0")
    run.start()
    assert run.wait() == 0
    starts = run.events("start")
    assert len(starts) == 2
    assert 0.9 <= gaps(starts)[0] < 3.0
    assert "restarting the host in 1 s" in run.log()


def test_death_by_signal_is_restarted(run: LauncherRun) -> None:
    run.write_manifest("--die-by", str(int(signal.SIGUSR1)))
    run.start()
    assert run.wait() == 0
    assert len(run.events("start")) == 2
    assert f"(signal {int(signal.SIGUSR1)})" in run.log()


def test_exit_78_is_not_restarted(run: LauncherRun) -> None:
    run.write_manifest("--exit-sequence", "78,0")
    run.start()
    assert run.wait() == 78
    assert len(run.events("start")) == 1


def test_manifest_is_reread_before_each_restart(run: LauncherRun) -> None:
    run.write_manifest("--exit-sequence", "1,0", "--sleep", "0.5")
    run.start()
    run.wait_for_starts(1)
    run.write_manifest("--exit-sequence", "1,0", env={"STUB_MARKER": "rewritten"})
    assert run.wait() == 0
    first, second = run.events("start")
    assert first["env"]["STUB_MARKER"] == "from-manifest"
    assert second["env"]["STUB_MARKER"] == "rewritten"


def test_gives_up_after_five_restarts_within_a_minute(run: LauncherRun) -> None:
    run.write_manifest("--exit-sequence", "1")
    run.start()
    assert run.wait(timeout=60) == 1
    starts = run.events("start")
    assert len(starts) == 6
    for measured, expected in zip(gaps(starts), [1, 2, 4, 8, 16], strict=True):
        assert expected - 0.2 <= measured <= expected + 1.5
    assert "giving up" in run.log()


# --- signals -------------------------------------------------------------------------------------


def test_sigterm_is_forwarded_and_stops_everything(run: LauncherRun) -> None:
    run.write_manifest("--on-term", "exit", "--sleep", "30")
    run.start()
    run.wait_for_starts(1)
    run.send(signal.SIGTERM)
    assert run.wait() == 0
    assert [e["signum"] for e in run.events("signal")] == [int(signal.SIGTERM)]
    assert len(run.events("start")) == 1  # a requested stop is not a crash


def test_status_of_a_host_killed_by_the_forwarded_signal(run: LauncherRun) -> None:
    run.write_manifest("--sleep", "30")
    run.start()
    run.wait_for_starts(1)
    run.send(signal.SIGTERM)
    assert run.wait() == 128 + signal.SIGTERM
    assert len(run.events("start")) == 1


def test_sigint_is_forwarded(run: LauncherRun) -> None:
    run.write_manifest("--sleep", "30")
    run.start()
    run.wait_for_starts(1)
    run.send(signal.SIGINT)
    assert run.wait() == 128 + signal.SIGINT  # Python re-raises SIGINT after KeyboardInterrupt
    assert len(run.events("start")) == 1


def test_host_ignoring_sigterm_is_killed_after_the_grace_period(run: LauncherRun) -> None:
    run.write_manifest("--on-term", "ignore", "--sleep", "30")
    run.start()
    run.wait_for_starts(1)
    sent = time.monotonic()
    run.send(signal.SIGTERM)
    assert run.wait() == 128 + signal.SIGKILL
    assert 9.5 <= time.monotonic() - sent <= 14
    assert "sending SIGKILL" in run.log()


def test_sighup_is_forwarded_without_stopping(run: LauncherRun) -> None:
    run.write_manifest("--on-hup", "record", "--on-term", "exit", "--sleep", "30")
    run.start()
    run.wait_for_starts(1)
    run.send(signal.SIGHUP)
    run.wait_until(lambda: bool(run.events("signal")))
    assert run.process is not None
    assert run.process.poll() is None
    run.send(signal.SIGTERM)
    assert run.wait() == 0
    assert [e["signum"] for e in run.events("signal")] == [int(signal.SIGHUP), int(signal.SIGTERM)]


def test_host_killed_by_sighup_is_restarted(run: LauncherRun) -> None:
    run.write_manifest("--on-term", "exit", "--sleep", "30")
    run.start()
    run.wait_for_starts(1)
    run.send(signal.SIGHUP)
    run.wait_for_starts(2)
    run.send(signal.SIGTERM)
    assert run.wait() == 0


def test_stop_signal_during_backoff_exits_immediately(run: LauncherRun) -> None:
    run.write_manifest("--exit-sequence", "1")
    run.start()
    run.wait_until(lambda: "restarting the host" in run.log())
    run.send(signal.SIGTERM)
    assert run.wait(timeout=5) == 0
    assert len(run.events("start")) == 1


# --- the bundle ----------------------------------------------------------------------------------


def test_bundle_is_ad_hoc_signed_and_universal(bundle: Path, launcher_bin: Path) -> None:
    subprocess.run(["codesign", "--verify", "--strict", bundle], check=True, timeout=30)
    details = subprocess.run(
        ["codesign", "-dvvv", bundle], capture_output=True, text=True, check=True, timeout=30
    ).stderr
    assert "Identifier=io.github.vansh3503.axpilot" in details.splitlines()
    assert "Signature=adhoc" in details.splitlines()
    archs = subprocess.run(
        ["lipo", "-archs", launcher_bin], capture_output=True, text=True, check=True, timeout=30
    ).stdout.split()
    assert {"arm64", "x86_64"} <= set(archs)


def test_bundle_info_plist(bundle: Path) -> None:
    with (bundle / "Contents" / "Info.plist").open("rb") as handle:
        info = plistlib.load(handle)
    assert info["CFBundleIdentifier"] == "io.github.vansh3503.axpilot"
    assert info["CFBundleExecutable"] == "AXPilot"
    assert info["LSUIElement"] is True
    assert info["LSMinimumSystemVersion"] == "14.0"
    assert info["NSAppleEventsUsageDescription"]
    assert os.access(bundle / "Contents" / "MacOS" / "AXPilot", os.X_OK)


@pytest.mark.e2e
@pytest.mark.skipif(not os.environ.get("AXPILOT_LAUNCHER_E2E"), reason="set AXPILOT_LAUNCHER_E2E=1")
def test_started_through_launchservices(run: LauncherRun, bundle: Path) -> None:
    """``open -g -a`` makes launchd the launcher's parent (needs a logged-in GUI session).

    The launcher reads AXPILOT_* from launchd's environment, not ours, so this uses --runtime and
    the default log location.
    """
    if subprocess.run(["pgrep", "-x", "AXPilot"], capture_output=True, check=False).returncode == 0:
        pytest.skip("AXPilot is already running; quit it first")
    run.write_manifest("--on-term", "exit", "--sleep", "30")
    subprocess.run(["open", "-g", "-a", bundle, "--args", "--runtime", run.manifest], check=True, timeout=30)
    run.wait_for_starts(1, timeout=30)
    (start,) = run.events("start")
    launcher_pid = start["ppid"]
    parent = subprocess.run(
        ["ps", "-o", "ppid=", "-p", str(launcher_pid)], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert parent == "1"
    os.kill(launcher_pid, signal.SIGTERM)
    run.wait_until(lambda: bool(run.events("signal")))
