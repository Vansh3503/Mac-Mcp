from __future__ import annotations

from collections.abc import Callable

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, RawNode, RawTree, Rect, WindowInfo
from axpilot.perception import Snapshot, build_snapshot, find_elements
from axpilot.perception.find import normalize, score_text
from axpilot.perception.serialize import assign_handles


def n(
    role: str, *children: RawNode, frame: tuple[float, float, float, float] = (0, 0, 50, 20), **kw: object
) -> RawNode:
    return RawNode(role=role, frame=Rect(*frame), children=list(children), **kw)  # type: ignore[arg-type]


def snap(*children: RawNode, mode: str = "interactive") -> Snapshot:
    root = RawNode(role="AXWindow", frame=Rect(0, 0, 800, 600), children=list(children))
    assign_handles(root)
    tree = RawTree(AppInfo("Demo", 1), WindowInfo(1, 1, "Demo", "Doc", Rect(0, 0, 800, 600)), root)
    return build_snapshot(tree, snapshot_id="s_find", mode=mode)  # type: ignore[arg-type]


def found_names(results: list) -> list[str | None]:
    return [el.name for el, _ in results]


def test_match_tiers_rank_exact_then_prefix_then_substring_then_tokens() -> None:
    s = snap(
        n("AXButton", title="Save a copy of this"),  # token overlap only for "save copy"
        n("AXButton", title="Autosave copy"),  # substring
        n("AXButton", title="Save copy as…"),  # prefix
        n("AXButton", title="Save Copy"),  # exact (case-insensitive)
    )
    results = find_elements(s, text="save copy")
    assert found_names(results) == ["Save Copy", "Save copy as…", "Autosave copy", "Save a copy of this"]
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 1.0


def test_role_filter_accepts_short_ax_and_subroles() -> None:
    s = snap(
        n("AXButton", title="Search"),
        n("AXTextField", subrole="AXSearchField", description="Search"),
        n("AXTextField", description="Name"),
    )
    assert found_names(find_elements(s, text="search", role="button")) == ["Search"]
    assert found_names(find_elements(s, role="AXButton")) == ["Search"]
    assert found_names(find_elements(s, role="search")) == ["Search"]
    assert found_names(find_elements(s, role="AXSearchField")) == ["Search"]
    assert found_names(find_elements(s, role="textfield")) == ["Search", "Name"]
    assert found_names(find_elements(s, role="field")) == ["Name"]


def test_identifier_filter_is_exact_and_case_insensitive() -> None:
    s = snap(
        n("AXButton", title="OK", identifier="dialog.ok"), n("AXButton", title="OK", identifier="dialog.ok2")
    )
    assert [el.identifier for el, _ in find_elements(s, identifier="Dialog.OK")] == ["dialog.ok"]


def test_text_matches_identifier_value_and_description() -> None:
    s = snap(
        n("AXTextField", description="Recipient", value="ada@example.com"),
        n("AXButton", identifier="sendButton"),
        n("AXButton", title="Go", description="Send the message"),
    )
    assert found_names(find_elements(s, text="ada@example.com")) == ["Recipient"]
    by_identifier = find_elements(s, text="sendbutton")
    assert by_identifier[0][0].identifier == "sendButton"
    (best, best_score), (weak, weak_score) = find_elements(s, text="send the message")
    assert best.name == "Go"
    assert weak.identifier == "sendButton"
    assert weak_score < 0.5 < best_score


def test_typos_are_tolerated() -> None:
    s = snap(n("AXButton", title="Sign In"), n("AXButton", title="Cancel"))
    assert found_names(find_elements(s, text="Sgin In")) == ["Sign In"]


def test_ellipses_are_normalized() -> None:
    s = snap(n("AXMenuItem", title="Save As…"))
    ((el, score),) = find_elements(s, text="save as...")
    assert el.name == "Save As…"
    assert score == 1.0
    assert normalize("  Save   As… ") == "save as..."


def test_role_only_search_keeps_document_order() -> None:
    s = snap(*[n("AXButton", title=f"B{i}", frame=(0, i * 20, 50, 20)) for i in range(5)])
    results = find_elements(s, role="button", limit=3)
    assert found_names(results) == ["B0", "B1", "B2"]
    assert {score for _, score in results} == {1.0}


def test_ties_prefer_interactive_elements() -> None:
    s = snap(n("AXStaticText", value="Delete"), n("AXButton", title="Delete"), mode="full")
    first, second = find_elements(s, text="delete")
    assert first[0].role == "button"
    assert second[0].role == "text"
    assert first[1] == second[1]


def test_no_match_returns_nothing() -> None:
    assert find_elements(snap(n("AXButton", title="OK")), text="zebra crossing") == []


def test_limit_caps_results() -> None:
    s = snap(*[n("AXButton", title=f"Item {i}", frame=(0, i * 20, 50, 20)) for i in range(30)])
    assert len(find_elements(s, text="item")) == 20
    assert len(find_elements(s, text="item", limit=5)) == 5


@pytest.mark.parametrize("kwargs", [{}, {"text": "   "}, {"text": "ok", "limit": 0}])
def test_invalid_queries_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(AXPilotError) as info:
        find_elements(snap(n("AXButton", title="OK")), **kwargs)  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_score_text_bounds() -> None:
    assert score_text("ok", "OK") == 1.0
    assert score_text("ok", None) == 0.0
    assert 0.0 < score_text("ok", "OK button") < 1.0


def test_find_in_demo_settings(snapshot_of: Callable[..., Snapshot]) -> None:
    s = snapshot_of("System Settings")
    (best, score), *_ = find_elements(s, text="privacy & security", role="row")
    assert best.name == "Privacy & Security"
    assert best.selected
    assert score == 1.0
    accessibility = find_elements(s, text="accessibility")
    assert {el.role for el, _ in accessibility} == {"row", "button"}


def test_single_word_typos_use_the_similarity_tier() -> None:
    s = snap(n("AXButton", title="Cancel"), n("AXButton", title="Close"))
    ((el, score),) = find_elements(s, text="Cancle")
    assert el.name == "Cancel"
    assert 0.0 < score < 0.31
