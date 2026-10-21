"""``locate`` and ``find(locator=…)``: success paths, windows, the error matrix, fault injection and
policy-bypass attempts (QUALITY.md B2, B6)."""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest
from mcp import Client

from axpilot.engine.fake import FakeEngine
from axpilot.errors import ErrorCode
from axpilot.server.context import ServerContext
from axpilot.server.results import error_code_of, text_of
from axpilot.testing import connected_client, make_test_context, structured
from axpilot.testing.faulty_engine import Fault, FaultyEngine

pytestmark = pytest.mark.anyio

FINDER = "com.apple.finder"
BACK = {"app": FINDER, "role": "AXButton", "name": "Back"}


@pytest.fixture
def ctx() -> ServerContext:
    ctx = make_test_context({"tools.profile": "standard"})
    ctx.sleep = _no_sleep
    return ctx


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[Client]:
    async with connected_client(ctx) as connected:
        yield connected


async def _no_sleep(_: float) -> None:
    await anyio.lowlevel.checkpoint()


async def call(client: Client, tool: str, args: dict[str, Any]) -> tuple[Any, str | None]:
    result = await client.call_tool(tool, args)
    return result, error_code_of(result)


async def ok(client: Client, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result, error = await call(client, tool, args)
    assert error is None, text_of(result)
    return structured(result)


async def ref_of(client: Client, app: str, text: str, role: str | None = None) -> str:
    args: dict[str, Any] = {"app": app, "text": text}
    if role is not None:
        args["role"] = role
    data = await ok(client, "find", args)
    return data["matches"][0]["ref"]


# =========================================================================== locate(ref)
async def test_locate_builds_ranked_locators_for_a_toolbar_button(client: Client) -> None:
    ref = await ref_of(client, "Finder", "Back", "button")
    result, error = await call(client, "locate", {"ref": ref})
    assert error is None, text_of(result)
    data = structured(result)
    assert data["element"]["name"] == "Back"
    assert data["recommended"] == 0
    best = data["candidates"][0]
    assert best["locator"] == {"v": 1, **BACK}
    assert (best["strategy"], best["grade"], best["unique"]) == ("name", "medium", True)
    assert data["candidates"][-1]["strategy"] == "path"
    assert data["snippets"] == {
        "axpilot": '{"v": 1, "app": "com.apple.finder", "role": "AXButton", "name": "Back"}'
    }
    assert data["app"]["bundle_id"] == FINDER
    assert data["window"]["title"] == "Downloads"
    text = text_of(result)
    assert text.startswith(f'locate {ref} button "Back" · Finder (com.apple.finder) · window "Downloads"')
    assert 'recommended: {"v": 1, "app": "com.apple.finder", "role": "AXButton", "name": "Back"}' in text
    assert any("sets no AXIdentifier" in w for w in data["warnings"])
    assert any("overflow menu" in w for w in data["warnings"])


async def test_locate_prefers_the_identifier_and_does_not_nag_about_it(client: Client) -> None:
    ref = await ref_of(client, "Login Demo", "Sign In", "button")
    data = await ok(client, "locate", {"ref": ref})
    best = data["candidates"][0]
    assert best["locator"] == {
        "v": 1,
        "app": "com.example.LoginDemo",
        "role": "AXButton",
        "identifier": "login.submit",
    }
    assert best["grade"] == "high"
    assert not any("AXIdentifier" in w for w in data["warnings"])


async def test_locate_returns_a_python_snippet_that_finds_the_element(client: Client) -> None:
    ref = await ref_of(client, "Finder", "Back", "button")
    both = await ok(client, "locate", {"ref": ref, "formats": ["axpilot-python", "axpilot"]})
    assert list(both["snippets"]) == ["axpilot-python", "axpilot"]
    data = await ok(client, "locate", {"ref": ref, "formats": ["axpilot-python", "axpilot-python"]})
    assert list(data["snippets"]) == ["axpilot-python"]
    script = data["snippets"]["axpilot-python"]
    tree = ast.parse(script)
    assign = next(stmt for stmt in tree.body if isinstance(stmt, ast.Assign))
    locator = ast.literal_eval(assign.value)
    assert locator == data["candidates"][0]["locator"]
    again = await ok(client, "find", {"locator": locator})
    assert [m["ref"] for m in again["matches"]] == [ref]


async def test_locate_names_another_window_by_its_title(ctx: ServerContext, client: Client) -> None:
    first = await ok(client, "snapshot", {"app": "TextEdit"})
    text_area = next(el for el in first["elements"] if el["role"] == "textarea")
    ctx.engine.menu_select("TextEdit", ["File", "New"])  # "Untitled 2" opens in front
    data = await ok(client, "locate", {"ref": text_area["ref"], "snapshot_id": first["snapshot_id"]})
    best = data["candidates"][0]
    assert best["locator"]["window"] == {"title": "Untitled"}
    assert best["locator"]["identifier"] == "First Text View"
    assert any("names the window by its title" in w for w in data["warnings"])
    found = await ok(client, "find", {"locator": best["locator"]})
    assert found["unique"] is True
    assert found["snapshot_id"] != data["snapshot_id"]
    front = await ok(client, "find", {"locator": {k: v for k, v in best["locator"].items() if k != "window"}})
    assert front["unique"] is True
    assert front["matches"][0]["ref"] != found["matches"][0]["ref"]  # the other window's text area


async def test_locate_respects_max_candidates_and_its_snapshot_is_stored(client: Client) -> None:
    ref = await ref_of(client, "Finder", "Back", "button")
    data = await ok(client, "locate", {"ref": ref, "max_candidates": 1})
    assert len(data["candidates"]) == 1
    inspected = await ok(client, "inspect", {"ref": ref, "snapshot_id": data["snapshot_id"]})
    assert inspected["element"]["name"] == "Back"


async def test_a_capture_cut_short_cannot_prove_uniqueness() -> None:
    ctx = make_test_context({"tools.profile": "standard", "perception.max_nodes": 100})
    async with connected_client(ctx) as client:
        ref = await ref_of(client, "Finder", "Back", "button")
        data = await ok(client, "locate", {"ref": ref})
        checked = await ok(client, "locate", {"locator": BACK})
    assert all(c["unique"] is None for c in data["candidates"])
    assert data["candidates"][0]["grade"] == "low"
    assert any("uniqueness could not be proven" in w for w in data["warnings"])
    assert checked["unique"] is None
    assert any("there may be more matches" in w for w in checked["warnings"])


# =========================================================================== locate(refs)
async def open_sign_in_sheet(client: Client) -> dict[str, str]:
    """Open Login Demo's sign-in sheet; returns the refs of its elements by name."""
    first = await ok(client, "snapshot", {"app": "Login Demo"})
    opener = next(el["ref"] for el in first["elements"] if el["name"] == "Sign In…")
    await ok(client, "click", {"ref": opener})
    sheet = await ok(client, "snapshot", {"app": "Login Demo"})
    return {el["name"]: el["ref"] for el in sheet["elements"] if el["name"]}


async def test_locate_refs_covers_a_screen_in_one_call(client: Client) -> None:
    by_name = await open_sign_in_sheet(client)
    refs = [by_name["Email"], by_name["Password"], by_name["Sign In"]]
    result, error = await call(client, "locate", {"refs": refs, "formats": ["axpilot-python", "axpilot"]})
    assert error is None, text_of(result)
    data = structured(result)
    assert [item["element"]["ref"] for item in data["items"]] == refs
    assert [item["candidates"][0]["locator"]["identifier"] for item in data["items"]] == [
        "login.email",
        "login.password",
        "login.submit",
    ]
    assert all(item["recommended"] == 0 for item in data["items"])
    assert data["element"] is None
    assert data["candidates"] == []
    assert all("inside a sheet" in item["warnings"][0] for item in data["items"])
    assert data["warnings"] == [
        f"{', '.join(refs)}: The element is inside a sheet: a script has to open the sheet before using it."
    ]
    script = data["snippets"]["axpilot-python"]
    tree = ast.parse(script)
    constants = [stmt.targets[0].id for stmt in tree.body if isinstance(stmt, ast.Assign)]  # type: ignore[attr-defined]
    assert constants == ["EMAIL", "PASSWORD", "SIGN_IN"]
    assert script.startswith("import os\n")
    assert 'mac.find(locator=PASSWORD).type_text(os.environ["PASSWORD"])' in script
    assert script.endswith("mac.find(locator=SIGN_IN).click()")
    assert '"SIGN_IN": {' in data["snippets"]["axpilot"]
    assert text_of(result).startswith("locate 3 elements · Login Demo")
    for item in data["items"]:
        found = await ok(client, "find", {"locator": item["candidates"][0]["locator"]})
        assert found["unique"] is True


async def test_a_flow_must_locate_each_screen_before_leaving_it(client: Client) -> None:
    by_name = await open_sign_in_sheet(client)
    await ok(client, "click", {"ref": by_name["Cancel"]})  # the sheet closes: its elements are gone
    result, error = await call(client, "locate", {"refs": [by_name["Email"], by_name["Sign In"]]})
    assert error == "STALE_REF"
    text = text_of(result)
    assert 'field "Email"' in text
    assert 'button "Sign In"' in text
    assert "Locate elements while their screen is open" in text


async def test_locate_refs_in_several_windows(client: Client) -> None:
    back = await ref_of(client, "Finder", "Back", "button")
    sign_in = await ref_of(client, "Login Demo", "Sign In…", "button")
    result, error = await call(client, "locate", {"refs": [back, sign_in], "formats": ["axpilot-python"]})
    assert error is None, text_of(result)
    data = structured(result)
    first, second = data["items"]
    assert first["snapshot_id"] == data["snapshot_id"]
    assert second["snapshot_id"] != first["snapshot_id"]
    assert data["app"]["bundle_id"] == FINDER
    assert "(Login Demo · window" in text_of(result)
    assert "BACK = " in data["snippets"]["axpilot-python"]
    assert "SIGN_IN = " in data["snippets"]["axpilot-python"]


async def test_repeated_refs_are_located_once(client: Client) -> None:
    back = await ref_of(client, "Finder", "Back", "button")
    snapshot_id = (await ok(client, "find", {"app": "Finder", "text": "Back"}))["snapshot_id"]
    data = await ok(client, "locate", {"refs": [back, back, f"{snapshot_id}:{back}"]})
    assert len(data["items"]) == 1


# =========================================================================== locate(locator)
async def test_checking_a_unique_locator_returns_the_element_and_better_locators(client: Client) -> None:
    result, error = await call(client, "locate", {"locator": {"app": "Finder", "name": "Back"}})
    assert error is None, text_of(result)
    data = structured(result)
    assert (data["count"], data["unique"]) == (1, True)
    assert data["locator"] == {"v": 1, "app": "Finder", "name": "Back"}
    assert data["element"]["name"] == "Back"
    assert [m["name"] for m in data["matches"]] == ["Back"]
    assert data["candidates"][0]["locator"]["app"] == FINDER  # upgraded to the bundle id
    assert text_of(result).startswith('locator element "Back" in Finder matches 1 element · Finder')


async def test_checking_an_ambiguous_locator_lists_the_matches(client: Client) -> None:
    result, error = await call(client, "locate", {"locator": {"app": FINDER, "role": "button"}})
    assert error is None
    data = structured(result)
    assert data["count"] > 1
    assert data["unique"] is False
    assert data["element"] is None
    assert data["candidates"] == []
    assert data["recommended"] is None
    assert data["snippets"] == {}
    assert len(data["matches"]) == data["count"]
    assert any(f"matches {data['count']} elements" in w for w in data["warnings"])
    refs = [m["ref"] for m in data["matches"]]
    picked = await ok(client, "locate", {"locator": {"app": FINDER, "role": "button", "index": 3}})
    assert [m["ref"] for m in picked["matches"]] == [refs[3]]


async def test_checking_a_locator_that_matches_nothing_is_not_an_error(client: Client) -> None:
    result, error = await call(client, "locate", {"locator": {"app": FINDER, "name": "Nope"}})
    assert error is None
    data = structured(result)
    assert (data["count"], data["unique"], data["matches"]) == (0, False, [])
    assert "matches nothing" in text_of(result)
    assert any("Nothing matches" in w for w in data["warnings"])


async def test_matches_are_capped_in_the_list_but_counted(client: Client) -> None:
    data = await ok(client, "locate", {"locator": {"app": FINDER, "role": "AXStaticText"}})
    assert data["count"] > 20
    assert len(data["matches"]) == 20


async def test_a_window_selector_that_matches_no_window_is_window_not_found(client: Client) -> None:
    for window in ({"title": "Nope"}, {"index": 5}):
        result, error = await call(client, "locate", {"locator": {**BACK, "window": window}})
        assert error == "WINDOW_NOT_FOUND", text_of(result)


# =========================================================================== find(locator)
async def test_find_with_a_locator_reports_count_and_uniqueness(client: Client) -> None:
    data = await ok(client, "find", {"locator": BACK})
    assert data["found"] is True
    assert (data["count"], data["unique"]) == (1, True)
    assert data["locator"] == {"v": 1, **BACK}
    assert data["matches"][0]["score"] == 1.0
    plain = await ok(client, "find", {"app": "Finder", "text": "Back"})
    assert plain["locator"] is None
    assert plain["unique"] is None


async def test_find_counts_every_match_even_past_the_limit(client: Client) -> None:
    result, error = await call(client, "find", {"locator": {"app": FINDER, "role": "button"}, "limit": 2})
    assert error is None
    data = structured(result)
    assert len(data["matches"]) == 2
    assert data["count"] > 2
    assert data["unique"] is False
    assert any("a script would not know which one" in w for w in data["warnings"])


async def test_find_with_a_locator_that_matches_nothing(client: Client) -> None:
    data = await ok(client, "find", {"locator": {"app": FINDER, "identifier": "back"}})
    assert (data["found"], data["count"], data["unique"]) == (False, 0, False)


async def test_find_with_a_locator_scrolls_a_long_list() -> None:
    ctx = make_test_context({"tools.profile": "standard"})
    ctx.sleep = _no_sleep
    ctx.engine.app_control("launch", "com.apple.mail")
    async with connected_client(ctx) as client:
        row = {"app": "com.apple.mail", "role": "AXRow", "name": "Invoice (94)", "name_match": "contains"}
        data = await ok(client, "find", {"locator": row, "scroll": True})
    assert data["found"] is True
    assert data["scrolls"] > 0
    assert "Invoice (94)" in data["matches"][0]["name"]
    assert data["unique"] is True


async def test_find_with_a_locator_inside_a_container(client: Client) -> None:
    toolbar = await ok(client, "find", {"app": "Finder", "role": "toolbar"})
    data = await ok(
        client,
        "find",
        {"locator": {"app": "Finder", "role": "AXButton"}, "container": toolbar["matches"][0]["ref"]},
    )
    names = {m["name"] for m in data["matches"]}
    assert "Back" in names
    assert "Close" not in names  # the window's own buttons are outside the toolbar
    assert data["count"] == len(data["matches"])


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"locator": BACK, "text": "Back"}, "do not combine it with text"),
        ({"locator": BACK, "role": "button"}, "do not combine it with role"),
        ({"locator": BACK, "identifier": "x"}, "do not combine it with identifier"),
        ({"locator": BACK, "app": "Finder", "window_id": 201}, "do not combine it with app, window_id"),
        ({"locator": {"app": FINDER}}, "at least one of role, identifier, name or path"),
        ({"locator": {"app": FINDER, "role": "AXButton", "name_match": "prefix"}}, "name_match needs a name"),
        ({"locator": {"app": FINDER, "path": "Toolbar"}}, 'of the form "AXRole[index]"'),
    ],
)
async def test_find_refuses_bad_locator_calls(client: Client, args: dict[str, Any], fragment: str) -> None:
    result, error = await call(client, "find", args)
    assert error == "INVALID_ARGUMENT"
    assert fragment in text_of(result)


async def test_find_refuses_a_container_in_another_app_or_with_a_window(client: Client) -> None:
    toolbar = (await ok(client, "find", {"app": "Finder", "role": "toolbar"}))["matches"][0]["ref"]
    for locator in ({"app": "Notes", "role": "AXButton"}, {**BACK, "window": {"index": 0}}):
        result, error = await call(client, "find", {"locator": locator, "container": toolbar})
        assert error == "INVALID_ARGUMENT", text_of(result)


# =========================================================================== error matrix
@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({}, ErrorCode.INVALID_ARGUMENT),
        ({"ref": "e1", "locator": BACK}, ErrorCode.INVALID_ARGUMENT),
        ({"ref": "e1", "refs": ["e2"]}, ErrorCode.INVALID_ARGUMENT),
        ({"refs": ["e2"], "locator": BACK}, ErrorCode.INVALID_ARGUMENT),
        ({"refs": []}, ErrorCode.INVALID_ARGUMENT),
        ({"refs": [f"e{i}" for i in range(31)]}, ErrorCode.INVALID_ARGUMENT),
        ({"refs": ["e1", "e99999"]}, ErrorCode.STALE_REF),
        ({"locator": BACK, "snapshot_id": "s_abcd"}, ErrorCode.INVALID_ARGUMENT),
        ({"locator": {**BACK, "colour": "red"}}, ErrorCode.INVALID_ARGUMENT),
        ({"locator": {**BACK, "v": 2}}, ErrorCode.INVALID_ARGUMENT),
        ({"locator": {"role": "AXButton"}}, ErrorCode.INVALID_ARGUMENT),
        ({"locator": BACK, "formats": ["appium-mac2"]}, ErrorCode.INVALID_ARGUMENT),
        ({"locator": BACK, "formats": []}, ErrorCode.INVALID_ARGUMENT),
        ({"locator": BACK, "max_candidates": 0}, ErrorCode.INVALID_ARGUMENT),
        ({"ref": "zz"}, ErrorCode.INVALID_ARGUMENT),
        ({"ref": "e99999"}, ErrorCode.STALE_REF),
        ({"ref": "e1", "snapshot_id": "s_zzzz"}, ErrorCode.SNAPSHOT_NOT_FOUND),
        ({"locator": {"app": "Keychain Access", "role": "AXButton"}}, ErrorCode.APP_NOT_FOUND),
        ({"locator": {**BACK, "window": {"title": "Nope"}}}, ErrorCode.WINDOW_NOT_FOUND),
        ({"locator": {"app": "com.1password.1password", "role": "AXButton"}}, ErrorCode.BLOCKED_BY_POLICY),
    ],
)
async def test_locate_error_matrix(client: Client, args: dict[str, Any], code: ErrorCode) -> None:
    await ok(client, "snapshot", {"app": "Finder"})
    result, error = await call(client, "locate", args)
    assert error == code.value, text_of(result)
    assert "Traceback" not in text_of(result)


async def test_locating_ocr_text_is_not_supported(ctx: ServerContext, client: Client) -> None:
    ctx.engine.app_control("launch", "Canvas Paint")
    data = await ok(client, "snapshot", {"app": "Canvas Paint"})
    ocr = next(el["ref"] for el in data["elements"] if el["ref"].startswith("o"))
    for args in ({"ref": ocr}, {"refs": [ocr]}):
        result, error = await call(client, "locate", args)
        assert error == "NOT_SUPPORTED"
        assert "OCR text" in text_of(result)


async def test_locating_an_element_that_is_gone_is_stale_ref(ctx: ServerContext, client: Client) -> None:
    first = await ok(client, "snapshot", {"app": "TextEdit"})
    text_area = next(el for el in first["elements"] if el["role"] == "textarea")
    ctx.engine.app_control("quit", "TextEdit")
    result, error = await call(
        client, "locate", {"ref": text_area["ref"], "snapshot_id": first["snapshot_id"]}
    )
    assert error == "STALE_REF", text_of(result)
    assert "no longer open" in text_of(result)


async def test_the_locator_tool_works_under_windows_mcp_naming() -> None:
    ctx = make_test_context({"tools.profile": "standard", "tools.naming": "windows-mcp"})
    async with connected_client(ctx) as client:
        names = [t.name for t in (await client.list_tools()).tools]
        assert "Locate" in names
        result, error = await call(client, "Locate", {"locator": BACK})
    assert error is None, text_of(result)
    assert structured(result)["unique"] is True


async def test_locate_is_in_standard_and_full_but_not_minimal() -> None:
    for profile, present in (("minimal", False), ("standard", True), ("full", True), ("computer", False)):
        ctx = make_test_context({"tools.profile": profile})
        async with connected_client(ctx) as client:
            names = [t.name for t in (await client.list_tools()).tools]
        assert ("locate" in names) is present, profile


# =========================================================================== faults
@pytest.mark.parametrize(
    ("args", "method"),
    [
        ({"locator": BACK}, "capture_tree"),
        ({"locator": BACK}, "find_app"),
        ({"locator": {**BACK, "window": {"index": 0}}}, "list_windows"),
    ],
)
async def test_raw_engine_errors_become_internal_and_the_server_survives(
    args: dict[str, Any], method: str
) -> None:
    faulty = FaultyEngine(FakeEngine.demo(), {method: Fault.crash("secret detail /Users/x")})
    ctx = make_test_context({"tools.profile": "standard"}, engine=faulty)
    async with connected_client(ctx) as client:
        result, error = await call(client, "locate", args)
        faulty.clear_faults()
        _again, again_error = await call(client, "locate", args)
    assert error == "INTERNAL"
    assert "secret detail" not in text_of(result)
    assert "err_" in text_of(result)
    assert again_error is None


async def test_a_hung_capture_times_out() -> None:
    faulty = FaultyEngine(FakeEngine.demo(), {"capture_tree": Fault.hang(10)})
    ctx = make_test_context({"tools.profile": "standard"}, engine=faulty)
    ctx.engine_timeout_s = 0.1
    ctx.capture_timeout_s = 0.1
    try:
        async with connected_client(ctx) as client:
            result, error = await call(client, "locate", {"locator": BACK})
    finally:
        faulty.release()
    assert error == "TIMEOUT", text_of(result)


async def test_engine_errors_keep_their_codes() -> None:
    fault = Fault.error(ErrorCode.PERMISSION_ACCESSIBILITY, "AX is disabled", hint="Grant it.")
    faulty = FaultyEngine(FakeEngine.demo(), {"capture_tree": fault})
    ctx = make_test_context({"tools.profile": "standard"}, engine=faulty)
    async with connected_client(ctx) as client:
        result, error = await call(client, "find", {"locator": BACK})
    assert error == "PERMISSION_ACCESSIBILITY"
    assert "Grant it." in text_of(result)


# =========================================================================== policy
async def test_a_denied_app_is_refused_before_its_windows_are_listed() -> None:
    faulty = FaultyEngine(FakeEngine.demo())
    ctx = make_test_context({"tools.profile": "standard"}, engine=faulty)
    locator = {"app": "1Password", "role": "AXButton", "window": {"index": 0}}
    async with connected_client(ctx) as client:
        located, located_error = await call(client, "locate", {"locator": locator})
        found, found_error = await call(client, "find", {"locator": locator})
    assert located_error == found_error == "BLOCKED_BY_POLICY"
    assert faulty.calls["list_windows"] == 0
    assert faulty.calls["capture_tree"] == 0
    assert "Reading 1Password" in text_of(located)
    assert "BLOCKED_BY_POLICY" in text_of(found)


async def test_allow_apps_limits_locators_too() -> None:
    ctx = make_test_context({"tools.profile": "standard", "safety.allow_apps": ["com.apple.Notes"]})
    async with connected_client(ctx) as client:
        result, error = await call(client, "locate", {"locator": BACK})
        notes, notes_error = await call(client, "locate", {"locator": {"app": "Notes", "role": "AXButton"}})
    assert error == "BLOCKED_BY_POLICY", text_of(result)
    assert notes_error is None, text_of(notes)


@pytest.mark.parametrize(
    "locator",
    [
        {"app": "System Settings", "role": "AXButton"},
        {
            "app": "com.apple.systempreferences",
            "role": "AXButton",
            "window": {"title": "Privacy", "match": "prefix"},
        },
    ],
)
async def test_a_denied_window_cannot_be_read_through_a_locator(
    client: Client, locator: dict[str, Any]
) -> None:
    for tool in ("locate", "find"):
        result, error = await call(client, tool, {"locator": locator})
        assert error == "BLOCKED_BY_POLICY", text_of(result)


async def test_a_ref_from_an_app_denied_since_is_refused(ctx: ServerContext, client: Client) -> None:
    data = await ok(client, "snapshot", {"app": "Finder"})
    ref = data["elements"][0]["ref"]
    safety = ctx.config.safety.model_copy(update={"deny_apps": [*ctx.config.safety.deny_apps, FINDER]})
    strict = dataclasses.replace(ctx, config=ctx.config.model_copy(update={"safety": safety}))
    async with connected_client(strict) as strict_client:
        result, error = await call(strict_client, "locate", {"ref": ref, "snapshot_id": data["snapshot_id"]})
        batch, batch_error = await call(
            strict_client, "locate", {"refs": [ref], "snapshot_id": data["snapshot_id"]}
        )
    assert error == "BLOCKED_BY_POLICY", text_of(result)
    assert batch_error == "BLOCKED_BY_POLICY", text_of(batch)
