"""The spike kit as a whole: layout, shell-script syntax, Python 3.9 syntax where promised."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SPIKES = Path(__file__).resolve().parents[1]
REPO = SPIKES.parent
SPIKE_DIRS = (
    "s1_launcher_tcc",
    "s2_ax_perf",
    "s3_input",
    "s4_capture",
    "s5_ci_permissions",
    "s6_mcp_interop",
)
SHELL_SCRIPTS = sorted([*SPIKES.rglob("*.sh"), *(REPO / "launcher").glob("*.sh")])
# Promised to run on any Python 3.9+, including the Command Line Tools' /usr/bin/python3.
PY39_SCRIPTS = (
    "spikelib.py",
    "s1_launcher_tcc/probe.py",
    "s1_launcher_tcc/install.py",
    "s3_input/layout_map.py",
    "s5_ci_permissions/grant_tcc.py",
)
ENTRY_POINTS = (
    "s1_launcher_tcc/probe.py",
    "s1_launcher_tcc/install.py",
    "s1_launcher_tcc/run_s1.sh",
    "s1_launcher_tcc/open_from_client.sh",
    "s2_ax_perf/ax_perf.py",
    "s3_input/strategies.py",
    "s3_input/hid_idle.py",
    "s3_input/layout_map.py",
    "s4_capture/capture_probe.py",
    "s5_ci_permissions/grant_tcc.py",
    "s6_mcp_interop/interop.py",
    "s6_mcp_interop/frameworks/agents_sdk_smoke.py",
    "s6_mcp_interop/frameworks/langchain_smoke.py",
)

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.mark.parametrize("name", SPIKE_DIRS)
def test_every_spike_has_a_readme_and_a_results_dir(name: str) -> None:
    assert (SPIKES / name / "README.md").is_file()
    assert (SPIKES / name / "results" / ".gitkeep").is_file()


def test_results_template_exists() -> None:
    template = (REPO / "docs" / "spikes" / "RESULTS_TEMPLATE.md").read_text(encoding="utf-8")
    assert "Go / no-go for ADR-2" in template


@pytest.mark.parametrize("relative", ENTRY_POINTS)
def test_entry_points_are_executable_scripts(relative: str) -> None:
    path = SPIKES / relative
    assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env ")
    assert os.access(path, os.X_OK), f"chmod +x {relative}"


@pytest.mark.parametrize("relative", PY39_SCRIPTS)
def test_stdlib_scripts_parse_as_python_3_9(relative: str) -> None:
    ast.parse((SPIKES / relative).read_text(encoding="utf-8"), filename=relative, feature_version=(3, 9))


@pytest.mark.parametrize("path", SHELL_SCRIPTS, ids=lambda p: str(p.relative_to(REPO)))
@needs_bash
def test_shell_scripts_parse(path: Path) -> None:
    subprocess.run(["bash", "-n", str(path)], check=True, timeout=30)


@needs_bash
def test_run_s1_help_and_argument_errors() -> None:
    script = SPIKES / "s1_launcher_tcc" / "run_s1.sh"
    shown = subprocess.run(
        ["bash", str(script), "--help"], capture_output=True, text=True, timeout=30, check=False
    )
    assert shown.returncode == 0
    assert "--wait-for-trust" in shown.stdout
    unknown = subprocess.run(
        ["bash", str(script), "--bogus"], capture_output=True, text=True, timeout=30, check=False
    )
    assert unknown.returncode == 1
    assert "unknown option" in unknown.stderr
    missing = subprocess.run(
        ["bash", str(script), "--label"], capture_output=True, text=True, timeout=30, check=False
    )
    assert missing.returncode == 1
    assert "needs a value" in missing.stderr


@needs_bash
@pytest.mark.skipif(sys.platform == "darwin", reason="checks the refusal off macOS")
def test_run_s1_refuses_to_run_off_macos() -> None:
    script = SPIKES / "s1_launcher_tcc" / "run_s1.sh"
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 1
    assert "runs on macOS" in result.stderr


@needs_bash
def test_open_from_client_never_writes_to_stdout() -> None:
    """An MCP client reads stdout as protocol, so even errors must go to stderr."""
    script = SPIKES / "s1_launcher_tcc" / "open_from_client.sh"
    result = subprocess.run(
        ["bash", str(script), "bad label!"], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "invalid client label" in result.stderr


def test_workflow_template_is_valid_yaml_and_inactive() -> None:
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load((SPIKES / "s5_ci_permissions" / "workflow.yml").read_text(encoding="utf-8"))
    assert set(workflow["jobs"]) == {"python-direct", "setup-a11y", "launcher"}
    assert not (REPO / ".github" / "workflows" / "spike-s5.yml").exists()
