from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from axpilot import paths
from axpilot.setup import (
    PlatformInfo,
    SetupReport,
    SetupStep,
    check_platform,
    launcher,
    parse_version,
    platform_info,
    run_setup,
)
from axpilot.setup.launcher import login_item_installed


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A minimal AXPilot source folder used as ``dev_source``."""
    folder = tmp_path / "checkout"
    folder.mkdir()
    (folder / "pyproject.toml").write_text('[project]\nname = "axpilot"\n')
    return folder


def mac_setup(runner: Any, checkout: Path, app: Path | None, **kwargs: Any) -> SetupReport:
    return run_setup(
        runner=runner,
        dev_source=checkout,
        launcher_source=app,
        system="darwin",
        os_version="15.4",
        machine="arm64",
        use_uv=False,
        **kwargs,
    )


def names(report: SetupReport) -> list[str]:
    return [step.name for step in report.steps]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("14", (14,)), ("14.6.1", (14, 6, 1)), ("26.0", (26, 0)), ("15.4b2", (15, 4)), ("", ()), ("beta", ())],
)
def test_parse_version(text: str, expected: tuple[int, ...]) -> None:
    assert parse_version(text) == expected


@pytest.mark.parametrize(
    ("info", "ok", "fragment"),
    [
        (PlatformInfo("darwin", "14.0", "arm64"), True, "macOS 14.0 (arm64)"),
        (PlatformInfo("darwin", "27.1", "arm64"), True, "macOS 27.1"),
        (PlatformInfo("darwin", "13.6.1", "x86_64"), False, "too old"),
        (PlatformInfo("darwin", None, "arm64"), False, "Could not determine"),
        (PlatformInfo("linux", None, "x86_64"), False, "--engine fake"),
    ],
)
def test_check_platform(info: PlatformInfo, ok: bool, fragment: str) -> None:
    supported, detail = check_platform(info)
    assert supported is ok
    assert fragment in detail


def test_platform_info_overrides() -> None:
    info = platform_info(system="darwin", os_version="15.0", machine="arm64")
    assert info == PlatformInfo("darwin", "15.0", "arm64")
    assert info.is_macos


def test_setup_on_linux_reports_macos_only(env: Path, runner: Any) -> None:
    report = run_setup(runner=runner, login_item=True, dev_source=None, system="linux")
    assert not report.supported
    assert not report.ok
    assert names(report) == ["platform"]
    assert "macOS-only" in report.steps[0].detail
    assert runner.calls == []
    assert not paths.app_support_dir().exists()


def test_setup_on_old_macos(env: Path, runner: Any) -> None:
    report = run_setup(runner=runner, login_item=False, dev_source=None, system="darwin", os_version="13.6")
    assert not report.supported
    assert report.steps == [SetupStep("platform", False, report.steps[0].detail)]
    assert "too old" in report.steps[0].detail


def test_full_setup_on_macos(env: Path, runner: Any, checkout: Path, make_app: Callable[..., Path]) -> None:
    report = mac_setup(runner, checkout, make_app(), login_item=True, launch_app=True)
    assert report.ok, report.format()
    assert names(report) == ["platform", "runtime", "manifest", "launcher", "login item", "launch"]
    manifest = json.loads(paths.runtime_manifest().read_text())
    assert manifest["python"] == str(paths.runtime_dir() / "bin" / "python")
    assert manifest["args"] == ["-m", "axpilot.host"]
    assert launcher.is_installed()
    assert login_item_installed()
    assert runner.kinds() == ["venv", "install", "probe", "open"]
    assert "Created" in report.steps[1].detail


def test_setup_twice_is_quiet(env: Path, runner: Any, checkout: Path, make_app: Callable[..., Path]) -> None:
    app = make_app()
    mac_setup(runner, checkout, app)
    report = mac_setup(runner, checkout, app)
    assert report.ok
    assert "Updated" in report.steps[1].detail
    assert "already up to date" in report.steps[3].detail


def test_runtime_failure_skips_manifest_but_installs_launcher(
    env: Path, runner: Any, checkout: Path, make_app: Callable[..., Path]
) -> None:
    runner.failures["install"] = (1, "error: network unreachable")
    report = mac_setup(runner, checkout, make_app(), launch_app=True)
    steps = {step.name: step for step in report.steps}
    assert not report.ok
    assert report.supported
    assert not steps["runtime"].ok
    assert "network unreachable" in steps["runtime"].detail
    assert not steps["manifest"].ok
    assert "Skipped" in steps["manifest"].detail
    assert steps["launcher"].ok
    assert not steps["launch"].ok
    assert "Skipped" in steps["launch"].detail
    assert "open" not in runner.kinds()
    assert not paths.runtime_manifest().exists()


def test_missing_launcher_is_reported(
    env: Path, runner: Any, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "bundled_app", lambda: None)
    report = mac_setup(runner, checkout, None)
    steps = {step.name: step for step in report.steps}
    assert steps["runtime"].ok
    assert steps["manifest"].ok
    assert not steps["launcher"].ok
    assert "direct mode" in steps["launcher"].detail


def test_a_checkout_installs_its_own_launcher_build(
    env: Path, runner: Any, checkout: Path, make_app: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A development install bundles no launcher; `launcher/build.sh` left one in the checkout."""
    monkeypatch.setattr(launcher, "bundled_app", lambda: None)
    built = make_app("checkout/launcher/build")
    report = mac_setup(runner, checkout, None)
    steps = {step.name: step for step in report.steps}
    assert steps["launcher"].ok, steps["launcher"].detail
    installed = paths.launcher_app_path() / "Contents" / "MacOS" / "AXPilot"
    assert installed.read_bytes() == (built / "Contents" / "MacOS" / "AXPilot").read_bytes()


def test_a_bundled_launcher_wins_over_a_checkout_build(
    env: Path, runner: Any, checkout: Path, make_app: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = make_app("bundle", binary=b"\xcf\xfa\xed\xfe bundled")
    make_app("checkout/launcher/build", binary=b"\xcf\xfa\xed\xfe local build")
    monkeypatch.setattr(launcher, "bundled_app", lambda: bundled)
    assert mac_setup(runner, checkout, None).ok
    installed = paths.launcher_app_path() / "Contents" / "MacOS" / "AXPilot"
    assert installed.read_bytes() == b"\xcf\xfa\xed\xfe bundled"


def test_quarantined_launcher_adds_a_step(
    env: Path, runner: Any, checkout: Path, make_app: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "has_xattr", lambda path, name: True)
    report = mac_setup(runner, checkout, make_app())
    step = report.steps[-1]
    assert step.name == "quarantine"
    assert not step.ok
    assert "xattr -dr com.apple.quarantine" in step.detail


def test_launch_failure_is_a_failed_step(
    env: Path, runner: Any, checkout: Path, make_app: Callable[..., Path]
) -> None:
    runner.failures["open"] = (1, "LSOpenURLsWithRole() failed")
    report = mac_setup(runner, checkout, make_app(), launch_app=True)
    assert report.steps[-1].name == "launch"
    assert not report.steps[-1].ok


def test_report_format_and_dict() -> None:
    report = SetupReport()
    report.add("platform", True, "macOS 15.4 (arm64)")
    report.add("launcher", False, "Not bundled.")
    assert report.format() == "✓ platform  macOS 15.4 (arm64)\n✗ launcher  Not bundled."
    assert report.to_dict() == {
        "ok": False,
        "supported": True,
        "steps": [
            {"name": "platform", "ok": True, "detail": "macOS 15.4 (arm64)"},
            {"name": "launcher", "ok": False, "detail": "Not bundled."},
        ],
    }
    name, ok, detail = report.steps[0]
    assert (name, ok, detail) == ("platform", True, "macOS 15.4 (arm64)")
    assert not SetupReport().ok
