"""User notifications through ``osascript``.

Permission: none for AXPilot itself; macOS shows these notifications as coming from Script
Editor, and the user can turn them off in System Settings → Notifications → Script Editor.

Why not ``UNUserNotificationCenter``: it only works for a process whose *main executable* is
inside an app bundle with a bundle identifier (otherwise it raises "bundleProxyForCurrentProcess
is nil"). AXPilot's host is a Python child of the launcher, so the main executable is the
Python interpreter. ``display notification`` via AppleScript needs no bundle. Every string is
escaped with :func:`axpilot.system.osascript.applescript_string` and the script goes through
stdin, so notification text can never inject AppleScript.
"""

from __future__ import annotations

from axpilot.engine.macos import helpers as h
from axpilot.system.osascript import run_applescript
from axpilot.system.runner import CommandRunner

NOTIFY_TIMEOUT_S = 10.0


def post_notification(runner: CommandRunner, title: str, message: str, subtitle: str | None = None) -> None:
    """Show a notification. Raises ``INVALID_ARGUMENT`` for an empty one and the
    ``osascript`` error codes (``TIMEOUT``, ``COMMAND_FAILED``…) if it cannot be posted."""
    run_applescript(runner, h.notification_script(title, message, subtitle), timeout_s=NOTIFY_TIMEOUT_S)
