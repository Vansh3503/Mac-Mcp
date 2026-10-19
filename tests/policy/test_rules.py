from __future__ import annotations

from collections.abc import Iterable

import pytest

from axpilot import BUNDLE_ID
from axpilot.config import DEFAULT_DENY_APPS, SafetyConfig
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo
from axpilot.policy.profiles import enabled_tools
from axpilot.policy.rules import TERMINAL_BUNDLE_IDS, Policy

NOTES = AppInfo("Notes", 812, "com.apple.Notes")
TEXTEDIT = AppInfo("TextEdit", 900, "com.apple.TextEdit")
ONEPASSWORD = AppInfo("1Password", 700, "com.1password.1password")
TERMINAL = AppInfo("Terminal", 600, "com.apple.Terminal")
SETTINGS = AppInfo("System Settings", 500, "com.apple.systempreferences")
SECURITY_AGENT = AppInfo("SecurityAgent", 400, "com.apple.SecurityAgent")
AXPILOT = AppInfo("AXPilot", 300, BUNDLE_ID)

KINDS = ("observe", "input", "capture")


def make_policy(enabled: Iterable[str] | None = None, **safety: object) -> Policy:
    tools = enabled_tools("standard") if enabled is None else enabled
    return Policy(SafetyConfig(**safety), tools)  # type: ignore[arg-type]


def blocked(policy: Policy, app: AppInfo | None, kind: str, window_title: str | None = None) -> AXPilotError:
    with pytest.raises(AXPilotError) as info:
        policy.check_app(app, kind=kind, window_title=window_title)  # type: ignore[arg-type]
    return info.value


# ----------------------------------------------------------------------------- defaults


@pytest.mark.parametrize("kind", KINDS)
def test_ordinary_apps_are_allowed_by_default(kind: str) -> None:
    make_policy().check_app(NOTES, kind=kind)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", KINDS)
def test_unknown_app_is_allowed_without_allow_list(kind: str) -> None:
    make_policy().check_app(None, kind=kind)  # type: ignore[arg-type]


def test_unknown_kind_is_rejected() -> None:
    error = blocked(make_policy(), NOTES, "write")
    assert error.code is ErrorCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------- deny_apps


@pytest.mark.parametrize("kind", KINDS)
def test_default_deny_list_blocks_password_managers(kind: str) -> None:
    error = blocked(make_policy(), ONEPASSWORD, kind)
    assert error.code is ErrorCode.BLOCKED_BY_POLICY
    assert "1Password (com.1password.1password)" in error.message
    assert "deny_apps" in error.message
    assert error.hint is not None
    assert "deny_apps" in error.hint
    assert error.details["rule"] == "deny_apps"
    assert error.details["bundle_id"] == "com.1password.1password"
    assert error.details["kind"] == kind


def test_every_default_deny_entry_blocks_its_app() -> None:
    policy = make_policy()
    for bundle_id in DEFAULT_DENY_APPS:
        blocked(policy, AppInfo("Some App", 1, bundle_id), "observe")


@pytest.mark.parametrize(
    "entry", ["com.apple.Notes", "COM.APPLE.NOTES", "Notes", "notes", "Notes.app", "  Notes  ", "com.apple.*"]
)
def test_deny_matches_bundle_id_or_name_case_insensitively(entry: str) -> None:
    error = blocked(make_policy(deny_apps=[entry]), NOTES, "input")
    assert error.details["entry"] == entry.strip()


@pytest.mark.parametrize("entry", ["com.apple.Note", "Note", "com.apple.Notes.helper", "*.TextEdit"])
def test_deny_does_not_match_partial_names(entry: str) -> None:
    make_policy(deny_apps=[entry]).check_app(NOTES, kind="input")


def test_deny_wildcard_matches_names_too() -> None:
    policy = make_policy(deny_apps=["1Pass*"])
    blocked(policy, AppInfo("1Password 7", 1, "com.agilebits.onepassword7-helper"), "observe")


def test_empty_deny_entries_are_ignored() -> None:
    make_policy(deny_apps=["", "   "]).check_app(NOTES, kind="input")


def test_deny_beats_allow() -> None:
    policy = make_policy(allow_apps=["com.apple.Notes"], deny_apps=["Notes"])
    assert blocked(policy, NOTES, "observe").details["rule"] == "deny_apps"


# --------------------------------------------------------------------------- allow_apps


@pytest.mark.parametrize("kind", KINDS)
def test_allow_list_blocks_everything_else(kind: str) -> None:
    policy = make_policy(allow_apps=["com.apple.Notes"])
    policy.check_app(NOTES, kind=kind)  # type: ignore[arg-type]
    error = blocked(policy, TEXTEDIT, kind)
    assert error.code is ErrorCode.BLOCKED_BY_POLICY
    assert "TextEdit (com.apple.TextEdit)" in error.message
    assert "allow_apps" in error.message
    assert "com.apple.Notes" in error.message
    assert "'com.apple.TextEdit'" in (error.hint or "")
    assert error.details["rule"] == "allow_apps"


def test_allow_list_matches_by_name() -> None:
    make_policy(allow_apps=["textedit"]).check_app(TEXTEDIT, kind="input")


def test_allow_list_blocks_input_to_unidentified_targets() -> None:
    policy = make_policy(allow_apps=["com.apple.Notes"])
    error = blocked(policy, None, "input")
    assert error.details["rule"] == "allow_apps"
    policy.check_app(None, kind="observe")
    policy.check_app(None, kind="capture")


# ------------------------------------------------------------------------- deny_windows


@pytest.mark.parametrize("kind", KINDS)
def test_default_deny_windows_block_privacy_pane(kind: str) -> None:
    error = blocked(make_policy(), SETTINGS, kind, window_title="Privacy & Security")
    assert error.code is ErrorCode.BLOCKED_BY_POLICY
    assert "'Privacy & Security'" in error.message
    assert "System Settings (com.apple.systempreferences)" in error.message
    assert error.details["rule"] == "deny_windows"
    assert error.details["window_title"] == "Privacy & Security"


def test_deny_windows_match_title_substrings_case_insensitively() -> None:
    blocked(make_policy(), SETTINGS, "input", window_title="privacy & security — Accessibility")


def test_other_windows_of_the_app_are_allowed() -> None:
    policy = make_policy()
    policy.check_app(SETTINGS, kind="input", window_title="Displays")
    policy.check_app(SETTINGS, kind="input")  # title unknown


def test_deny_windows_only_apply_to_their_app() -> None:
    make_policy().check_app(NOTES, kind="input", window_title="Privacy & Security")


def test_deny_windows_entry_by_app_name() -> None:
    policy = make_policy(deny_windows=["notes::Secret"])
    blocked(policy, NOTES, "observe", window_title="My Secret Note")


def test_deny_windows_entry_without_app_applies_to_every_app() -> None:
    policy = make_policy(deny_windows=["Bank Statement"])
    blocked(policy, NOTES, "capture", window_title="bank statement.pdf")
    blocked(policy, TEXTEDIT, "capture", window_title="Bank Statement")
    policy = make_policy(deny_windows=["::Bank"])
    blocked(policy, TEXTEDIT, "observe", window_title="Bank")


@pytest.mark.parametrize("entry", ["com.apple.Notes::", "com.apple.Notes::   "])
def test_deny_windows_entry_needs_a_title(entry: str) -> None:
    with pytest.raises(AXPilotError) as info:
        make_policy(deny_windows=[entry])
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "deny_apps" in (info.value.hint or "")


# ------------------------------------------------------------------------ terminal rule


@pytest.mark.parametrize("bundle_id", sorted(TERMINAL_BUNDLE_IDS))
def test_terminals_block_input_without_shell(bundle_id: str) -> None:
    app = AppInfo("Some Terminal", 1, bundle_id)
    policy = make_policy()
    error = blocked(policy, app, "input")
    assert error.code is ErrorCode.BLOCKED_BY_POLICY
    assert error.details["rule"] == "terminal"
    assert "terminal rule" in error.message
    assert "scripting" in (error.hint or "")
    policy.check_app(app, kind="observe")
    policy.check_app(app, kind="capture")


def test_terminal_input_allowed_when_shell_enabled() -> None:
    policy = make_policy(enabled_tools("standard", enable=["scripting"]))
    assert policy.shell_enabled
    policy.check_app(TERMINAL, kind="input")


def test_terminal_still_blocked_with_other_scripting_tools_only() -> None:
    policy = make_policy(enabled_tools("standard", enable=["applescript"]))
    blocked(policy, TERMINAL, "input")


def test_terminal_list_matches_the_spec() -> None:
    assert {
        "com.apple.Terminal",
        "com.googlecode.iterm2",
        "com.mitchellh.ghostty",
        "dev.warp.Warp-Stable",
        "net.kovidgoyal.kitty",
        "org.alacritty",
        "io.alacritty",
        "com.github.wez.wezterm",
        "co.zeit.hyper",
        "org.tabby",
    } <= TERMINAL_BUNDLE_IDS


@pytest.mark.parametrize(
    ("app", "expected"),
    [
        (TERMINAL, True),
        (AppInfo("iTerm2", 1, "COM.GOOGLECODE.ITERM2"), True),
        (AppInfo("iTerm2", 1, "com.googlecode.iterm2.nightly"), True),
        (AppInfo("Ghostty", 1, None), True),
        (AppInfo("kitty", 1, None), True),
        (NOTES, False),
        (None, False),
    ],
)
def test_is_terminal(app: AppInfo | None, expected: bool) -> None:
    assert make_policy().is_terminal(app) is expected


# --------------------------------------------------------------- AXPilot and auth dialogs


@pytest.mark.parametrize(
    "app", [AXPILOT, AppInfo("AXPilot", 2, None), AppInfo("AXPilot.app", 2, "org.python")]
)
def test_axpilot_itself_is_readable_but_not_controllable(app: AppInfo) -> None:
    policy = make_policy(allow_apps=["com.apple.Notes"], deny_apps=["AXPilot", BUNDLE_ID])
    policy.check_app(app, kind="observe")
    policy.check_app(app, kind="capture")
    error = blocked(policy, app, "input")
    assert error.code is ErrorCode.BLOCKED_BY_POLICY
    assert error.details["rule"] == "self"


def test_authentication_dialogs_need_the_user() -> None:
    policy = make_policy(enabled_tools("full"))
    error = blocked(policy, SECURITY_AGENT, "input")
    assert error.code is ErrorCode.NEEDS_USER
    policy.check_app(SECURITY_AGENT, kind="observe")


# --------------------------------------------------------------------- secure fields


def test_secure_fields_blocked_by_default() -> None:
    policy = make_policy()
    with pytest.raises(AXPilotError) as info:
        policy.check_secure_target(True)
    assert info.value.code is ErrorCode.BLOCKED_BY_POLICY
    assert "allow_secure_fields" in (info.value.hint or "")
    policy.check_secure_target(False)


def test_secure_fields_allowed_by_config() -> None:
    make_policy(allow_secure_fields=True).check_secure_target(True)


# ---------------------------------------------------------------------- confirmations


@pytest.mark.parametrize(
    ("mode", "tool", "destructive", "expected"),
    [
        ("never", "shell", True, False),
        ("never", "app", True, False),
        ("destructive", "app", True, True),
        ("destructive", "app", False, False),
        ("destructive", "click", False, False),
        ("destructive", "shell", False, True),
        ("destructive", "applescript", False, True),
        ("destructive", "browser_eval_js", False, True),
        ("destructive", "snapshot", False, False),
        ("always", "click", False, True),
        ("always", "clipboard", False, True),
        ("always", "snapshot", False, False),
        ("always", "status", False, False),
        ("always", "app", True, True),
    ],
)
def test_requires_confirmation(mode: str, tool: str, destructive: bool, expected: bool) -> None:
    policy = make_policy(confirm=mode)
    assert policy.requires_confirmation(tool, destructive=destructive) is expected


# --------------------------------------------------------------------- capture exclusion


def test_capture_exclusions_list_literal_bundle_ids() -> None:
    policy = make_policy(
        deny_apps=[
            "com.1password.1password",
            "Bitwarden",
            "com.lastpass.*",
            "Keychain Access.app",
            "1Password.app",
        ]
    )
    assert policy.capture_exclusions() == ["com.1password.1password"]


def test_capture_exclusions_never_hide_axpilot_itself() -> None:
    policy = make_policy(deny_apps=[BUNDLE_ID, "AXPilot"])
    assert policy.capture_exclusions([AXPILOT]) == []


def test_capture_exclusions_resolve_names_and_wildcards_with_running_apps() -> None:
    policy = make_policy(deny_apps=["com.1password.1password", "Bitwarden", "com.lastpass.*"])
    running = [
        NOTES,
        AppInfo("Bitwarden", 2, "com.bitwarden.desktop"),
        AppInfo("LastPass", 3, "com.lastpass.LastPass"),
        ONEPASSWORD,
        AppInfo("No Bundle", 4, None),
    ]
    assert policy.capture_exclusions(running) == [
        "com.1password.1password",
        "com.bitwarden.desktop",
        "com.lastpass.LastPass",
    ]


def test_capture_exclusions_hide_apps_outside_the_allow_list() -> None:
    policy = make_policy(deny_apps=[], allow_apps=["Notes"])
    assert policy.capture_exclusions([NOTES, TEXTEDIT, AXPILOT]) == ["com.apple.TextEdit"]


def test_capture_exclusions_default_config() -> None:
    assert make_policy().capture_exclusions() == list(DEFAULT_DENY_APPS)


# ----------------------------------------------------------------------- tool enabled


def test_check_tool() -> None:
    policy = make_policy()
    policy.check_tool("snapshot")
    with pytest.raises(AXPilotError) as info:
        policy.check_tool("shell")
    assert info.value.code is ErrorCode.TOOL_DISABLED
    assert "'scripting'" in info.value.message
    assert '["shell"]' in (info.value.hint or "")
    with pytest.raises(AXPilotError):
        policy.check_tool("not_a_tool")


# --------------------------------------------------------------------- live config edits


def test_config_edits_apply_without_rebuilding_the_policy() -> None:
    safety = SafetyConfig(deny_apps=[])
    policy = Policy(safety, enabled_tools("standard"))
    policy.check_app(NOTES, kind="input")
    safety.deny_apps = ["com.apple.Notes"]
    blocked(policy, NOTES, "input")
    safety.deny_apps.clear()
    policy.check_app(NOTES, kind="input")
    safety.allow_secure_fields = True
    policy.check_secure_target(True)
