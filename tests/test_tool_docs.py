"""``docs/tools.md`` is generated from the tool registry: it must match what the generator writes
(run ``uv run python scripts/gen_tool_docs.py`` after changing a tool)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from axpilot.policy.profiles import TOOL_ORDER

ROOT = Path(__file__).resolve().parents[1]


def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_tool_docs", ROOT / "scripts" / "gen_tool_docs.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docs_tools_md_is_up_to_date() -> None:
    expected = generator().render()
    actual = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    assert actual == expected, "docs/tools.md is out of date: run `uv run python scripts/gen_tool_docs.py`."


def test_every_tool_is_documented() -> None:
    text = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    for name in TOOL_ORDER:
        assert f"### `{name}`:" in text, name


def test_check_mode() -> None:
    assert generator().main(["--check"]) == 0
