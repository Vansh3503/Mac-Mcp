"""Shared pydantic models: tool output schemas and common parameter types.

Every tool's ``structuredContent`` is one of the ``*Model`` classes below (advertised as the
tool's ``outputSchema``). Top-level outputs derive from :class:`OutputModel`, so each carries a
``warnings`` list. Coordinates are global screen **points**; rectangles are
``[x, y, width, height]`` and points ``[x, y]``.

Models built from :mod:`axpilot.perception.format` payloads (elements, snapshots, diffs)
ignore unknown keys, so perception can add fields without breaking the server; models the
server fills itself forbid them, so a typo fails tests instead of reaching clients.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from axpilot.models import AppInfo, Capture, DisplayInfo, PermissionStatus, WindowInfo

__all__ = [
    "ActionResult",
    "AppArg",
    "AppListModel",
    "AppModel",
    "AppRefModel",
    "CaptureModel",
    "ChangedElementModel",
    "ControlModel",
    "DiffCountsModel",
    "DiffModel",
    "DisplayListModel",
    "DisplayModel",
    "ElementBriefModel",
    "ElementModel",
    "InspectModel",
    "LocateModel",
    "LocatorArg",
    "LocatorCandidateModel",
    "LocatorParam",
    "LocatorStepArg",
    "LocatorWindowArg",
    "OcrLineModel",
    "OcrModel",
    "OutputModel",
    "PermissionsModel",
    "PointList",
    "RateLimitModel",
    "RectArg",
    "RectList",
    "RefArg",
    "ScreenshotModel",
    "SnapshotIdArg",
    "SnapshotModel",
    "StatusModel",
    "WaitForModel",
    "WindowIdArg",
    "WindowListModel",
    "WindowModel",
    "WindowRefModel",
]

# --------------------------------------------------------------------------- shared types
RectList = Annotated[
    list[float], Field(min_length=4, max_length=4, description="[x, y, width, height] in screen points.")
]
PointList = Annotated[list[float], Field(min_length=2, max_length=2, description="[x, y] in screen points.")]
AxQuality = Literal["good", "partial", "poor"]

# --------------------------------------------------------------------------- tool parameters
AppArg = Annotated[
    str | None,
    Field(
        description="Target app: name ('Notes'), bundle id ('com.apple.Notes') or pid. "
        "Default: the frontmost app.",
        min_length=1,
        max_length=256,
    ),
]
WindowIdArg = Annotated[
    int | None, Field(description="Window id from list_windows; scopes the call to that window.", ge=0)
]
RefArg = Annotated[
    str,
    Field(
        description='Element ref from a snapshot, e.g. "e12" (or "s_ab12:e12" to pin the snapshot).',
        min_length=2,
        max_length=32,
    ),
]
SnapshotIdArg = Annotated[
    str | None,
    Field(description="Snapshot the ref belongs to. Default: the latest snapshot.", max_length=16),
]
RectArg = Annotated[
    list[float] | None,
    Field(
        description="Region [x, y, width, height] in screen points (e.g. a frame from a snapshot).",
        min_length=4,
        max_length=4,
    ),
]


# --------------------------------------------------------------------------- bases
class OutputModel(BaseModel):
    """Base of every tool output."""

    model_config = ConfigDict(extra="forbid")

    warnings: list[str] = Field(
        default_factory=list,
        description="Degradations the caller should know about (e.g. a screenshot that was skipped).",
    )


class _Payload(BaseModel):
    """Nested data derived from perception payloads (unknown keys are ignored)."""

    model_config = ConfigDict(extra="ignore")


class _Strict(BaseModel):
    """Nested data the server fills itself."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- apps, windows, displays
class AppRefModel(_Payload):
    """Identity of an app."""

    name: str
    bundle_id: str | None = None
    pid: int


class AppModel(_Strict):
    """A running (or, with ``running=false``, installed) app."""

    name: str
    bundle_id: str | None = None
    pid: int = Field(description="Process id; 0 for an installed app that is not running.")
    path: str | None = None
    running: bool = True
    frontmost: bool = False
    hidden: bool = False
    blocked: bool = Field(default=False, description="True if AXPilot's safety policy blocks this app.")

    @classmethod
    def from_info(cls, app: AppInfo, *, blocked: bool = False) -> AppModel:
        """Build from an engine :class:`AppInfo`."""
        return cls(
            name=app.name,
            bundle_id=app.bundle_id,
            pid=app.pid,
            path=app.path,
            running=app.pid > 0,
            frontmost=app.frontmost,
            hidden=app.hidden,
            blocked=blocked,
        )


class WindowRefModel(_Payload):
    """Identity and frame of a window."""

    window_id: int
    title: str
    frame: RectList


class WindowModel(_Strict):
    """An on-screen window."""

    window_id: int
    title: str = Field(description="Window title; empty when hidden by the safety policy.")
    app: str
    pid: int
    bundle_id: str | None = None
    frame: RectList
    display_id: int | None = None
    focused: bool = False
    main: bool = False
    minimized: bool = False
    fullscreen: bool = False
    blocked: bool = Field(default=False, description="True if AXPilot's safety policy blocks this window.")

    @classmethod
    def from_info(cls, window: WindowInfo, *, blocked: bool = False) -> WindowModel:
        """Build from an engine :class:`WindowInfo` (a blocked window's title is withheld)."""
        return cls(
            window_id=window.window_id,
            title="" if blocked else window.title,
            app=window.app,
            pid=window.pid,
            bundle_id=window.bundle_id,
            frame=window.frame.to_list(),
            display_id=window.display_id,
            focused=window.focused,
            main=window.main,
            minimized=window.minimized,
            fullscreen=window.fullscreen,
            blocked=blocked,
        )


class DisplayModel(_Strict):
    """A display. ``frame`` and ``work_area`` (without menu bar and Dock) are in points."""

    display_id: int
    name: str | None = None
    frame: RectList
    work_area: RectList
    scale: float = Field(description="Backing scale factor (pixels per point), e.g. 2.0 on Retina.")
    main: bool = False

    @classmethod
    def from_info(cls, display: DisplayInfo) -> DisplayModel:
        """Build from an engine :class:`DisplayInfo`."""
        return cls(
            display_id=display.display_id,
            name=display.name,
            frame=display.frame.to_list(),
            work_area=display.work_area.to_list(),
            scale=display.scale,
            main=display.main,
        )


class AppListModel(OutputModel):
    """Output of ``list_apps``."""

    apps: list[AppModel]
    count: int


class WindowListModel(OutputModel):
    """Output of ``list_windows`` (front-to-back order)."""

    windows: list[WindowModel]
    count: int


class DisplayListModel(OutputModel):
    """Output of ``list_displays``."""

    displays: list[DisplayModel]
    count: int


# --------------------------------------------------------------------------- elements & snapshots
class ElementModel(_Payload):
    """One element of a snapshot. Act on it by passing ``ref`` to other tools."""

    ref: str = Field(description='"e12" for accessibility elements, "o3" for OCR text.')
    role: str = Field(description="Short role: button, field, textarea, row, menuitem, ocrtext…")
    name: str | None = None
    value: str | None = Field(default=None, description="Current value (never shown for secure fields).")
    frame: RectList | None = None
    center: PointList | None = None
    states: list[str] = Field(default_factory=list, description="disabled, selected, focused, checked…")
    actions: list[str] = Field(default_factory=list, description="AX actions, e.g. AXPress, AXShowMenu.")
    depth: int = 0
    parent_ref: str | None = None
    extra: dict[str, Any] = Field(
        default_factory=dict, description='Role-specific facts, e.g. {"rows": 12, "scrollable": true}.'
    )


class ElementBriefModel(_Payload):
    """A short reference to an element (used in diffs and lists)."""

    ref: str
    role: str
    name: str | None = None
    value: str | None = None
    center: PointList | None = None
    states: list[str] = Field(default_factory=list)


class CaptureModel(_Strict):
    """A stored screenshot. Pass ``screenshot_id`` with ``coordinate_space="screenshot"`` to act on
    pixel coordinates of this image."""

    screenshot_id: str
    width: int = Field(description="Image width in pixels.")
    height: int = Field(description="Image height in pixels.")
    format: Literal["png", "jpeg"]
    mime_type: str
    region: RectList = Field(description="Screen region the image shows, in points.")
    scale_x: float = Field(description="Image pixels per screen point, horizontally.")
    scale_y: float = Field(description="Image pixels per screen point, vertically.")
    annotated: bool = Field(default=False, description="True if element refs are drawn on the image.")

    @classmethod
    def from_capture(cls, capture: Capture, screenshot_id: str, *, annotated: bool = False) -> CaptureModel:
        """Describe a stored :class:`Capture`."""
        return cls(
            screenshot_id=screenshot_id,
            width=capture.width,
            height=capture.height,
            format=capture.format,
            mime_type="image/png" if capture.format == "png" else "image/jpeg",
            region=capture.region.to_list(),
            scale_x=round(capture.scale_x, 4),
            scale_y=round(capture.scale_y, 4),
            annotated=annotated,
        )


class SnapshotModel(OutputModel):
    """A page of a UI snapshot: the elements of one app/window with refs to act on."""

    model_config = ConfigDict(extra="ignore")

    snapshot_id: str
    app: AppRefModel
    window: WindowRefModel | None = None
    mode: Literal["interactive", "full", "text"] = Field(description="How elements were selected.")
    requested_mode: Literal["auto", "interactive", "full", "text", "vision"] = "interactive"
    focused_ref: str | None = None
    elements: list[ElementModel]
    shown: int
    total: int
    offset: int
    next_offset: int | None = Field(default=None, description="Pass as offset to get the next page.")
    truncated: bool = Field(description="True if the app's UI was too large to capture completely.")
    menus: list[str] = Field(default_factory=list, description="Top-level menus (use select_menu).")
    app_kind: str | None = Field(default=None, description="native, electron, java, qt, catalyst, …")
    ax_quality: AxQuality | None = Field(default=None, description="How useful the accessibility tree is.")
    strategies: list[str] = Field(default_factory=list, description="Interaction strategies, best first.")
    notes: list[str] = Field(default_factory=list, description="App-specific guidance.")
    vision_elements: int = Field(default=0, description="OCR pseudo-elements (refs o1…) added to the tree.")
    screenshot: CaptureModel | None = None


class DiffCountsModel(_Payload):
    """Full sizes of the diff lists (the lists themselves may be capped)."""

    added: int
    removed: int
    changed: int


class ChangedElementModel(_Payload):
    """An element whose name, value or state changed: ``changes`` maps field → [old, new]."""

    ref: str
    role: str
    name: str | None = None
    changes: dict[str, list[Any]]


class DiffModel(_Payload):
    """What changed between two snapshots. Refs of ``removed`` belong to ``before_id``."""

    before_id: str
    after_id: str
    empty: bool
    app_changed: bool = False
    app_before: str | None = None
    app_after: str | None = None
    window_changed: bool = False
    window_before: str | None = None
    window_after: str | None = None
    focus_before: str | None = None
    focus_after: str | None = None
    focus_after_ref: str | None = None
    added: list[ElementBriefModel] = Field(default_factory=list)
    removed: list[ElementBriefModel] = Field(default_factory=list)
    changed: list[ChangedElementModel] = Field(default_factory=list)
    counts: DiffCountsModel
    truncated: bool = False


class ActionResult(OutputModel):
    """Outcome of an input action and what it changed."""

    ok: bool = True
    action: str = Field(description="What was done, e.g. click, type_text, select_menu.")
    strategy: str | None = Field(default=None, description="The strategy that worked (ax_press, click…).")
    attempts: list[str] = Field(default_factory=list, description="Strategies tried, in order.")
    target: ElementBriefModel | None = Field(default=None, description="The element acted on, if any.")
    point: PointList | None = Field(default=None, description="Screen point acted on, if any.")
    message: str | None = None
    diff: DiffModel | None = Field(default=None, description='Changes (when return="diff").')
    snapshot: SnapshotModel | None = Field(default=None, description='Fresh snapshot (return="snapshot").')
    snapshot_id: str | None = Field(default=None, description="Snapshot taken after the action.")


class InspectModel(OutputModel):
    """Everything known about one element: snapshot data, live attributes and its neighbours."""

    snapshot_id: str
    element: ElementModel
    live: bool = Field(description="True if attributes were read from the live element just now.")
    attributes: dict[str, Any] = Field(default_factory=dict, description="Raw AX attributes.")
    identifier: str | None = None
    locator_path: list[str] = Field(default_factory=list, description="AX role path from the window.")
    ancestors: list[ElementBriefModel] = Field(default_factory=list, description="Nearest parent first.")
    children: list[ElementBriefModel] = Field(default_factory=list)


# --------------------------------------------------------------------------- locators
class LocatorStepArg(_Strict):
    """An ancestor the element must be inside (any of role, identifier, name)."""

    role: str | None = Field(default=None, max_length=64, description='AX role ("AXToolbar") or short role.')
    identifier: str | None = Field(default=None, max_length=256, description="Exact AXIdentifier.")
    name: str | None = Field(default=None, max_length=500, description="Exact name.")


class LocatorWindowArg(_Strict):
    """Which of the app's windows to search (title or index). Default: its focused window."""

    title: str | None = Field(default=None, max_length=500, description="Window title.")
    match: Literal["exact", "prefix"] | None = Field(default=None, description="How to compare the title.")
    index: int | None = Field(default=None, ge=0, le=99, description="Position among the app's windows.")


class LocatorArg(_Strict):
    """A portable locator: names an element by what the app says about it (see `locate`)."""

    v: Literal[1] | None = Field(default=None, description="Locator format version (1).")
    app: str = Field(min_length=1, max_length=256, description="Bundle id (preferred) or app name.")
    window: LocatorWindowArg | None = None
    within: list[LocatorStepArg] | None = Field(
        default=None, max_length=5, description="Ancestors the element is inside, outermost first."
    )
    role: str | None = Field(default=None, max_length=64, description='AX role ("AXButton") or short role.')
    subrole: str | None = Field(default=None, max_length=64, description='AX subrole ("AXSearchField").')
    identifier: str | None = Field(default=None, max_length=256, description="Exact AXIdentifier.")
    name: str | None = Field(
        default=None, max_length=500, description="The element's name, as snapshots show it."
    )
    name_match: Literal["exact", "prefix", "contains"] | None = Field(
        default=None, description="How to compare the name (default exact)."
    )
    index: int | None = Field(default=None, ge=0, le=9999, description="Pick this match (document order).")
    path: str | None = Field(
        default=None, max_length=2000, description='Position in the window tree, "AXToolbar[0]/AXButton[2]".'
    )


LocatorParam = Annotated[
    LocatorArg | None,
    Field(
        description='A locator from locate, e.g. {"app": "com.apple.finder", "role": "AXButton", '
        '"name": "Back"}. Exact matching: every field given must hold.'
    ),
]


class LocatorCandidateModel(_Strict):
    """One locator for an element, checked against the live window."""

    locator: dict[str, Any] = Field(description="The locator, ready for find(locator=…) or a script.")
    strategy: Literal["identifier", "name", "name_within", "role_within", "index", "path"]
    grade: Literal["high", "medium", "low"] = Field(
        description="high: survives language and layout changes; medium: survives layout changes; "
        "low: breaks when the layout changes."
    )
    unique: bool | None = Field(
        description="true: matches only this element; null: unique in a capture that was cut short."
    )
    reasons: list[str] = Field(default_factory=list, description="Why it got this grade.")


class LocateModel(OutputModel):
    """Output of ``locate``."""

    snapshot_id: str = Field(description="Snapshot the refs belong to.")
    app: AppRefModel
    window: WindowRefModel | None = None
    element: ElementBriefModel | None = Field(
        default=None, description="The element located (for a locator: when it matches exactly one)."
    )
    candidates: list[LocatorCandidateModel] = Field(
        default_factory=list, description="Locators for the element, best first."
    )
    recommended: int | None = Field(default=None, description="Index of the locator to use (null: none).")
    snippets: dict[str, str] = Field(
        default_factory=dict, description="The recommended locator in each requested format."
    )
    locator: dict[str, Any] | None = Field(default=None, description="The locator that was checked.")
    count: int | None = Field(default=None, description="How many elements the checked locator matches.")
    unique: bool | None = Field(
        default=None,
        description="true: the checked locator matches exactly one element of a complete capture.",
    )
    matches: list[ElementBriefModel] = Field(
        default_factory=list, description="Elements the checked locator matches (at most 20)."
    )


class ScreenshotModel(OutputModel, CaptureModel):
    """Output of ``screenshot``; the image itself is in the result content."""

    target: Literal["display", "window", "app", "region"]
    display_id: int | None = None
    window_id: int | None = None
    app: str | None = None
    excluded_apps: list[str] = Field(
        default_factory=list, description="Apps left out of the image by the safety policy."
    )


class OcrLineModel(_Strict):
    """One line of recognized text."""

    text: str
    confidence: float
    frame: RectList
    center: PointList


class OcrModel(OutputModel):
    """Output of ``ocr``: recognized text with positions (top-to-bottom, left-to-right)."""

    screenshot_id: str = Field(description="The capture the text was read from.")
    region: RectList
    lines: list[OcrLineModel]
    count: int
    text: str = Field(description="All lines joined with newlines.")


# --------------------------------------------------------------------------- wait_for & status
class WaitForModel(OutputModel):
    """Output of ``wait_for`` (a timeout is reported as a ``TIMEOUT`` error instead)."""

    condition: Literal["element", "element_gone", "window", "app_running", "text_visible", "idle", "duration"]
    query: str | None = None
    satisfied: bool = True
    waited_ms: float
    polls: int
    element: ElementBriefModel | None = Field(
        default=None, description="Matching element (condition=element)."
    )
    snapshot_id: str | None = Field(default=None, description="Snapshot the element ref belongs to.")
    window: WindowModel | None = None
    app: AppModel | None = None
    text: str | None = Field(default=None, description="The text that matched (condition=text_visible).")


class PermissionsModel(_Strict):
    """macOS privacy permissions as seen by AXPilot."""

    accessibility: bool
    screen_recording: bool
    automation: dict[str, bool] = Field(default_factory=dict)
    responsible_app: str | None = None
    notes: list[str] = Field(default_factory=list)

    @classmethod
    def from_status(cls, status: PermissionStatus) -> PermissionsModel:
        """Build from the engine's :class:`PermissionStatus`."""
        return cls(
            accessibility=status.accessibility,
            screen_recording=status.screen_recording,
            automation=dict(status.automation),
            responsible_app=status.responsible_app,
            notes=list(status.notes),
        )


class ControlModel(_Payload):
    """Kill switch and input-lock state."""

    paused: bool
    pause_reason: str | None = None
    busy: bool = False
    current_action: str | None = None
    yield_to_user: bool = True
    idle_threshold_s: float = 1.0


class RateLimitModel(_Strict):
    """Input rate limit."""

    max_per_minute: int
    remaining: int


class StatusModel(OutputModel):
    """Output of ``status``."""

    server: str
    version: str
    engine: str
    os_version: str | None = None
    mode: Literal["direct", "host"] = Field(description="direct: this process; host: via AXPilot.app.")
    client: str | None = None
    pairing: str = Field(default="unknown", description="Client pairing state (unknown in direct mode).")
    clients: list[dict[str, Any]] | None = Field(default=None, description="Connected clients (host mode).")
    permissions: PermissionsModel | None = None
    control: ControlModel
    profile: str
    naming: str
    enabled_tools: list[str]
    confirm: str = Field(description="Confirmation mode: never | destructive | always.")
    rate_limit: RateLimitModel
    audit_enabled: bool
    frontmost_app: AppRefModel | None = None
