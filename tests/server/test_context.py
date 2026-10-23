"""ServerContext captures (#23): an implicit target (the frontmost app) stays pinned to the app the
policy checked, and captures that only report on an app the call did not target leave its
accessibility tree as it is (``enhance=False``)."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from axpilot.engine.fake import FakeEngine
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.models import AppInfo, RawTree
from axpilot.server.context import CallState, ServerContext, call_scope
from axpilot.server.guard import with_call_warnings
from axpilot.server.results import error_result, ok_result, text_of
from axpilot.server.tools.input import observe_after
from axpilot.testing import make_test_context, structured

pytestmark = pytest.mark.anyio

ONE_PASSWORD_PID = 990  # denied by the default deny_apps


class RecordingEngine(FakeEngine):
    """The demo desktop. Records ``capture_tree``'s arguments and can bring another app forward
    right after the server asked which app is frontmost (the window a race happens in)."""

    captures: list[dict[str, Any]]
    switch_to: str | None = None

    def frontmost_app(self) -> AppInfo | None:
        front = super().frontmost_app()
        if self.switch_to is not None:
            self.app_control("activate", self.switch_to)
            self.switch_to = None
        return front

    def capture_tree(self, **kwargs: Any) -> RawTree:
        self.captures.append(kwargs)
        return super().capture_tree(**kwargs)


async def no_sleep(_: float) -> None:
    await anyio.lowlevel.checkpoint()


def recording() -> tuple[ServerContext, RecordingEngine]:
    engine = RecordingEngine.demo()
    assert isinstance(engine, RecordingEngine)
    engine.captures = []
    ctx = make_test_context({"tools.profile": "full"}, engine=engine)
    ctx.sleep = no_sleep
    return ctx, engine


async def test_an_implicit_target_is_pinned_to_the_app_the_policy_checked() -> None:
    ctx, engine = recording()
    front = engine.frontmost_app()
    assert front is not None
    engine.switch_to = "1Password"  # comes forward between the policy check and the capture
    tree = await ctx.capture_tree()
    assert tree.app.pid == front.pid
    assert [call["app"] for call in engine.captures] == [str(front.pid)]
    assert engine.captures[0]["enhance"] is True  # the target itself may be enhanced
    current = engine.frontmost_app()
    assert current is not None
    assert current.pid == ONE_PASSWORD_PID


async def test_a_named_target_is_passed_through_unchanged() -> None:
    ctx, engine = recording()
    await ctx.capture_tree(app="Finder")
    await ctx.take_snapshot(app="Finder", enhance=False)
    assert [(call["app"], call["enhance"]) for call in engine.captures] == [
        ("Finder", True),
        ("Finder", False),
    ]


async def test_a_window_target_is_not_pinned_to_the_frontmost_app() -> None:
    ctx, engine = recording()
    window = next(w for w in engine.list_windows() if w.app == "Finder")
    engine.app_control("activate", "Notes")
    tree = await ctx.capture_tree(window_id=window.window_id)
    assert tree.window is not None
    assert tree.window.window_id == window.window_id
    assert engine.captures[0]["app"] is None


async def test_the_policy_still_refuses_a_denied_frontmost_app() -> None:
    ctx, engine = recording()
    engine.app_control("activate", "1Password")
    with pytest.raises(AXPilotError) as caught:
        await ctx.capture_tree()
    assert caught.value.code is ErrorCode.BLOCKED_BY_POLICY
    assert engine.captures == []


async def test_reporting_the_frontmost_app_after_the_target_quit_leaves_its_tree_alone() -> None:
    """The fallback of ``observe_after`` reads an app the call did not target: it must not switch
    on that app's tree (in the issue, the MCP client's own Electron window)."""
    ctx, engine = recording()
    front = engine.frontmost_app()
    assert front is not None
    gone = AppInfo(name="Gone", pid=424242, bundle_id="com.example.gone")
    observation = await observe_after(ctx, app=gone, before=None, report="snapshot")
    assert observation.snapshot is not None
    assert observation.snapshot.app.pid == front.pid
    assert [(call["app"], call["enhance"]) for call in engine.captures] == [(str(front.pid), False)]


async def test_observing_the_target_itself_may_enhance_it() -> None:
    ctx, engine = recording()
    finder = engine.find_app("Finder")
    await observe_after(ctx, app=finder, before=None, report="snapshot")
    assert [(call["app"], call["enhance"]) for call in engine.captures] == [("com.apple.finder", True)]


# =========================================================================== several processes, one app
TWO_CODES = [
    AppInfo(name="Code", pid=100, bundle_id="com.microsoft.VSCode"),
    AppInfo(name="Code", pid=200, bundle_id="com.microsoft.VSCode", frontmost=True),
    AppInfo(name="Finder", pid=300, bundle_id="com.apple.finder"),
]


def two_codes(monkeypatch: pytest.MonkeyPatch, apps: list[AppInfo]) -> ServerContext:
    engine = FakeEngine.demo()
    monkeypatch.setattr(engine, "list_apps", lambda include_installed=False: list(apps))
    return make_test_context({}, engine=engine)


async def test_several_processes_of_one_app_resolve_to_the_frontmost_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = two_codes(monkeypatch, TWO_CODES)
    state = CallState(tool="snapshot", public_name="snapshot")
    with call_scope(state):
        app = await ctx.resolve_app("com.microsoft.VSCode")
    assert app is not None
    assert app.pid == 200
    assert state.warnings == [
        "2 running processes match 'com.microsoft.VSCode'; using pid 200 (frontmost). "
        "The other: pid 100. Pass app=<pid> to choose."
    ]


async def test_without_a_frontmost_copy_the_first_process_is_used_and_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = two_codes(monkeypatch, [app for app in TWO_CODES[:2] if not app.frontmost] + [
        AppInfo(name="Code", pid=200, bundle_id="com.microsoft.VSCode")
    ])  # fmt: skip
    state = CallState(tool="find", public_name="find")
    with call_scope(state):
        app = await ctx.resolve_app("Code")
    assert app is not None
    assert app.pid == 100
    assert state.warnings == [
        "2 running processes match 'Code'; using pid 100. The other: pid 200. Pass app=<pid> to choose."
    ]


async def test_a_pid_or_a_unique_app_resolves_without_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = two_codes(monkeypatch, TWO_CODES)
    state = CallState(tool="snapshot", public_name="snapshot")
    with call_scope(state):
        assert (await ctx.resolve_app("100")).pid == 100  # type: ignore[union-attr]
        assert (await ctx.resolve_app("Finder")).pid == 300  # type: ignore[union-attr]
    assert state.warnings == []


def test_call_warnings_join_the_result_once() -> None:
    result = ok_result("done", {"warnings": ["already there"]})
    merged = with_call_warnings(result, ["already there", "2 running processes match 'Code'"])
    assert structured(merged)["warnings"] == ["already there", "2 running processes match 'Code'"]
    assert text_of(merged).splitlines()[-1] == "warning: 2 running processes match 'Code'"
    failed = error_result(AXPilotError(ErrorCode.APP_NOT_FOUND, "No app."))
    assert with_call_warnings(failed, ["ignored"]) is failed
