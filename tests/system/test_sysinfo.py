from __future__ import annotations

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import FakeRunner
from axpilot.system.sysinfo import (
    SYSCTL_KEYS,
    appearance_script,
    format_duration,
    parse_pmset_batt,
    parse_sw_vers,
    parse_sysctl,
    parse_volume_settings,
    set_appearance,
    set_volume,
    system_info,
)

NOW = 1_700_100_000.0
SW_VERS = "ProductName:\t\tmacOS\nProductVersion:\t\t15.4\nBuildVersion:\t\t24E248\n"
SYSCTL = (
    "hw.model: Mac14,2\n"
    "hw.memsize: 17179869184\n"
    "machdep.cpu.brand_string: Apple M2\n"
    "hw.ncpu: 8\n"
    "kern.boottime: { sec = 1700000000, usec = 123456 } Tue Nov 14 22:13:20 2023\n"
    "vm.loadavg: { 1.23 1.45 1.67 }\n"
)
PMSET_LAPTOP = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=4653155)\t95%; charging; 0:42 remaining present: true\n"
)
PMSET_DESKTOP = "Now drawing from 'AC Power'\n"
VOLUME = "output volume:50, input volume:75, alert volume:100, output muted:false\n"


def register_all(runner: FakeRunner) -> None:
    runner.register(["sw_vers"], stdout=SW_VERS)
    runner.register(["sysctl"], stdout=SYSCTL)
    runner.register(["pmset", "-g", "batt"], stdout=PMSET_LAPTOP)
    runner.register(["defaults", "read", "-g", "AppleInterfaceStyle"], stdout="Dark\n")
    runner.register(["osascript"], stdout=VOLUME)


def test_system_info_gathers_every_source(runner: FakeRunner) -> None:
    register_all(runner)
    info = system_info(runner, clock=lambda: NOW)
    assert info == {
        "os_name": "macOS",
        "os_version": "15.4",
        "os_build": "24E248",
        "model": "Mac14,2",
        "cpu": "Apple M2",
        "cpu_count": 8,
        "memory_bytes": 17179869184,
        "memory_gb": 16.0,
        "boot_time": 1700000000,
        "uptime_s": 100000,
        "uptime": "1d 3h 46m",
        "load_average": [1.23, 1.45, 1.67],
        "power_source": "AC Power",
        "battery": {"percent": 95, "state": "charging", "remaining": "0:42"},
        "appearance": "dark",
        "volume": {"output": 50, "input": 75, "alert": 100, "muted": False},
    }
    assert ["sysctl", *SYSCTL_KEYS] in [call.argv for call in runner.calls]
    assert runner.calls_for(["osascript"])[0].input_text == "get volume settings"


def test_system_info_never_raises_and_reports_unavailable_sources(runner: FakeRunner) -> None:
    # Nothing registered: every probe "is missing" (exit 127), as on Linux.
    assert system_info(runner) == {"unavailable": ["os", "hardware", "power", "appearance", "volume"]}


def test_system_info_tolerates_individual_failures(runner: FakeRunner) -> None:
    register_all(runner)
    runner.register(["sw_vers"], stderr="boom", exit_code=1)
    runner.register(["pmset", "-g", "batt"], timed_out=True)
    runner.register(["osascript"], stderr="execution error: nope (-1)", exit_code=1)
    runner.register(["sysctl"], stdout="hw.model: Mac14,2\n", stderr="sysctl: unknown oid 'x'\n", exit_code=1)
    info = system_info(runner, clock=lambda: NOW)
    assert info["model"] == "Mac14,2"
    assert info["appearance"] == "dark"
    assert "os_version" not in info
    assert "power_source" not in info
    assert "volume" not in info
    assert info["unavailable"] == ["os", "power", "volume"]


def test_system_info_tolerates_garbage_output(runner: FakeRunner) -> None:
    runner.register(["sw_vers"], stdout="\x00\x01 not what we expect")
    runner.register(["sysctl"], stdout="hw.memsize: lots\nkern.boottime: soon\nvm.loadavg: high\n")
    runner.register(["pmset", "-g", "batt"], stdout="???")
    runner.register(["defaults", "read", "-g", "AppleInterfaceStyle"], stderr="weird failure", exit_code=1)
    runner.register(["osascript"], stdout="output volume:missing value\n")
    assert system_info(runner) == {"unavailable": ["os", "hardware", "power", "appearance", "volume"]}


def test_light_mode_when_the_style_key_is_absent(runner: FakeRunner) -> None:
    runner.register(
        ["defaults", "read", "-g", "AppleInterfaceStyle"],
        stderr="The domain/default pair of (kCFPreferencesAnyApplication, AppleInterfaceStyle) "
        "does not exist\n",
        exit_code=1,
    )
    assert system_info(runner)["appearance"] == "light"


def test_parse_sw_vers_old_format() -> None:
    assert parse_sw_vers("ProductName:\tMac OS X\nProductVersion:\t10.15.7\nBuildVersion:\t19H2\n") == {
        "os_name": "Mac OS X",
        "os_version": "10.15.7",
        "os_build": "19H2",
    }


def test_parse_sysctl_keeps_labelled_lines_only() -> None:
    assert parse_sysctl("hw.ncpu: 8\nsysctl: unknown oid 'x'\nbogus\n") == {
        "hw.ncpu": "8",
        "sysctl": "unknown oid 'x'",
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            PMSET_LAPTOP,
            {
                "power_source": "AC Power",
                "battery": {"percent": 95, "state": "charging", "remaining": "0:42"},
            },
        ),
        (PMSET_DESKTOP, {"power_source": "AC Power"}),
        (
            "Now drawing from 'Battery Power'\n"
            " -InternalBattery-0 (id=1)\t80%; discharging; (no estimate) present: true\n",
            {
                "power_source": "Battery Power",
                "battery": {"percent": 80, "state": "discharging", "remaining": None},
            },
        ),
        (
            "Now drawing from 'AC Power'\n"
            " -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining present: true\n",
            {
                "power_source": "AC Power",
                "battery": {"percent": 100, "state": "charged", "remaining": "0:00"},
            },
        ),
        (
            "Now drawing from 'AC Power'\n"
            " -InternalBattery-0 (id=1)\t79%; AC attached; not charging present: true\n",
            {
                "power_source": "AC Power",
                "battery": {"percent": 79, "state": "ac attached", "remaining": None},
            },
        ),
        ("", {}),
    ],
)
def test_parse_pmset_batt(text: str, expected: dict) -> None:
    assert parse_pmset_batt(text) == expected


def test_parse_volume_settings() -> None:
    assert parse_volume_settings(VOLUME) == {"output": 50, "input": 75, "alert": 100, "muted": False}
    assert parse_volume_settings("output volume:missing value, output muted:true") == {"muted": True}
    assert parse_volume_settings("garbage") == {}


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0m"), (59, "0m"), (61, "1m"), (3700, "1h 1m"), (93780, "1d 2h 3m"), (-5, "0m")],
)
def test_format_duration(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text


# ---------------------------------------------------------------------------------------------
# Controls


def test_set_volume_level_and_mute(runner: FakeRunner) -> None:
    runner.register(
        ["osascript"], stdout="output volume:30, input volume:75, alert volume:100, output muted:true\n"
    )
    settings = set_volume(runner, 30, True)
    assert (
        runner.last_call.input_text
        == "set volume output volume 30\nset volume output muted true\nget volume settings"
    )
    assert settings == {"output": 30, "input": 75, "alert": 100, "muted": True}


def test_set_volume_only_mute(runner: FakeRunner) -> None:
    runner.register(["osascript"], stdout=VOLUME)
    set_volume(runner, mute=False)
    assert runner.last_call.input_text == "set volume output muted false\nget volume settings"


def test_set_volume_only_level(runner: FakeRunner) -> None:
    runner.register(["osascript"], stdout=VOLUME)
    set_volume(runner, level=0)
    assert runner.last_call.input_text == "set volume output volume 0\nget volume settings"


@pytest.mark.parametrize(
    ("level", "mute"),
    [
        (None, None),
        (-1, None),
        (101, None),
        (50.5, None),
        (True, None),
        ("50", None),
        (None, "yes"),
        (None, 1),
    ],
)
def test_set_volume_validation(runner: FakeRunner, level: object, mute: object) -> None:
    with pytest.raises(AXPilotError) as info:
        set_volume(runner, level, mute)  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert runner.calls == []


@pytest.mark.parametrize("dark", [True, False])
def test_set_appearance(runner: FakeRunner, dark: bool) -> None:
    runner.register(["osascript"], stdout="")
    set_appearance(runner, dark)
    script = runner.last_call.input_text or ""
    assert script == appearance_script(dark)
    assert 'tell application "System Events"' in script
    assert f"set dark mode to {'true' if dark else 'false'}" in script


def test_set_appearance_validation(runner: FakeRunner) -> None:
    with pytest.raises(AXPilotError) as info:
        set_appearance(runner, "dark")  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_set_appearance_without_automation_permission(runner: FakeRunner) -> None:
    runner.register(
        ["osascript"],
        stderr="0:80: execution error: Not authorized to send Apple events to System Events. (-1743)\n",
        exit_code=1,
    )
    with pytest.raises(AXPilotError) as info:
        set_appearance(runner, True)
    assert info.value.code is ErrorCode.PERMISSION_AUTOMATION
    assert info.value.details["app"] == "System Events"


class ExplodingRunner(FakeRunner):
    """A runner whose every call raises, to prove system_info never propagates errors."""

    def run(self, argv, **kwargs):  # type: ignore[no-untyped-def, override]
        raise AXPilotError(ErrorCode.COMMAND_FAILED, "cannot start anything")


def test_system_info_survives_a_runner_that_raises() -> None:
    assert system_info(ExplodingRunner()) == {
        "unavailable": ["os", "hardware", "power", "appearance", "volume"]
    }


def test_parse_volume_settings_skips_unknown_mute_state() -> None:
    assert parse_volume_settings("output volume:10, output muted:missing value") == {"output": 10}
