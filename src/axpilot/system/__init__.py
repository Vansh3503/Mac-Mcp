"""Command-line integrations with macOS: AppleScript, shell, Spotlight, Shortcuts, preferences,
processes, system information and browsers.

Everything here is pure Python on top of :class:`~axpilot.system.runner.CommandRunner`, which
runs argv lists (never through a shell, except the explicit ``shell`` tool) with deadlines and
bounded output. Tests use :class:`~axpilot.system.runner.FakeRunner`, so the package imports and
is fully tested on any OS; the commands themselves only exist on macOS.
"""

from __future__ import annotations

from axpilot.system.browser import (
    BROWSERS,
    BrowserSpec,
    TabInfo,
    TabRef,
    activate_tab,
    close_tab,
    eval_js,
    frontmost_browser,
    list_tabs,
    open_url,
    resolve_browser,
)
from axpilot.system.defaults import defaults_read, defaults_write
from axpilot.system.osascript import applescript_string, jxa_string, output_text, run_applescript
from axpilot.system.processes import ProcessInfo, kill_process, list_processes
from axpilot.system.runner import (
    CommandResult,
    CommandRunner,
    FakeCall,
    FakeRunner,
    ensure_success,
)
from axpilot.system.shell import run_shell
from axpilot.system.shortcuts import list_shortcuts, run_shortcut
from axpilot.system.spotlight import SPOTLIGHT_KINDS, spotlight_search
from axpilot.system.sysinfo import set_appearance, set_volume, system_info

__all__ = [
    "BROWSERS",
    "SPOTLIGHT_KINDS",
    "BrowserSpec",
    "CommandResult",
    "CommandRunner",
    "FakeCall",
    "FakeRunner",
    "ProcessInfo",
    "TabInfo",
    "TabRef",
    "activate_tab",
    "applescript_string",
    "close_tab",
    "defaults_read",
    "defaults_write",
    "ensure_success",
    "eval_js",
    "frontmost_browser",
    "jxa_string",
    "kill_process",
    "list_processes",
    "list_shortcuts",
    "list_tabs",
    "open_url",
    "output_text",
    "resolve_browser",
    "run_applescript",
    "run_shell",
    "run_shortcut",
    "set_appearance",
    "set_volume",
    "spotlight_search",
    "system_info",
]
