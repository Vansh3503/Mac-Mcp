"""System information and simple system controls (volume, light/dark appearance).

:func:`system_info` is **best effort**: each source (``sw_vers``, ``sysctl``, ``pmset``,
``defaults``, ``osascript``) is queried separately, and a source that fails, times out or prints
something unexpected only leaves its keys out. The names of failed sources are listed under
``"unavailable"``. It never raises for a failing command.

Keys (each present only when its source worked)::

    os_name, os_version, os_build                    sw_vers
    model, cpu, cpu_count, memory_bytes, memory_gb,  sysctl hw.model hw.memsize
    boot_time, uptime_s, uptime, load_average          machdep.cpu.brand_string hw.ncpu
                                                       kern.boottime vm.loadavg
    power_source, battery {percent, state, remaining}  pmset -g batt (battery only on laptops)
    appearance ("dark" | "light")                      defaults read -g AppleInterfaceStyle
    volume {output, input, alert, muted}               osascript "get volume settings"
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.osascript import output_text, run_applescript
from axpilot.system.runner import CommandResult, CommandRunner

__all__ = [
    "SYSCTL_KEYS",
    "appearance_script",
    "format_duration",
    "parse_pmset_batt",
    "parse_sw_vers",
    "parse_sysctl",
    "parse_volume_settings",
    "set_appearance",
    "set_volume",
    "system_info",
]

_log = logging.getLogger(__name__)

SYSCTL_KEYS: tuple[str, ...] = (
    "hw.model",
    "hw.memsize",
    "machdep.cpu.brand_string",
    "hw.ncpu",
    "kern.boottime",
    "vm.loadavg",
)
INFO_TIMEOUT_S = 5.0
CONTROL_TIMEOUT_S = 10.0

_SW_VERS_KEYS = {"ProductName": "os_name", "ProductVersion": "os_version", "BuildVersion": "os_build"}
_KEY_VALUE_RE = re.compile(r"^\s*([A-Za-z0-9_.]+)\s*:\s*(.*?)\s*$")
_BOOTTIME_RE = re.compile(r"\bsec\s*=\s*(\d+)")
_LOADAVG_RE = re.compile(r"\{\s*([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s*\}")
_POWER_SOURCE_RE = re.compile(r"drawing from '([^']+)'")
_BATTERY_RE = re.compile(r"InternalBattery[^\n]*?(\d{1,3})%;\s*([^;\n]+?)\s*;\s*([^\n]*)")
_REMAINING_RE = re.compile(r"(\d+:\d{2}) remaining")
_VOLUME_RE = re.compile(r"(output volume|input volume|alert volume|output muted)\s*:\s*([^,]+)")
_VOLUME_KEYS = {
    "output volume": "output",
    "input volume": "input",
    "alert volume": "alert",
    "output muted": "muted",
}


# --------------------------------------------------------------------------------------------
# Parsers (pure functions, tolerant of missing pieces)


def parse_sw_vers(text: str) -> dict[str, str]:
    """``ProductName: macOS`` … → ``{"os_name": "macOS", "os_version": …, "os_build": …}``."""
    info: dict[str, str] = {}
    for line in text.splitlines():
        match = _KEY_VALUE_RE.match(line)
        if match and match.group(1) in _SW_VERS_KEYS and match.group(2):
            info[_SW_VERS_KEYS[match.group(1)]] = match.group(2)
    return info


def parse_sysctl(text: str) -> dict[str, str]:
    """``name: value`` lines of ``sysctl <names>`` → ``{name: value}`` (unknown names are absent)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _KEY_VALUE_RE.match(line)
        if match and match.group(2):
            values[match.group(1)] = match.group(2)
    return values


def format_duration(seconds: float) -> str:
    """``93780`` → ``"1d 2h 3m"``; ``3700`` → ``"1h 1m"``; ``59`` → ``"0m"``."""
    total_minutes = max(int(seconds), 0) // 60
    days, rest = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _hardware_from_sysctl(values: dict[str, str], now: float) -> dict[str, Any]:
    info: dict[str, Any] = {}
    if model := values.get("hw.model"):
        info["model"] = model
    if cpu := values.get("machdep.cpu.brand_string"):
        info["cpu"] = cpu
    if (ncpu := values.get("hw.ncpu", "")).isdigit():
        info["cpu_count"] = int(ncpu)
    if (memsize := values.get("hw.memsize", "")).isdigit():
        info["memory_bytes"] = int(memsize)
        info["memory_gb"] = round(int(memsize) / 2**30, 1)
    if boot := _BOOTTIME_RE.search(values.get("kern.boottime", "")):
        boot_time = int(boot.group(1))
        uptime_s = max(int(now) - boot_time, 0)
        info.update(boot_time=boot_time, uptime_s=uptime_s, uptime=format_duration(uptime_s))
    if load := _LOADAVG_RE.search(values.get("vm.loadavg", "")):
        info["load_average"] = [float(group.replace(",", ".")) for group in load.groups()]
    return info


def parse_pmset_batt(text: str) -> dict[str, Any]:
    """``pmset -g batt`` → ``{"power_source": …, "battery": {percent, state, remaining}}``.

    ``battery`` is absent on Macs without one; ``remaining`` is ``None`` while macOS has no
    estimate.
    """
    info: dict[str, Any] = {}
    if source := _POWER_SOURCE_RE.search(text):
        info["power_source"] = source.group(1)
    if battery := _BATTERY_RE.search(text):
        remaining = _REMAINING_RE.search(battery.group(3))
        info["battery"] = {
            "percent": int(battery.group(1)),
            "state": battery.group(2).strip().lower(),
            "remaining": remaining.group(1) if remaining else None,
        }
    return info


def parse_volume_settings(text: str) -> dict[str, Any]:
    """``output volume:50, input volume:75, alert volume:100, output muted:false`` →
    ``{"output": 50, "input": 75, "alert": 100, "muted": False}`` (``missing value`` is skipped).
    """
    settings: dict[str, Any] = {}
    for label, raw in _VOLUME_RE.findall(text):
        key, value = _VOLUME_KEYS[label], raw.strip()
        if key == "muted":
            if value in ("true", "false"):
                settings[key] = value == "true"
        elif value.isdigit():
            settings[key] = int(value)
    return settings


# --------------------------------------------------------------------------------------------
# Gathering


def _quiet_run(runner: CommandRunner, argv: list[str]) -> CommandResult | None:
    """Run a probe; ``None`` if it timed out, is missing or could not start (logged)."""
    try:
        result = runner.run(argv, timeout_s=INFO_TIMEOUT_S)
    except AXPilotError as exc:
        _log.warning("system info probe %s failed: %s", argv[0], exc)
        return None
    if result.timed_out or result.exit_code == 127:
        _log.info("system info probe %s unavailable (exit %d)", argv[0], result.exit_code)
        return None
    return result


def _gather_os(runner: CommandRunner, now: float) -> dict[str, Any]:
    result = _quiet_run(runner, ["sw_vers"])
    return parse_sw_vers(result.stdout) if result and result.ok else {}


def _gather_hardware(runner: CommandRunner, now: float) -> dict[str, Any]:
    # Without -n, sysctl labels every line, so an unknown name cannot shift the others.
    # A non-zero exit (one unknown name) still leaves the other lines usable.
    result = _quiet_run(runner, ["sysctl", *SYSCTL_KEYS])
    return _hardware_from_sysctl(parse_sysctl(result.stdout), now) if result else {}


def _gather_power(runner: CommandRunner, now: float) -> dict[str, Any]:
    result = _quiet_run(runner, ["pmset", "-g", "batt"])
    return parse_pmset_batt(result.stdout) if result and result.ok else {}


def _gather_appearance(runner: CommandRunner, now: float) -> dict[str, Any]:
    result = _quiet_run(runner, ["defaults", "read", "-g", "AppleInterfaceStyle"])
    if result is None:
        return {}
    if result.ok:
        return {"appearance": "dark" if result.stdout.strip().lower() == "dark" else "light"}
    if "does not exist" in result.stderr:  # the key is only set in dark mode
        return {"appearance": "light"}
    return {}


def _gather_volume(runner: CommandRunner, now: float) -> dict[str, Any]:
    try:
        result = run_applescript(runner, "get volume settings", timeout_s=INFO_TIMEOUT_S)
    except AXPilotError as exc:
        _log.info("volume settings unavailable: %s", exc)
        return {}
    settings = parse_volume_settings(output_text(result))
    return {"volume": settings} if settings else {}


_Gatherer = Callable[[CommandRunner, float], dict[str, Any]]
_GATHERERS: tuple[tuple[str, _Gatherer], ...] = (
    ("os", _gather_os),
    ("hardware", _gather_hardware),
    ("power", _gather_power),
    ("appearance", _gather_appearance),
    ("volume", _gather_volume),
)


def system_info(runner: CommandRunner, *, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Best-effort facts about this Mac (see the module docstring for the keys).

    ``clock`` returns the current Unix time (injectable for tests; used for ``uptime_s``).
    """
    now = clock()
    info: dict[str, Any] = {}
    unavailable: list[str] = []
    for source, gather in _GATHERERS:
        values = gather(runner, now)
        if values:
            info.update(values)
        else:
            unavailable.append(source)
    if unavailable:
        info["unavailable"] = unavailable
    return info


# --------------------------------------------------------------------------------------------
# Controls


def set_volume(runner: CommandRunner, level: int | None = None, mute: bool | None = None) -> dict[str, Any]:
    """Set the output volume (``level`` 0–100) and/or mute state; returns the new settings.

    The result has the same shape as ``system_info()["volume"]``. Raises ``INVALID_ARGUMENT``
    when neither argument is given or a value is out of range.
    """
    if level is None and mute is None:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "Pass a volume level, a mute state, or both.")
    lines: list[str] = []
    if level is not None:
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"The volume level must be an integer from 0 to 100, got {level!r}.",
            )
        lines.append(f"set volume output volume {level}")
    if mute is not None:
        if not isinstance(mute, bool):
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"mute must be true or false, got {mute!r}.")
        lines.append(f"set volume output muted {'true' if mute else 'false'}")
    lines.append("get volume settings")
    result = run_applescript(runner, "\n".join(lines), timeout_s=CONTROL_TIMEOUT_S)
    return parse_volume_settings(output_text(result))


def appearance_script(dark: bool) -> str:
    """The System Events script that switches dark mode on or off."""
    return "\n".join(
        [
            'tell application "System Events"',
            "\ttell appearance preferences",
            f"\t\tset dark mode to {'true' if dark else 'false'}",
            "\tend tell",
            "end tell",
        ]
    )


def set_appearance(runner: CommandRunner, dark: bool) -> None:
    """Switch the system appearance to dark (``True``) or light (``False``).

    Uses System Events, so the first call may trigger the Automation prompt
    (``PERMISSION_AUTOMATION`` until the user allows it).
    """
    if not isinstance(dark, bool):
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"dark must be true or false, got {dark!r}.")
    run_applescript(runner, appearance_script(dark), timeout_s=CONTROL_TIMEOUT_S)
