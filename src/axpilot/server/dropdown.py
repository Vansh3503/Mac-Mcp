"""Choose an option of a dropdown: open it, wait for its options, pick one, check that it took.

This is the one flow behind ``select_option`` for pop-up buttons, menu buttons and web or
Electron pickers, and behind ``set_value``/``fill_form`` on pop-up buttons. It works through the
:class:`~axpilot.engine.base.Engine` API only, so the fake desktop exercises the same code that
drives a real Mac.

1. **Open.** Native controls get ``AXPress`` (``AXShowMenu`` when they have no press), with a
   mouse click as the fallback. Controls in web content (a web area, or an Electron app) get a
   real mouse click: page scripts (React Select, MUI, Slack, VS Code) listen to mouse events
   and may ignore accessibility actions. A web text combo box that shows nothing on click gets
   the Down arrow, which opens ARIA comboboxes.
2. **Wait** (at most :data:`OPEN_WAIT_S`) for what appeared, re-capturing the window: the
   control's own menu, any other new menu (application level, context menus, ARIA menus), a new
   list of options in a web page, or a new window of the app holding a menu or list (Java and
   Qt pop-ups). See :mod:`axpilot.perception.dropdown`.
3. **Pick** the option by title (exact, prefix, substring) or 1-based index. When a web text
   combo box lists no match, the title is typed to filter the list (autocomplete), once.
4. **Press** it: menu items with ``AXPress``; options in web content with a real click.
5. **Verify**: a pop-up must show the chosen title (``ELEMENT_NOT_ACTIONABLE`` otherwise); a web
   picker should show it in its value, title or the text next to it (a warning when it does not:
   the page may show it elsewhere); a menu that stays open is closed with a warning.

Whatever fails after the control was opened, the menu or list is closed again (``AXCancel``,
else Escape when the app is frontmost and the user has not paused AXPilot), shielded from
cancellation and bounded by :data:`CLEANUP_TIMEOUT_S`. Input is never repeated: each rung of the
ladder is a different strategy, tried only when the previous one visibly did nothing.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, KeyCombo, RawNode, RawTree, Rect
from axpilot.perception import dropdown as dd
from axpilot.perception.appkind import detect_app_kind
from axpilot.perception.format import quote
from axpilot.perception.refs import HandleIndex
from axpilot.server.context import ServerContext
from axpilot.server.tools.input import (
    Target,
    activate_app,
    click_target,
    ensure_frontmost,
    require_keyboard,
    resolve_point,
    type_keystrokes,
)

__all__ = ["DropdownOutcome", "choose_option", "in_web"]

_log = logging.getLogger(__name__)

OPEN_WAIT_S = 2.0
"""How long the options may take to appear after the control is opened."""
FILTER_WAIT_S = 1.5
"""How long a web combo box may take to update its list after typing."""
POLL_S = 0.1
CLEANUP_TIMEOUT_S = 2.0
CLOSE_WAIT_S = 0.5
"""How long a menu may take to close by itself after a pick (macOS fades it out) before AXCancel."""
OPTION_CHECKS = 3
"""How many options :func:`_still_open` looks at to tell whether a list is still shown."""
"""Closing a menu or list after a failure never takes longer than this."""
WEB_APP_KINDS = frozenset({"electron", "cef"})
"""App kinds whose whole window is web content."""
TEXT_COMBO_ROLES = frozenset({"AXComboBox", "AXTextField"})
OPEN_KEY = KeyCombo("down")
ESCAPE = KeyCombo("escape")
SELECT_ALL = KeyCombo("a", frozenset({"cmd"}))
DELETE = KeyCombo("delete")


@dataclass
class DropdownOutcome:
    """What :func:`choose_option` did."""

    strategy: str | None
    """``menu`` (a native menu), ``menu_window`` (a pop-up window), ``listbox`` (a web list),
    or ``None`` when the option was already chosen."""
    chosen: str
    message: str
    attempts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.strategy is not None


@dataclass
class _Baseline:
    """The window before opening: what was visible, and which windows the app had."""

    tree: RawTree
    visible: HandleIndex
    windows: frozenset[int]


def _app_key(app: AppInfo | None) -> str | None:
    return (app.bundle_id or str(app.pid)) if app is not None else None


def in_web(ctx: ServerContext, target: Target) -> bool:
    """True if the control is part of web content: inside a web area, or in an Electron/CEF app."""
    el = target.el
    if el is not None and any(role == dd.WEB_AREA_ROLE for role, _ in el.locator.path):
        return True
    app = target.app
    if app is None:
        return False
    try:
        return detect_app_kind(app.bundle_id, app.path) in WEB_APP_KINDS
    except Exception as exc:  # detection is a heuristic; never fail a choice because of it (A8)
        _log.debug("App-kind detection failed for %s: %s", app.name, exc)
        return False


# --------------------------------------------------------------------------- reading
async def _capture(ctx: ServerContext, target: Target) -> RawTree:
    window_id = target.window.window_id if target.window is not None and target.window.window_id else None
    return await ctx.capture_tree(app=_app_key(target.app), window_id=window_id, kind="input")


async def _window_ids(ctx: ServerContext, app: AppInfo | None) -> frozenset[int]:
    if app is None:
        return frozenset()
    windows = await ctx.run_engine(ctx.engine.list_windows, _app_key(app))
    return frozenset(window.window_id for window in windows if window.window_id)


async def _baseline(ctx: ServerContext, target: Target) -> _Baseline:
    tree = await _capture(ctx, target)
    return _Baseline(tree, dd.visible_handles(tree.root), await _window_ids(ctx, target.app))


def _locate(tree: RawTree, target: Target, baseline: _Baseline, web: bool) -> dd.OptionList | None:
    found = dd.find_node(tree.root, target.handle)
    if found is not None:
        control, ancestors = found
        menu = dd.open_menu_of(control)
        if menu is not None:
            return dd.OptionList(menu, dd.menu_options(menu), "menu", in_web=dd.in_web_content(ancestors))
    menus = dd.new_menus(tree.root, baseline.visible)
    if menus:
        menu, ancestors = menus[0]
        return dd.OptionList(menu, dd.menu_options(menu), "app_menu", in_web=dd.in_web_content(ancestors))
    if web:
        return dd.page_options(tree.root, baseline.visible, exclude=target.handle)
    return None


async def _in_new_window(ctx: ServerContext, target: Target, baseline: _Baseline) -> dd.OptionList | None:
    for window_id in sorted(await _window_ids(ctx, target.app) - baseline.windows):
        try:
            tree = await ctx.capture_tree(app=_app_key(target.app), window_id=window_id, kind="input")
        except AXPilotError as err:
            if err.code in (ErrorCode.WINDOW_NOT_FOUND, ErrorCode.STALE_REF):
                continue  # it closed again while we looked
            raise
        found = dd.options_in(tree.root)
        if found is not None:
            found.window_id = window_id
            return found
    return None


async def _wait_for_options(
    ctx: ServerContext,
    target: Target,
    baseline: _Baseline,
    web: bool,
    *,
    timeout_s: float = OPEN_WAIT_S,
    accept: Callable[[dd.OptionList], bool] | None = None,
) -> dd.OptionList | None:
    """Poll until the opened control shows options (and ``accept`` likes them), or time out."""
    deadline = ctx.clock() + timeout_s
    await ctx.sleep(max(ctx.config.perception.settle_ms / 1000, POLL_S))
    while True:
        ctx.control.ensure_running()
        tree = await _capture(ctx, target)
        found = _locate(tree, target, baseline, web) or await _in_new_window(ctx, target, baseline)
        if found is not None and (accept is None or accept(found)):
            return found
        if ctx.clock() >= deadline:
            return found if accept is None else None
        await ctx.sleep(POLL_S)


# --------------------------------------------------------------------------- acting
async def _bring_forward(ctx: ServerContext, target: Target) -> None:
    """Make the control's window the key window, as a user's click would: menus check their items
    against it (Finder's Action menu opened in a background window showed New Folder disabled,
    macOS 26)."""
    window, app = target.window, target.app
    front = await ctx.run_engine(ctx.engine.frontmost_app)
    in_front = front is not None and app is not None and front.pid == app.pid
    if in_front and (window is None or window.focused):
        return
    if window is not None and window.window_id:
        await ctx.run_engine(ctx.engine.window_control, window.window_id, "focus", None)
        await ctx.sleep(ctx.config.perception.settle_ms / 1000)
    else:
        await activate_app(ctx, app)


async def _open(ctx: ServerContext, target: Target, web: bool) -> str:
    """Open the control; returns the rung used (``open:ax:AXPress``, ``open:mouse``)."""
    el = target.el
    assert el is not None
    if not web:
        await _bring_forward(ctx, target)
        action = next((a for a in ("AXPress", "AXShowMenu") if a in el.actions), None)
        if action is not None and target.handle is not None:
            await ctx.run_engine(ctx.engine.perform_action, target.handle, action)
            return f"open:ax:{action}"
    used, _ = await click_target(ctx, target, button="left", count=1, modifiers=(), strategy="mouse")
    return f"open:{used}"


async def _press_keys(ctx: ServerContext, target: Target, combo: KeyCombo) -> None:
    """Keystrokes go to the frontmost app: only when it is the control's (and the policy allows)."""
    await ensure_frontmost(ctx, target.app)
    window = target.window
    ctx.policy.check_app(target.app, kind="input", window_title=window.title if window is not None else None)
    await require_keyboard(ctx)
    await ctx.run_engine(ctx.engine.press_key, combo)


def _visible_center(node: RawNode, found: dd.OptionList, target: Target) -> Rect | None:
    frame = node.frame
    if frame is None or frame.w <= 0 or frame.h <= 0:
        return None
    bounds = found.container.frame
    if bounds is not None and bounds.w > 0 and bounds.h > 0:
        frame = frame.intersection(bounds) or None
    if frame is not None and found.where != "window" and target.window is not None:
        frame = frame.intersection(target.window.frame) or None
    return frame


async def _press_option(ctx: ServerContext, target: Target, found: dd.OptionList, option: dd.Option) -> str:
    """Press the option: AX for menu items and native lists, a real click in web content."""
    node = option.node
    by_ax = node.role == "AXMenuItem" or not found.in_web
    if by_ax and "AXPress" in node.actions and node.handle is not None:
        await ctx.run_engine(ctx.engine.perform_action, node.handle, "AXPress")
        return "pick:ax:AXPress"
    visible = _visible_center(node, found, target)
    if (
        visible is None
        and node.handle is not None
        and await ctx.run_engine(ctx.engine.scroll_to_visible, node.handle)
    ):
        refreshed = await ctx.run_engine(ctx.engine.refresh_element, node.handle)
        visible = _visible_center(refreshed, found, target)
    if visible is None:
        if "AXPress" in node.actions and node.handle is not None:
            await ctx.run_engine(ctx.engine.perform_action, node.handle, "AXPress")
            return "pick:ax:AXPress"
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"The option {quote(option.title, 60)} of {target.describe()} is not on screen.",
            hint="Scroll the list (scroll tool), take a snapshot and click the option.",
        )
    point_target = await resolve_point(ctx, visible.center.x, visible.center.y)
    if target.app is not None and (point_target.app is None or point_target.app.pid != target.app.pid):
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"The option {quote(option.title, 60)} of {target.describe()} is covered by another window.",
        )
    used, _ = await click_target(ctx, point_target, button="left", count=1, modifiers=(), strategy="mouse")
    return f"pick:{used}"


async def _still_open(ctx: ServerContext, found: dd.OptionList, *, wait_s: float = 0.0) -> bool:
    """Whether the options are still shown, giving them up to ``wait_s`` to close by themselves:
    right after a pick a menu is still fading out (macOS 26), and AXCancel or Escape then would
    only reach the app."""
    handle = found.container.handle
    for _ in range(max(1, math.ceil(wait_s / POLL_S) + 1)):
        if handle is None or not await _shown(ctx, handle) or not await _any_option_shown(ctx, found):
            return False
        if wait_s <= 0:
            return True
        await ctx.sleep(POLL_S)
    return True


async def _shown(ctx: ServerContext, handle: Any, min_height: float = 0.0) -> bool:
    if not await ctx.run_engine(ctx.engine.element_valid, handle):
        return False
    node = await ctx.run_engine(ctx.engine.refresh_element, handle)
    frame = node.frame
    return node.visible and (frame is None or (frame.w > 0 and frame.h > max(0.0, min_height)))


async def _any_option_shown(ctx: ServerContext, found: dd.OptionList) -> bool:
    """A list whose options are gone is closed, even if its container stays. Safari (macOS 26)
    keeps a hidden ARIA listbox and its options valid and visible, all collapsed into the list's
    10 pt (its padding): an option counts while it keeps at least half the height it had."""
    checked = [option.node for option in found.options[:OPTION_CHECKS] if option.node.handle is not None]
    if not checked:
        return True  # nothing to look at: trust the container
    for node in checked:
        before = node.frame.h if node.frame is not None else 0.0
        if await _shown(ctx, node.handle, min_height=before / 2):
            return True
    return False


async def _close(ctx: ServerContext, target: Target, found: dd.OptionList) -> str | None:
    """Close an open menu or list (``AXCancel``, else Escape); returns a warning if it stays open.
    Shielded from cancellation and bounded: it runs on every failure path."""
    with anyio.CancelScope(shield=True) as scope:
        scope.deadline = anyio.current_time() + CLEANUP_TIMEOUT_S
        try:
            if not await _still_open(ctx, found, wait_s=CLOSE_WAIT_S):
                return None
            handle = found.container.handle
            if "AXCancel" in found.container.actions and handle is not None:
                await ctx.run_engine(ctx.engine.perform_action, handle, "AXCancel")
                if not await _still_open(ctx, found):
                    return None
            front = await ctx.run_engine(ctx.engine.frontmost_app)
            if (
                not ctx.control.paused
                and target.app is not None
                and front is not None
                and front.pid == target.app.pid
                and not await ctx.run_engine(ctx.engine.secure_input_active)
            ):
                await ctx.run_engine(ctx.engine.press_key, ESCAPE)
                if not await _still_open(ctx, found):
                    return None
        except AXPilotError as err:
            _log.warning("Could not close the options of %s: %s", target.describe(), err.message)
        if scope.cancelled_caught:
            _log.warning("Closing the options of %s took too long", target.describe())
    return f"The options of {target.describe()} may still be open; press Escape to close them."


async def _clean_up(ctx: ServerContext, run: _Run) -> None:
    """After a failure (or a cancellation, such as the kill switch) once the control was opened:
    put back text typed to filter, and close what it opened. When the failure came before the
    options were located, one capture looks for them. Shielded and bounded."""
    target = run.target
    with anyio.CancelScope(shield=True) as scope:
        scope.deadline = anyio.current_time() + 2 * CLEANUP_TIMEOUT_S
        if run.typed_over is not None:
            await _restore_text(ctx, target, run.typed_over)
        found = run.found
        if found is None:
            try:
                tree = await _capture(ctx, target)
                found = _locate(tree, target, run.baseline, run.web) or await _in_new_window(
                    ctx, target, run.baseline
                )
            except AXPilotError as err:
                _log.debug("Could not look for the options of %s: %s", target.describe(), err.message)
        if found is not None:
            warning = await _close(ctx, target, found)
            if warning:
                _log.warning("%s", warning)


async def _restore_text(ctx: ServerContext, target: Target, text: str) -> None:
    """Put back the text a failed filter replaced (select all, delete, type it again). Shielded
    and bounded like :func:`_close`; skipped while paused or when the app is not in front."""
    with anyio.CancelScope(shield=True) as scope:
        scope.deadline = anyio.current_time() + CLEANUP_TIMEOUT_S
        try:
            front = await ctx.run_engine(ctx.engine.frontmost_app)
            if ctx.control.paused or target.app is None or front is None or front.pid != target.app.pid:
                _log.warning(
                    "Left the filter text in %s (AXPilot is paused or the app moved)", target.describe()
                )
                return
            await require_keyboard(ctx)
            await ctx.run_engine(ctx.engine.press_key, SELECT_ALL)
            await ctx.run_engine(ctx.engine.press_key, DELETE)
            if text:
                await ctx.run_engine(ctx.engine.type_text, text)
        except AXPilotError as err:
            _log.warning("Could not restore the text of %s: %s", target.describe(), err.message)


def _pick(found: dd.OptionList, option: str | None, index: int | None) -> dd.Option:
    if index is not None:
        if not 1 <= index <= len(found.options):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"index {index} is out of range: there are {len(found.options)} options.",
                details={"options": found.titles()[: dd.MAX_LISTED]},
            )
        return found.options[index - 1]
    assert option is not None
    return found.options[dd.match_option(found.titles(), option)]


def _no_match(found: dd.OptionList, option: str) -> bool:
    """True if nothing matches ``option`` (ambiguity is not "no match": typing would not help)."""
    try:
        dd.match_option(found.titles(), option)
    except AXPilotError as err:
        return err.message.startswith("No option")
    return False


async def _filter(ctx: ServerContext, run: _Run, found: dd.OptionList, option: str) -> dd.OptionList:
    """A web text combo box without a matching option: type the title to filter it, once."""
    target = run.target
    await ensure_frontmost(ctx, target.app)
    if target.handle is not None:
        try:
            await ctx.run_engine(ctx.engine.focus, target.handle)
        except AXPilotError as err:
            if err.code is not ErrorCode.ELEMENT_NOT_ACTIONABLE:
                raise
    ctx.policy.check_secure_target(bool(target.el is not None and target.el.secure))
    window = target.window
    ctx.policy.check_app(target.app, kind="input", window_title=window.title if window is not None else None)
    run.attempts.append("filter:keys")
    if target.handle is not None:
        before = await ctx.run_engine(ctx.engine.refresh_element, target.handle)
        run.typed_over = str(before.value or "")
    await type_keystrokes(ctx, option)
    updated = await _wait_for_options(
        ctx,
        target,
        run.baseline,
        True,
        timeout_s=FILTER_WAIT_S,
        accept=lambda listed: not _no_match(listed, option),
    )
    return updated or found


# --------------------------------------------------------------------------- the flow
async def choose_option(
    ctx: ServerContext, target: Target, *, option: str | None, index: int | None
) -> DropdownOutcome:
    """Open the dropdown at ``target``, choose ``option`` (a title) or ``index`` (1-based) and
    verify it (see the module docstring). Raises ``ELEMENT_NOT_ACTIONABLE`` (disabled control or
    option, nothing opened, the choice did not take), ``INVALID_ARGUMENT`` (no or several
    matching options; the message lists them), ``STALE_REF``, ``BLOCKED_BY_POLICY``,
    ``PAUSED_BY_USER`` and engine errors; the menu is closed again on every failure."""
    el = target.el
    if el is None or target.handle is None:
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE, "Choosing an option needs an accessibility element (an e-ref)."
        )
    if not el.enabled:
        raise AXPilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"{target.describe()} is disabled.")
    web = in_web(ctx, target)
    if option is not None and el.ax_role == "AXPopUpButton":
        current = await ctx.run_engine(ctx.engine.refresh_element, target.handle)
        if dd.fold(str(current.value or "")) == dd.fold(option):
            shown = quote(str(current.value), 60)
            return DropdownOutcome(None, str(current.value), f"{shown} was already chosen")
    baseline = await _baseline(ctx, target)
    run = _Run(target, baseline, web, [await _open(ctx, target, web)])
    try:
        chosen = await _choose(ctx, run, option, index)
    except BaseException:
        await _clean_up(ctx, run)
        raise
    assert run.found is not None
    return await _verify(ctx, run, run.found, chosen)


@dataclass
class _Run:
    """One choice in progress: what was opened, and the rungs tried."""

    target: Target
    baseline: _Baseline
    web: bool
    attempts: list[str]
    found: dd.OptionList | None = None
    typed_over: str | None = None
    """The combo box's text before AXPilot typed into it to filter (restored on failure)."""


async def _choose(ctx: ServerContext, run: _Run, option: str | None, index: int | None) -> dd.Option:
    """Wait for the options, pick one and press it (``run.found`` tracks what is open)."""
    target, el = run.target, run.target.el
    assert el is not None
    run.found = await _wait_for_options(ctx, target, run.baseline, run.web)
    if run.found is None and run.web and el.ax_role in TEXT_COMBO_ROLES:
        run.attempts.append("open:keys:down")
        await _press_keys(ctx, target, OPEN_KEY)
        run.found = await _wait_for_options(ctx, target, run.baseline, run.web)
    if run.found is None:
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"{target.describe()} showed no menu or list of options after {', '.join(run.attempts)}.",
            hint="Take a snapshot to see what it opened; click options by ref, or use select_menu for "
            "menu bar commands.",
            details={"attempts": list(run.attempts)},
        )
    if option is not None and run.web and el.ax_role in TEXT_COMBO_ROLES and _no_match(run.found, option):
        run.found = await _filter(ctx, run, run.found, option)
    chosen = _pick(run.found, option, index)
    if not chosen.enabled:
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"The option {quote(chosen.title, 60)} of {target.describe()} is disabled.",
        )
    ctx.control.ensure_running()
    run.attempts.append(await _press_option(ctx, target, run.found, chosen))
    return chosen


async def _verify(ctx: ServerContext, run: _Run, found: dd.OptionList, chosen: dd.Option) -> DropdownOutcome:
    target, attempts = run.target, run.attempts
    el = target.el
    assert el is not None
    assert target.handle is not None
    strategy = {"window": "menu_window", "page": "listbox"}.get(found.where, "menu")
    outcome = DropdownOutcome(strategy, chosen.title, f"chose {quote(chosen.title, 60)}", attempts)
    await ctx.sleep(ctx.config.perception.settle_ms / 1000)
    if await _still_open(ctx, found, wait_s=CLOSE_WAIT_S):
        warning = await _close(ctx, target, found)
        closed = f"The options of {target.describe()} stayed open after the choice; AXPilot closed them."
        outcome.warnings.append(warning or closed)
    if el.role == "menubutton":
        return outcome
    shown = await ctx.run_engine(ctx.engine.refresh_element, target.handle)
    if dd.shows_choice(shown, chosen.title):
        return outcome
    if el.ax_role == "AXPopUpButton" and not run.web:
        raise AXPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"Could not choose {quote(chosen.title, 60)} in {target.describe()}: it shows "
            f"{quote(str(shown.value or shown.title or ''), 60)}.",
            hint="Take a snapshot; the app may have refused the option.",
            details={"attempts": attempts},
        )
    tree = await _capture(ctx, target)
    if dd.text_near(tree.root, target.handle, chosen.title) or _selected_now(tree, chosen):
        return outcome
    unsure = f"Could not confirm that {target.describe()} took {quote(chosen.title, 60)}"
    outcome.warnings.append(f"{unsure}; take a snapshot to check.")
    return outcome


def _selected_now(tree: RawTree, chosen: dd.Option) -> bool:
    found = dd.find_node(tree.root, chosen.node.handle)
    return found is not None and found[0].visible and found[0].selected
