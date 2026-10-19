from __future__ import annotations

import errno
import os
import plistlib
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from axpilot import BUNDLE_ID, paths
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.setup import launcher
from axpilot.setup.launcher import (
    LOGIN_AGENT_LABEL,
    QUARANTINE_XATTR,
    executable_path,
    expected_cdhashes,
    file_sha256,
    has_xattr,
    install_launcher,
    install_login_item,
    installed_cdhash,
    installed_version,
    is_installed,
    launch,
    launcher_status,
    login_agent_path,
    login_item_installed,
    parse_cdhash_file,
    quarantined_paths,
    remove_login_item,
    tree_manifest,
)

ARM = "a" * 40
INTEL = "b" * 40


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------------------------------
# Install


def test_install_copies_byte_for_byte_with_modes(env: Path, make_app: Callable[..., Path]) -> None:
    source = make_app()
    result = install_launcher(source=source)
    dest = paths.launcher_app_path()
    assert result.path == dest
    assert result.changed
    assert result.version == "1.0.0"
    assert result.sha256 == file_sha256(executable_path(source))
    assert tree_manifest(dest) == tree_manifest(source)
    assert executable_path(dest).read_bytes() == executable_path(source).read_bytes()
    assert mode_of(executable_path(dest)) == 0o755
    assert os.access(executable_path(dest), os.X_OK)
    assert is_installed()
    assert installed_version() == "1.0.0"
    assert [p.name for p in dest.parent.iterdir()] == ["AXPilot.app"]  # no staging leftovers


def test_install_is_idempotent(env: Path, make_app: Callable[..., Path]) -> None:
    source = make_app()
    install_launcher(source=source)
    exe = executable_path(paths.launcher_app_path())
    os.utime(exe, (1_000_000, 1_000_000))
    again = install_launcher(source=source)
    assert not again.changed
    assert exe.stat().st_mtime == 1_000_000


def test_install_replaces_an_older_launcher(env: Path, make_app: Callable[..., Path]) -> None:
    install_launcher(source=make_app("v1", binary=b"old binary", version="1.0.0"))
    dest = paths.launcher_app_path()
    (dest / "Contents" / "Resources").mkdir()
    (dest / "Contents" / "Resources" / "leftover").write_text("from v1")
    new = make_app("v2", binary=b"new binary", version="2.0.0")
    result = install_launcher(source=new)
    assert result.changed
    assert tree_manifest(dest) == tree_manifest(new)
    assert not (dest / "Contents" / "Resources").exists()
    assert installed_version() == "2.0.0"
    assert sorted(p.name for p in dest.parent.iterdir()) == ["AXPilot.app"]


def test_install_restores_mode_drift(env: Path, make_app: Callable[..., Path]) -> None:
    source = make_app()
    install_launcher(source=source)
    executable_path(paths.launcher_app_path()).chmod(0o644)
    assert install_launcher(source=source).changed
    assert mode_of(executable_path(paths.launcher_app_path())) == 0o755


def test_install_keeps_symlinks(env: Path, make_app: Callable[..., Path]) -> None:
    source = make_app()
    (source / "Contents" / "Current").symlink_to("MacOS")
    install_launcher(source=source)
    link = paths.launcher_app_path() / "Contents" / "Current"
    assert link.is_symlink()
    assert os.readlink(link) == "MacOS"


def test_install_to_explicit_destination(tmp_path: Path, env: Path, make_app: Callable[..., Path]) -> None:
    dest = tmp_path / "elsewhere" / "AXPilot.app"
    assert install_launcher(source=make_app(), dest=dest).path == dest
    assert is_installed(dest)


def test_failed_copy_verification_cleans_up(
    env: Path, make_app: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_app()
    real_manifest = launcher.tree_manifest
    calls = {"n": 0}

    def flaky_manifest(root: Path) -> dict[str, tuple[str, ...]]:
        calls["n"] += 1
        result = real_manifest(root)
        return {**result, "corrupt": ("file",)} if calls["n"] == 2 else result

    monkeypatch.setattr(launcher, "tree_manifest", flaky_manifest)
    with pytest.raises(AXPilotError) as info:
        install_launcher(source=source)
    assert info.value.code is ErrorCode.INTERNAL
    dest = paths.launcher_app_path()
    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []


def test_failed_move_keeps_the_previous_launcher(
    env: Path, make_app: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    old = make_app("v1", binary=b"old")
    install_launcher(source=old)
    real_replace = os.replace

    def replace(src: Any, dst: Any) -> None:
        if Path(dst) == paths.launcher_app_path() and Path(src).name == "AXPilot.app":
            raise OSError(errno.EACCES, "Permission denied")
        real_replace(src, dst)

    monkeypatch.setattr(launcher.os, "replace", replace)
    with pytest.raises(AXPilotError) as info:
        install_launcher(source=make_app("v2", binary=b"new"))
    assert info.value.code is ErrorCode.COMMAND_FAILED
    assert executable_path(paths.launcher_app_path()).read_bytes() == b"old"
    assert sorted(p.name for p in paths.launcher_app_path().parent.iterdir()) == ["AXPilot.app"]


def test_install_without_bundled_launcher(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "bundled_app", lambda: None)
    with pytest.raises(AXPilotError) as info:
        install_launcher()
    assert info.value.code is ErrorCode.NOT_SUPPORTED
    assert "direct mode" in (info.value.hint or "")


def test_install_rejects_a_damaged_source(env: Path, make_app: Callable[..., Path]) -> None:
    source = make_app()
    executable_path(source).chmod(0o644)
    with pytest.raises(AXPilotError) as info:
        install_launcher(source=source)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_bundled_launcher_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_app: Callable[..., Path]
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    monkeypatch.setattr(launcher.resources, "files", lambda name: package)
    assert launcher.bundled_dir() is None
    assert launcher.bundled_app() is None
    (package / "_launcher").mkdir()
    assert launcher.bundled_app() is None
    app = make_app("pkg/_launcher")
    assert launcher.bundled_app() == app


# --------------------------------------------------------------------------------------------
# Status and verification


def test_status_not_installed(env: Path, make_app: Callable[..., Path]) -> None:
    status = launcher_status(source=make_app(cdhash=f"arm64 {ARM}\n"))
    assert not status.installed
    assert status.bundled_sha256 is not None
    assert status.hash_matches is None
    assert status.expected_cdhash == {"arm64": ARM}


def test_status_identical(env: Path, make_app: Callable[..., Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "has_xattr", lambda path, name: False)
    source = make_app()
    install_launcher(source=source)
    status = launcher_status(source=source)
    assert status.installed
    assert status.identical
    assert status.hash_matches
    assert status.version == status.bundled_version == "1.0.0"
    assert status.quarantined == ()


def test_status_detects_a_changed_executable(env: Path, make_app: Callable[..., Path]) -> None:
    source = make_app()
    install_launcher(source=source)
    executable_path(paths.launcher_app_path()).write_bytes(b"tampered")
    status = launcher_status(source=source)
    assert status.identical is False
    assert status.hash_matches is False


def test_status_detects_other_changed_files(env: Path, make_app: Callable[..., Path]) -> None:
    source = make_app()
    install_launcher(source=source)
    (paths.launcher_app_path() / "Contents" / "Info.plist").write_bytes(b"changed")
    status = launcher_status(source=source)
    assert status.identical is False
    assert status.hash_matches is True


def test_status_without_bundled_copy(
    env: Path, make_app: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_launcher(source=make_app())
    monkeypatch.setattr(launcher, "bundled_app", lambda: None)
    status = launcher_status()
    assert status.installed
    assert status.identical is None
    assert status.hash_matches is None


def test_installed_version_requires_an_executable(env: Path, make_app: Callable[..., Path]) -> None:
    assert installed_version() is None
    install_launcher(source=make_app())
    (paths.launcher_app_path() / "Contents" / "Info.plist").write_text("not a plist")
    assert installed_version() is None


# --------------------------------------------------------------------------------------------
# cdhash


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"arm64 {ARM}\nx86_64 {INTEL}\n", {"arm64": ARM, "x86_64": INTEL}),
        (
            f"# published hashes\narm64={ARM.upper()}\n\nx86_64: {INTEL}  # intel\n",
            {"arm64": ARM, "x86_64": INTEL},
        ),
        (f"{ARM}\n", {"*": ARM}),
        ("", {}),
    ],
)
def test_parse_cdhash_file(text: str, expected: dict[str, str]) -> None:
    assert parse_cdhash_file(text) == expected


@pytest.mark.parametrize("text", ["arm64 xyz\n", "arm64\n", f"arm64 {ARM} extra\n"])
def test_parse_cdhash_file_rejects_garbage(text: str) -> None:
    with pytest.raises(AXPilotError) as info:
        parse_cdhash_file(text)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_expected_cdhashes_from_folder(tmp_path: Path) -> None:
    assert expected_cdhashes(tmp_path) == {}
    (tmp_path / "CDHASH").write_text(f"arm64 {ARM}\n")
    assert expected_cdhashes(tmp_path) == {"arm64": ARM}


def test_installed_cdhash_parses_codesign_output(env: Path, runner: Any) -> None:
    runner.codesign_stderr = f"Executable=/x\nIdentifier=io.github.vansh3503.axpilot\nCDHash={ARM.upper()}\n"
    assert installed_cdhash(runner, arch="arm64") == ARM
    assert runner.calls[-1].argv == ["codesign", "-dvvv", "-a", "arm64", str(paths.launcher_app_path())]
    runner.codesign_stderr = "no hash here"
    assert installed_cdhash(runner) is None
    runner.failures["codesign"] = (1, "code object is not signed at all")
    assert installed_cdhash(runner, arch="arm64") is None


# --------------------------------------------------------------------------------------------
# Quarantine


def test_quarantine_unknown_on_linux(env: Path, make_app: Callable[..., Path]) -> None:
    app = make_app()
    if has_xattr(app, QUARANTINE_XATTR) is not None:  # pragma: no cover - macOS
        pytest.skip("this filesystem supports com.apple.* attributes")
    assert quarantined_paths(app) is None


def test_quarantined_paths_lists_flagged_items(
    env: Path, make_app: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    flagged = {app, executable_path(app)}
    monkeypatch.setattr(launcher, "has_xattr", lambda path, name: Path(path) in flagged)
    assert quarantined_paths(app) == (".", "Contents/MacOS/AXPilot")
    flagged.clear()
    assert quarantined_paths(app) == ()


def test_has_xattr_with_a_supported_namespace(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_text("x")
    try:
        os.setxattr(path, "user.axpilot-test", b"1")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pytest.skip("user extended attributes are not supported here")
    assert has_xattr(path, "user.axpilot-test") is True
    assert has_xattr(path, "user.absent") is False


# --------------------------------------------------------------------------------------------
# Launch and login item


def test_launch_is_macos_only(env: Path, runner: Any) -> None:
    with pytest.raises(AXPilotError) as info:
        launch(runner, system="linux")
    assert info.value.code is ErrorCode.UNSUPPORTED_OS
    assert runner.calls == []


def test_launch_requires_an_installed_launcher(env: Path, runner: Any) -> None:
    with pytest.raises(AXPilotError) as info:
        launch(runner, system="darwin")
    assert info.value.code is ErrorCode.HOST_UNAVAILABLE


def test_launch_uses_open(env: Path, runner: Any, make_app: Callable[..., Path]) -> None:
    install_launcher(source=make_app())
    launch(runner, system="darwin")
    launch(runner, system="darwin", args=["--runtime", "/x/runtime.json"])
    app = str(paths.launcher_app_path())
    assert [call.argv for call in runner.calls] == [
        ["open", "-g", "-a", app],
        ["open", "-g", "-a", app, "--args", "--runtime", "/x/runtime.json"],
    ]


def test_launch_failure(env: Path, runner: Any, make_app: Callable[..., Path]) -> None:
    install_launcher(source=make_app())
    runner.failures["open"] = (1, "LSOpenURLsWithRole() failed with error -10810")
    with pytest.raises(AXPilotError) as info:
        launch(runner, system="darwin")
    assert info.value.code is ErrorCode.COMMAND_FAILED


def test_login_item_lifecycle(env: Path) -> None:
    assert not login_item_installed()
    path, changed = install_login_item()
    assert changed
    assert path == env / "Library" / "LaunchAgents" / f"{LOGIN_AGENT_LABEL}.plist" == login_agent_path()
    plist = plistlib.loads(path.read_bytes())
    assert plist["Label"] == LOGIN_AGENT_LABEL
    assert plist["ProgramArguments"] == ["/usr/bin/open", "-g", "-a", str(paths.launcher_app_path())]
    assert plist["RunAtLoad"] is True
    assert plist["AssociatedBundleIdentifiers"] == [BUNDLE_ID]
    assert mode_of(path) == 0o644
    assert install_login_item() == (path, False)
    assert login_item_installed()
    assert remove_login_item() is True
    assert remove_login_item() is False
    assert not login_item_installed()
