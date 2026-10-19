"""App, window and field rules: what an agent may observe, capture and control.

The server asks the :class:`Policy` before every call that touches an app:

* ``kind="observe"``: reading the accessibility tree (snapshot, find, inspect…).
* ``kind="capture"``: pixels (screenshot, OCR).
* ``kind="input"``: anything that clicks, types or otherwise changes the app.

Rules, checked in this order (the first match wins):

1. AXPilot's own windows: always readable; never controllable, so an agent cannot
   approve its own confirmation dialogs or change its settings.
2. macOS authentication dialogs (``com.apple.SecurityAgent``) are never operated:
   ``NEEDS_USER``.
3. ``deny_apps``: matching apps are blocked for every kind.
4. ``allow_apps``: when non-empty, every other app is blocked for every kind.
5. ``deny_windows``: ``"<bundle id or app name>::<title substring>"`` blocks one window
   (for example the Privacy & Security pane of System Settings).
6. Terminal rule: input into terminal apps is blocked unless the ``shell`` tool is
   enabled, because typing into a terminal is equivalent to shell access.

App entries match an app's bundle id or name, case-insensitively, and may use ``*`` as a
wildcard (``"com.agilebits.*"``). Errors name the app, the rule and how the user can
change it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from axpilot import BUNDLE_ID
from axpilot.config import SafetyConfig
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo
from axpilot.policy.profiles import TOOLSETS, toolset_of

__all__ = [
    "AUTH_DIALOG_BUNDLE_IDS",
    "CONFIRM_BY_DEFAULT_TOOLS",
    "TERMINAL_APP_NAMES",
    "TERMINAL_BUNDLE_IDS",
    "AccessKind",
    "Policy",
]

AccessKind = Literal["observe", "input", "capture"]
_KINDS: frozenset[str] = frozenset({"observe", "input", "capture"})

TERMINAL_BUNDLE_IDS: frozenset[str] = frozenset(
    {
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
    }
)
"""Bundle ids of terminal emulators (input into them is gated behind the ``shell`` tool)."""

TERMINAL_APP_NAMES: frozenset[str] = frozenset(
    {"terminal", "iterm", "iterm2", "ghostty", "warp", "kitty", "alacritty", "wezterm", "hyper", "tabby"}
)
"""Casefolded app names treated as terminals too (covers nightly or renamed builds)."""

AUTH_DIALOG_BUNDLE_IDS: frozenset[str] = frozenset({"com.apple.SecurityAgent"})
"""System authentication dialogs. AXPilot never operates them (``NEEDS_USER``)."""

CONFIRM_BY_DEFAULT_TOOLS: frozenset[str] = frozenset({"shell", "applescript", "browser_eval_js"})
"""Tools that run arbitrary code: confirmed in ``destructive`` mode even when the caller
does not flag the call as destructive."""

_READ_ONLY_TOOLS: frozenset[str] = frozenset(TOOLSETS["observe"])
_TERMINAL_IDS_CF: frozenset[str] = frozenset(b.casefold() for b in TERMINAL_BUNDLE_IDS)
_AUTH_IDS_CF: frozenset[str] = frozenset(b.casefold() for b in AUTH_DIALOG_BUNDLE_IDS)
_SELF_NAME = "axpilot"
_KIND_VERB: dict[str, str] = {"observe": "Reading", "input": "Controlling", "capture": "Capturing"}
_CONFIG_HINT = "`axpilot config edit` opens the config file."


@dataclass(frozen=True, slots=True)
class _AppPattern:
    """One ``deny_apps``/``allow_apps`` entry (bundle id or app name, ``*`` wildcards)."""

    raw: str
    regex: re.Pattern[str] | None
    literal: str

    @classmethod
    def parse(cls, raw: str) -> _AppPattern:
        text = raw.strip()
        folded = text.casefold().removesuffix(".app")
        regex = None
        if "*" in folded:
            regex = re.compile(".*".join(re.escape(part) for part in folded.split("*")), re.DOTALL)
        return cls(raw=text, regex=regex, literal=folded)

    @property
    def is_bundle_id(self) -> bool:
        """True if the entry looks like a literal bundle id (usable for capture exclusion)."""
        return (
            self.regex is None
            and "." in self.literal
            and not self.raw.casefold().endswith(".app")
            and not any(c.isspace() for c in self.raw)
        )

    def matches(self, app: AppInfo) -> bool:
        candidates = [app.name.casefold(), app.name.casefold().removesuffix(".app")]
        if app.bundle_id:
            candidates.append(app.bundle_id.casefold())
        if self.regex is not None:
            return any(self.regex.fullmatch(c) for c in candidates)
        return self.literal in candidates


@dataclass(frozen=True, slots=True)
class _WindowRule:
    """One ``deny_windows`` entry: ``"<app>::<title substring>"`` (no ``::`` = any app)."""

    raw: str
    app: _AppPattern | None
    title: str

    @classmethod
    def parse(cls, raw: str) -> _WindowRule:
        text = raw.strip()
        app_part, sep, title_part = text.partition("::")
        if not sep:
            app_part, title_part = "", text
        title = title_part.strip().casefold()
        if not title:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Invalid [safety] deny_windows entry {raw!r}: the window title part is empty.",
                hint="Use '<bundle id>::<title substring>'. To block a whole app, use deny_apps. "
                + _CONFIG_HINT,
            )
        app = _AppPattern.parse(app_part) if app_part.strip() else None
        return cls(raw=text, app=app, title=title)

    def matches(self, app: AppInfo, window_title: str) -> bool:
        if self.app is not None and not self.app.matches(app):
            return False
        return self.title in window_title.casefold()


@dataclass(frozen=True, slots=True)
class _Compiled:
    key: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
    deny: tuple[_AppPattern, ...]
    allow: tuple[_AppPattern, ...]
    deny_windows: tuple[_WindowRule, ...]


def _compile(deny: Sequence[str], allow: Sequence[str], windows: Sequence[str]) -> _Compiled:
    return _Compiled(
        key=(tuple(deny), tuple(allow), tuple(windows)),
        deny=tuple(_AppPattern.parse(e) for e in deny if e.strip()),
        allow=tuple(_AppPattern.parse(e) for e in allow if e.strip()),
        deny_windows=tuple(_WindowRule.parse(e) for e in windows if e.strip()),
    )


def _describe(app: AppInfo) -> str:
    return f"{app.name} ({app.bundle_id})" if app.bundle_id else app.name


def _blocked(
    message: str, hint: str, *, rule: str, kind: str, app: AppInfo | None, **extra: str
) -> AXPilotError:
    details: dict[str, object] = {"rule": rule, "kind": kind, **extra}
    if app is not None:
        details["app"] = app.name
        if app.bundle_id:
            details["bundle_id"] = app.bundle_id
    return AXPilotError(ErrorCode.BLOCKED_BY_POLICY, message, hint=hint, details=details)


class Policy:
    """Safety rules derived from ``[safety]`` and the set of enabled tools.

    The config lists are re-read on every check, so edits to ``safety`` (for example from
    the settings UI) apply immediately. Malformed ``deny_windows`` entries raise
    ``INVALID_ARGUMENT`` (at construction, and again if one is added later).
    """

    def __init__(self, safety: SafetyConfig, enabled: Iterable[str]) -> None:
        self.safety = safety
        self.enabled: frozenset[str] = frozenset(enabled)
        self._compiled: _Compiled | None = None
        self._rules()  # validate deny_windows entries now

    # ----------------------------------------------------------------- properties
    @property
    def shell_enabled(self) -> bool:
        """True when the ``shell`` tool is enabled (which lifts the terminal rule)."""
        return "shell" in self.enabled

    # --------------------------------------------------------------------- checks
    def check_tool(self, tool: str) -> None:
        """Raise ``TOOL_DISABLED`` unless ``tool`` is enabled."""
        if tool in self.enabled:
            return
        try:
            where = f" (toolset {toolset_of(tool)!r})"
        except AXPilotError:
            where = ""
        raise AXPilotError(
            ErrorCode.TOOL_DISABLED,
            f"The {tool!r} tool{where} is disabled by the AXPilot profile/config.",
            hint=f'The user can enable it with [tools] enable = ["{tool}"]. {_CONFIG_HINT}',
            details={"tool": tool},
        )

    def check_app(
        self,
        app: AppInfo | None,
        *,
        kind: AccessKind,
        window_title: str | None = None,
    ) -> None:
        """Raise ``BLOCKED_BY_POLICY`` (or ``NEEDS_USER``) if ``kind`` access to ``app`` is
        not allowed. ``app=None`` means the target app is unknown (for example a point
        over no window). ``window_title`` enables the ``deny_windows`` rule."""
        if kind not in _KINDS:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unknown access kind {kind!r}; expected one of {sorted(_KINDS)}.",
            )
        rules = self._rules()
        if app is None:
            self._check_unknown_target(rules, kind)
            return
        if self.is_self(app):
            self._check_self(app, kind)
            return
        self._check_auth_dialog(app, kind)
        self._check_deny(rules, app, kind)
        self._check_allow(rules, app, kind)
        if window_title:
            self._check_windows(rules, app, kind, window_title)
        if kind == "input":
            self._check_terminal(app)

    def check_secure_target(self, secure: bool) -> None:
        """Raise ``BLOCKED_BY_POLICY`` before typing into a secure (password) field unless
        ``allow_secure_fields`` is set."""
        if secure and not self.safety.allow_secure_fields:
            raise AXPilotError(
                ErrorCode.BLOCKED_BY_POLICY,
                "Typing into a password (secure) field is blocked by the secure-field rule.",
                hint="Ask the user to enter it themselves. To allow agents to type into password "
                f"fields, the user can set [safety] allow_secure_fields = true. {_CONFIG_HINT}",
                details={"rule": "allow_secure_fields", "kind": "input"},
            )

    def requires_confirmation(self, tool: str, *, destructive: bool) -> bool:
        """Whether this call needs the user's confirmation under ``[safety] confirm``.

        * ``never``: no confirmations.
        * ``destructive`` (default): destructive calls, plus the code-running tools in
          :data:`CONFIRM_BY_DEFAULT_TOOLS`.
        * ``always``: every call except the read-only ``observe`` tools.
        """
        mode = self.safety.confirm
        if mode == "never":
            return False
        if destructive:
            return True
        if mode == "always":
            return tool not in _READ_ONLY_TOOLS
        return tool in CONFIRM_BY_DEFAULT_TOOLS

    def capture_exclusions(self, running_apps: Iterable[AppInfo] | None = None) -> list[str]:
        """Bundle ids to leave out of screenshots.

        Always includes literal bundle ids from ``deny_apps``. With ``running_apps``, also
        resolves name and wildcard entries, and (when ``allow_apps`` is set) excludes every
        running app that is not allowed. ``deny_windows`` cannot be enforced this way; window
        captures are checked with :meth:`check_app` instead.
        """
        rules = self._rules()
        out: dict[str, None] = {}
        for pattern in rules.deny:
            if pattern.is_bundle_id and pattern.literal != BUNDLE_ID.casefold():
                out.setdefault(pattern.raw)
        for app in running_apps or ():
            if not app.bundle_id or self.is_self(app):
                continue
            denied = any(p.matches(app) for p in rules.deny)
            not_allowed = bool(rules.allow) and not any(p.matches(app) for p in rules.allow)
            if denied or not_allowed:
                out.setdefault(app.bundle_id)
        return list(out)

    def is_terminal(self, app: AppInfo | None) -> bool:
        """True if ``app`` is a terminal emulator (by bundle id or name)."""
        if app is None:
            return False
        if app.bundle_id and app.bundle_id.casefold() in _TERMINAL_IDS_CF:
            return True
        return app.name.casefold().removesuffix(".app") in TERMINAL_APP_NAMES

    @staticmethod
    def is_self(app: AppInfo | None) -> bool:
        """True if ``app`` is AXPilot itself (its launcher or host UI), by bundle id or name."""
        if app is None:
            return False
        if app.bundle_id and app.bundle_id.casefold() == BUNDLE_ID.casefold():
            return True
        return app.name.casefold().removesuffix(".app") == _SELF_NAME

    # ------------------------------------------------------------------ internals
    def _rules(self) -> _Compiled:
        safety = self.safety
        key = (tuple(safety.deny_apps), tuple(safety.allow_apps), tuple(safety.deny_windows))
        compiled = self._compiled
        if compiled is None or compiled.key != key:
            compiled = _compile(safety.deny_apps, safety.allow_apps, safety.deny_windows)
            self._compiled = compiled
        return compiled

    def _check_unknown_target(self, rules: _Compiled, kind: str) -> None:
        if kind == "input" and rules.allow:
            raise _blocked(
                "Controlling this target is blocked by the allow_apps rule: the app under it "
                "could not be identified.",
                "Target an element (ref) or a window of an allowed app instead.",
                rule="allow_apps",
                kind=kind,
                app=None,
            )

    @staticmethod
    def _check_self(app: AppInfo, kind: str) -> None:
        if kind == "input":
            raise _blocked(
                "Controlling AXPilot's own windows is blocked: its confirmations and settings "
                "must be answered by the user.",
                "Ask the user to respond to AXPilot's prompt themselves.",
                rule="self",
                kind=kind,
                app=app,
            )

    @staticmethod
    def _check_auth_dialog(app: AppInfo, kind: str) -> None:
        if kind == "input" and app.bundle_id and app.bundle_id.casefold() in _AUTH_IDS_CF:
            raise AXPilotError(
                ErrorCode.NEEDS_USER,
                f"{_describe(app)} is a macOS authentication dialog; AXPilot never operates it.",
                details={"rule": "auth_dialog", "kind": kind, "app": app.name, "bundle_id": app.bundle_id},
            )

    @staticmethod
    def _check_deny(rules: _Compiled, app: AppInfo, kind: str) -> None:
        for pattern in rules.deny:
            if pattern.matches(app):
                raise _blocked(
                    f"{_KIND_VERB[kind]} {_describe(app)} is blocked by the deny_apps rule "
                    f"(entry {pattern.raw!r}).",
                    "Apps on [safety] deny_apps (password managers, Keychain…) are off limits. "
                    f"Only the user can lift this by removing {pattern.raw!r} from deny_apps. "
                    + _CONFIG_HINT,
                    rule="deny_apps",
                    kind=kind,
                    app=app,
                    entry=pattern.raw,
                )

    @staticmethod
    def _check_allow(rules: _Compiled, app: AppInfo, kind: str) -> None:
        if not rules.allow or any(p.matches(app) for p in rules.allow):
            return
        allowed = ", ".join(p.raw for p in rules.allow)
        raise _blocked(
            f"{_KIND_VERB[kind]} {_describe(app)} is blocked by the allow_apps rule "
            f"(only these apps are allowed: {allowed}).",
            f"The user can add {(app.bundle_id or app.name)!r} to [safety] allow_apps, or empty "
            f"that list to allow all apps. {_CONFIG_HINT}",
            rule="allow_apps",
            kind=kind,
            app=app,
        )

    @staticmethod
    def _check_windows(rules: _Compiled, app: AppInfo, kind: str, window_title: str) -> None:
        for rule in rules.deny_windows:
            if rule.matches(app, window_title):
                raise _blocked(
                    f"{_KIND_VERB[kind]} the window {window_title!r} of {_describe(app)} is blocked "
                    f"by the deny_windows rule (entry {rule.raw!r}).",
                    "This window is protected. Ask the user to do this step themselves; only they "
                    f"can lift it by removing {rule.raw!r} from [safety] deny_windows. {_CONFIG_HINT}",
                    rule="deny_windows",
                    kind=kind,
                    app=app,
                    entry=rule.raw,
                    window_title=window_title,
                )

    def _check_terminal(self, app: AppInfo) -> None:
        if self.shell_enabled or not self.is_terminal(app):
            return
        raise _blocked(
            f"Controlling {_describe(app)} is blocked by the terminal rule: typing or clicking in "
            "a terminal is equivalent to running shell commands, and the `shell` tool is disabled.",
            'The user can enable shell access with [tools] enable = ["scripting"] (or ["shell"]); '
            f"then use the `shell` tool instead of the terminal window. {_CONFIG_HINT}",
            rule="terminal",
            kind="input",
            app=app,
        )
