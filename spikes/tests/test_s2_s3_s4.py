"""The platform-independent logic of spikes S2 (traversal), S3 (input) and S4 (capture)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SPIKES = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def s2(load: Callable[[str], ModuleType]) -> ModuleType:
    return load("s2_ax_perf/ax_perf.py")


@pytest.fixture(scope="module")
def strategies(load: Callable[[str], ModuleType]) -> ModuleType:
    return load("s3_input/strategies.py")


@pytest.fixture(scope="module")
def hid(load: Callable[[str], ModuleType]) -> ModuleType:
    return load("s3_input/hid_idle.py")


@pytest.fixture(scope="module")
def layout(load: Callable[[str], ModuleType]) -> ModuleType:
    return load("s3_input/layout_map.py")


@pytest.fixture(scope="module")
def s4(load: Callable[[str], ModuleType]) -> ModuleType:
    return load("s4_capture/capture_probe.py")


# --- S2 ------------------------------------------------------------------------------------------

TREE = {"root": ["a", "b"], "a": ["a1", "a2"], "b": ["b1"], "a1": [], "a2": ["deep"], "b1": [], "deep": []}


def reader(visited: list[str]) -> Callable[[str], tuple[list[str], int, int]]:
    def read(node: str) -> tuple[list[str], int, int]:
        visited.append(node)
        return TREE[node], 3, 1 if node == "b" else 0

    return read


def test_walk_is_depth_first_preorder(s2: ModuleType) -> None:
    visited: list[str] = []
    result = s2.walk("root", reader(visited), mode="naive", max_nodes=100, max_depth=10)
    assert visited == ["root", "a", "a1", "a2", "deep", "b", "b1"]
    assert (result.nodes, result.calls, result.attribute_errors) == (7, 21, 1)
    assert result.max_depth_seen == 3
    assert not result.truncated
    assert len(result.node_ms) == 7


def test_walk_respects_node_and_depth_budgets(s2: ModuleType) -> None:
    by_nodes = s2.walk("root", reader([]), mode="naive", max_nodes=3, max_depth=10)
    assert (by_nodes.nodes, by_nodes.truncated) == (3, True)
    visited: list[str] = []
    by_depth = s2.walk("root", reader(visited), mode="naive", max_nodes=100, max_depth=1)
    assert visited == ["root", "a", "b"]
    assert by_depth.truncated  # "a" and "b" had children below the limit


def test_walk_summary_and_mode_comparison(s2: ModuleType) -> None:
    naive = [s2.Walk("naive", nodes=100, calls=1100, total_ms=t, node_ms=[1.0] * 100) for t in (400.0, 500.0)]
    batched = [
        s2.Walk("batched", nodes=100, calls=100, total_ms=t, node_ms=[0.2] * 100) for t in (100.0, 100.0)
    ]
    summary = naive[0].summary()
    assert summary["nodes_per_s"] == 250.0
    assert summary["node_ms"]["p50"] == 1.0
    modes = s2.compare_modes([*naive, *batched])
    assert modes["naive"]["total_ms"]["p50"] == 450.0
    assert modes["batched"]["nodes_per_s_p50"] == 1000.0
    assert modes["speedup_batched_vs_naive"] == 4.5
    assert "speedup_batched_vs_naive" not in s2.compare_modes(naive)


def test_s2_attributes_cover_children(s2: ModuleType) -> None:
    assert s2.ATTRIBUTES[s2.CHILDREN_INDEX] == "AXChildren"
    assert len(set(s2.ATTRIBUTES)) == len(s2.ATTRIBUTES)


# --- S3: strategies ------------------------------------------------------------------------------


def test_matching_elements_by_label(strategies: ModuleType) -> None:
    button = {"AXRole": "AXButton", "AXTitle": " 7 ", "AXDescription": None}
    field = {"AXRole": "AXTextField", "AXValue": "7", "AXIdentifier": "display"}
    assert strategies.label_candidates(button) == ["7"]
    assert strategies.matches(button, "7")
    assert strategies.matches(field, "DISPLAY")
    assert not strategies.matches(button, "8")
    assert strategies.rank_matches([field, button]) == [button, field]


def test_fingerprint_changes(strategies: ModuleType) -> None:
    assert strategies.fingerprint_changes(["a", "b"], ["b", "a"]) == 0
    assert strategies.fingerprint_changes(["AXStaticText|0|None"], ["AXStaticText|7|None"]) == 2


def test_utf16_chunks_never_split_surrogate_pairs(strategies: ModuleType) -> None:
    text = "ab" + "😀" * 12  # each emoji is 2 UTF-16 units
    chunks = strategies.utf16_chunks(text, size=5)
    assert "".join(chunks) == text
    assert all(strategies.utf16_length(chunk) <= 5 for chunk in chunks)
    assert strategies.utf16_length("é✓😀") == 4
    assert strategies.utf16_chunks("") == []


def test_markers_and_point_parsing(strategies: ModuleType) -> None:
    assert strategies.marker_for("cg_unicode", "hi") == "hi [cg_unicode]"
    assert strategies.parse_point("<AXValue {value = x:12.5 y:-3.0 type = kAXValueCGPointType}>") == (
        12.5,
        -3.0,
    )
    assert strategies.parse_point("{value = w:100.0 h:20.0 type = kAXValueCGSizeType}") == (100.0, 20.0)
    assert strategies.parse_point("nothing") is None


def test_example_plan_loads_with_defaults(strategies: ModuleType) -> None:
    plan = strategies.load_plan(SPIKES / "s3_input" / "plan.example.json")
    assert plan
    for entry in plan:
        assert entry["action"] in ("click", "type")
        expected = strategies.CLICK_STRATEGIES if entry["action"] == "click" else strategies.TYPE_STRATEGIES
        assert entry["strategies"] == list(expected)
        if entry["action"] == "click":
            assert entry["target"]


@pytest.mark.parametrize(
    "apps",
    [[{"app": "X", "action": "drag"}], [{"app": "X", "action": "click"}]],
    ids=["unknown-action", "click-without-target"],
)
def test_bad_plans_are_rejected(strategies: ModuleType, tmp_path: Path, apps: list[dict[str, Any]]) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"apps": apps}), encoding="utf-8")
    with pytest.raises(ValueError, match=r"needs a target|unknown action"):
        strategies.load_plan(path)


# --- S3: HID idle --------------------------------------------------------------------------------


def test_hands_off_series_has_no_resets(hid: ModuleType) -> None:
    samples = [(t * 0.5, 10.0 + t * 0.5, 0.1) for t in range(10)]
    result = hid.analyze(samples)
    assert result["hid_unaffected"] is True
    assert result["hid_resets"] == 0
    assert result["combined_resets"] == 9  # pinned near zero: every synthetic event resets it


def test_resets_are_detected(hid: ModuleType) -> None:
    samples = [(0.0, 5.0, 3.0), (0.5, 5.5, 3.5), (1.0, 0.1, 0.1), (1.5, 0.6, 4.6)]
    result = hid.analyze(samples)
    assert result["hid_resets"] == 1
    assert result["combined_resets"] == 1
    assert result["hid_unaffected"] is False
    assert result["hid_min_idle"] == 0.1


def test_sampling_jitter_is_tolerated(hid: ModuleType) -> None:
    samples = [(0.0, 5.0, 5.0), (0.5, 5.3, 5.3)]  # grew 0.3 s in 0.5 s: within the 0.25 s tolerance
    assert hid.analyze(samples)["hid_unaffected"] is True
    assert hid.analyze([])["samples"] == 0


def test_positive_control(hid: ModuleType) -> None:
    assert hid.saw_human_input([(0.0, 4.0, 4.0), (0.2, 0.05, 0.05)])
    assert not hid.saw_human_input([(0.0, 4.0, 4.0), (0.2, 4.2, 4.2)])


# --- S3: layouts ---------------------------------------------------------------------------------

US_ROW = {12: "q", 13: "w", 0: "a", 1: "s", 8: "c", 9: "v"}
AZERTY_ROW = {12: "a", 13: "z", 0: "q", 1: "s", 8: "c", 9: "v"}


def fake_translate(row: dict[int, str]) -> Callable[[int, int], str]:
    def translate(keycode: int, modifiers: int) -> str:
        char = row.get(keycode, "")
        return char.upper() if modifiers == 0x02 else ("" if modifiers else char)

    return translate


def test_modifier_state(layout: ModuleType) -> None:
    assert layout.modifier_state(()) == 0
    assert layout.modifier_state(("shift", "option")) == 0x0A
    assert layout.modifier_state(("cmd",)) == 0x01


def test_build_map_prefers_unmodified_keys(layout: ModuleType) -> None:
    mapping = layout.build_map(fake_translate(US_ROW), range(16))
    assert mapping["q"] == {"keycode": 12, "modifiers": []}
    assert mapping["Q"] == {"keycode": 12, "modifiers": ["shift"]}


def test_azerty_differs_from_us(layout: ModuleType) -> None:
    mapping = layout.build_map(fake_translate(AZERTY_ROW), range(16))
    differences = layout.diff_against_us(mapping)
    assert differences["a"] == {"us": 0, "here": 12, "modifiers": []}
    assert differences["q"] == {"us": 12, "here": 0, "modifiers": []}
    assert "s" not in differences
    assert differences["m"] == {"us": 46, "here": None}  # not in this fake layout
    assert layout.shortcut_keys(mapping)["a"] == 12


def test_us_reference_has_unique_keycodes(layout: ModuleType) -> None:
    assert len(set(layout.US_ANSI.values())) == len(layout.US_ANSI)
    assert set(layout.SHORTCUT_CHARS) <= set(layout.US_ANSI)


# --- S4 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "max_edge", "expected"),
    [
        ((2880, 1800), 1280, (1280, 800)),
        ((1000, 2000), 1280, (640, 1280)),
        ((800, 600), 1280, (800, 600)),
        ((3, 3), None, (3, 3)),
    ],
)
def test_scaled_size(
    s4: ModuleType, size: tuple[int, int], max_edge: int | None, expected: tuple[int, int]
) -> None:
    assert s4.scaled_size(*size, max_edge) == expected


def test_to_pixel_rect_offsets_scales_and_clamps(s4: ModuleType) -> None:
    assert s4.to_pixel_rect((100, 50, 200, 100), (0, 0), 2.0, (2880, 1800)) == (200, 100, 400, 200)
    # a secondary display to the left of the main one has a negative origin
    assert s4.to_pixel_rect((-1500, 0, 100, 100), (-1920, 0), 1.0, (1920, 1080)) == (420, 0, 100, 100)
    # partly off the image
    assert s4.to_pixel_rect((-10, -10, 30, 30), (0, 0), 1.0, (100, 100)) == (0, 0, 20, 20)
    assert s4.to_pixel_rect((90, 90, 50, 50), (0, 0), 1.0, (100, 100)) == (90, 90, 10, 10)


def bitmap(width: int, height: int, paint: Callable[[int, int], int]) -> bytes:
    return bytes(value for y in range(height) for x in range(width) for value in (paint(x, y),) * 4)


def test_region_diff_inside_and_outside(s4: ModuleType) -> None:
    size = (32, 16)
    first = bitmap(*size, lambda x, y: 10)
    second = bitmap(*size, lambda x, y: 110 if 8 <= x < 16 and 4 <= y < 12 else 10)
    common = {
        "bytes_per_row": size[0] * 4,
        "bytes_per_pixel": 4,
        "size": size,
        "rect": (8, 4, 8, 8),
        "step": 1,
    }
    assert s4.region_mean_abs_diff(first, second, inside=True, **common) == 100.0
    assert s4.region_mean_abs_diff(first, second, inside=False, **common) == 0.0
    assert s4.region_mean_abs_diff(first, first, inside=True, **common) == 0.0
    empty = {**common, "rect": (0, 0, 0, 0)}
    assert s4.region_mean_abs_diff(first, second, inside=True, **empty) is None


def test_exclusion_verdict(s4: ModuleType) -> None:
    assert s4.exclusion_verdict(40.0, 1.0) == {
        "inside_diff": 40.0,
        "outside_diff": 1.0,
        "excluded_region_changed": True,
        "rest_stable": True,
    }
    assert s4.exclusion_verdict(2.0, 0.0)["excluded_region_changed"] is False
    assert s4.exclusion_verdict(40.0, 30.0)["rest_stable"] is False
    assert s4.exclusion_verdict(None, None)["excluded_region_changed"] is False
