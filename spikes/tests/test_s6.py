"""Spike S6: the interop checks, end to end against a stub MCP server over stdio."""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent, Tool, ToolAnnotations

STUB = Path(__file__).resolve().parent / "stub_mcp_server.py"


@pytest.fixture(scope="module")
def interop(load: Callable[[str], ModuleType]) -> ModuleType:
    return load("s6_mcp_interop/interop.py")


def stub_command(*extra: str) -> str:
    return shlex.join([sys.executable, str(STUB), *extra])


def args_for(interop: ModuleType, *argv: str) -> object:
    return interop.parse_args(["--no-save", "--timeout", "60", *argv])


def tool(name: str, **fields: object) -> Tool:
    defaults: dict[str, object] = {
        "description": f"{name} does things",
        "input_schema": {"type": "object", "properties": {}},
        "annotations": ToolAnnotations(read_only_hint=True),
    }
    return Tool(name=name, **{**defaults, **fields})


def test_tool_report_counts_and_flags(interop: ModuleType) -> None:
    tools = [
        tool("snapshot", output_schema={"type": "object"}),
        tool("click", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False)),
        tool("shell", annotations=ToolAnnotations(destructive_hint=True)),
    ]
    report = interop.tool_report(tools)
    assert report["count"] == 3
    assert report["names"] == ["snapshot", "click", "shell"]
    assert report["with_output_schema"] == ["snapshot"]
    assert report["read_only"] == ["snapshot"]
    assert report["destructive"] == ["shell"]
    assert report["problems"] == []


def test_tool_report_problems(interop: ModuleType) -> None:
    tools = [
        tool("a", description=""),
        tool("b", annotations=None),
        tool("c", input_schema={"type": "array"}),
        tool("d"),
        tool("d"),
    ]
    problems = interop.tool_report(tools)["problems"]
    assert "a: no description" in problems
    assert "b: no annotations" in problems
    assert any(p.startswith("c: inputSchema root type is 'array'") for p in problems)
    assert "d: listed more than once" in problems


def test_call_report(interop: ModuleType) -> None:
    result = CallToolResult(
        content=[
            TextContent(type="text", text="x" * 400),
            ImageContent(type="image", data="AA==", mime_type="image/png"),
        ],
        structured_content={"b": 1, "a": 2},
    )
    report = interop.call_report(result)
    assert report["is_error"] is False
    assert report["content_types"] == ["text", "image"]
    assert report["structured_type"] == "dict"
    assert report["structured_keys"] == ["a", "b"]
    assert len(report["text_excerpt"]) == 300


def test_call_arguments_must_be_a_json_object(interop: ModuleType) -> None:
    assert interop.parse_arguments_json(None) == {}
    assert interop.parse_arguments_json('{"app": "Notes"}') == {"app": "Notes"}
    with pytest.raises(interop.argparse.ArgumentTypeError):
        interop.parse_arguments_json("[1, 2]")


@pytest.mark.anyio
async def test_both_protocol_modes_against_a_stub_server(interop: ModuleType) -> None:
    report = await interop.run(args_for(interop, "--command", stub_command()))
    assert report["ok"] is True, report
    auto, legacy = report["modes"]
    assert (auto["mode"], legacy["mode"]) == ("auto", "legacy")
    assert auto["protocol_version"] == "2026-07-28"
    assert legacy["protocol_version"] != "2026-07-28"  # the initialize handshake era
    for mode in (auto, legacy):
        assert mode["server"] == {"name": "stub-axpilot", "version": "0.0.1"}
        assert mode["tools"]["names"] == ["status", "press"]
        assert mode["tools"]["problems"] == []
        assert mode["deterministic_order"] is True
        assert mode["call"]["tool"] == "status"
        assert mode["call"]["is_error"] is False
        assert mode["call"]["structured_keys"] == ["ok", "version"]


@pytest.mark.anyio
async def test_missing_annotations_are_reported(interop: ModuleType) -> None:
    report = await interop.run(
        args_for(interop, "--command", stub_command("--no-annotations"), "--modes", "auto")
    )
    (auto,) = report["modes"]
    assert "press: no annotations" in auto["tools"]["problems"]


@pytest.mark.anyio
async def test_call_of_an_unknown_tool_is_skipped(interop: ModuleType) -> None:
    argv = ["--command", stub_command(), "--modes", "legacy", "--call", "nope"]
    report = await interop.run(args_for(interop, *argv))
    assert report["modes"][0]["call"] == {"skipped": "server has no tool named 'nope'"}
    assert report["ok"] is True


@pytest.mark.anyio
async def test_a_server_that_cannot_start_is_a_failure(interop: ModuleType, tmp_path: Path) -> None:
    argv = ["--command", str(tmp_path / "no-such-server"), "--modes", "auto", "--timeout", "20"]
    report = await interop.run(interop.parse_args(["--no-save", *argv]))
    assert report["ok"] is False
    assert report["modes"][0]["error"]


def test_main_prints_a_summary(interop: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert interop.main(["--no-save", "--command", stub_command(), "--modes", "auto"]) == 0
    out = capsys.readouterr().out
    assert "[auto] protocol 2026-07-28" in out
    assert "overall: ok" in out


def test_main_writes_results(interop: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(interop, "SPIKE_DIR", tmp_path)
    assert interop.main(["--command", stub_command(), "--modes", "auto", "--label", "stub"]) == 0
    (written,) = (tmp_path / "results").glob("*-stub.json")
    assert written.read_text().count('"mode": "auto"') == 1


@pytest.mark.skipif(
    not os.environ.get("S6_REAL_CLI"), reason="set S6_REAL_CLI=1 to run against `axpilot stdio`"
)
def test_real_cli_with_the_fake_engine(interop: ModuleType) -> None:
    script = Path(sys.executable).parent / "axpilot"
    assert (
        interop.main(["--no-save", "--command", shlex.join([str(script), "stdio", "--engine", "fake"])]) == 0
    )
