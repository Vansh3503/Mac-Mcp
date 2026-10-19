"""Controls with choices and dialogs: ``select_option`` and ``handle_dialog``.

``select_option`` picks one option of a control the way its role expects: pop-up buttons and combo
boxes take the option's title (AXPilot opens the menu and chooses the item); radio groups, tab
groups and segmented controls press the matching child; lists, tables and outlines select the
matching row. Options match by title (exact, then prefix, then substring; ambiguous matches are an
error that lists them) or by 1-based index.

``handle_dialog`` finds the sheet or dialog in front of an app and reports its text, fields and
buttons; it can fill its first text field and press a button: the default one (``accept``), the
cancel one (``cancel``, or Escape) or one by title. Pressing a button whose title destroys or
discards something (Delete, Replace, Don't Save…) asks the user first.
"""

# No ``from __future__ import annotations`` here: the MCP SDK evaluates tool annotations.

import re
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from mcp_types import CallToolResult
from pydantic import Field

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.input.keys import KeyCombo
from axpilot.perception.format import quote
from axpilot.perception.model import Element, Snapshot
from axpilot.perception.navigate import descendants, element_text
from axpilot.perception.roles import DIALOG_SUBROLES
from axpilot.server.context import ServerContext
from axpilot.server.registry import ToolFn, tool
from axpilot.server.results import ok_result
from axpilot.server.schemas import ActionResult, ElementBriefModel, SnapshotIdArg
from axpilot.server.tools.input import (
    ReportArg,
    Target,
    action_result,
    activate_app,
    click_target,
    element_brief,
    observe_after,
    require_keyboard,
    resolve_target,
    set_control,
    snapshot_before,
)

__all__ = ["DESTRUCTIVE_BUTTON_RE", "match_option"]

MAX_LISTED = 30
GROUP_ROLES = frozenset({"radiogroup", "tabs", "segmentedcontrol", "group", "toolbar"})
GROUP_CHOICES = frozenset({"radio", "tab", "button", "toggle", "checkbox"})
COLLECTION_ROLES = frozenset({"list", "table", "outline"})
SELF_SELECTABLE = frozenset({"radio", "tab", "row"})
"""Option elements that may be passed themselves (they have a selected state)."""
VALUE_ROLES = frozenset({"popup", "combobox"})
DIALOG_ROLES = frozenset({"sheet", "dialog"})
FIELD_ROLES = frozenset({"field", "search", "textarea", "combobox", "secure"})

DESTRUCTIVE_BUTTON_RE = re.compile(
    r"\b(delete|erase|remove|replace|discard|don.t save|trash|overwrite|reset|revert|format|uninstall|"
    r"empty|destroy|clear all|quit anyway|force quit|deauthori[sz]e|sign out|log out)\b",
    re.IGNORECASE,
)
"""Button titles that destroy or discard something: pressing one asks the user first."""
_CANCEL_TITLES = (
    "cancel",
    "not now",
    "don't allow",
    "don’t allow",
    "no",
    "close",
    "later",
    "skip",
    "dismiss",
)
_ACCEPT_ORDER = (
    "ok",
    "allow",
    "continue",
    "done",
    "open",
    "save",
    "yes",
    "sign in",
    "log in",
    "next",
    "confirm",
)


def _fold(text: str | None) -> str:
    return (text or "").strip().rstrip("…").rstrip(".").casefold()


def match_option(names: Sequence[str], option: str) -> int:
    """Index of the name matching ``option``: exact (case-insensitive), then unique prefix, then
    unique substring. Raises ``INVALID_ARGUMENT`` (listing the choices) otherwise."""
    wanted = _fold(option)
    folded = [_fold(name) for name in names]
    for matches in (
        [i for i, name in enumerate(folded) if name == wanted],
        [i for i, name in enumerate(folded) if name.startswith(wanted)],
        [i for i, name in enumerate(folded) if wanted in name],
    ):
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            shown = ", ".join(quote(names[i], 40) for i in matches[:MAX_LISTED])
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, f"{option!r} matches several options: {shown}.")
    shown = ", ".join(quote(name, 40) for name in names[:MAX_LISTED]) or "none"
    raise AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"No option matches {option!r}. Options: {shown}.",
        hint="Pass the exact title (or index, 1-based).",
    )


def _label(el: Element) -> str:
    return element_text(el) or el.name or el.value or ""


# =========================================================================== select_option
SELECT_DESCRIPTION = """\
Choose an option of a control by its title (or 1-based index): a pop-up button or combo box
(AXPilot opens it and picks the item), a radio group, tab group or segmented control (presses the
matching button), or a list, table or outline (selects the matching row).
Params: ref (the control, from a snapshot), option (title: exact, then prefix, then substring; pop-ups
and combo boxes need the exact title, since their items are listed only while open) or index (not
for pop-ups); snapshot_id; report. Choosing the option that is already selected changes nothing.
Errors: INVALID_ARGUMENT (no or several matching options: the message lists them), STALE_REF,
ELEMENT_NOT_ACTIONABLE (disabled, or not a control with options; menu buttons: use
perform_action AXShowMenu), BLOCKED_BY_POLICY, USER_ACTIVE, PAUSED_BY_USER."""


def _choices(snap: Snapshot, el: Element) -> list[Element]:
    inside = descendants(snap, el)
    if el.role in COLLECTION_ROLES:
        return [child for child in inside if child.role == "row"]
    return [child for child in inside if child.role in GROUP_CHOICES]


def _pick(choices: list[Element], option: str | None, index: int | None) -> Element:
    if index is not None:
        if not 1 <= index <= len(choices):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"index {index} is out of range: there are {len(choices)} options.",
            )
        return choices[index - 1]
    assert option is not None
    return choices[match_option([_label(choice) for choice in choices], option)]


def _is_chosen(el: Element) -> bool:
    return el.selected or str(el.value) in ("1", "true", "True")


@tool(
    "select_option",
    title="Choose an option",
    description=SELECT_DESCRIPTION,
    input_action=True,
    idempotent=True,
    windows_mcp_name="SelectOption",
)
def select_option_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``select_option`` tool."""

    async def select_option(
        ref: Annotated[
            str,
            Field(
                description='The control: a pop-up, combo box, radio/tab group or list ("e7").',
                min_length=2,
                max_length=48,
            ),
        ],
        option: Annotated[str | None, Field(description="The option's title.", max_length=500)] = None,
        index: Annotated[
            int | None, Field(description="The option's position, from 1.", ge=1, le=10_000)
        ] = None,
        snapshot_id: SnapshotIdArg = None,
        report: ReportArg = "diff",
    ) -> Annotated[CallToolResult, ActionResult]:
        if (option is None) == (index is None):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, "Pass option (a title) or index, not both or neither."
            )
        target = await resolve_target(ctx, ref=ref, snapshot_id=snapshot_id, x=None, y=None)
        el, snap = target.el, target.snap
        assert el is not None
        assert snap is not None
        if not el.enabled:
            raise AXPilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"{target.describe()} is disabled.")
        before = await snapshot_before(ctx, target, report)
        if el.role in VALUE_ROLES:
            if option is None:
                raise AXPilotError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"{target.describe()} lists its options only while open: pass option (the title).",
                )
            outcome = await set_control(ctx, target, option)
            observation = await observe_after(ctx, app=target.app, before=before, report=report)
            return action_result(
                "select_option",
                f"{target.describe()}: {outcome.message}",
                observation,
                strategy=outcome.strategy,
                attempts=[outcome.strategy],
                target=target,
            )
        if el.role in GROUP_ROLES or el.role in COLLECTION_ROLES:
            choices = _choices(snap, el)
            if not choices:
                raise AXPilotError(
                    ErrorCode.ELEMENT_NOT_ACTIONABLE,
                    f"{target.describe()} shows no options.",
                    hint='Take a snapshot with mode="full", or pass the option element\'s own ref.',
                )
            chosen = _pick(choices, option, index)
        elif el.role in SELF_SELECTABLE:
            chosen = el
        elif el.role == "menubutton":
            raise AXPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"{target.describe()} opens a menu whose items are not listed until it is open.",
                hint="perform_action AXShowMenu (or AXPress) on it, take a snapshot, and click the item.",
            )
        else:
            raise AXPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"{target.describe()} is not a control with options (it is a {el.role}).",
                hint="Use set_value for fields, checkboxes and sliders, or click.",
            )
        name = quote(_label(chosen), 60)
        if _is_chosen(chosen):
            observation = await observe_after(ctx, app=target.app, before=before, report=report)
            return action_result("select_option", f"{name} was already selected", observation, target=target)
        option_target = await resolve_target(
            ctx, ref=chosen.ref, snapshot_id=snap.snapshot_id, x=None, y=None
        )
        used, attempts = await click_target(
            ctx, option_target, button="left", count=1, modifiers=(), strategy="auto"
        )
        observation = await observe_after(ctx, app=target.app, before=before, report=report)
        return action_result(
            "select_option",
            f"selected {name} in {target.describe()} via {used}",
            observation,
            strategy=used,
            attempts=attempts,
            target=option_target,
        )

    return select_option


# =========================================================================== handle_dialog
DIALOG_DESCRIPTION = """\
Deal with the sheet, alert or dialog in front of an app: read it, or answer it.
action: inspect (default: its title, text, fields and buttons), accept (press the default button:
OK, Allow, Save…, else the rightmost one), cancel (press Cancel/Not Now/No, else press Escape), or
press (the button titled button). text fills the dialog's first text field before pressing.
Pressing a button that deletes, replaces or discards something asks the user first. Params: app
(default: the frontmost app), action, button, text, report.
Errors: ELEMENT_NOT_ACTIONABLE (no dialog is open), INVALID_ARGUMENT (no such button: the message
lists them), USER_DENIED, BLOCKED_BY_POLICY (secure fields: the user types passwords), NEEDS_USER
(system authentication dialogs), USER_ACTIVE, PAUSED_BY_USER."""

DialogAction = Literal["inspect", "accept", "cancel", "press"]


class DialogResult(ActionResult):
    """Output of ``handle_dialog``."""

    found: bool = Field(description="True if a sheet or dialog is open.")
    kind: Literal["sheet", "dialog", "none"] = "none"
    title: str | None = None
    texts: list[str] = Field(default_factory=list, description="The dialog's text, in order.")
    buttons: list[ElementBriefModel] = Field(default_factory=list)
    fields: list[ElementBriefModel] = Field(default_factory=list)
    pressed: str | None = Field(default=None, description="Title of the button pressed, if any.")


def _dialog_of(snap: Snapshot, window_is_dialog: bool) -> tuple[Element | None, str]:
    for el in snap.all_elements:
        if el.role in DIALOG_ROLES:
            return el, "sheet" if el.role == "sheet" else "dialog"
    return None, "dialog" if window_is_dialog else "none"


def _parts(snap: Snapshot, dialog: Element | None) -> tuple[list[str], list[Element], list[Element]]:
    inside = descendants(snap, dialog) if dialog is not None else list(snap.all_elements)
    texts = [text for el in inside if el.role == "text" and (text := element_text(el) or el.name)]
    buttons = [el for el in inside if el.role == "button" and el.subrole not in _WINDOW_BUTTONS]
    fields = [el for el in inside if el.role in FIELD_ROLES or el.role in ("checkbox", "popup")]
    return texts, buttons, fields


_WINDOW_BUTTONS = frozenset({"AXCloseButton", "AXMinimizeButton", "AXFullScreenButton", "AXZoomButton"})


def _right_to_left(buttons: list[Element]) -> list[Element]:
    return sorted(
        buttons, key=lambda el: (el.frame.y + el.frame.h, el.frame.x) if el.frame else (0, 0), reverse=True
    )


def _default_button(buttons: list[Element]) -> Element | None:
    enabled = [el for el in buttons if el.enabled and _fold(el.name) not in _CANCEL_TITLES]
    for title in _ACCEPT_ORDER:
        for el in enabled:
            if _fold(el.name) == title:
                return el
    ordered = _right_to_left(enabled)
    return ordered[0] if ordered else None


def _cancel_button(buttons: list[Element]) -> Element | None:
    for title in _CANCEL_TITLES:
        for el in buttons:
            if el.enabled and _fold(el.name) == title:
                return el
    return None


def _brief(el: Element) -> ElementBriefModel:
    brief = element_brief(el)
    assert brief is not None
    return brief


@tool(
    "handle_dialog",
    title="Answer a dialog",
    description=DIALOG_DESCRIPTION,
    input_action=True,
    destructive=True,
    windows_mcp_name="HandleDialog",
    confirm_when=lambda args: False,  # asks itself, only for buttons that destroy or discard
)
def handle_dialog_tool(ctx: ServerContext) -> ToolFn:
    """Factory of the ``handle_dialog`` tool."""

    async def handle_dialog(
        *,
        app: Annotated[
            str | None, Field(description="App name or bundle id (default: frontmost).", max_length=256)
        ] = None,
        action: Annotated[DialogAction, Field(description="inspect | accept | cancel | press.")] = "inspect",
        button: Annotated[str | None, Field(description="press: the button's title.", max_length=200)] = None,
        text: Annotated[
            str | None, Field(description="Text for the dialog's first text field.", max_length=20_000)
        ] = None,
        report: ReportArg = "diff",
    ) -> Annotated[CallToolResult, DialogResult]:
        if (button is not None) != (action == "press"):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT, "button goes with action=press (and press needs it)."
            )
        if text is not None and action == "inspect":
            raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "text goes with accept, cancel or press.")
        tree = await ctx.capture_tree(app=app)
        snap = ctx.build_snapshot(tree, mode="full")
        dialog, kind = _dialog_of(snap, tree.root.subrole in DIALOG_SUBROLES)
        texts, buttons, fields = _parts(snap, dialog)
        found = kind != "none"
        info: dict[str, Any] = {
            "found": found,
            "kind": kind,
            "title": (dialog.name if dialog is not None else None)
            or (tree.window.title if tree.window else None),
            "texts": texts,
            "buttons": [_brief(el) for el in buttons],
            "fields": [_brief(el) for el in fields],
        }
        if action == "inspect":
            summary = _summary(info, buttons, fields, tree.app.name)
            model = DialogResult(action="handle_dialog", snapshot_id=snap.snapshot_id, **info)
            return ok_result(summary, model)
        if not found:
            raise AXPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"No sheet or dialog is open in {tree.app.name}.",
                hint="Take a snapshot to see what is on screen; the dialog may belong to another app.",
            )
        if text is not None:
            editable = [el for el in fields if el.role in FIELD_ROLES]
            if not editable:
                raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "The dialog has no text field for text.")
            field_target = await resolve_target(
                ctx, ref=editable[0].ref, snapshot_id=snap.snapshot_id, x=None, y=None
            )
            await set_control(ctx, field_target, text)
        if action == "press":
            assert button is not None
            chosen: Element | None = buttons[match_option([el.name or "" for el in buttons], button)]
        elif action == "accept":
            chosen = _default_button(buttons)
            if chosen is None:
                raise AXPilotError(
                    ErrorCode.ELEMENT_NOT_ACTIONABLE, "The dialog has no button to accept it with."
                )
        else:
            chosen = _cancel_button(buttons)
        if chosen is not None and DESTRUCTIVE_BUTTON_RE.search(chosen.name or ""):
            detail = "\n".join(texts[:6]) or None
            await ctx.confirm_action(
                f"Press {quote(chosen.name or '', 60)} in {tree.app.name}'s dialog?", detail, destructive=True
            )
        if chosen is None:  # cancel without a cancel button: Escape dismisses macOS dialogs
            await activate_app(ctx, tree.app)
            await require_keyboard(ctx)
            await ctx.run_engine(ctx.engine.press_key, KeyCombo("escape"))
            observation = await observe_after(ctx, app=tree.app, before=snap, report=report)
            return _answered(info, observation, "pressed Escape to cancel the dialog", "keys", None)
        target: Target = await resolve_target(
            ctx, ref=chosen.ref, snapshot_id=snap.snapshot_id, x=None, y=None
        )
        if not chosen.enabled:
            raise AXPilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"{target.describe()} is disabled.")
        used, _ = await click_target(ctx, target, button="left", count=1, modifiers=(), strategy="auto")
        observation = await observe_after(ctx, app=tree.app, before=snap, report=report)
        return _answered(
            info, observation, f"pressed {quote(chosen.name or '', 60)} via {used}", used, chosen.name
        )

    return handle_dialog


def _summary(info: dict[str, Any], buttons: list[Element], fields: list[Element], app: str) -> str:
    if not info["found"]:
        return f"No sheet or dialog is open in {app}."
    title = info["title"]
    lines = [f"{info['kind']} in {app}" + (f": {quote(title, 80)}" if title else "")]
    lines += [f"  {quote(text, 200)}" for text in info["texts"][:12]]
    if fields:
        lines.append(
            "fields: " + ", ".join(f"{el.ref} {el.role} {quote(el.name or '', 40)}" for el in fields)
        )
    lines.append(
        "buttons: " + (", ".join(f"{el.ref} {quote(el.name or '', 40)}" for el in buttons) or "none")
    )
    return "\n".join(lines)


def _answered(
    info: dict[str, Any], observation: Any, summary: str, strategy: str, pressed: str | None
) -> CallToolResult:
    result = action_result("handle_dialog", summary, observation, strategy=strategy, attempts=[strategy])
    base = dict(result.structured_content or {})
    model = DialogResult(**{**base, **info, "pressed": pressed})
    result.structured_content = model.model_dump(mode="json", exclude_none=False)
    return result
