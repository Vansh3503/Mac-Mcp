"""Spike S1: the probe's pure logic and graceful degradation, and the installer."""

from __future__ import annotations

import ctypes
import json
import os
import plistlib
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

BUNDLE_ID = "io.github.vansh3503.axpilot"


@pytest.fixture(scope="module")
def probe(load: Callable[[str], ModuleType]) -> ModuleType:
    return load("s1_launcher_tcc/probe.py")


@pytest.fixture(scope="module")
def install(load: Callable[[str], ModuleType]) -> ModuleType:
    return load("s1_launcher_tcc/install.py")


def make_bundle(path: Path, bundle_id: str = BUNDLE_ID) -> Path:
    (path / "Contents" / "MacOS").mkdir(parents=True)
    with (path / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": "1.0.0"}, handle)
    executable = path / "Contents" / "MacOS" / "AXPilot"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    return path


# --- probe: pure helpers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("executable", "bundle"),
    [
        ("/Users/me/Applications/AXPilot.app/Contents/MacOS/AXPilot", "/Users/me/Applications/AXPilot.app"),
        (
            "/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app/Contents/MacOS/Simulator",
            "/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app",
        ),
        ("/usr/bin/python3", None),
        ("/Applications/Foo.app/Contents/Resources/helper", None),
        (None, None),
    ],
)
def test_bundle_of(probe: ModuleType, executable: str | None, bundle: str | None) -> None:
    assert probe.bundle_of(executable) == bundle


@pytest.mark.parametrize(
    ("text", "pid"),
    [
        ('"pid"=1234', 1234),
        ('"pid" = 77\n', 77),
        ("pid=5", 5),
        ("", None),
        (None, None),
        ("no pid here", None),
    ],
)
def test_parse_lsappinfo_pid(probe: ModuleType, text: str | None, pid: int | None) -> None:
    assert probe.parse_lsappinfo_pid(text) == pid


def test_parse_quarantine(probe: ModuleType) -> None:
    decoded = probe.parse_quarantine(b"0083;65f1c2a3;Safari;6C3A9D1E-0000-0000-0000-000000000000")
    assert decoded["flags"] == "0083"
    assert decoded["agent"] == "Safari"
    assert decoded["time"].startswith("2024-03-13")
    assert probe.parse_quarantine(None) is None
    assert probe.parse_quarantine(b"garbage") == {"raw": "garbage"}
    assert probe.parse_quarantine(b"0083;zz;curl;x")["time"] is None


def test_parse_codesign(probe: ModuleType) -> None:
    text = "\n".join(
        [
            "Executable=/x/AXPilot.app/Contents/MacOS/AXPilot",
            f"Identifier={BUNDLE_ID}",
            "CandidateCDHash sha256=ffff",
            "CDHash=abcd",
            "Signature=adhoc",
            "TeamIdentifier=not set",
        ]
    )
    fields = probe.parse_codesign(text)
    assert fields == {
        "Executable": "/x/AXPilot.app/Contents/MacOS/AXPilot",
        "Identifier": BUNDLE_ID,
        "CDHash": "abcd",
        "Signature": "adhoc",
        "TeamIdentifier": "not set",
    }
    assert probe.parse_codesign(None) == {}


def test_parent_chain_walks_to_launchd(probe: ModuleType) -> None:
    table = {
        300: {"pid": 300, "ppid": 200, "name": "python3", "path": "/usr/bin/python3"},
        200: {"pid": 200, "ppid": 1, "name": "AXPilot", "path": "/A/AXPilot.app/Contents/MacOS/AXPilot"},
        1: {"pid": 1, "ppid": 0, "name": "launchd", "path": "/sbin/launchd"},
    }
    chain = probe.parent_chain(300, table.get)
    assert [entry["pid"] for entry in chain] == [300, 200, 1]


def test_parent_chain_is_bounded_and_cycle_safe(probe: ModuleType) -> None:
    looping = probe.parent_chain(10, lambda pid: {"pid": pid, "ppid": 11 if pid == 10 else 10})
    assert [entry["pid"] for entry in looping] == [10, 11]
    endless = probe.parent_chain(5, lambda pid: {"pid": pid, "ppid": pid + 1})
    assert len(endless) == probe.MAX_CHAIN
    unknown = probe.parent_chain(7, lambda pid: None)
    assert unknown == [{"pid": 7, "ppid": None, "path": None, "name": None}]


def test_proc_bsdinfo_matches_the_c_layout(probe: ModuleType) -> None:
    assert ctypes.sizeof(probe.ProcBSDInfo) == 136
    assert probe.ProcBSDInfo.pbi_ppid.offset == 16
    assert probe.ProcBSDInfo.pbi_start_tvsec.offset == 120


# --- probe: verdicts -----------------------------------------------------------------------------


def report_for(
    *,
    under_launcher: bool = True,
    trusted: bool | None = True,
    read_ok: bool | None = True,
    responsible: str | None = "/Users/me/Applications/AXPilot.app/Contents/MacOS/AXPilot",
    responsibility_available: bool = True,
    quarantined: bool = False,
    ax_available: bool = True,
) -> dict[str, Any]:
    app = "/Users/me/Applications/AXPilot.app"
    return {
        "launcher": {"under_launcher": under_launcher},
        "accessibility": {"available": ax_available, "trusted": trusted, "read_ok": read_ok},
        "screen_recording": {"available": True, "preflight": False, "other_app_window_titles_visible": False},
        "responsibility": {
            "available": responsibility_available,
            "path": responsible,
            "bundle": responsible.rsplit("/Contents/", 1)[0]
            if responsible and ".app/" in responsible
            else None,
        },
        "bundle": {"path": app, "quarantined": quarantined},
    }


def test_verdict_pass(probe: ModuleType) -> None:
    summary = probe.summarize_report(report_for())
    assert summary["verdict"] == "pass"
    assert summary["responsible_is_launcher"] is True
    assert summary["reasons"] == []


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"trusted": False}, "AXIsProcessTrusted() is false"),
        ({"read_ok": False}, "reading the frontmost app over AX failed"),
        ({"responsible": "/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"}, "Terminal"),
        ({"quarantined": True}, "com.apple.quarantine"),
    ],
)
def test_verdict_fail_reasons(probe: ModuleType, changes: dict[str, Any], reason: str) -> None:
    summary = probe.summarize_report(report_for(**changes))
    assert summary["verdict"] == "fail"
    assert any(reason in text for text in summary["reasons"])


def test_verdict_without_the_responsibility_api_relies_on_trust(probe: ModuleType) -> None:
    summary = probe.summarize_report(report_for(responsibility_available=False))
    assert summary["responsible_is_launcher"] is None
    assert summary["verdict"] == "pass"


def test_direct_runs_are_controls_and_unloadable_ax_is_inconclusive(probe: ModuleType) -> None:
    assert probe.summarize_report(report_for(under_launcher=False, trusted=False))["verdict"] == "control"
    assert probe.summarize_report(report_for(ax_available=False))["verdict"] == "inconclusive"


# --- probe: running it ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "darwin", reason="checks the non-macOS degradation path")
def test_collect_degrades_gracefully_off_macos(probe: ModuleType) -> None:
    report = probe.collect(probe.parse_args(["--label", "ci"]))
    json.dumps(report)  # serialisable as is
    assert report["label"] == "ci"
    assert report["accessibility"]["available"] is False
    assert report["screen_recording"]["available"] is False
    assert report["responsibility"]["available"] is False
    assert report["process"]["pid"] == os.getpid()
    assert report["process"]["parent_chain"][0]["pid"] == os.getpid()
    assert report["summary"]["verdict"] == "inconclusive"
    assert set(report["native_load_errors"]) >= {"CoreFoundation", "CoreGraphics", "libSystem"}


def test_launcher_environment_is_detected(probe: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AXPILOT_LAUNCHER_VERSION", "1.0.0")
    monkeypatch.setenv("AXPILOT_LAUNCHER_BUNDLE", "/tmp/AXPilot.app")
    report = probe.collect(probe.parse_args([]))
    assert report["launcher"] == {"under_launcher": True, "version": "1.0.0", "bundle": "/tmp/AXPilot.app"}
    assert report["bundle"]["path"] == "/tmp/AXPilot.app"
    assert report["process"]["env"]["AXPILOT_LAUNCHER_VERSION"] == "1.0.0"


def test_main_writes_both_reports(probe: ModuleType, tmp_path: Path, isolated_home: Path) -> None:
    out = tmp_path / "report.json"
    assert probe.main(["--label", "t", "--out-file", str(out)]) == 0
    assert json.loads(out.read_text())["label"] == "t"
    assert json.loads((isolated_home / "spike-s1.json").read_text())["label"] == "t"


def test_main_strict_fails_only_on_fail(
    probe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_collect(args: Any, native: Any = None) -> dict[str, Any]:
        report = report_for(trusted=False)
        report.update(label=args.label, environment={}, summary=probe.summarize_report(report))
        return report

    monkeypatch.setattr(probe, "collect", fake_collect)
    out = str(tmp_path / "r.json")
    assert probe.main(["--out-file", out, "--no-app-support-copy"]) == 0
    assert probe.main(["--out-file", out, "--no-app-support-copy", "--strict"]) == 1


def test_probe_bundle_reports_quarantine_and_executable_bit(
    probe: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_bundle(tmp_path / "AXPilot.app")
    quarantined = {app, app / "Contents" / "MacOS" / "AXPilot"}
    monkeypatch.setattr(
        probe, "xattr_names", lambda native, path: [probe.QUARANTINE_XATTR] if path in quarantined else []
    )
    monkeypatch.setattr(
        probe,
        "xattr_value",
        lambda native, path, name: b"0083;65f1c2a3;Safari;x" if path in quarantined else None,
    )
    result = probe.probe_bundle(probe.Native(), app)
    assert result["exists"] is True
    assert result["bundle_id"] == BUNDLE_ID
    assert result["executable_is_executable"] is True
    assert result["quarantined"] is True
    assert result["quarantine"]["agent"] == "Safari"
    assert result["quarantined_files"] == ["Contents/MacOS/AXPilot"]


def test_probe_bundle_of_a_clean_or_missing_bundle(probe: ModuleType, tmp_path: Path) -> None:
    app = make_bundle(tmp_path / "AXPilot.app")
    result = probe.probe_bundle(probe.Native(), app)
    assert result["quarantined"] is False
    assert result["quarantine"] is None
    assert probe.probe_bundle(probe.Native(), tmp_path / "missing.app") == {
        "path": str(tmp_path / "missing.app"),
        "exists": False,
    }


# --- installer -----------------------------------------------------------------------------------


def test_install_bundle_copies_contents_and_mode_only(install: ModuleType, tmp_path: Path) -> None:
    source = make_bundle(tmp_path / "build" / "AXPilot.app")
    destination = tmp_path / "Applications" / "AXPilot.app"
    assert install.install_bundle(source, destination) == destination
    executable = destination / "Contents" / "MacOS" / "AXPilot"
    assert executable.read_text() == "#!/bin/sh\n"
    assert executable.stat().st_mode & stat.S_IXUSR
    assert not list(destination.parent.glob(".*installing"))


def test_install_bundle_replaces_an_older_axpilot(install: ModuleType, tmp_path: Path) -> None:
    source = make_bundle(tmp_path / "build" / "AXPilot.app")
    destination = make_bundle(tmp_path / "Applications" / "AXPilot.app")
    (destination / "Contents" / "stale").write_text("old", encoding="utf-8")
    install.install_bundle(source, destination)
    assert not (destination / "Contents" / "stale").exists()


@pytest.mark.parametrize("case", ["foreign-source", "not-an-app", "foreign-destination"])
def test_install_bundle_refusals(install: ModuleType, tmp_path: Path, case: str) -> None:
    source = make_bundle(
        tmp_path / "src" / "AXPilot.app", "com.example.other" if case == "foreign-source" else BUNDLE_ID
    )
    destination = tmp_path / ("AXPilot" if case == "not-an-app" else "AXPilot.app")
    if case == "foreign-destination":
        make_bundle(destination, "com.example.precious")
    with pytest.raises(install.InstallError):
        install.install_bundle(source, destination)
    if case == "foreign-destination":
        assert (destination / "Contents" / "Info.plist").exists()  # untouched


def test_manifest_round_trip(install: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "rt" / "runtime.json"
    code = install.main(
        [
            "manifest",
            str(out),
            "--python",
            sys.executable,
            "--env",
            "A=1",
            "--env",
            "B=x=y",
            "--",
            "probe.py",
            "--x",
        ]
    )
    assert code == 0
    document = json.loads(out.read_text())
    assert document == {
        "version": 1,
        "python": sys.executable,
        "args": ["probe.py", "--x"],
        "env": {"A": "1", "B": "x=y"},
        "cwd": None,
    }
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("python", "cwd", "env"),
    [
        ("python3", None, {}),
        ("/nonexistent/python", None, {}),
        (sys.executable, "relative", {}),
        (sys.executable, None, {"": "x"}),
    ],
)
def test_manifest_validation_matches_the_launcher(
    install: ModuleType, python: str, cwd: str | None, env: dict[str, str]
) -> None:
    with pytest.raises(install.InstallError):
        install.build_manifest(python, [], env, cwd)


def test_installer_cli_reports_errors(
    install: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert install.main(["manifest", str(tmp_path / "r.json"), "--python", "python3"]) == 1
    assert "absolute path" in capsys.readouterr().err
    assert (
        install.main(["manifest", str(tmp_path / "r.json"), "--python", sys.executable, "--env", "noequals"])
        == 1
    )
    assert install.main(["bundle", str(tmp_path / "nope.app"), str(tmp_path / "AXPilot.app")]) == 1
