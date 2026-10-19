"""Browser tabs and JavaScript through AppleScript (Safari and the Chromium family).

Two scripting dialects are supported:

* **Safari** (``family="safari"``): tabs have ``name`` and ``URL``, a window has a
  ``current tab``, and JavaScript runs with ``do JavaScript <code> in <tab>``.
* **Chromium** (``family="chromium"``: Chrome, Arc, Edge, Brave, Chromium, Vivaldi): tabs have
  ``title`` and ``URL``, a window has an ``active tab``, and JavaScript runs with
  ``execute <tab> javascript <code>``.

Scripts address the browser by bundle id (``application id "…"``), so localized or renamed apps
still work. Every user-supplied string (URLs, JavaScript) is embedded with
:func:`~axpilot.system.osascript.applescript_string`; numbers are validated integers.

Tabs are addressed as ``(window_index, tab_index)``, both 1-based, where window 1 is the
frontmost window. Indices change when windows are reordered or tabs opened/closed, so list the
tabs again before acting on stale ones.

Tab listings are returned by the script as records separated by ASCII RS (``0x1E``) with fields
separated by ASCII US (``0x1F``). Titles and URLs are cleaned of those characters (and line
breaks) inside AppleScript, so parsing is unambiguous.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.osascript import applescript_string, output_text, run_applescript
from axpilot.system.runner import CommandRunner

__all__ = [
    "BROWSERS",
    "BrowserFamily",
    "BrowserSpec",
    "TabInfo",
    "TabRef",
    "activate_tab",
    "build_activate_tab_script",
    "build_close_tab_script",
    "build_eval_js_script",
    "build_frontmost_script",
    "build_list_tabs_script",
    "build_open_url_script",
    "close_tab",
    "eval_js",
    "frontmost_browser",
    "list_tabs",
    "normalize_url",
    "open_url",
    "parse_tabs",
    "resolve_browser",
]

_log = logging.getLogger(__name__)

BrowserFamily = Literal["safari", "chromium"]
TabSelect = Literal["current_tab", "active_tab_index", "select"]
TabRef = tuple[int, int]
"""``(window_index, tab_index)``, both 1-based; window 1 is the frontmost window."""


@dataclass(frozen=True, slots=True)
class BrowserSpec:
    """A scriptable browser.

    ``tab_select`` is how the browser's dictionary switches tabs: Safari sets ``current tab``,
    Chrome-like browsers set ``active tab index``, Arc uses the ``select`` command.
    """

    key: str
    name: str
    bundle_id: str
    family: BrowserFamily
    tab_select: TabSelect


BROWSERS: dict[str, BrowserSpec] = {
    spec.key: spec
    for spec in (
        BrowserSpec("safari", "Safari", "com.apple.Safari", "safari", "current_tab"),
        BrowserSpec(
            "safari_preview",
            "Safari Technology Preview",
            "com.apple.SafariTechnologyPreview",
            "safari",
            "current_tab",
        ),
        BrowserSpec("chrome", "Google Chrome", "com.google.Chrome", "chromium", "active_tab_index"),
        BrowserSpec("arc", "Arc", "company.thebrowser.Browser", "chromium", "select"),
        BrowserSpec("edge", "Microsoft Edge", "com.microsoft.edgemac", "chromium", "active_tab_index"),
        BrowserSpec("brave", "Brave Browser", "com.brave.Browser", "chromium", "active_tab_index"),
        BrowserSpec("chromium", "Chromium", "org.chromium.Chromium", "chromium", "active_tab_index"),
        BrowserSpec("vivaldi", "Vivaldi", "com.vivaldi.Vivaldi", "chromium", "active_tab_index"),
    )
}
"""Supported browsers by key. Order matters for :func:`frontmost_browser`'s fallback."""

_ALIASES: dict[str, str] = {
    "google chrome": "chrome",
    "googlechrome": "chrome",
    "microsoft edge": "edge",
    "brave browser": "brave",
    "brave": "brave",
    "safari technology preview": "safari_preview",
}

US = "\x1f"
"""ASCII unit separator: between the fields of one tab record."""
RS = "\x1e"
"""ASCII record separator: between tab records."""

MAX_INDEX = 10_000
MAX_JS_CHARS = 100_000
MAX_URL_CHARS = 8192
LIST_TIMEOUT_S = 15.0
ACTION_TIMEOUT_S = 15.0
JS_TIMEOUT_S = 30.0
_LIST_MAX_OUTPUT = 2_000_000
_JS_MAX_OUTPUT = 100_000

_BLOCKED_SCHEMES = frozenset({"javascript", "data", "vbscript", "blob"})
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_HOST_PORT_RE = re.compile(r"^(?:localhost|[^:/]*\.[^:/]*):\d+(?:[/?#]|$)", re.IGNORECASE)
_JS_DISABLED_RE = re.compile(
    r"allow javascript from apple ?events|javascript through applescript is turned off",
    re.IGNORECASE,
)
_NO_SUCH_OBJECT = frozenset({-1728, -1719})
_APP_NOT_RUNNING = -600
_NO_RESULT = -2763

_JS_HINTS: dict[BrowserFamily, str] = {
    "safari": (
        "Turn on Safari → Settings → Advanced → 'Show features for web developers', then "
        "Develop → 'Allow JavaScript from Apple Events'. Or use `browser_read` to read the page."
    ),
    "chromium": (
        "In the browser's menu bar choose View → Developer → 'Allow JavaScript from Apple Events'. "
        "Or use `browser_read` to read the page."
    ),
}


@dataclass(frozen=True, slots=True)
class TabInfo:
    """One browser tab. ``active`` means it is the selected tab of its window."""

    window_index: int
    tab_index: int
    title: str
    url: str
    active: bool

    @property
    def ref(self) -> TabRef:
        """``(window_index, tab_index)`` for :func:`activate_tab`, :func:`close_tab`, :func:`eval_js`."""
        return (self.window_index, self.tab_index)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready dict."""
        return asdict(self)


# --------------------------------------------------------------------------------------------
# Validation


def resolve_browser(browser: str | BrowserSpec) -> BrowserSpec:
    """Find a browser by key (``"chrome"``), app name (``"Google Chrome"``) or bundle id."""
    if isinstance(browser, BrowserSpec):
        return browser
    query = browser.strip().casefold() if isinstance(browser, str) else ""
    query = query.removesuffix(".app")
    key = _ALIASES.get(query, query)
    if key in BROWSERS:
        return BROWSERS[key]
    for spec in BROWSERS.values():
        if query in (spec.name.casefold(), spec.bundle_id.casefold()):
            return spec
    raise AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Unsupported browser {browser!r}.",
        hint=f"Use one of: {', '.join(BROWSERS)} (or the app name / bundle id).",
    )


def _validate_index(value: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_INDEX:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"The {what} index must be a positive integer (1 is the first), got {value!r}.",
            hint="Call `browser_tabs` with action `list` to see window and tab indices.",
        )
    return value


def _validate_tab(tab: TabRef | None) -> TabRef | None:
    if tab is None:
        return None
    if not isinstance(tab, tuple | list) or len(tab) != 2:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, "A tab is addressed as (window_index, tab_index), e.g. (1, 2)."
        )
    return (_validate_index(tab[0], "window"), _validate_index(tab[1], "tab"))


def normalize_url(url: str) -> str:
    """Validate a URL to open; a bare host such as ``example.com`` gets ``https://``.

    ``javascript:``, ``data:``, ``blob:`` and ``vbscript:`` URLs are refused with
    ``BLOCKED_BY_POLICY``: they run code or hide content, so they must go through
    ``browser_eval_js`` and its confirmation instead.
    """
    if not isinstance(url, str) or not url.strip():
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "The URL is empty.")
    text = url.strip()
    if len(text) > MAX_URL_CHARS:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, f"The URL is too long (max {MAX_URL_CHARS} characters)."
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, "URLs cannot contain control characters or line breaks."
        )
    has_scheme = bool(_SCHEME_RE.match(text)) and not _HOST_PORT_RE.match(text)
    if not has_scheme:
        text = "https://" + text
    scheme = urlsplit(text).scheme.lower()
    if scheme in _BLOCKED_SCHEMES:
        raise AXPilotError(
            ErrorCode.BLOCKED_BY_POLICY,
            f"Opening {scheme}: URLs is not allowed.",
            hint="To run JavaScript in a page use `browser_eval_js`; otherwise pass an http(s) URL.",
        )
    return text


# --------------------------------------------------------------------------------------------
# Script builders (pure functions; unit-tested on any OS)

_CLEAN_HANDLER = "\n".join(
    [
        "on axpClean(v)",
        '\tif v is missing value then return ""',
        "\tset v to v as text",
        "\tset oldDelims to AppleScript's text item delimiters",
        "\tset AppleScript's text item delimiters to {character id 30, character id 31, return, linefeed}",
        "\tset parts to text items of v",
        '\tset AppleScript\'s text item delimiters to " "',
        "\tset v to parts as text",
        "\tset AppleScript's text item delimiters to oldDelims",
        "\treturn v",
        "end axpClean",
    ]
)


def _tell(spec: BrowserSpec) -> str:
    return f"tell application id {applescript_string(spec.bundle_id)}"


def _running_guard(spec: BrowserSpec) -> str:
    """Fail with error -600 instead of launching a browser that is not running."""
    app = f"application id {applescript_string(spec.bundle_id)}"
    message = applescript_string(f"{spec.name} is not running.")
    return f"if not ({app} is running) then error {message} number {_APP_NOT_RUNNING}"


def _front_tab(spec: BrowserSpec) -> str:
    return "current tab of window 1" if spec.family == "safari" else "active tab of window 1"


def _tab_specifier(spec: BrowserSpec, tab: TabRef | None) -> str:
    if tab is None:
        return _front_tab(spec)
    window_index, tab_index = tab
    return f"tab {int(tab_index)} of window {int(window_index)}"


def build_list_tabs_script(spec: BrowserSpec) -> str:
    """A script returning one ``window US tab US active US url US title`` record per tab, joined by RS."""
    if spec.family == "safari":
        active_setup = [
            "\t\t\tset axpCurrent to index of current tab of axpWindow",
        ]
        active_test = "\t\t\t\tset axpIsActive to (axpTabIndex = axpCurrent)"
        title = "name of axpTab"
    else:
        active_setup = [
            "\t\t\tset axpActiveId to missing value",
            "\t\t\ttry",
            "\t\t\t\tset axpActiveId to id of active tab of axpWindow",
            "\t\t\tend try",
        ]
        active_test = (
            "\t\t\t\tset axpIsActive to (axpActiveId is not missing value and (id of axpTab) = axpActiveId)"
        )
        title = "title of axpTab"
    body = [
        *active_setup,
        "\t\t\tset axpTabIndex to 0",
        "\t\t\trepeat with axpTab in tabs of axpWindow",
        "\t\t\t\tset axpTabIndex to axpTabIndex + 1",
        active_test,
        "\t\t\t\tset end of axpRecords to (axpWindowIndex as text) & US & (axpTabIndex as text) & US & "
        f"(axpIsActive as text) & US & my axpClean(URL of axpTab) & US & my axpClean({title})",
        "\t\t\tend repeat",
    ]
    # A window without tabs (e.g. a Settings window) is skipped, but keeps its index.
    body = ["\t\ttry", *body, "\t\tend try"]
    return "\n".join(
        [
            _CLEAN_HANDLER,
            _running_guard(spec),
            "set US to character id 31",
            "set RS to character id 30",
            "set axpRecords to {}",
            _tell(spec),
            "\tset axpWindowIndex to 0",
            "\trepeat with axpWindow in windows",
            "\t\tset axpWindowIndex to axpWindowIndex + 1",
            *body,
            "\tend repeat",
            "end tell",
            "set AppleScript's text item delimiters to RS",
            "set axpOut to axpRecords as text",
            'set AppleScript\'s text item delimiters to ""',
            "return axpOut",
        ]
    )


def build_activate_tab_script(spec: BrowserSpec, tab: TabRef) -> str:
    """A script that selects ``tab``, brings its window to the front and activates the browser."""
    validated = _validate_tab(tab)
    if validated is None:  # pragma: no cover - _validate_tab only returns None for None
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "A tab is required.")
    window_index, tab_index = validated
    window = f"window {window_index}"
    if spec.tab_select == "current_tab":
        select = f"\tset current tab of {window} to tab {tab_index} of {window}"
    elif spec.tab_select == "active_tab_index":
        select = f"\tset active tab index of {window} to {tab_index}"
    else:
        select = f"\ttell tab {tab_index} of {window} to select"
    return "\n".join(
        [
            _running_guard(spec),
            _tell(spec),
            f"\tget tab {tab_index} of {window}",  # fails with -1719/-1728 if it does not exist
            select,
            "\ttry",
            f"\t\tset index of {window} to 1",
            "\tend try",
            "\tactivate",
            "end tell",
        ]
    )


def build_open_url_script(spec: BrowserSpec, url: str, *, new_window: bool = False) -> str:
    """A script that opens ``url`` in a new tab of the front window (or a new window)."""
    literal = applescript_string(normalize_url(url))
    condition = "true" if new_window else "(count of windows) = 0"
    if spec.family == "safari":
        open_window = [f"\t\tmake new document with properties {{URL:{literal}}}"]
        open_tab = [
            "\t\ttell window 1",
            f"\t\t\tset axpTab to make new tab at end of tabs with properties {{URL:{literal}}}",
            "\t\t\tset current tab to axpTab",
            "\t\tend tell",
        ]
    else:
        open_window = [
            "\t\tset axpWindow to make new window",
            f"\t\tset URL of active tab of axpWindow to {literal}",
        ]
        open_tab = [f"\t\ttell window 1 to make new tab with properties {{URL:{literal}}}"]
    return "\n".join(
        [
            _tell(spec),
            "\tactivate",
            f"\tif {condition} then",
            *open_window,
            "\telse",
            *open_tab,
            "\tend if",
            "end tell",
        ]
    )


def build_close_tab_script(spec: BrowserSpec, tab: TabRef | None = None) -> str:
    """A script that closes ``tab`` (default: the front window's selected tab)."""
    target = _tab_specifier(spec, _validate_tab(tab))
    return "\n".join([_running_guard(spec), _tell(spec), f"\tclose {target}", "end tell"])


def build_eval_js_script(spec: BrowserSpec, js: str, tab: TabRef | None = None) -> str:
    """A script that runs ``js`` in ``tab`` (default: the front window's selected tab).

    The script returns the JavaScript result (``""`` for ``undefined``/``null``).
    """
    if not isinstance(js, str) or not js.strip():
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "The JavaScript code is empty.")
    if len(js) > MAX_JS_CHARS:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT, f"The JavaScript code is too long (max {MAX_JS_CHARS} characters)."
        )
    code = applescript_string(js)
    target = _tab_specifier(spec, _validate_tab(tab))
    if spec.family == "safari":
        command = f"do JavaScript {code} in {target}"
    else:
        command = f"execute {target} javascript {code}"
    return "\n".join(
        [
            _running_guard(spec),
            "set axpResult to missing value",
            _tell(spec),
            "\ttry",
            f"\t\tset axpResult to ({command})",
            "\ton error axpMessage number axpNumber",
            f"\t\tif axpNumber is not {_NO_RESULT} then error axpMessage number axpNumber",
            "\tend try",
            "end tell",
            'if axpResult is missing value then return ""',
            "return axpResult",
        ]
    )


def build_frontmost_script() -> str:
    """A script returning the bundle id of the browser to target, or ``""``.

    The frontmost app wins if it is a supported browser; otherwise the first running browser in
    :data:`BROWSERS` order. Uses only Standard Additions (no Automation permission needed).
    """
    ids = ", ".join(applescript_string(spec.bundle_id) for spec in BROWSERS.values())
    return "\n".join(
        [
            f"set axpIds to {{{ids}}}",
            'set axpFront to ""',
            "try",
            "\tset axpFront to id of application ((path to frontmost application) as text)",
            "end try",
            "if axpIds contains axpFront then return axpFront",
            "repeat with axpIndex from 1 to count of axpIds",
            "\tset axpId to item axpIndex of axpIds",
            "\ttry",
            "\t\tif application id axpId is running then return axpId",
            "\tend try",
            "end repeat",
            'return ""',
        ]
    )


# --------------------------------------------------------------------------------------------
# Parsing


def parse_tabs(output: str) -> list[TabInfo]:
    """Parse the output of :func:`build_list_tabs_script`. Malformed records are skipped."""
    text = output.removesuffix("\n")
    tabs: list[TabInfo] = []
    for record in text.split(RS):
        if not record.strip():
            continue
        fields = record.split(US, 4)
        if len(fields) != 5:
            _log.debug("skipping malformed tab record: %r", record[:200])
            continue
        window_text, tab_text, active_text, url, title = fields
        try:
            window_index, tab_index = int(window_text), int(tab_text)
        except ValueError:
            _log.debug("skipping tab record with bad indices: %r", record[:200])
            continue
        tabs.append(TabInfo(window_index, tab_index, title, url, active_text.strip() == "true"))
    return tabs


# --------------------------------------------------------------------------------------------
# Running


def _run(
    runner: CommandRunner, spec: BrowserSpec, script: str, *, timeout_s: float, max_output: int = 20_000
) -> str:
    try:
        return output_text(run_applescript(runner, script, timeout_s=timeout_s, max_output=max_output))
    except AXPilotError as exc:
        raise _refine(exc, spec) from exc


def _refine(exc: AXPilotError, spec: BrowserSpec) -> AXPilotError:
    """Turn generic osascript errors into browser-specific ones."""
    stderr = str(exc.details.get("stderr", ""))
    number = exc.details.get("error_number")
    details = {**exc.details, "browser": spec.key}
    if _JS_DISABLED_RE.search(stderr) or _JS_DISABLED_RE.search(exc.message):
        return AXPilotError(
            ErrorCode.NOT_SUPPORTED,
            f"{spec.name} blocks scripted JavaScript: its 'Allow JavaScript from Apple Events' "
            "setting is off.",
            hint=_JS_HINTS[spec.family],
            details=details,
        )
    if exc.code is ErrorCode.APP_NOT_FOUND:
        if number == _APP_NOT_RUNNING:
            return AXPilotError(
                ErrorCode.APP_NOT_FOUND,
                f"{spec.name} is not running.",
                hint=f"Open it first (the `app` tool with action `launch`, app {spec.name!r}).",
                details=details,
            )
        return AXPilotError(
            ErrorCode.APP_NOT_FOUND,
            f"{spec.name} is not installed.",
            hint=f"Supported browsers: {', '.join(BROWSERS)}.",
            details=details,
        )
    if exc.code is ErrorCode.COMMAND_FAILED and number in _NO_SUCH_OBJECT:
        return AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"That window or tab does not exist in {spec.name}.",
            hint="Call `browser_tabs` with action `list` to get current window and tab indices.",
            details=details,
        )
    return AXPilotError(exc.code, exc.message, hint=exc.hint, details=details)


def list_tabs(runner: CommandRunner, browser: str | BrowserSpec) -> list[TabInfo]:
    """All tabs of all windows of ``browser``. Raises ``APP_NOT_FOUND`` if it is not running."""
    spec = resolve_browser(browser)
    output = _run(
        runner, spec, build_list_tabs_script(spec), timeout_s=LIST_TIMEOUT_S, max_output=_LIST_MAX_OUTPUT
    )
    return parse_tabs(output)


def activate_tab(runner: CommandRunner, browser: str | BrowserSpec, tab: TabRef) -> None:
    """Select ``tab`` (``(window_index, tab_index)``), raise its window and focus the browser."""
    spec = resolve_browser(browser)
    _run(runner, spec, build_activate_tab_script(spec, tab), timeout_s=ACTION_TIMEOUT_S)


def open_url(
    runner: CommandRunner, browser: str | BrowserSpec, url: str, *, new_window: bool = False
) -> None:
    """Open ``url`` in a new tab of the front window (or a ``new_window``); launches the browser."""
    spec = resolve_browser(browser)
    _run(runner, spec, build_open_url_script(spec, url, new_window=new_window), timeout_s=ACTION_TIMEOUT_S)


def close_tab(runner: CommandRunner, browser: str | BrowserSpec, tab: TabRef | None = None) -> None:
    """Close ``tab`` (default: the front window's selected tab)."""
    spec = resolve_browser(browser)
    _run(runner, spec, build_close_tab_script(spec, tab), timeout_s=ACTION_TIMEOUT_S)


def eval_js(
    runner: CommandRunner,
    browser: str | BrowserSpec,
    js: str,
    tab: TabRef | None = None,
    *,
    timeout_s: float = JS_TIMEOUT_S,
) -> str:
    """Run ``js`` in ``tab`` (default: the front window's selected tab) and return the result.

    Return a string from the code (e.g. ``JSON.stringify(...)``) to get structured data back
    unchanged. Raises ``NOT_SUPPORTED`` when the browser's "Allow JavaScript from Apple Events"
    setting is off (the hint says where to turn it on).
    """
    spec = resolve_browser(browser)
    return _run(
        runner, spec, build_eval_js_script(spec, js, tab), timeout_s=timeout_s, max_output=_JS_MAX_OUTPUT
    )


def frontmost_browser(runner: CommandRunner) -> str | None:
    """The :data:`BROWSERS` key of the browser to target by default, or ``None``.

    That is the frontmost app if it is a supported browser, else the first running supported
    browser, else ``None``.
    """
    output = output_text(run_applescript(runner, build_frontmost_script(), timeout_s=ACTION_TIMEOUT_S))
    bundle_id = output.strip()
    for spec in BROWSERS.values():
        if spec.bundle_id == bundle_id:
            return spec.key
    return None
