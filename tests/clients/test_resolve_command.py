from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from axpilot.clients import find_axpilot_executable, resolve_command


def test_prefers_axpilot_on_path(home: Path, bin_dir: Path, make_executable: Callable[[Path], Path]) -> None:
    exe = make_executable(bin_dir / "axpilot")
    make_executable(home / ".local" / "bin" / "axpilot")
    assert resolve_command(argv0="") == (str(exe), ["stdio"])


def test_uses_argv0_when_it_is_the_axpilot_entry_point(
    home: Path, tmp_path: Path, make_executable: Callable[[Path], Path]
) -> None:
    exe = make_executable(tmp_path / "venv" / "bin" / "axpilot")
    assert resolve_command(argv0=str(exe)) == (str(exe), ["stdio"])


def test_ignores_argv0_of_other_programs(
    home: Path, tmp_path: Path, make_executable: Callable[[Path], Path]
) -> None:
    other = make_executable(tmp_path / "bin2" / "pytest")
    assert find_axpilot_executable(argv0=str(other)) is None


def test_ignores_non_executable_argv0(home: Path, tmp_path: Path) -> None:
    script = tmp_path / "axpilot"
    script.write_text("not executable")
    script.chmod(0o644)
    assert find_axpilot_executable(argv0=str(script)) is None


def test_falls_back_to_uv_tool_bin_dir(home: Path, make_executable: Callable[[Path], Path]) -> None:
    exe = make_executable(home / ".local" / "bin" / "axpilot")
    assert resolve_command(argv0="") == (str(exe), ["stdio"])


@pytest.mark.parametrize("variable", ["UV_TOOL_BIN_DIR", "XDG_BIN_HOME", "PIPX_BIN_DIR"])
def test_respects_tool_bin_dir_variables(
    variable: str,
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_executable: Callable[[Path], Path],
) -> None:
    exe = make_executable(tmp_path / "tools" / "axpilot")
    make_executable(home / ".local" / "bin" / "axpilot")
    monkeypatch.setenv(variable, str(tmp_path / "tools"))
    assert resolve_command(argv0="") == (str(exe), ["stdio"])


def test_falls_back_to_uvx_with_absolute_path(
    home: Path, bin_dir: Path, make_executable: Callable[[Path], Path]
) -> None:
    uvx = make_executable(bin_dir / "uvx")
    assert resolve_command(argv0="") == (str(uvx), ["axpilot", "stdio"])


def test_falls_back_to_bare_uvx(home: Path) -> None:
    assert resolve_command(argv0="") == ("uvx", ["axpilot", "stdio"])


def test_symlinked_executable_is_not_resolved(
    home: Path, tmp_path: Path, make_executable: Callable[[Path], Path]
) -> None:
    real = make_executable(tmp_path / "uv-tools" / "axpilot" / "bin" / "axpilot")
    link = home / ".local" / "bin" / "axpilot"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    assert resolve_command(argv0="")[0] == str(link)


def test_reads_home_at_call_time(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_executable: Callable[[Path], Path]
) -> None:
    assert find_axpilot_executable(argv0="") is None
    other_home = tmp_path / "second-home"
    exe = make_executable(other_home / ".local" / "bin" / "axpilot")
    monkeypatch.setenv("HOME", str(other_home))
    assert find_axpilot_executable(argv0="") == exe
