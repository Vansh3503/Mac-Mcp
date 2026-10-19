"""The automation SDK: drive the Mac (or the simulated desktop) from Python scripts with the same
tools, policy, audit and error codes as agents. See :mod:`axpilot.sdk.desktop`."""

from __future__ import annotations

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.sdk.desktop import AsyncDesktop, Desktop
from axpilot.sdk.results import (
    ActionResult,
    AppHandle,
    AsyncAppHandle,
    AsyncElementHandle,
    Element,
    ElementHandle,
    FindResult,
    ReadResult,
    ScreenshotResult,
    SnapshotResult,
    ToolResult,
)

__all__ = [
    "AXPilotError",
    "ActionResult",
    "AppHandle",
    "AsyncAppHandle",
    "AsyncDesktop",
    "AsyncElementHandle",
    "Desktop",
    "Element",
    "ElementHandle",
    "ErrorCode",
    "FindResult",
    "ReadResult",
    "ScreenshotResult",
    "SnapshotResult",
    "ToolResult",
]
