"""Stable, user-facing error codes.

Every failure that reaches an MCP client is an :class:`AXPilotError` with a code from
:class:`ErrorCode`. Codes are part of the public API: agents branch on them, so never
rename or reuse one. Add new codes at the end of their group.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    # Permissions / environment
    PERMISSION_ACCESSIBILITY = "PERMISSION_ACCESSIBILITY"
    PERMISSION_SCREEN_RECORDING = "PERMISSION_SCREEN_RECORDING"
    PERMISSION_AUTOMATION = "PERMISSION_AUTOMATION"
    LAUNCHER_CHANGED = "LAUNCHER_CHANGED"
    UNSUPPORTED_OS = "UNSUPPORTED_OS"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    HOST_UNAVAILABLE = "HOST_UNAVAILABLE"

    # Targets
    APP_NOT_FOUND = "APP_NOT_FOUND"
    WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
    DISPLAY_NOT_FOUND = "DISPLAY_NOT_FOUND"
    MENU_NOT_FOUND = "MENU_NOT_FOUND"
    SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
    STALE_REF = "STALE_REF"
    ELEMENT_NOT_ACTIONABLE = "ELEMENT_NOT_ACTIONABLE"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    LOCATOR_AMBIGUOUS = "LOCATOR_AMBIGUOUS"

    # Policy / user
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"
    TOOL_DISABLED = "TOOL_DISABLED"
    USER_DENIED = "USER_DENIED"
    USER_ACTIVE = "USER_ACTIVE"
    PAUSED_BY_USER = "PAUSED_BY_USER"
    RATE_LIMITED = "RATE_LIMITED"
    SECURE_INPUT_ACTIVE = "SECURE_INPUT_ACTIVE"
    NEEDS_USER = "NEEDS_USER"
    CLIENT_NOT_PAIRED = "CLIENT_NOT_PAIRED"

    # Execution
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    TIMEOUT = "TIMEOUT"
    BUSY = "BUSY"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    COMMAND_FAILED = "COMMAND_FAILED"
    INTERNAL = "INTERNAL"


_DEFAULT_HINTS: dict[ErrorCode, str] = {
    ErrorCode.PERMISSION_ACCESSIBILITY: (
        "Grant Accessibility to AXPilot in System Settings → Privacy & Security → Accessibility, "
        "or run `axpilot doctor`."
    ),
    ErrorCode.PERMISSION_SCREEN_RECORDING: (
        "Screenshots need Screen Recording permission (System Settings → Privacy & Security → "
        "Screen & System Audio Recording). Snapshot-based tools work without it."
    ),
    ErrorCode.PERMISSION_AUTOMATION: (
        "macOS asked whether AXPilot may control this app (Automation). Approve it in System Settings → "
        "Privacy & Security → Automation."
    ),
    ErrorCode.LAUNCHER_CHANGED: (
        "The AXPilot launcher changed. Re-grant Accessibility once; see `axpilot doctor`."
    ),
    ErrorCode.STALE_REF: "The UI changed since that snapshot. Call `snapshot` again and use the new refs.",
    ErrorCode.SNAPSHOT_NOT_FOUND: "Snapshots expire. Call `snapshot` again.",
    ErrorCode.ELEMENT_NOT_FOUND: (
        "Nothing in the window matches the locator. Check that the app shows it now (open its "
        "window, sheet or menu first), or build the locator again with `locate`."
    ),
    ErrorCode.LOCATOR_AMBIGUOUS: (
        "The locator matches several elements. Use `locate` on the one you mean for a locator that "
        "names only it (or add within, name or index)."
    ),
    ErrorCode.USER_ACTIVE: "The user is using the mouse/keyboard. Wait a moment and retry.",
    ErrorCode.PAUSED_BY_USER: ("The user paused AXPilot (kill switch). Ask them before continuing."),
    ErrorCode.NEEDS_USER: (
        "A system dialog needs the user (e.g. a password prompt). Ask the user to handle it."
    ),
    ErrorCode.SECURE_INPUT_ACTIVE: (
        "Secure keyboard entry is active (a password field or a terminal setting), so synthetic "
        "keystrokes are blocked. Ask the user to complete this step."
    ),
    ErrorCode.TOOL_DISABLED: "This tool is disabled by the AXPilot profile/config.",
    ErrorCode.BLOCKED_BY_POLICY: (
        "AXPilot's safety policy blocks this action. The user can change it in settings."
    ),
    ErrorCode.RATE_LIMITED: "Too many actions in a short time. Slow down and verify the results.",
}


class AXPilotError(Exception):
    """An error with a stable :class:`ErrorCode`, a human message and an optional hint."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint if hint is not None else _DEFAULT_HINTS.get(code)
        self.details = details or {}

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AXPilotError:
        """Rebuild an error from :meth:`to_dict` output (e.g. a tool result's error meta). An
        unknown code becomes ``INTERNAL``, keeping the original in ``details["code"]``."""
        raw = str(data.get("code", ""))
        details = dict(data.get("details") or {}) if isinstance(data.get("details"), Mapping) else {}
        try:
            code = ErrorCode(raw)
        except ValueError:
            code = ErrorCode.INTERNAL
            details.setdefault("code", raw)
        hint = data.get("hint")
        message = data.get("message")
        return cls(
            code,
            message if isinstance(message, str) and message else raw or "Unknown error",
            hint=hint if isinstance(hint, str) else None,
            details=details,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.details:
            out["details"] = self.details
        return out

    def to_text(self) -> str:
        text = f"{self.code.value}: {self.message}"
        if self.hint:
            text += f"\nHint: {self.hint}"
        return text
