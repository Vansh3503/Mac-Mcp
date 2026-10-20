from __future__ import annotations

import importlib.util
import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import axpilot
from axpilot import paths
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.setup import runtime
from axpilot.setup.runtime import (
    HOST_ARGS,
    RuntimeManifest,
    detect_dev_source,
    ensure_runtime,
    find_uv,
    install_spec,
    is_dev_version,
    manifest_problems,
    parse_manifest,
    read_manifest,
    runtime_python,
    venv_is_healthy,
    write_manifest,
)

PY_TAG = f"{sys.version_info.major}.{sys.version_info.minor}"


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def _release_build(runner: Any) -> None:
    """The simulated runtime reports the release these tests install (0.3.0)."""
    runner.version = "0.3.0"


@pytest.fixture
def uv(env: Path, make_executable: Callable[[Path], Path]) -> Path:
    return make_executable(env / ".local" / "bin" / "uv")


# --------------------------------------------------------------------------------------------
# Versions and specs


@pytest.mark.parametrize(
    ("version", "dev"),
    [
        ("0.1.0", False),
        ("1.2.3rc1", False),
        ("0.1.0.dev0", True),
        ("0.1.0.dev3+g1234", True),
        ("1.0+local", True),
    ],
)
def test_is_dev_version(version: str, dev: bool) -> None:
    assert is_dev_version(version) is dev


def test_install_spec_pins_release_versions() -> None:
    assert install_spec("0.3.0") == "axpilot==0.3.0"


def test_install_spec_refuses_unpublished_dev_versions() -> None:
    with pytest.raises(AXPilotError) as info:
        install_spec("0.1.0.dev0")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "--dev-source" in (info.value.hint or "")


def test_install_spec_accepts_checkouts_and_wheels(tmp_path: Path) -> None:
    checkout = tmp_path / "src"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "axpilot"\n')
    wheel = tmp_path / "axpilot-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"PK")
    assert install_spec("0.1.0.dev0", checkout) == str(checkout.resolve())
    assert install_spec("0.1.0", wheel) == str(wheel.resolve())


@pytest.mark.parametrize("name", ["empty-dir", "notes.txt"])
def test_install_spec_rejects_other_sources(tmp_path: Path, name: str) -> None:
    source = tmp_path / name
    if name.endswith(".txt"):
        source.write_text("x")
    else:
        source.mkdir()
    with pytest.raises(AXPilotError) as info:
        install_spec("0.1.0", source)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_detect_dev_source_finds_this_checkout() -> None:
    source = detect_dev_source()
    if source is None:  # pragma: no cover - only when tests run against an installed wheel
        pytest.skip("axpilot is not imported from a source checkout")
    assert (source / "pyproject.toml").is_file()
    assert (source / "src" / "axpilot" / "__init__.py").is_file()


def test_find_uv_prefers_uv_variable(
    env: Path, tmp_path: Path, make_executable: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert find_uv() is None
    homebrew = make_executable(tmp_path / "homebrew" / "bin" / "uv")  # the env fixture's SYSTEM_UV_PATHS
    assert find_uv() == homebrew
    fallback = make_executable(env / ".cargo" / "bin" / "uv")
    assert find_uv() == fallback
    explicit = make_executable(tmp_path / "custom" / "uv")
    monkeypatch.setenv("UV", str(explicit))
    assert find_uv() == explicit


# --------------------------------------------------------------------------------------------
# ensure_runtime


def test_creates_runtime_with_uv(env: Path, uv: Path, runner: Any) -> None:
    result = ensure_runtime(runner, version="0.3.0")
    root = paths.runtime_dir()
    python = root / "bin" / "python"
    assert [call.argv for call in runner.calls] == [
        [str(uv), "venv", "--python", PY_TAG, str(root)],
        [str(uv), "pip", "install", "--compile-bytecode", "--python", str(python), "axpilot==0.3.0"],
        [str(python), "-I", "-c", runtime._VERSION_PROBE],
        [str(python), "-I", "-c", runtime.WARM_UP],
    ]
    assert all(call.cwd == str(root.parent) for call in runner.calls)
    assert result.created
    assert result.installer == "uv"
    assert result.python == python == runtime_python()
    assert result.axpilot_version == "0.3.0"
    assert result.commands == tuple(tuple(call.argv) for call in runner.calls)
    assert mode_of(root.parent) == 0o700


def test_reuses_a_healthy_runtime(env: Path, uv: Path, runner: Any) -> None:
    ensure_runtime(runner, version="0.3.0")
    runner.calls.clear()
    result = ensure_runtime(runner, version="0.3.0")
    assert not result.created
    assert runner.kinds() == ["install", "probe", "warm_up"]


def test_local_source_is_reinstalled(env: Path, uv: Path, runner: Any, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname = 'axpilot'\n")
    runner.version = "0.1.0.dev0"
    result = ensure_runtime(runner, source=checkout, version="0.1.0.dev0")
    install = runner.calls[1].argv
    assert install[-3:] == [str(checkout.resolve()), "--reinstall-package", "axpilot"]
    assert result.spec == str(checkout.resolve())


def test_without_uv_uses_venv_and_pip(env: Path, runner: Any) -> None:
    result = ensure_runtime(runner, version="0.3.0", base_python="/usr/bin/python3")
    root = paths.runtime_dir()
    python = str(root / "bin" / "python")
    assert runner.calls[0].argv == ["/usr/bin/python3", "-m", "venv", str(root)]
    assert runner.calls[1].argv == [
        *[python, "-m", "pip", "install", "--disable-pip-version-check"],
        *["--no-input", "--upgrade", "axpilot==0.3.0"],
    ]
    assert result.installer == "venv"


def test_use_uv_false_ignores_uv(env: Path, uv: Path, runner: Any) -> None:
    ensure_runtime(runner, version="0.3.0", use_uv=False, base_python="/usr/bin/python3")
    assert runner.calls[0].argv[0] == "/usr/bin/python3"


def test_broken_venv_is_recreated(env: Path, uv: Path, runner: Any) -> None:
    root = paths.runtime_dir()
    root.mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = /gone\n")
    (root / "stale.txt").write_text("x")
    result = ensure_runtime(runner, version="0.3.0")
    assert result.created
    assert not (root / "stale.txt").exists()


def test_refuses_to_delete_a_folder_that_is_not_a_venv(env: Path, uv: Path, runner: Any) -> None:
    root = paths.runtime_dir()
    root.mkdir(parents=True)
    (root / "important.txt").write_text("keep me")
    with pytest.raises(AXPilotError) as info:
        ensure_runtime(runner, version="0.3.0")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert (root / "important.txt").read_text() == "keep me"
    assert runner.calls == []


@pytest.mark.parametrize(
    ("kind", "code", "error"),
    [
        ("venv", 1, ErrorCode.COMMAND_FAILED),
        ("install", 1, ErrorCode.COMMAND_FAILED),
        ("install", 127, ErrorCode.NOT_SUPPORTED),
    ],
)
@pytest.mark.usefixtures("env", "uv")
def test_command_failures(runner: Any, kind: str, code: int, error: ErrorCode) -> None:
    runner.failures[kind] = (code, "error: No solution found when resolving dependencies")
    with pytest.raises(AXPilotError) as info:
        ensure_runtime(runner, version="0.3.0")
    assert info.value.code is error


def test_runtime_that_cannot_import_axpilot(env: Path, uv: Path, runner: Any) -> None:
    runner.failures["probe"] = (1, "ModuleNotFoundError: No module named 'axpilot'")
    with pytest.raises(AXPilotError, match="cannot import axpilot"):
        ensure_runtime(runner, version="0.3.0")


def test_runtime_that_cannot_load_the_host(env: Path, uv: Path, runner: Any) -> None:
    runner.failures["warm_up"] = (1, "ImportError: dlopen(_AppKit.cpython-312-darwin.so): image not found")
    with pytest.raises(AXPilotError, match="Loading the AXPilot host") as caught:
        ensure_runtime(runner, version="0.3.0")
    assert caught.value.code is ErrorCode.COMMAND_FAILED


def test_wrong_installed_version(env: Path, uv: Path, runner: Any) -> None:
    runner.version = "0.2.9"
    with pytest.raises(AXPilotError, match=r"has axpilot 0\.2\.9, but 0\.3\.0 was requested"):
        ensure_runtime(runner, version="0.3.0")


def test_healthy_check(env: Path, make_executable: Callable[[Path], Path]) -> None:
    root = paths.runtime_dir()
    assert not venv_is_healthy(root)
    make_executable(root / "bin" / "python")
    assert not venv_is_healthy(root)
    (root / "pyvenv.cfg").write_text("")
    assert venv_is_healthy(root)


# --------------------------------------------------------------------------------------------
# Manifest


def test_write_and_read_manifest(env: Path) -> None:
    python = paths.runtime_dir() / "bin" / "python"
    written = write_manifest(python, env={"AXPILOT_TOOLS_PROFILE": "minimal"}, axpilot_version="0.3.0")
    path = paths.runtime_manifest()
    assert json.loads(path.read_text()) == {
        "version": 1,
        "python": str(python),
        "args": ["-m", "axpilot.host"],
        "env": {"AXPILOT_TOOLS_PROFILE": "minimal"},
        "axpilot_version": "0.3.0",
    }
    assert mode_of(path) == 0o600
    assert mode_of(path.parent) == 0o700
    assert read_manifest() == written
    assert written.args == HOST_ARGS


def test_write_manifest_tightens_an_open_folder(env: Path) -> None:
    folder = paths.app_support_dir()
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    write_manifest(Path("/x/bin/python"))
    assert mode_of(folder) == 0o700


def test_write_manifest_defaults_and_cwd(env: Path) -> None:
    manifest = write_manifest(Path("/x/bin/python"), cwd=Path("/tmp"))
    assert manifest.axpilot_version == axpilot.__version__
    assert json.loads(paths.runtime_manifest().read_text())["cwd"] == "/tmp"


def test_write_manifest_requires_absolute_python(env: Path) -> None:
    with pytest.raises(AXPilotError):
        write_manifest(Path("bin/python"))


def test_read_missing_manifest(env: Path) -> None:
    with pytest.raises(AXPilotError) as info:
        read_manifest()
    assert info.value.code is ErrorCode.HOST_UNAVAILABLE
    assert "axpilot setup" in (info.value.hint or "")


def test_read_manifest_with_bad_json(env: Path) -> None:
    path = paths.runtime_manifest()
    path.parent.mkdir(parents=True)
    path.write_text("{nope")
    with pytest.raises(AXPilotError) as info:
        read_manifest()
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "not valid JSON" in info.value.message


VALID = {"version": 1, "python": "/r/bin/python", "args": ["-m", "axpilot.host"]}


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"version": 2}, "`version` must be 1"),
        ({"version": True}, "`version` must be 1"),
        ({"version": "1"}, "`version` must be 1"),
        ({"python": "bin/python"}, "`python` must be an absolute path"),
        ({"python": ""}, "`python`"),
        ({"python": None}, "`python`"),
        ({"args": "-m axpilot.host"}, "`args` must be a list"),
        ({"args": ["-m", 1]}, "`args` must be a list"),
        ({"env": {"A": 1}}, "`env` must be an object of strings"),
        ({"env": ["A"]}, "`env`"),
        ({"cwd": "relative"}, "`cwd`"),
        ({"axpilot_version": 3}, "`axpilot_version`"),
    ],
)
def test_parse_manifest_rejects(change: dict[str, Any], fragment: str) -> None:
    with pytest.raises(AXPilotError) as info:
        parse_manifest({**VALID, **change})
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert fragment in info.value.message


def test_parse_manifest_rejects_non_objects() -> None:
    with pytest.raises(AXPilotError, match="top level"):
        parse_manifest([VALID])


def test_parse_manifest_ignores_unknown_keys_and_accepts_launcher_examples() -> None:
    manifest = parse_manifest({**VALID, "env": {"A": "b"}, "cwd": None, "future": {"x": 1}})
    assert manifest == RuntimeManifest(python="/r/bin/python", env={"A": "b"})


def test_manifest_problems(tmp_path: Path, make_executable: Callable[[Path], Path]) -> None:
    python = tmp_path / "bin" / "python"
    assert manifest_problems(RuntimeManifest(python=str(python))) == [
        f"the interpreter {python} does not exist"
    ]
    python.parent.mkdir()
    python.write_text("")
    python.chmod(0o644)
    assert "not an executable file" in manifest_problems(RuntimeManifest(python=str(python)))[0]
    make_executable(python)
    assert manifest_problems(RuntimeManifest(python=str(python))) == []
    gone = str(tmp_path / "gone")
    assert manifest_problems(RuntimeManifest(python=str(python), cwd=gone)) == [
        f"the working folder {gone} does not exist"
    ]


def test_the_host_args_name_a_runnable_module() -> None:
    """``python -m <module>`` needs a module, or a package with ``__main__``: the launcher
    starts the host with exactly these arguments."""
    assert HOST_ARGS[0] == "-m"
    spec = importlib.util.find_spec(HOST_ARGS[1])
    assert spec is not None
    if spec.submodule_search_locations is not None:  # a package
        assert importlib.util.find_spec(f"{HOST_ARGS[1]}.__main__") is not None
