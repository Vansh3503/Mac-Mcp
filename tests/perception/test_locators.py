"""Portable locators: parsing, exact matching, candidate generation and grading, windows,
warnings and exports (docs/dev/locators-plan.md)."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, OcrLine, RawNode, RawTree, Rect, WindowInfo
from axpilot.perception import Snapshot, build_snapshot
from axpilot.perception.locators import (
    EXPORT_FORMATS,
    LocatorSpec,
    LocatorStep,
    WindowSelector,
    candidates_for,
    default_window,
    export_locator,
    export_locators,
    format_path,
    is_unique,
    locator_warnings,
    looks_generated,
    match_elements,
    normalize_name,
    parse_locator,
    parse_path,
    pick_window,
    window_selector_for,
)
from axpilot.perception.serialize import assign_handles
from axpilot.perception.vision import merge_vision, ocr_elements

GOLDEN = Path(__file__).parent / "golden"
UPDATE = os.environ.get("AXPILOT_UPDATE_GOLDEN") == "1"
APP = "com.example.demo"


# --------------------------------------------------------------------------- helpers
def node(role: str, *children: RawNode, y: float = 10, **kw: Any) -> RawNode:
    return RawNode(role=role, frame=Rect(10, y, 60, 20), children=list(children), **kw)


def snap_of(*children: RawNode, truncated: bool = False) -> Snapshot:
    root = RawNode(role="AXWindow", title="Doc", frame=Rect(0, 0, 800, 600), children=list(children))
    assign_handles(root, None)
    app = AppInfo(name="Demo", pid=100, bundle_id=APP)
    window = WindowInfo(window_id=7, pid=100, app="Demo", title="Doc", frame=Rect(0, 0, 800, 600))
    tree = RawTree(app=app, window=window, root=root, truncated=truncated)
    return build_snapshot(tree, snapshot_id="s_test", mode="full", max_elements=10_000)


def named(snap: Snapshot, name: str, nth: int = 0):  # type: ignore[no-untyped-def]
    return [el for el in snap.all_elements if el.name == name][nth]


def spec(**fields: Any) -> LocatorSpec:
    return parse_locator({"app": APP, **fields})


def refs(snap: Snapshot, **fields: Any) -> list[str]:
    return [el.ref for el in match_elements(snap, spec(**fields))]


def code_of(exc: pytest.ExceptionInfo[AXPilotError]) -> ErrorCode:
    return exc.value.code


def window(window_id: int, title: str, **flags: Any) -> WindowInfo:
    return WindowInfo(window_id=window_id, pid=100, app="Demo", title=title, frame=Rect(0, 0, 9, 9), **flags)


# =========================================================================== parsing
def test_a_minimal_locator_parses_and_serializes_canonically() -> None:
    parsed = parse_locator({"role": "AXButton", "app": " com.apple.finder ", "name": "  Back  "})
    assert parsed == LocatorSpec(app="com.apple.finder", role="AXButton", name="Back")
    assert parsed.to_dict() == {"v": 1, "app": "com.apple.finder", "role": "AXButton", "name": "Back"}
    assert parsed.to_json() == '{"v": 1, "app": "com.apple.finder", "role": "AXButton", "name": "Back"}'


def test_every_field_round_trips_through_to_dict() -> None:
    data = {
        "v": 1,
        "app": APP,
        "window": {"title": "Library", "match": "prefix"},
        "within": [{"role": "AXSheet"}, {"role": "AXGroup", "identifier": "pane", "name": "Pane"}],
        "role": "AXButton",
        "subrole": "AXToggle",
        "identifier": "save",
        "name": "Save",
        "name_match": "contains",
        "index": 2,
        "path": "AXSheet[0]/AXButton[1]",
    }
    parsed = parse_locator(data)
    assert parsed.to_dict() == data
    assert parse_locator(parsed.to_dict()) == parsed


def test_defaults_are_left_out_of_the_canonical_form() -> None:
    parsed = parse_locator({"app": APP, "name": "Save", "name_match": "exact", "window": {"index": 1}})
    assert parsed.to_dict() == {"v": 1, "app": APP, "window": {"index": 1}, "name": "Save"}
    assert parse_locator({"app": APP, "name": "Save", "window": {"title": "A", "match": "exact"}}).to_dict()[
        "window"
    ] == {"title": "A"}


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ("not an object", "must be an object"),
        ({"role": "AXButton"}, "locator.app is required"),
        ({"app": "", "role": "AXButton"}, "locator.app must not be empty"),
        ({"app": 3, "role": "AXButton"}, "locator.app must be a string"),
        ({"app": APP}, "at least one of role, identifier, name or path"),
        ({"app": APP, "index": 0}, "at least one of role, identifier, name or path"),
        ({"app": APP, "role": "AXButton", "colour": "red"}, "unknown field(s): colour"),
        ({"app": APP, "role": "AXButton", "v": 2}, "Unsupported locator version 2"),
        ({"app": APP, "role": "AXButton", "v": True}, "Unsupported locator version True"),
        ({"app": APP, "role": "AXButton", "index": -1}, "between 0 and 9999"),
        ({"app": APP, "role": "AXButton", "index": 10_000}, "between 0 and 9999"),
        ({"app": APP, "role": "AXButton", "index": True}, "must be an integer"),
        ({"app": APP, "role": "AXButton", "index": "1"}, "must be an integer"),
        ({"app": APP, "role": "AXButton", "name_match": "prefix"}, "name_match needs a name"),
        ({"app": APP, "name": "Save", "name_match": "regex"}, "name_match must be one of"),
        ({"app": APP, "name": "   "}, "locator.name must not be empty"),
        ({"app": APP, "name": "x" * 501}, "longer than 500"),
        ({"app": APP, "identifier": ""}, "locator.identifier must not be empty"),
        ({"app": APP, "role": "AXButton", "within": {"role": "AXSheet"}}, "within must be a list"),
        ({"app": APP, "role": "AXButton", "within": ["AXSheet"]}, "within[0] must be an object"),
        ({"app": APP, "role": "AXButton", "within": [{}]}, "within[0] needs a role, identifier or name"),
        (
            {"app": APP, "role": "AXButton", "within": [{"title": "x"}]},
            "within[0] has unknown field(s): title",
        ),
        ({"app": APP, "role": "AXButton", "within": [{"role": "AXGroup"}] * 6}, "more than 5 steps"),
        ({"app": APP, "role": "AXButton", "window": "Doc"}, "locator.window must be an object"),
        ({"app": APP, "role": "AXButton", "window": {}}, "exactly one of title or index"),
        (
            {"app": APP, "role": "AXButton", "window": {"title": "A", "index": 0}},
            "exactly one of title or index",
        ),
        (
            {"app": APP, "role": "AXButton", "window": {"index": 0, "match": "prefix"}},
            "only applies to a title",
        ),
        ({"app": APP, "role": "AXButton", "window": {"title": "A", "match": "glob"}}, "match must be one of"),
        ({"app": APP, "role": "AXButton", "window": {"index": 100}}, "between 0 and 99"),
        ({"app": APP, "path": ""}, "at least one step"),
        ({"app": APP, "path": "AXButton"}, 'of the form "AXRole[index]"'),
        ({"app": APP, "path": "AXGroup[0]//AXButton[1]"}, 'of the form "AXRole[index]"'),
        ({"app": APP, "path": [1, 2]}, "steps must be strings"),
        ({"app": APP, "path": 5}, "locator.path must be a string"),
        ({"app": APP, "path": "/".join(["AXGroup[0]"] * 65)}, "more than 64 steps"),
    ],
)
def test_invalid_locators_are_refused_with_the_field_named(data: Any, fragment: str) -> None:
    with pytest.raises(AXPilotError) as exc:
        parse_locator(data)
    assert code_of(exc) is ErrorCode.INVALID_ARGUMENT
    assert fragment in exc.value.message
    assert exc.value.hint


def test_paths_parse_from_strings_and_from_inspect_lists() -> None:
    expected = (("AXToolbar", 0), ("AXButton", 2))
    assert parse_path("AXToolbar[0]/AXButton[2]") == expected
    assert parse_path("/AXToolbar[0]/AXButton[2]") == expected
    assert parse_path(["AXToolbar[0]", " AXButton[2] "]) == expected
    assert format_path(expected) == "AXToolbar[0]/AXButton[2]"


def test_names_compare_with_whitespace_collapsed_and_ellipses_unified() -> None:
    assert normalize_name("  Export...\n as  PDF ") == "Export… as PDF"
    assert parse_locator({"app": APP, "name": "Export..."}).name == "Export…"


def test_describe_names_the_element_for_messages() -> None:
    assert spec(role="AXButton", name="Back").describe() == f'AXButton "Back" in {APP}'
    assert spec(identifier="dl", index=1).describe() == f"element #dl [1] in {APP}"
    assert spec(path="AXButton[0]").describe() == f"element at AXButton[0] in {APP}"


# =========================================================================== matching
def test_roles_match_as_ax_roles_or_short_roles_in_any_case() -> None:
    snap = snap_of(node("AXButton", title="OK"), node("AXTextField", subrole="AXSearchField", title="Find"))
    ok = named(snap, "OK").ref
    assert refs(snap, role="AXButton") == [ok]
    assert refs(snap, role="axbutton") == [ok]
    assert refs(snap, role="button") == [ok]
    assert refs(snap, role="Button") == [ok]
    find = named(snap, "Find").ref
    assert refs(snap, role="AXTextField") == [find]
    assert refs(snap, role="search") == [find]
    assert refs(snap, role="field") == []  # the short role of a search field is "search"
    assert refs(snap, role="AXTextField", subrole="axsearchfield") == [find]
    assert refs(snap, role="AXTextField", subrole="AXSecureTextField") == []


def test_identifiers_match_exactly_and_case_sensitively() -> None:
    snap = snap_of(
        node("AXButton", identifier="Download", title="A"), node("AXButton", identifier="download")
    )
    assert refs(snap, identifier="Download") == [named(snap, "A").ref]
    assert refs(snap, identifier="DOWNLOAD") == []
    assert refs(snap, identifier="Down") == []


def test_names_match_exactly_by_prefix_or_by_substring_with_case() -> None:
    snap = snap_of(
        node("AXButton", title="Download (3)"),
        node("AXButton", title="Downloads"),
        node("AXButton", title="Load"),
    )
    three, downloads, load = (named(snap, n).ref for n in ("Download (3)", "Downloads", "Load"))
    assert refs(snap, name="Download (3)") == [three]
    assert refs(snap, name="download (3)") == []
    assert refs(snap, name="Download", name_match="prefix") == [three, downloads]
    assert refs(snap, name="ownload", name_match="contains") == [three, downloads]
    assert refs(snap, name="Load", name_match="contains") == [load]


def test_within_matches_ancestors_as_a_subsequence_outermost_first() -> None:
    snap = snap_of(
        node("AXToolbar", node("AXGroup", node("AXButton", title="Share"))),
        node("AXSheet", node("AXButton", title="Share")),
    )
    in_toolbar, in_sheet = named(snap, "Share").ref, named(snap, "Share", 1).ref
    assert refs(snap, name="Share", within=[{"role": "AXToolbar"}]) == [in_toolbar]  # not a direct parent
    assert refs(snap, name="Share", within=[{"role": "sheet"}]) == [in_sheet]
    assert refs(snap, name="Share", within=[{"role": "AXToolbar"}, {"role": "AXGroup"}]) == [in_toolbar]
    assert refs(snap, name="Share", within=[{"role": "AXGroup"}, {"role": "AXToolbar"}]) == []  # wrong order
    assert refs(snap, name="Share", within=[{"role": "AXTable"}]) == []


def test_within_steps_can_name_an_ancestor_by_identifier_or_name() -> None:
    snap = snap_of(
        node("AXGroup", node("AXButton", title="Go"), identifier="left", title="Left"),
        node("AXGroup", node("AXButton", title="Go"), identifier="right", title="Right"),
    )
    right = named(snap, "Go", 1).ref
    assert refs(snap, name="Go", within=[{"identifier": "right"}]) == [right]
    assert refs(snap, name="Go", within=[{"name": "Right"}]) == [right]
    assert refs(snap, name="Go", within=[{"role": "AXGroup", "name": "Right", "identifier": "left"}]) == []


def test_index_picks_one_match_in_document_order() -> None:
    snap = snap_of(*(node("AXButton", title="Row", y=10 + 30 * i) for i in range(3)))
    all_rows = refs(snap, name="Row")
    assert len(all_rows) == 3
    assert refs(snap, name="Row", index=1) == [all_rows[1]]
    assert refs(snap, name="Row", index=3) == []


def test_paths_match_the_position_in_the_window_tree() -> None:
    snap = snap_of(node("AXToolbar", node("AXButton", title="A"), node("AXButton", title="B")))
    assert refs(snap, path="AXToolbar[0]/AXButton[1]") == [named(snap, "B").ref]
    assert refs(snap, path="AXToolbar[0]/AXButton[1]", role="AXButton", name="A") == []  # fields AND
    assert refs(snap, path="AXToolbar[0]/AXButton[5]") == []


def test_every_field_must_hold() -> None:
    snap = snap_of(
        node("AXButton", title="Save", identifier="a"), node("AXButton", title="Save", identifier="b")
    )
    assert refs(snap, name="Save", identifier="b") == [named(snap, "Save", 1).ref]
    assert refs(snap, name="Save", identifier="b", role="AXCheckBox") == []


def test_ocr_text_never_matches() -> None:
    snap = snap_of(node("AXButton", title="OK"))
    lines = [OcrLine(text="OK", confidence=0.9, frame=Rect(100, 100, 30, 10))]
    merged = merge_vision(snap, ocr_elements(lines))
    assert any(el.ref.startswith("o") for el in merged.all_elements)
    assert refs(merged, name="OK") == [named(snap, "OK").ref]


def test_uniqueness_is_unknown_in_a_capture_cut_short() -> None:
    complete, partial = (
        snap_of(node("AXButton", title="OK")),
        snap_of(node("AXButton", title="OK"), truncated=True),
    )
    assert is_unique(complete, 1) is True
    assert is_unique(partial, 1) is None
    assert is_unique(complete, 0) is False
    assert is_unique(partial, 2) is False


# =========================================================================== candidates
def first_candidate(snap: Snapshot, name: str, **kw: Any):  # type: ignore[no-untyped-def]
    return candidates_for(snap, named(snap, name), app=APP, **kw)[0]


def test_an_identifier_is_the_best_locator() -> None:
    snap = snap_of(node("AXToolbar", node("AXButton", identifier="downloadButton", description="Download")))
    found = candidates_for(snap, named(snap, "Download"), app=APP)
    best = found[0]
    assert best.strategy == "identifier"
    assert best.grade == "high"
    assert best.unique is True
    assert best.spec.to_dict() == {"v": 1, "app": APP, "role": "AXButton", "identifier": "downloadButton"}
    assert [c.strategy for c in found] == ["identifier", "name", "path"]
    assert found[1].grade == "medium"
    assert found[2].grade == "low"
    assert "survives language" in best.reasons[0]


@pytest.mark.parametrize(
    "identifier", ["_NS:123", "42", "6F9619FF-8B86-D011-B42D-00C04FC964FF", "a1b2c3d4e5f60718"]
)
def test_generated_identifiers_are_graded_low_and_lose_to_the_name(identifier: str) -> None:
    assert looks_generated(identifier)
    snap = snap_of(node("AXButton", identifier=identifier, title="Save"))
    found = candidates_for(snap, named(snap, "Save"), app=APP)
    assert found[0].strategy == "name"
    by_id = next(c for c in found if c.strategy == "identifier")
    assert by_id.grade == "low"
    assert "generated" in by_id.reasons[0]


@pytest.mark.parametrize("identifier", ["downloadButton", "login.submit", "First Text View", "row_12_title"])
def test_chosen_identifiers_do_not_look_generated(identifier: str) -> None:
    assert not looks_generated(identifier)


def test_a_shared_identifier_is_scoped_by_an_identified_ancestor() -> None:
    snap = snap_of(
        node("AXGroup", node("AXButton", identifier="ok", title="OK"), identifier="left"),
        node("AXGroup", node("AXButton", identifier="ok", title="OK"), identifier="right"),
    )
    best = candidates_for(snap, named(snap, "OK", 1), app=APP)[0]
    assert best.strategy == "identifier"
    assert best.grade == "high"
    assert best.spec.within == (LocatorStep(role="AXGroup", identifier="right"),)


def test_a_shared_identifier_without_identified_ancestors_adds_the_name() -> None:
    snap = snap_of(
        node("AXButton", identifier="btn", title="Yes"), node("AXButton", identifier="btn", title="No")
    )
    by_id = next(c for c in candidates_for(snap, named(snap, "No"), app=APP) if c.strategy == "identifier")
    assert by_id.spec.identifier == "btn"
    assert by_id.spec.name == "No"
    assert by_id.grade == "medium"


def test_a_name_is_medium_and_warns_that_text_changes() -> None:
    snap = snap_of(node("AXButton", description="Back"))
    best = first_candidate(snap, "Back")
    assert (best.strategy, best.grade) == ("name", "medium")
    assert best.spec.to_dict() == {"v": 1, "app": APP, "role": "AXButton", "name": "Back"}
    assert "language" in best.reasons[0]


def test_a_repeated_name_is_scoped_by_its_toolbar_or_sheet() -> None:
    snap = snap_of(
        node("AXToolbar", node("AXButton", title="Share")),
        node("AXSheet", node("AXButton", title="Share"), title="Export"),
    )
    in_toolbar = first_candidate(snap, "Share")
    assert in_toolbar.strategy == "name_within"
    assert in_toolbar.spec.within == (LocatorStep(role="AXToolbar"),)
    assert "scoped in toolbar" in in_toolbar.reasons[-1]
    in_sheet = candidates_for(snap, named(snap, "Share", 1), app=APP)[0]
    assert in_sheet.spec.within == (LocatorStep(role="AXSheet", name="Export"),)


def test_scoping_adds_outer_containers_until_unique() -> None:
    def pane(title: str) -> RawNode:
        return node("AXGroup", node("AXList", node("AXButton", title="Open"), title="Files"), title=title)

    snap = snap_of(pane("Left"), pane("Right"))
    best = candidates_for(snap, named(snap, "Open", 1), app=APP)[0]
    assert best.strategy == "name_within"
    assert best.spec.within == (
        LocatorStep(role="AXGroup", name="Right"),
        LocatorStep(role="AXList", name="Files"),
    )


def test_a_button_in_a_table_row_is_scoped_by_the_row_name() -> None:
    def table_row(title: str) -> RawNode:
        cell = node("AXCell", node("AXStaticText", value=title), node("AXButton", title="Open"))
        return node("AXRow", cell)

    snap = snap_of(node("AXTable", table_row("Report.pdf"), table_row("Notes.txt"), title="Files"))
    button = named(snap, "Open", 1)
    best = candidates_for(snap, button, app=APP)[0]
    assert best.strategy == "name_within"
    assert best.spec.within == (LocatorStep(role="AXRow", name="Notes.txt"),)


def test_unnamed_unscoped_elements_fall_back_to_their_position() -> None:
    snap = snap_of(node("AXImage"), node("AXImage", y=40))
    second = [el for el in snap.all_elements if el.ax_role == "AXImage"][1]
    found = candidates_for(snap, second, app=APP)
    assert [c.strategy for c in found] == ["index", "path"]
    assert found[0].spec.to_dict() == {"v": 1, "app": APP, "role": "AXImage", "index": 1}
    assert all(c.grade == "low" for c in found)


def test_unnamed_elements_in_a_container_use_the_container() -> None:
    snap = snap_of(node("AXToolbar", node("AXImage")), node("AXImage", y=50))
    in_toolbar = next(el for el in snap.all_elements if el.ax_role == "AXImage")
    best = candidates_for(snap, in_toolbar, app=APP)[0]
    assert best.strategy == "role_within"
    assert best.grade == "low"


def test_a_count_in_a_button_name_adds_a_prefix_alternative() -> None:
    snap = snap_of(node("AXButton", title="Download (3)"), node("AXButton", title="Delete"))
    found = candidates_for(snap, named(snap, "Download (3)"), app=APP)
    assert found[0].spec.name == "Download (3)"
    assert any("contains a number" in reason for reason in found[0].reasons)
    prefix = found[1]
    assert prefix.spec.to_dict() == {
        "v": 1,
        "app": APP,
        "role": "AXButton",
        "name": "Download",
        "name_match": "prefix",
    }


def test_rows_never_get_a_prefix_locator() -> None:
    snap = snap_of(node("AXRow", title="Report 2024.pdf"), node("AXRow", title="Notes", y=40))
    found = candidates_for(snap, named(snap, "Report 2024.pdf"), app=APP, limit=10)
    assert all(c.spec.name_match == "exact" for c in found)


def test_names_with_ellipses_round_trip() -> None:
    snap = snap_of(node("AXMenuItem", title="Export..."))
    best = candidates_for(snap, named(snap, "Export..."), app=APP)[0]
    assert best.spec.name == "Export…"
    assert [el.ref for el in match_elements(snap, parse_locator(best.spec.to_dict()))] == [
        named(snap, "Export...").ref
    ]


def test_a_capture_cut_short_lowers_every_grade_and_marks_uniqueness_unknown() -> None:
    snap = snap_of(node("AXButton", identifier="dl", title="Download"), truncated=True)
    found = candidates_for(snap, named(snap, "Download"), app=APP)
    assert found[0].grade == "medium"  # high, one step down
    assert all(c.unique is None for c in found)
    assert all("not proven unique" in c.reasons[-1] for c in found)


def test_the_window_selector_and_limit_are_applied() -> None:
    snap = snap_of(node("AXButton", identifier="dl", title="Download"))
    selector = WindowSelector(title="Library")
    found = candidates_for(snap, named(snap, "Download"), app=APP, window=selector, limit=1)
    assert len(found) == 1
    assert found[0].spec.to_dict()["window"] == {"title": "Library"}


def test_ocr_text_and_bad_limits_are_refused() -> None:
    snap = snap_of(node("AXButton", title="OK"))
    merged = merge_vision(snap, ocr_elements([OcrLine(text="Hi", confidence=0.9, frame=Rect(1, 1, 9, 9))]))
    ocr = next(el for el in merged.all_elements if el.ref.startswith("o"))
    with pytest.raises(AXPilotError) as exc:
        candidates_for(merged, ocr, app=APP)
    assert code_of(exc) is ErrorCode.NOT_SUPPORTED
    with pytest.raises(AXPilotError) as exc:
        candidates_for(snap, named(snap, "OK"), app=APP, limit=0)
    assert code_of(exc) is ErrorCode.INVALID_ARGUMENT


def test_every_demo_desktop_element_gets_locators_that_find_only_it() -> None:
    """The key invariant: each candidate, serialized and parsed again, matches exactly its element."""
    engine = FakeEngine.demo()
    checked = 0
    for info in engine.list_windows():
        if info.minimized:
            continue
        tree = engine.capture_tree(window_id=info.window_id)
        snap = build_snapshot(tree, snapshot_id="s_demo", mode="full", max_elements=10_000)
        for el in snap.all_elements:
            found = candidates_for(snap, el, app=snap.app.bundle_id or snap.app.name, limit=10)
            assert found, f"{info.app}: no locator for {el.ref} {el.role} {el.name!r}"
            for candidate in found:
                again = parse_locator(candidate.spec.to_dict())
                assert [m.ref for m in match_elements(snap, again)] == [el.ref], candidate
            checked += 1
    assert checked > 300


_LEAF_ROLES = ("AXButton", "AXTextField", "AXCheckBox", "AXStaticText", "AXImage", "AXRow", "AXMenuItem")
_CONTAINER_ROLES = ("AXGroup", "AXToolbar", "AXList", "AXSheet", "AXScrollArea", "AXMenu")
_labels = st.none() | st.sampled_from(["Save", "Open", "Save (2)", "Report 2024", "Back", "Export..."])
_identifiers = st.none() | st.sampled_from(["save", "open", "_NS:12", "6F9619FF8B86D011B42D00C04FC964FF"])


def _leaf(role: str, title: str | None, identifier: str | None, y: int) -> RawNode:
    return RawNode(role=role, title=title, identifier=identifier, frame=Rect(10, y, 60, 20))


def _container(role: str, title: str | None, identifier: str | None, children: list[RawNode]) -> RawNode:
    return RawNode(
        role=role, title=title, identifier=identifier, frame=Rect(0, 0, 400, 400), children=children
    )


_trees = st.recursive(
    st.builds(_leaf, st.sampled_from(_LEAF_ROLES), _labels, _identifiers, st.integers(0, 560)),
    lambda kids: st.builds(
        _container, st.sampled_from(_CONTAINER_ROLES), _labels, _identifiers, st.lists(kids, max_size=4)
    ),
    max_leaves=25,
)


@settings(max_examples=150, deadline=None)
@given(st.lists(_trees, min_size=1, max_size=4), st.booleans())
def test_candidates_of_random_trees_find_exactly_their_element(
    children: list[RawNode], truncated: bool
) -> None:
    """Duplicated names and identifiers on purpose: every candidate still names only its element."""
    snap = snap_of(*children, truncated=truncated)
    for el in snap.all_elements:
        found = candidates_for(snap, el, app=APP, limit=10)
        if el.locator.path:
            assert found  # the path always works
        for candidate in found:
            again = parse_locator(candidate.spec.to_dict())
            assert [m.ref for m in match_elements(snap, again)] == [el.ref]
            assert candidate.unique is (None if truncated else True)
        assert [c.grade for c in found] == sorted(
            (c.grade for c in found), key=["high", "medium", "low"].index
        )


# =========================================================================== windows
def test_the_default_window_is_the_focused_then_the_main_then_the_front_one() -> None:
    a, b, c = window(1, "A"), window(2, "B", main=True), window(3, "C", focused=True)
    assert default_window([a, b, c]) is c
    assert default_window([a, b]) is b
    assert default_window([window(4, "D", minimized=True), a]) is a
    assert default_window([]) is None


def test_a_window_is_named_only_when_a_capture_would_not_read_it_anyway() -> None:
    front, other, same_a, same_b = (
        window(1, "Main", focused=True),
        window(2, "Library"),
        window(3, "Doc"),
        window(4, "Doc"),
    )
    windows = [front, other, same_a, same_b]
    assert window_selector_for(windows, front) is None
    assert window_selector_for(windows, None) is None
    assert window_selector_for(windows, other) == WindowSelector(title="Library")
    assert window_selector_for(windows, same_b) == WindowSelector(index=3)
    assert window_selector_for([front, window(5, "")], window(5, "")) == WindowSelector(index=1)


def test_pick_window_by_title_prefix_or_index() -> None:
    windows = [
        window(1, "Untitled", focused=True),
        window(2, "Untitled 2"),
        window(3, "Hidden", minimized=True),
    ]
    assert pick_window(windows, WindowSelector(title="Untitled 2"), "Demo").window_id == 2
    assert pick_window(windows, WindowSelector(title=" Untitled "), "Demo").window_id == 1
    assert pick_window(windows, WindowSelector(title="Untitled", match="prefix"), "Demo").window_id == 1
    assert pick_window(windows, WindowSelector(index=1), "Demo").window_id == 2
    for selector in (WindowSelector(title="Hidden"), WindowSelector(index=2), WindowSelector(title="Nope")):
        with pytest.raises(AXPilotError) as exc:
            pick_window(windows, selector, "Demo")
        assert code_of(exc) is ErrorCode.WINDOW_NOT_FOUND
        assert exc.value.hint
        assert "list_windows" in exc.value.hint


# =========================================================================== warnings
def test_warnings_explain_how_to_make_an_unidentified_element_locatable() -> None:
    snap = snap_of(node("AXToolbar", node("AXButton", description="Back")))
    back = named(snap, "Back")
    warnings = locator_warnings(snap, back, candidates_for(snap, back, app=APP)[0])
    assert any("sets no AXIdentifier" in w and ".accessibilityIdentifier(_:)" in w for w in warnings)
    assert any("overflow menu" in w for w in warnings)


def test_warnings_for_generated_ids_transient_containers_and_partial_captures() -> None:
    snap = snap_of(node("AXMenu", node("AXMenuItem", title="Copy", identifier="_NS:7")), truncated=True)
    copy = named(snap, "Copy")
    warnings = locator_warnings(snap, copy, None)
    assert any("No locator names" in w for w in warnings)
    assert any("only a generated identifier" in w for w in warnings)
    assert any("inside a menu" in w for w in warnings)
    assert any("uniqueness could not be proven" in w for w in warnings)


def test_an_identified_element_outside_containers_has_no_warnings() -> None:
    snap = snap_of(node("AXButton", identifier="dl", title="Download"))
    el = named(snap, "Download")
    assert locator_warnings(snap, el, candidates_for(snap, el, app=APP)[0]) == []


# =========================================================================== exports
def test_the_axpilot_export_is_the_json() -> None:
    parsed = spec(role="AXButton", name="Back")
    assert export_locator(parsed, "axpilot") == parsed.to_json()


def test_the_python_export_is_valid_python_with_the_locator_literal() -> None:
    snap = snap_of(node("AXButton", identifier="downloadButton", description="Download"))
    el = named(snap, "Download")
    best = candidates_for(snap, el, app=APP)[0]
    script = export_locator(best.spec, "axpilot-python", element=el)
    tree = ast.parse(script)
    assign = next(stmt for stmt in tree.body if isinstance(stmt, ast.Assign))
    target = assign.targets[0]
    assert isinstance(target, ast.Name)
    assert target.id == "DOWNLOAD"
    assert ast.literal_eval(assign.value) == best.spec.to_dict()
    assert "mac.find(locator=DOWNLOAD).click()" in script


@pytest.mark.parametrize(
    ("title", "role", "constant", "action"),
    [
        ("New Note", "AXButton", "NEW_NOTE", "click()"),
        ("Télécharger", "AXButton", "TÉLÉCHARGER", "click()"),
        ("123 Go", "AXButton", "ELEMENT_123_GO", "click()"),
        ("…", "AXButton", "ELEMENT", "click()"),
        ("Email", "AXTextField", "EMAIL", 'type_text("your text")'),
        ('Say "hi"\\now', "AXTextArea", "SAY_HI_NOW", 'type_text("your text")'),
    ],
)
def test_python_exports_pick_a_constant_name_and_an_action(
    title: str, role: str, constant: str, action: str
) -> None:
    snap = snap_of(node(role, title=title))
    el = next(e for e in snap.all_elements if e.ax_role == role)
    script = export_locator(candidates_for(snap, el, app=APP)[0].spec, "axpilot-python", element=el)
    ast.parse(script)
    assert f"{constant} = " in script
    assert script.endswith(f"mac.find(locator={constant}).{action}")


def test_a_flow_exports_as_one_script_with_a_step_per_locator() -> None:
    snap = snap_of(
        node("AXTextField", identifier="login.email", placeholder="Email"),
        node("AXTextField", identifier="login.password", title="Password", secure=True, y=40),
        node("AXButton", title="Share", y=70),
        node("AXSheet", node("AXButton", title="Share"), title="Export"),
    )
    elements = [named(snap, "Email"), named(snap, "Password"), named(snap, "Share"), named(snap, "Share", 1)]
    steps = [(candidates_for(snap, el, app=APP)[0].spec, el) for el in elements]
    script = export_locators(steps, "axpilot-python")
    tree = ast.parse(script)
    constants = [stmt.targets[0].id for stmt in tree.body if isinstance(stmt, ast.Assign)]  # type: ignore[attr-defined]
    assert constants == ["EMAIL", "PASSWORD", "SHARE", "SHARE_2"]
    assert script.splitlines()[:2] == ["import os", ""]
    assert script.splitlines()[-4:] == [
        '    mac.find(locator=EMAIL).type_text("your text")',
        '    mac.find(locator=PASSWORD).type_text(os.environ["PASSWORD"])  # a secure field: needs [safety] '
        "allow_secure_fields",
        "    mac.find(locator=SHARE).click()",
        "    mac.find(locator=SHARE_2).click()",
    ]
    named_json = json.loads(export_locators(steps, "axpilot"))
    assert list(named_json) == constants
    assert named_json["SHARE_2"] == steps[3][0].to_dict()
    assert "import os" not in export_locators(steps[2:], "axpilot-python")


def test_exporting_nothing_is_refused() -> None:
    with pytest.raises(AXPilotError) as exc:
        export_locators([], "axpilot")
    assert code_of(exc) is ErrorCode.INVALID_ARGUMENT


def test_unknown_export_formats_are_refused() -> None:
    with pytest.raises(AXPilotError) as exc:
        export_locator(spec(role="AXButton"), "appium-mac2")  # type: ignore[arg-type]
    assert code_of(exc) is ErrorCode.INVALID_ARGUMENT


def test_export_golden() -> None:
    """The exact text of each export format for a few typical elements (AXPILOT_UPDATE_GOLDEN=1)."""
    snap = snap_of(
        node("AXToolbar", node("AXButton", identifier="downloadButton", description="Download")),
        node(
            "AXSheet",
            node("AXButton", title="Share"),
            node("AXTextField", placeholder="Email"),
            title="Export",
        ),
        node("AXButton", title="Share", y=80),
    )
    parts: list[str] = []
    for name in ("Download", "Share", "Email"):
        el = named(snap, name, 0)
        best = candidates_for(snap, el, app=APP)[0]
        for fmt in EXPORT_FORMATS:
            exported = export_locator(best.spec, fmt, element=el)
            parts.append(f"## {name} · {best.strategy} · {fmt}\n{exported}\n")
    text = "\n".join(parts)
    path = GOLDEN / "locator_exports.txt"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.is_file(), f"{path.name} is missing: run with AXPILOT_UPDATE_GOLDEN=1"
    assert text == path.read_text(encoding="utf-8"), "export format changed: regenerate deliberately"
