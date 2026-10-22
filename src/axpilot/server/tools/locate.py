"""The ``locate`` tool: portable locators for automation scripts (docs/dev/locators-plan.md).

``locate(ref=…)`` builds ranked locators for one element and proves each one unique in a fresh
``full`` snapshot of its window; ``locate(locator=…)`` checks a locator against the live window.
Both are read-only. :func:`locator_snapshot` is shared with ``find(locator=…)``: it resolves the
locator's app and window, applies the ``observe`` policy before anything is read, and captures
the window as a ``full`` snapshot (every visible element, which is what locators match).
"""

# No ``from __future__ import annotations`` here: the MCP SDK evaluates tool annotations.

from dataclasses import dataclass
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
    export_locators,
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
    LocatedItemModel,
    LocateModel,
    LocatorArg,
    LocatorCandidateModel,
    LocatorParam,
    SnapshotIdArg,
    WindowRefModel,
)
from axpilot.server.tools.input import element_brief

__all__ = ["MATCHES_SHOWN", "MAX_REFS", "app_windows", "locator_from_arg", "locator_snapshot"]

MATCHES_SHOWN = 20
"""``locate(locator=…)`` lists at most this many of the elements a locator matches."""
MAX_REFS = 30
"""``locate(refs=…)`` takes at most this many elements."""

_LEFT_SCREEN_HINT = (
    "Locate elements while their screen is open: before signing in, closing a sheet or moving on. "
    "Take a new snapshot to locate what is shown now."
)

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
        app = await ctx.resolve_app(spec.app)  # several copies running: the frontmost, with a warning
    except AXPilotError as err:
        if err.code is ErrorCode.APP_NOT_FOUND:
            raise AXPilotError(
                ErrorCode.APP_NOT_FOUND,
                f"The locator's app {spec.app!r} is not running.",
                hint='Launch it first (app with action="launch"), then try again.',
            ) from err
        raise
    assert app is not None  # a named app resolves or raises
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


def _where(snap: Snapshot) -> str:
    title = snap.window.title if snap.window is not None else ""
    return f"{snap.app.name} · window {quote(title, 60)} · snapshot {snap.snapshot_id}"


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


def _summary(candidate: Candidate) -> str:
    """``{"v": 1, …}  (identifier · high · unique)``."""
    unique = "unique" if candidate.unique else "unique in a partial capture"
    return f"{candidate.spec.to_json()}  ({candidate.strategy} · {candidate.grade} · {unique})"


def _candidate_lines(candidates: list[Candidate]) -> list[str]:
    return [
        f"{'recommended' if index == 0 else 'alternative'}: {_summary(candidate)}"
        for index, candidate in enumerate(candidates)
    ]


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
Call it only when the person asks for locators, a script or a test, never as a step of an
ordinary task. Find or snapshot the element, then locate(ref=…). For a flow (open the app, sign
in, …), locate each screen's elements with refs=[…] before acting on that screen: once a screen
is gone, its elements cannot be checked. Params: ref or refs (+ snapshot_id) to build locators,
or locator to check one (exact matching: every field must hold); formats (axpilot = the JSON,
axpilot-python = a script with the AXPilot SDK; for refs, one script with every step);
max_candidates.
Returns candidates, best first, each checked against a fresh capture of the window with a grade
(high: AXIdentifier; medium: role and name, which change with the app's language; low: position)
and reasons; recommended; snippets; warnings (no identifier, toolbar overflow, a menu or sheet that
must be open); for refs, one such entry per element in items. For a locator: count, unique and the
matching elements. Read-only: nothing is
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
                hint=_LEFT_SCREEN_HINT,
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


@dataclass(frozen=True, slots=True)
class _Target:
    """An element to locate, found again in a fresh ``full`` snapshot of its window."""

    snap: Snapshot
    el: Element
    selector: WindowSelector | None


async def _targets(ctx: ServerContext, refs: list[str], snapshot_id: str | None) -> list[_Target]:
    """Resolve ``refs`` and find each element in one fresh capture per window (policy checked first).
    Repeats are dropped. Raises ``NOT_SUPPORTED`` for OCR text and ``STALE_REF`` naming every
    element that is gone."""
    resolved: list[tuple[Snapshot, Element]] = []
    seen: set[tuple[int, int | None, str]] = set()
    for ref in refs:
        snap, el = ctx.snapshots.resolve(ref, snapshot_id)
        ctx.policy.check_app(
            snap.app, kind="observe", window_title=snap.window.title if snap.window else None
        )
        if is_vision_element(el):
            raise AXPilotError(
                ErrorCode.NOT_SUPPORTED,
                f"{el.ref} is OCR text, not an accessibility element, so no locator can name it.",
                hint='Use an element from snapshot(mode="interactive"), or the menu bar (select_menu).',
            )
        key = (snap.app.pid, snap.window.window_id if snap.window else None, el.ref)
        if key not in seen:
            seen.add(key)
            resolved.append((snap, el))
    fresh_of: dict[tuple[int, int | None], tuple[Snapshot, WindowSelector | None]] = {}
    targets: list[_Target] = []
    gone: list[Element] = []
    for snap, el in resolved:
        window = (snap.app.pid, snap.window.window_id if snap.window else None)
        if window not in fresh_of:
            fresh = await _fresh_full_snapshot(ctx, snap)
            fresh_of[window] = (fresh, window_selector_for(await app_windows(ctx, fresh.app), fresh.window))
        fresh, selector = fresh_of[window]
        current = fresh.element(el.ref)
        if current is None:
            gone.append(el)
        else:
            targets.append(_Target(snap=fresh, el=current, selector=selector))
    if gone:
        what = ", ".join(_label(el) for el in gone)
        verb = "is no longer in its window" if len(gone) == 1 else "are no longer in their window"
        raise AXPilotError(
            ErrorCode.STALE_REF,
            f"{what} {verb} (the screen changed since the snapshot).",
            hint=_LEFT_SCREEN_HINT,
        )
    return targets


def _located(target: _Target, limit: int) -> tuple[list[Candidate], list[str]]:
    """Candidates and warnings for one target."""
    candidates = candidates_for(
        target.snap, target.el, app=_app_key(target.snap), window=target.selector, limit=limit
    )
    warnings = [
        *locator_warnings(target.snap, target.el, candidates[0] if candidates else None),
        *_window_warnings(target.selector),
    ]
    return candidates, warnings


async def _locate_ref(
    ctx: ServerContext, ref: str, snapshot_id: str | None, formats: list[ExportFormat], limit: int
) -> CallToolResult:
    (target,) = await _targets(ctx, [ref], snapshot_id)
    fresh, current = target.snap, target.el
    candidates, warnings = _located(target, limit)
    snippets = _snippets(candidates, formats, current)
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


async def _locate_refs(
    ctx: ServerContext, refs: list[str], snapshot_id: str | None, formats: list[ExportFormat], limit: int
) -> CallToolResult:
    targets = await _targets(ctx, refs, snapshot_id)
    first = targets[0].snap
    items: list[LocatedItemModel] = []
    steps: list[tuple[LocatorSpec, Element]] = []
    shared: dict[str, list[str]] = {}  # warning -> refs it applies to, in order
    lines = [_head(first, f"locate {len(targets)} element{'s' if len(targets) != 1 else ''}")]
    for target in targets:
        candidates, warnings = _located(target, limit)
        brief = element_brief(target.el)
        assert brief is not None
        items.append(
            LocatedItemModel(
                element=brief,
                snapshot_id=target.snap.snapshot_id,
                candidates=[_candidate_model(candidate) for candidate in candidates],
                recommended=0 if candidates else None,
                warnings=warnings,
            )
        )
        where = "" if target.snap is first else f" ({_where(target.snap)})"
        if candidates:
            steps.append((candidates[0].spec, target.el))
            lines.append(f"{_label(target.el)}{where}: {_summary(candidates[0])}")
        else:
            lines.append(f"{_label(target.el)}{where}: no locator names it on its own")
        for warning in warnings:
            shared.setdefault(warning, []).append(target.el.ref)
    snippets = {fmt: export_locators(steps, fmt) for fmt in formats} if steps else {}
    for fmt, text in snippets.items():
        lines.append(f"{fmt}:")
        lines.extend(f"    {line}" if line else "" for line in text.splitlines())
    model = LocateModel(
        snapshot_id=first.snapshot_id,
        app=_app_model(first.app),
        window=_window_model(first.window),
        items=items,
        snippets=snippets,
    )
    merged = [f"{', '.join(refs)}: {warning}" for warning, refs in shared.items()]
    return ok_result("\n".join(lines), model, warnings=merged)


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
        refs: Annotated[
            list[Annotated[str, Field(min_length=2, max_length=32)]] | None,
            Field(
                description='Several element refs, e.g. ["e3", "e5", "e9"]: the steps of a flow on the '
                "current screen, located in one call.",
                min_length=1,
                max_length=MAX_REFS,
            ),
        ] = None,
        snapshot_id: SnapshotIdArg = None,
        locator: LocatorParam = None,
        formats: FormatsArg = None,
        max_candidates: Annotated[
            int, Field(description="Most locators to return for the element.", ge=1, le=10)
        ] = 3,
    ) -> Annotated[CallToolResult, LocateModel]:
        if sum(arg is not None for arg in (ref, refs, locator)) != 1:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "locate needs exactly one of ref, refs or locator.",
                hint='locate(ref="e12") builds locators, locate(refs=["e3", "e5"]) for several elements, '
                "locate(locator={…}) checks one.",
            )
        if locator is not None and snapshot_id is not None:
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "snapshot_id only goes with ref or refs: a locator is checked against the live window.",
            )
        chosen: list[ExportFormat] = list(dict.fromkeys(formats)) if formats else ["axpilot"]
        if ref is not None:
            return await _locate_ref(ctx, ref, snapshot_id, chosen, max_candidates)
        if refs is not None:
            return await _locate_refs(ctx, refs, snapshot_id, chosen, max_candidates)
        assert locator is not None
        return await _check_locator(ctx, locator, chosen, max_candidates)

    return locate
