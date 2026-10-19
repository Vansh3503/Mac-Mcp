"""The examples: every automation script runs against the simulated desktop, and every framework
example is valid Python (the frameworks themselves are not installed here)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
SCRIPTS = sorted((EXAMPLES / "scripts").glob("*.py"))
FRAMEWORKS = sorted((EXAMPLES / "frameworks").glob("*.py"))
TYPESCRIPT = sorted((EXAMPLES / "frameworks" / "ts").glob("*.ts"))


def load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_examples_exist() -> None:
    assert len(SCRIPTS) >= 5
    assert {p.stem for p in FRAMEWORKS} == {
        f"{name}_example"
        for name in (
            "openai_agents",
            "langgraph",
            "crewai",
            "google_adk",
            "pydantic_ai",
            "llamaindex",
            "smolagents",
            "ms_agent_framework",
            "claude_agent_sdk",
        )
    }
    assert {p.stem for p in TYPESCRIPT} == {"ai_sdk_example", "mastra_example"}
    assert (EXAMPLES / "README.md").is_file()


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_scripts_run_on_the_fake_desktop(
    path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = load(path)
    args = ["--fake"]
    if "--out" in path.read_text(encoding="utf-8"):
        args += ["--out", str(tmp_path / "out")]
    assert module.main(args) == 0, capsys.readouterr().err
    output = capsys.readouterr().out
    assert output.strip()
    if (tmp_path / "out").exists():
        assert (tmp_path / "out").stat().st_size > 0


@pytest.mark.parametrize("path", [*FRAMEWORKS, *SCRIPTS], ids=lambda p: p.stem)
def test_examples_are_valid_python(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert ast.get_docstring(tree), "each example explains itself"


@pytest.mark.parametrize("path", FRAMEWORKS, ids=lambda p: p.stem)
def test_framework_examples_use_axpilot_stdio_and_no_real_model(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert '"axpilot"' in text
    assert '"stdio"' in text
    assert "<your-model>" in text


@pytest.mark.parametrize("path", TYPESCRIPT, ids=lambda p: p.stem)
def test_typescript_examples(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert 'command: "axpilot", args: ["stdio"]' in text
    assert "<your-model>" in text
    assert text.count("{") == text.count("}")


def test_script_errors_are_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    module = load(EXAMPLES / "scripts" / "fill_form.py")
    assert module.main(["--fake", "--app", "NoSuchApp"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: APP_NOT_FOUND")
    assert "Traceback" not in err
