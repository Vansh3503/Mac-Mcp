"""The ``locate`` tool: portable locators for automation scripts (docs/dev/locators-plan.md).

``locate(ref=…)`` builds ranked locators for one element and proves each one unique in a fresh
``full`` snapshot of its window; ``locate(locator=…)`` checks a locator against the live window.
Both are read-only. :func:`locator_snapshot` is shared with ``find(locator=…)``: it resolves the
locator's app and window, applies the ``observe`` policy before anything is read, and captures
the window as a ``full`` snapshot (every visible element, which is what locators match).
"""

# No ``from __future__ import annotations`` here: the MCP SDK evaluates tool annotations.

from typing import Annotated, Literal

from mcp_types import CallToolResult
from pydantic import Field

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, WindowInfo
from axpilot.perception.format import quote
from axpilot.perception.locators import (
    Candidate,
    ExportFormat,
    LocatorSpec,
    WindowSelector,
    candidates_for,
    export_locator,
    is_unique,
    locator_warnings,
    match_elements,
    parse_locator,
    pick_window,
    window_selector_for,
)
from axpilot.perception.model import Element, Snapshot
from axpilot.perception.vision import is_vision_element
from axpilot.server.context import ServerContext
from axpilot.server.registry import ToolFn, tool
from axpilot.server.results import ok_result
from axpilot.server.schemas import (
    AppRefModel,
    LocateModel,
    LocatorArg,
    LocatorCandidateModel,
    LocatorParam,
    SnapshotIdArg,
    WindowRefModel,
)
from axpilot.server.tools.input import element_brief

__all__ = ["MATCHES_SHOWN", "app_windows", "locator_from_arg", "locator_snapshot"]

MATCHES_SHOWN = 20
"""``locate(locator=…)`` lists at most this many of the elements a locator matches."""

_GONE_CODES = frozenset({ErrorCode.APP_NOT_FOUND, ErrorCode.WINDOW_NOT_FOUND})


# --------------------------------------------------------------------------- shared helpers
def locator_from_arg(arg: LocatorArg) -> LocatorSpec:
    """The validated :class:`LocatorSpec` of a tool's ``locator`` argument (``INVALID_ARGUMENT``)."""
    return parse_locator(arg.model_dump(exclude_none=True))


async def app_windows(ctx: ServerContext, app: AppInfo) -> list[WindowInfo]:
    """``app``'s windows on the current Space, front to back."""
    windows = await ctx.run_engine(ctx.engine.list_windows, str(app.pid))
    return [window for window in windows if window.pid == app.pid]


async def locator_snapshot(ctx: ServerContext, spec: LocatorSpec) -> Snapshot:
    """A ``full``, unstored snapshot of the window ``spec`` names (its app's focused window when
    it names none). The ``observe`` policy is checked on the app before its windows are listed.
    Raises ``APP_NOT_FOUND``, ``WINDOW_NOT_FOUND`` or ``BLOCKED_BY_POLICY``."""
    try:
        app = await ctx.run_engine(ctx.engine.find_app, spec.app)
    except AXPilotError as err:
        if err.code is ErrorCode.APP_NOT_FOUND:
            raise AXPilotError(
                ErrorCode.APP_NOT_FOUND,
                f"The locator's app {spec.app!r} is not running.",
                hint='Launch it first (app with action="launch"), then try again.',
            ) from err
        raise
    ctx.policy.check_app(app, kind="observe")
    window_id: int | None = None
    if spec.window is not None:
        window_id = pick_window(await app_windows(ctx, app), spec.window, app.name).window_id
    return await ctx.take_snapshot(app=str(app.pid), window_id=window_id, mode="full", store=False)


def _app_key(snap: Snapshot) -> str:
    """How generated locators name the app: its bundle id, else its name."""
    return snap.app.bundle_id or snap.app.name


def _window_model(window: WindowInfo | None) -> WindowRefModel | None:
    if window is None:
        return None
    return WindowRefModel(window_id=window.window_id, title=window.title, frame=window.frame.to_list())


def _app_model(app: AppInfo) -> AppRefModel:
    return AppRefModel(name=app.name, bundle_id=app.bundle_id, pid=app.pid)


def _label(el: Element) -> str:
    return f"{el.ref} {el.role}" + (f" {quote(el.name, 60)}" if el.name else "")


def _head(snap: Snapshot, what: str) -> str:
    bundle = f" ({snap.app.bundle_id})" if snap.app.bundle_id else ""
    window = f" · window {quote(snap.window.title, 60)}" if snap.window and snap.window.title else ""
    return f"{what} · {snap.app.name}{bundle}{window} · snapshot {snap.snapshot_id}"


def _candidate_model(candidate: Candidate) -> LocatorCandidateModel:
    return LocatorCandidateModel(
        locator=candidate.spec.to_dict(),
        strategy=candidate.strategy,
        grade=candidate.grade,
        unique=candidate.unique,
        reasons=list(candidate.reasons),
    )


def _candidate_lines(candidates: list[Candidate]) -> list[str]:
    lines: list[str] = []
    for index, candidate in enumerate(candidates):
        label = "recommended" if index == 0 else "alternative"
        unique = "unique" if candidate.unique else "unique in a partial capture"
        lines.append(
            f"{label}: {candidate.spec.to_json()}  ({candidate.strategy} · {candidate.grade} · {unique})"
        )
    return lines


def _snippet_lines(snippets: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for fmt, text in snippets.items():
        if fmt == "axpilot":
            continue  # the JSON is already on the "recommended" line
        lines.append(f"{fmt}:")
        lines.extend(f"    {line}" if line else "" for line in text.splitlines())
    return lines


# =========================================================================== locate
LOCATE_DESCRIPTION = """\
Build a stable locator for an element, for automation scripts, or check one against the window.
Refs (e12) only last for this session; a locator is a small JSON object that names the element by
its AXIdentifier, role and name, its containers or its position, and works in later runs.
Use it when the user wants a script, a test or a locator for "the Download button": find or
snapshot the element, then locate(ref=…). Params: ref (+ snapshot_id) to build locators, or
locator to check one (exact matching: every field must hold); formats (axpilot = the JSON,
axpilot-python = a script with the AXPilot SDK); max_candidates.
Returns candidates, best first, each checked against a fresh capture of the window with a grade
(high: AXIdentifier; medium: role and name, which change with the app's language; low: position)
and reasons; recommended; snippets; warnings (no identifier, toolbar overflow, a menu or sheet that
must be open). For a locator: count, unique and the matching elements. Read-only: nothing is
clicked. Errors: INVALID_ARGUMENT, STALE_REF, SNAPSHOT_NOT_FOUND, NOT_SUPPORTED (OCR text),
APP_NOT_FOUND, WINDOW_NOT_FOUND, BLOCKED_BY_POLICY, TIMEOUT."""

FormatsArg = Annotated[
    list[Literal["axpilot", "axpilot-python"]] | None,
    Field(
        description='Snippet formats for the recommended locator: "axpilot" (the JSON, default) and '
        '"axpilot-python" (a script with the AXPilot SDK).',
        min_length=1,
        max_length=2,
    ),
]


async def _fresh_full_snapshot(ctx: ServerContext, snap: Snapshot) -> Snapshot:
    """A new, stored ``full`` snapshot of ``snap``'s window, with its refs carried."""
    window_id = snap.window.window_id if snap.window is not None else None
    try:
        return await ctx.take_snapshot(
            app=str(snap.app.pid), window_id=window_id or None, mode="full", previous=snap
        )
    except AXPilotError as err:
        if err.code in _GONE_CODES:
            raise AXPilotError(
                ErrorCode.STALE_REF,
                f"The window of snapshot {snap.snapshot_id} ({snap.app.name}) is no longer open.",
                hint="Take a new snapshot and use its refs.",
            ) from err
        raise


def _snippets(candidates: list[Candidate], formats: list[ExportFormat], el: Element | None) -> dict[str, str]:
    if not candidates:
        return {}
    return {fmt: export_locator(candidates[0].spec, fmt, element=el) for fmt in formats}


def _window_warnings(selector: WindowSelector | None) -> list[str]:
    if selector is None or selector.title is None:
        return []
    return [
        f"The locator names the window by its title ({quote(selector.title, 60)}): the app has other "
        "windows. Titles often contain document names; if it changes, pick the window another way."
    ]


async def _locate_ref(
    ctx: ServerContext, ref: str, snapshot_id: str | None, formats: list[ExportFormat], limit: int
) -> CallToolResult:
    snap, el = ctx.snapshots.resolve(ref, snapshot_id)
    ctx.policy.check_app(snap.app, kind="observe", window_title=snap.window.title if snap.window else None)
    if is_vision_element(el):
        raise AXPilotError(
            ErrorCode.NOT_SUPPORTED,
            f"{el.ref} is OCR text, not an accessibility element, so no locator can name it.",
            hint='Use an element from snapshot(mode="interactive"), or the menu bar (select_menu).',
        )
    fresh = await _fresh_full_snapshot(ctx, snap)
    current = fresh.element(el.ref)
    if current is None:
        raise AXPilotError(
            ErrorCode.STALE_REF,
            f"{_label(el)} is no longer in its window.",
            hint="Take a new snapshot (or find it again) and locate the new ref.",
        )
    selector = window_selector_for(await app_windows(ctx, fresh.app), fresh.window)
    candidates = candidates_for(fresh, current, app=_app_key(fresh), window=selector, limit=limit)
    snippets = _snippets(candidates, formats, current)
    warnings = [
        *locator_warnings(fresh, current, candidates[0] if candidates else None),
        *_window_warnings(selector),
    ]
    model = LocateModel(
        snapshot_id=fresh.snapshot_id,
        app=_app_model(fresh.app),
        window=_window_model(fresh.window),
        element=element_brief(current),
        candidates=[_candidate_model(candidate) for candidate in candidates],
        recommended=0 if candidates else None,
        snippets=snippets,
    )
    lines = [
        _head(fresh, f"locate {_label(current)}"),
        *_candidate_lines(candidates),
        *_snippet_lines(snippets),
    ]
    return ok_result("\n".join(lines), model, warnings=warnings)


async def _check_locator(
    ctx: ServerContext, arg: LocatorArg, formats: list[ExportFormat], limit: int
) -> CallToolResult:
    spec = locator_from_arg(arg)
    snap = await locator_snapshot(ctx, spec)
    ctx.snapshots.put(snap)
    found = match_elements(snap, spec)
    unique = is_unique(snap, len(found))
    element = found[0] if len(found) == 1 else None
    candidates: list[Candidate] = []
    warnings: list[str] = []
    if element is not None:
        candidates = candidates_for(snap, element, app=_app_key(snap), window=spec.window, limit=limit)
        warnings = locator_warnings(snap, element, candidates[0] if candidates else None)
    elif not found:
        warnings.append(
            "Nothing matches. The element may be in a window, sheet or menu that is not open, or the "
            "app changed it; snapshot the window and locate the element again."
        )
    else:
        warnings.append(
            f"The locator matches {len(found)} elements; a script would not know which one to use. "
            "locate(ref=…) the one you mean, or add within, name or index."
        )
    if snap.truncated:
        warnings.append("This window has more elements than one capture reads; there may be more matches.")
    snippets = _snippets(candidates, formats, element)
    model = LocateModel(
        snapshot_id=snap.snapshot_id,
        app=_app_model(snap.app),
        window=_window_model(snap.window),
        element=element_brief(element),
        candidates=[_candidate_model(candidate) for candidate in candidates],
        recommended=0 if candidates else None,
        snippets=snippets,
        locator=spec.to_dict(),
        count=len(found),
        unique=unique,
        matches=[brief for el in found[:MATCHES_SHOWN] if (brief := element_brief(el)) is not None],
    )
    if not found:
        summary = "matches nothing"
    elif len(found) == 1:
        summary = "matches 1 element"
    else:
        summary = f"matches {len(found)} elements"
    lines = [_head(snap, f"locator {spec.describe()} {summary}")]
    lines.extend(_label(el) for el in found[:MATCHES_SHOWN])
    if len(found) > MATCHES_SHOWN:
        lines.append(f"… and {len(found) - MATCHES_SHOWN} more")
    lines.extend(_candidate_lines(candidates))
    lines.extend(_snippet_lines(snippets))
    return ok_result("\n".join(lines), model, warnings=warnings)


@tool(
    "locate",
    title="Locate an element for scripts",
    description=LOCATE_DESCRIPTION,
    read_only=True,
    windows_mcp_name="Locate",
)
def locate_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``locate`` tool."""

    async def locate(
        *,
        ref: Annotated[
            str | None,
            Field(
                description='Element ref from snapshot, find or explore, e.g. "e12".',
                min_length=2,
                max_length=32,
            ),
        ] = None,
        snapshot_id: SnapshotIdArg = None,
        locator: LocatorParam = None,
        formats: FormatsArg = None,
        max_candidates: Annotated[
            int, Field(description="Most locators to return for the element.", ge=1, le=10)
        ] = 3,
    ) -> Annotated[CallToolResult, LocateModel]:
        if (ref is None) == (locator is None):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "locate needs exactly one of ref or locator.",
                hint='locate(ref="e12") builds locators; locate(locator={…}) checks one.',
            )
        if locator is not None and snapshot_id is not None:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "snapshot_id only goes with ref: a locator is always checked against the live window.",
            )
        chosen: list[ExportFormat] = list(dict.fromkeys(formats)) if formats else ["axpilot"]
        if ref is not None:
            return await _locate_ref(ctx, ref, snapshot_id, chosen, max_candidates)
        assert locator is not None
        return await _check_locator(ctx, locator, chosen, max_candidates)

    return locate
