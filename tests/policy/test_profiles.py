from __future__ import annotations

import pytest

from axpilot.errors import AXPilotError, ErrorCode
from axpilot.policy.profiles import (
    ALL_TOOLS,
    PROFILES,
    TOOL_ORDER,
    TOOLSETS,
    enabled_tools,
    expand_names,
    toolset_of,
)


def test_toolsets_match_the_contract() -> None:
    assert list(TOOLSETS) == ["observe", "input", "apps", "system", "scripting", "browser", "computer"]
    assert TOOLSETS["observe"] == (
        "snapshot",
        "screenshot",
        "find",
        "explore",
        "read",
        "inspect",
        "locate",
        "ocr",
        "list_apps",
        "list_windows",
        "list_displays",
        "wait_for",
        "status",
    )
    assert TOOLSETS["input"] == (
        "click",
        "type_text",
        "press_keys",
        "scroll",
        "drag",
        "move_mouse",
        "set_value",
        "select_option",
        "perform_action",
        "select_menu",
        "fill_form",
        "handle_dialog",
    )
    assert TOOLSETS["apps"] == ("app", "window", "open")
    assert TOOLSETS["system"] == (
        "clipboard",
        "notify",
        "spotlight_search",
        "system_info",
        "system_control",
        "processes",
    )
    assert TOOLSETS["scripting"] == ("shell", "applescript", "shortcuts", "defaults")
    assert TOOLSETS["browser"] == ("browser_tabs", "browser_read", "browser_eval_js")
    assert TOOLSETS["computer"] == ("computer",)


def test_tool_order_is_toolset_order_then_tool_order() -> None:
    assert tuple(t for tools in TOOLSETS.values() for t in tools) == TOOL_ORDER
    assert len(TOOL_ORDER) == len(set(TOOL_ORDER)) == len(ALL_TOOLS)
    assert TOOL_ORDER[0] == "snapshot"
    assert TOOL_ORDER[-1] == "computer"


def test_minimal_profile() -> None:
    assert enabled_tools("minimal") == [
        "snapshot",
        "screenshot",
        "explore",
        "wait_for",
        "status",
        "click",
        "type_text",
        "press_keys",
        "scroll",
        "app",
    ]


def test_standard_profile() -> None:
    tools = enabled_tools("standard")
    expected = (set(TOOLSETS["observe"]) - {"list_displays"}) | set(TOOLSETS["input"])
    expected |= set(TOOLSETS["apps"]) | {"clipboard"}
    assert set(tools) == expected
    assert "list_displays" not in tools
    assert "shell" not in tools
    assert "browser_tabs" not in tools


@pytest.mark.parametrize("profile", ["standard", "full"])
def test_navigation_tools_follow_find(profile: str) -> None:
    tools = enabled_tools(profile)
    start = tools.index("find")
    assert tools[start : start + 3] == ["find", "explore", "read"]


def test_minimal_profile_explores_but_does_not_read() -> None:
    tools = enabled_tools("minimal")
    assert "explore" in tools
    assert "read" not in tools
    assert "find" not in tools


def test_full_profile() -> None:
    tools = set(enabled_tools("full"))
    for name in ("observe", "input", "apps", "system", "scripting", "browser"):
        assert set(TOOLSETS[name]) <= tools
    assert "computer" not in tools


def test_computer_profile() -> None:
    assert enabled_tools("computer") == ["status", "computer"]


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_results_follow_tool_order(profile: str) -> None:
    tools = enabled_tools(profile)
    assert tools == sorted(tools, key=TOOL_ORDER.index)
    assert set(tools) == PROFILES[profile]


def test_enable_a_toolset() -> None:
    tools = enabled_tools("standard", enable=["scripting"])
    assert {"shell", "applescript", "shortcuts", "defaults"} <= set(tools)
    assert tools == sorted(tools, key=TOOL_ORDER.index)


def test_enable_a_single_tool() -> None:
    tools = enabled_tools("minimal", enable=["shell", "list_displays"])
    assert "shell" in tools
    assert "list_displays" in tools
    assert "applescript" not in tools


def test_disable_a_tool_and_a_toolset() -> None:
    tools = enabled_tools("full", disable=["browser", "shell"])
    assert not set(TOOLSETS["browser"]) & set(tools)
    assert "shell" not in tools
    assert "applescript" in tools


def test_disable_wins_over_enable() -> None:
    tools = enabled_tools("standard", enable=["scripting"], disable=["shell"])
    assert "shell" not in tools
    assert "applescript" in tools


def test_names_are_case_and_whitespace_insensitive() -> None:
    assert "shell" in enabled_tools(" Standard ", enable=[" SCRIPTING "])


def test_computer_is_both_a_tool_and_a_toolset() -> None:
    assert "computer" in enabled_tools("standard", enable=["computer"])
    assert expand_names(["computer"]) == {"computer"}


@pytest.mark.parametrize("bad", ["standart", "", "everything"])
def test_unknown_profile(bad: str) -> None:
    with pytest.raises(AXPilotError) as info:
        enabled_tools(bad)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "minimal" in (info.value.hint or "")


def test_unknown_profile_suggests_a_correction() -> None:
    with pytest.raises(AXPilotError) as info:
        enabled_tools("standart")
    assert "Did you mean 'standard'?" in info.value.message


@pytest.mark.parametrize(
    ("enable", "disable"), [(["shel"], []), ([], ["no_such_tool"]), (["Scripting!"], [])]
)
def test_unknown_tool_or_toolset(enable: list[str], disable: list[str]) -> None:
    with pytest.raises(AXPilotError) as info:
        enabled_tools("standard", enable=enable, disable=disable)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_unknown_tool_suggests_a_correction() -> None:
    with pytest.raises(AXPilotError) as info:
        enabled_tools("standard", enable=["shel"])
    assert "Did you mean 'shell'?" in info.value.message


def test_toolset_of() -> None:
    assert toolset_of("snapshot") == "observe"
    assert toolset_of("press_keys") == "input"
    assert toolset_of("browser_eval_js") == "browser"
    assert toolset_of("computer") == "computer"
    assert all(toolset_of(tool) for tool in TOOL_ORDER)
    with pytest.raises(AXPilotError) as info:
        toolset_of("rm_rf")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
