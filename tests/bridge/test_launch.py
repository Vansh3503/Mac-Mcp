"""Starting the host on demand and identifying the client app behind a bridge."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from axpilot.bridge.launch import ensure_host_running, identify_parent, parse_ps_parent
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.bundles import bundle_id_of, enclosing_app
from axpilot.system.runner import FakeRunner


def make_app(folder: Path, bundle_id: str | None) -> Path:
    contents = folder / "Contents" / "MacOS"
    contents.mkdir(parents=True)
    if bundle_id is not None:
        with (folder / "Contents" / "Info.plist").open("wb") as file:
            plistlib.dump({"CFBundleIdentifier": bundle_id}, file)
    return folder


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# =========================================================================== ensure_host_running
def test_a_running_host_is_used_as_is(tmp_path: Path) -> None:
    runner = FakeRunner()
    path = ensure_host_running(tmp_path / "s.sock", runner=runner, live=lambda _: True, platform="linux")
    assert path == tmp_path / "s.sock"
    assert runner.calls == []


def test_there_is_no_host_outside_macos(tmp_path: Path) -> None:
    with pytest.raises(AXPilotError) as caught:
        ensure_host_running(tmp_path / "s.sock", live=lambda _: False, platform="linux")
    assert caught.value.code is ErrorCode.HOST_UNAVAILABLE
    assert "--engine fake" in (caught.value.hint or "")


def test_the_launcher_must_be_installed(tmp_path: Path) -> None:
    with pytest.raises(AXPilotError) as caught:
        ensure_host_running(
            tmp_path / "s.sock", live=lambda _: False, platform="darwin", app_path=tmp_path / "AXPilot.app"
        )
    assert caught.value.code is ErrorCode.HOST_UNAVAILABLE
    assert "axpilot setup" in (caught.value.hint or "")


def test_the_launcher_is_opened_and_awaited(tmp_path: Path) -> None:
    app = make_app(tmp_path / "AXPilot.app", "io.github.vansh3503.axpilot")
    runner = FakeRunner()
    runner.register(["open"])
    clock = Clock()
    checks = iter([False, False, False, True])
    path = ensure_host_running(
        tmp_path / "s.sock", runner=runner, live=lambda _: next(checks), platform="darwin", app_path=app,
        sleep=clock.sleep, clock=clock,
    )  # fmt: skip
    assert path == tmp_path / "s.sock"
    assert runner.calls[0].argv == ["open", "-g", "-a", str(app)]


def test_a_failing_open_is_reported(tmp_path: Path) -> None:
    app = make_app(tmp_path / "AXPilot.app", None)
    runner = FakeRunner()
    runner.register(["open"], exit_code=1, stderr="LSOpenURLsWithRole() failed with error -10810")
    with pytest.raises(AXPilotError) as caught:
        ensure_host_running(
            tmp_path / "s.sock", runner=runner, live=lambda _: False, platform="darwin", app_path=app
        )
    assert caught.value.code is ErrorCode.HOST_UNAVAILABLE
    assert "-10810" in caught.value.message


def test_a_host_that_never_comes_up_times_out(tmp_path: Path) -> None:
    app = make_app(tmp_path / "AXPilot.app", None)
    runner = FakeRunner()
    runner.register(["open"])
    clock = Clock()
    with pytest.raises(AXPilotError) as caught:
        ensure_host_running(
            tmp_path / "s.sock", runner=runner, live=lambda _: False, platform="darwin", app_path=app,
            timeout_s=2, sleep=clock.sleep, clock=clock,
        )  # fmt: skip
    assert caught.value.code is ErrorCode.HOST_UNAVAILABLE
    assert "within 2 s" in caught.value.message
    assert clock.now >= 2


# =========================================================================== identify_parent
def test_the_client_is_the_outermost_app_among_the_ancestors(tmp_path: Path) -> None:
    claude = make_app(tmp_path / "Claude.app", "com.anthropic.claudefordesktop")
    helper = claude / "Contents" / "Frameworks" / "Claude Helper.app" / "Contents" / "MacOS" / "Claude Helper"
    runner = FakeRunner()
    runner.register(["ps", "-o", "ppid=,comm=", "-p", "100"], stdout="  200 /bin/zsh\n")
    runner.register(["ps", "-o", "ppid=,comm=", "-p", "200"], stdout=f"  300 {helper}\n")
    identity = identify_parent(runner=runner, start_pid=100, own_pid=99)
    assert identity.parent_app == "Claude"
    assert identity.parent_bundle_id == "com.anthropic.claudefordesktop"
    assert identity.pid == 99


def test_without_an_app_the_nearest_command_names_the_client() -> None:
    runner = FakeRunner()
    runner.register(["ps", "-o", "ppid=,comm=", "-p", "100"], stdout="  1 /usr/local/bin/node\n")
    identity = identify_parent(runner=runner, start_pid=100, own_pid=99)
    assert identity.parent_app == "node"
    assert identity.parent_bundle_id is None


def test_identifying_never_fails() -> None:
    runner = FakeRunner()  # every ps call "fails" (exit 127)
    identity = identify_parent(runner=runner, start_pid=100, own_pid=99)
    assert identity.parent_app is None
    assert identity.key == "unknown"
    assert identify_parent(runner=runner, start_pid=1, own_pid=99).parent_app is None


NOTES = "/System/Applications/Notes.app/Contents/MacOS/Notes"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (f"  812 {NOTES}\n", (812, NOTES)),
        (f"1 {CHROME}", (1, CHROME)),
        ("", None),
        ("abc /bin/zsh", None),
    ],
)
def test_parse_ps_parent(output: str, expected: tuple[int, str] | None) -> None:
    assert parse_ps_parent(output) == expected


# =========================================================================== bundles
def test_enclosing_app_is_the_outermost_bundle() -> None:
    assert enclosing_app("/Applications/Claude.app/Contents/Frameworks/Helper.app/Contents/MacOS/H") == Path(
        "/Applications/Claude.app"
    )
    assert enclosing_app("/usr/bin/python3") is None
    assert enclosing_app("/weird/.app/x") is None


def test_bundle_id_of(tmp_path: Path) -> None:
    assert bundle_id_of(make_app(tmp_path / "A.app", "com.example.a")) == "com.example.a"
    assert bundle_id_of(make_app(tmp_path / "B.app", None)) is None
    broken = make_app(tmp_path / "C.app", None)
    (broken / "Contents" / "Info.plist").write_text("garbage", encoding="utf-8")
    assert bundle_id_of(broken) is None
    blank = make_app(tmp_path / "D.app", None)
    with (blank / "Contents" / "Info.plist").open("wb") as file:
        plistlib.dump({"CFBundleIdentifier": "  "}, file)
    assert bundle_id_of(blank) is None
