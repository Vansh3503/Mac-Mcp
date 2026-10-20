"""The dedicated runtime: the virtual environment ``AXPilot.app`` starts the host from.

``axpilot setup`` creates it at :func:`axpilot.paths.runtime_dir` with a pinned ``axpilot`` (or a
local checkout/wheel for development builds) and writes ``runtime.json``
(:func:`axpilot.paths.runtime_manifest`), which tells the launcher what to spawn::

    {"version": 1, "python": "<runtime>/bin/python", "args": ["-m", "axpilot.host"],
     "env": {}, "axpilot_version": "0.3.0"}

The schema is the launcher's contract (``launcher/README.md``): ``version`` must be ``1``,
``python`` an absolute path, ``args`` a list of strings; ``env`` and ``cwd`` are optional and
unknown keys are ignored. The manifest is written with mode ``0600`` in a ``0700`` folder,
because whoever can edit it decides what runs with AXPilot's permissions.

Every external command goes through a :class:`~axpilot.system.runner.CommandRunner`, so tests
never install anything. uv is preferred; without it, ``python -m venv`` and pip are used.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import axpilot
from axpilot import paths
from axpilot.clients.fileops import atomic_write_text, display_path
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import CommandResult, CommandRunner, ensure_success

__all__ = [
    "HOST_ARGS",
    "MANIFEST_VERSION",
    "RuntimeInstall",
    "RuntimeManifest",
    "detect_dev_source",
    "ensure_runtime",
    "find_uv",
    "install_spec",
    "is_dev_version",
    "manifest_problems",
    "parse_manifest",
    "read_manifest",
    "runtime_python",
    "runtime_version",
    "venv_is_healthy",
    "write_manifest",
]

MANIFEST_VERSION = 1
HOST_ARGS: tuple[str, ...] = ("-m", "axpilot.host")

VENV_TIMEOUT_S = 300.0
INSTALL_TIMEOUT_S = 900.0
CHECK_TIMEOUT_S = 60.0
WARM_UP_TIMEOUT_S = 300.0

Installer = Literal["uv", "venv"]

_VERSION_PROBE = "import axpilot, sys; sys.stdout.write(axpilot.__version__)"
WARM_UP = (
    "import axpilot.host.daemon, axpilot.host.ui.app, axpilot.engine.macos.engine; "
    "from axpilot.server.app import load_tool_modules; load_tool_modules()"
)
"""What the host loads when it starts. macOS checks every new native library on its first load:
in a fresh runtime that took 13 s (macOS 26, M1 Pro; 1.4 s afterwards), longer than a client's
bridge waits for the host, so setup loads them once."""
_DEV_VERSION_RE = re.compile(r"\.dev\d*|\+")


# --------------------------------------------------------------------------------------------
# Manifest


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """The contents of ``runtime.json``."""

    python: str
    args: tuple[str, ...] = HOST_ARGS
    env: dict[str, str] = field(default_factory=dict)
    axpilot_version: str | None = None
    cwd: str | None = None
    version: int = MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": self.version,
            "python": self.python,
            "args": list(self.args),
            "env": dict(self.env),
        }
        if self.axpilot_version is not None:
            data["axpilot_version"] = self.axpilot_version
        if self.cwd is not None:
            data["cwd"] = self.cwd
        return data


def _bad_manifest(problem: str, source: Path | None) -> AXPilotError:
    where = display_path(source) if source else "runtime.json"
    return AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"{where} is invalid: {problem}.",
        hint="Run `axpilot setup` to write it again.",
    )


def _string_map(value: Any, key: str, source: Path | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise _bad_manifest(f"`{key}` must be an object of strings", source)
    return dict(value)


def parse_manifest(data: Any, *, source: Path | None = None) -> RuntimeManifest:
    """Validate decoded ``runtime.json`` data with the launcher's rules. Unknown keys are ignored.

    Raises ``INVALID_ARGUMENT`` describing the first problem.
    """
    if not isinstance(data, dict):
        raise _bad_manifest("the top level must be an object", source)
    version = data.get("version")
    if isinstance(version, bool) or version != MANIFEST_VERSION:
        raise _bad_manifest(f"`version` must be {MANIFEST_VERSION}, not {version!r}", source)
    python = data.get("python")
    if not isinstance(python, str) or not python or not Path(python).is_absolute():
        raise _bad_manifest("`python` must be an absolute path", source)
    args = data.get("args")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise _bad_manifest("`args` must be a list of strings", source)
    cwd = data.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute()):
        raise _bad_manifest("`cwd` must be null or an absolute path", source)
    axpilot_version = data.get("axpilot_version")
    if axpilot_version is not None and not isinstance(axpilot_version, str):
        raise _bad_manifest("`axpilot_version` must be a string", source)
    return RuntimeManifest(
        python=python,
        args=tuple(args),
        env=_string_map(data.get("env"), "env", source),
        axpilot_version=axpilot_version,
        cwd=cwd,
    )


def read_manifest(path: Path | None = None) -> RuntimeManifest:
    """Read and validate ``runtime.json``.

    Raises ``HOST_UNAVAILABLE`` if it does not exist (AXPilot is not set up), ``INVALID_ARGUMENT``
    if it is malformed and ``COMMAND_FAILED`` if it cannot be read.
    """
    target = path or paths.runtime_manifest()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AXPilotError(
            ErrorCode.HOST_UNAVAILABLE,
            f"No runtime manifest at {display_path(target)}: AXPilot is not set up for launcher mode.",
            hint="Run `axpilot setup`.",
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED,
            f"Could not read {display_path(target)}: {exc}.",
            hint="Run `axpilot setup` to write it again.",
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _bad_manifest(f"not valid JSON ({exc.msg} at line {exc.lineno})", target) from exc
    return parse_manifest(data, source=target)


def _ensure_private_dir(directory: Path) -> None:
    """Create ``directory`` (mode 0700) or tighten it to owner-only access."""
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(directory.stat().st_mode) & 0o077:
            directory.chmod(0o700)
    except OSError as exc:
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED,
            f"Could not prepare {display_path(directory)}: {exc.strerror or exc}.",
            hint="Check that you own this folder.",
        ) from exc


def write_manifest(
    python: Path,
    *,
    env: Mapping[str, str] | None = None,
    axpilot_version: str | None = axpilot.__version__,
    cwd: Path | None = None,
    path: Path | None = None,
) -> RuntimeManifest:
    """Write ``runtime.json`` atomically (mode 0600, folder 0700) and return what was written.

    ``python`` must be an absolute path; it is not required to exist yet.
    """
    manifest = parse_manifest(
        RuntimeManifest(
            python=str(python),
            env=dict(env or {}),
            axpilot_version=axpilot_version,
            cwd=str(cwd) if cwd is not None else None,
        ).to_dict()
    )
    target = path or paths.runtime_manifest()
    _ensure_private_dir(target.parent)
    atomic_write_text(target, json.dumps(manifest.to_dict(), indent=2) + "\n", mode=0o600)
    return manifest


def manifest_problems(manifest: RuntimeManifest) -> list[str]:
    """Reasons the launcher could not start the host from ``manifest`` (empty if none)."""
    problems: list[str] = []
    python = Path(manifest.python)
    if not python.exists():
        problems.append(f"the interpreter {manifest.python} does not exist")
    elif not python.is_file() or not os.access(python, os.X_OK):
        problems.append(f"the interpreter {manifest.python} is not an executable file")
    if manifest.cwd is not None and not Path(manifest.cwd).is_dir():
        problems.append(f"the working folder {manifest.cwd} does not exist")
    return problems


# --------------------------------------------------------------------------------------------
# Installation


@dataclass(frozen=True, slots=True)
class RuntimeInstall:
    """The outcome of :func:`ensure_runtime`."""

    runtime_dir: Path
    python: Path
    installer: Installer
    spec: str
    """What was installed: ``axpilot==<version>`` or a local path."""
    axpilot_version: str
    """The version the runtime reports after installation."""
    created: bool
    """True if the virtual environment was (re)created, False if an existing one was updated."""
    commands: tuple[tuple[str, ...], ...] = ()


def runtime_python(runtime_dir: Path | None = None) -> Path:
    """The interpreter inside the runtime (``<runtime>/bin/python``)."""
    return (runtime_dir or paths.runtime_dir()) / "bin" / "python"


def venv_is_healthy(runtime_dir: Path | None = None) -> bool:
    """True if ``runtime_dir`` is a virtual environment with an executable interpreter."""
    root = runtime_dir or paths.runtime_dir()
    python = runtime_python(root)
    return (root / "pyvenv.cfg").is_file() and python.is_file() and os.access(python, os.X_OK)


def is_dev_version(version: str) -> bool:
    """True for versions that are never published (``.devN`` or a ``+local`` part)."""
    return _DEV_VERSION_RE.search(version) is not None


def detect_dev_source() -> Path | None:
    """The source checkout this ``axpilot`` was imported from, if it is one (editable installs)."""
    package_dir = Path(axpilot.__file__).resolve().parent
    for candidate in (package_dir.parent.parent, package_dir.parent):
        pyproject = candidate / "pyproject.toml"
        try:
            project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        if isinstance(project, dict) and project.get("name") == "axpilot":
            return candidate
    return None


def install_spec(version: str = axpilot.__version__, source: Path | None = None) -> str:
    """The requirement to install: ``source`` (a checkout or wheel) or ``axpilot==version``.

    Raises ``INVALID_ARGUMENT`` if ``source`` is not a project folder or wheel, or if ``version``
    is a development version (which is not on PyPI) and no ``source`` is given.
    """
    if source is not None:
        resolved = Path(source).expanduser().resolve()
        if resolved.is_dir() and (resolved / "pyproject.toml").is_file():
            return str(resolved)
        if resolved.is_file() and resolved.suffix == ".whl":
            return str(resolved)
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{display_path(resolved)} is neither an AXPilot source folder (with pyproject.toml) "
            "nor a wheel.",
            hint="Pass the folder of your AXPilot checkout or a built .whl file.",
        )
    if is_dev_version(version):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"This is a development build of AXPilot ({version}), which is not published on PyPI.",
            hint="Run setup with the path of your source checkout (`axpilot setup --dev-source .`).",
        )
    return f"axpilot=={version}"


#: Homebrew's uv on Apple silicon and Intel; tests replace this to stay hermetic.
SYSTEM_UV_PATHS: tuple[Path, ...] = (Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv"))


def find_uv() -> Path | None:
    """Locate the ``uv`` executable: ``$UV`` (set by uv itself), ``PATH``, then usual folders."""
    candidates: list[Path] = []
    if env_uv := os.environ.get("UV"):
        candidates.append(Path(env_uv).expanduser())
    if found := shutil.which("uv"):
        candidates.append(Path(found))
    home = Path.home()
    candidates += [home / ".local" / "bin" / "uv", home / ".cargo" / "bin" / "uv", *SYSTEM_UV_PATHS]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.absolute()
    return None


def _python_tag() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _reset_runtime(runtime_dir: Path) -> None:
    """Remove a broken runtime so it can be recreated, refusing to delete anything else."""
    if not runtime_dir.exists() and not runtime_dir.is_symlink():
        return
    try:
        if runtime_dir.is_dir() and not runtime_dir.is_symlink():
            if (runtime_dir / "pyvenv.cfg").is_file():
                shutil.rmtree(runtime_dir)
                return
            if not any(runtime_dir.iterdir()):
                runtime_dir.rmdir()
                return
    except OSError as exc:
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED,
            f"Could not remove the broken runtime at {display_path(runtime_dir)}: {exc.strerror or exc}.",
            hint="Delete that folder yourself, then run `axpilot setup` again.",
        ) from exc
    raise AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"{display_path(runtime_dir)} exists but is not a virtual environment, so setup will not touch it.",
        hint="Move it away (or set AXPILOT_HOME to another folder), then run `axpilot setup` again.",
    )


def _run(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    what: str,
    timeout_s: float,
    cwd: Path,
    log: list[tuple[str, ...]],
) -> CommandResult:
    log.append(tuple(argv))
    result = runner.run(argv, timeout_s=timeout_s, cwd=str(cwd))
    return ensure_success(
        result,
        what=what,
        hint="Check your network connection and the output above, then run `axpilot setup` again.",
        timeout_s=timeout_s,
        missing_hint="Install uv (https://docs.astral.sh/uv/) or make sure Python's venv module works.",
    )


def runtime_version(runner: CommandRunner, python: Path, *, cwd: Path | None = None) -> str | None:
    """The ``axpilot`` version importable by ``python`` (``None`` if it cannot be determined)."""
    result = runner.run(
        [str(python), "-I", "-c", _VERSION_PROBE],
        timeout_s=CHECK_TIMEOUT_S,
        cwd=str(cwd or python.parent),
    )
    version = result.stdout.strip()
    return version if result.ok and version else None


def ensure_runtime(
    runner: CommandRunner,
    *,
    source: Path | None = None,
    version: str = axpilot.__version__,
    runtime_dir: Path | None = None,
    use_uv: bool = True,
    uv: Path | None = None,
    base_python: str = sys.executable,
) -> RuntimeInstall:
    """Create or update the runtime virtual environment and install AXPilot into it.

    With uv (``uv`` or :func:`find_uv`; skipped if ``use_uv`` is false): ``uv venv --python
    <major.minor>`` and ``uv pip install --compile-bytecode --python <runtime>/bin/python <spec>``. Otherwise
    ``<base_python> -m venv`` and ``pip install``. A healthy existing environment is reused; a
    broken one is recreated. Installing a local ``source`` always reinstalls the package. Finally
    the runtime loads the host once (:data:`WARM_UP`).

    Raises ``INVALID_ARGUMENT`` for a bad ``source``/``version``, ``COMMAND_FAILED``/``TIMEOUT``
    if a command fails, and ``COMMAND_FAILED`` if the installed version is not the requested one.
    """
    spec = install_spec(version, source)
    local = source is not None
    root = runtime_dir or paths.runtime_dir()
    python = runtime_python(root)
    uv_path = (uv or find_uv()) if use_uv else None
    installer: Installer = "uv" if uv_path is not None else "venv"
    commands: list[tuple[str, ...]] = []

    _ensure_private_dir(root.parent)
    created = not venv_is_healthy(root)
    if created:
        _reset_runtime(root)
        if uv_path is not None:
            create = [str(uv_path), "venv", "--python", _python_tag(), str(root)]
        else:
            create = [base_python, "-m", "venv", str(root)]
        _run(
            runner,
            create,
            what="Creating the AXPilot runtime",
            timeout_s=VENV_TIMEOUT_S,
            cwd=root.parent,
            log=commands,
        )

    if uv_path is not None:
        # uv skips bytecode by default (pip compiles it); without it, the host's first start took 21 s
        # (macOS 26), longer than the bridge waits for it.
        install = [str(uv_path), "pip", "install", "--compile-bytecode", "--python", str(python), spec]
        if local:
            install += ["--reinstall-package", "axpilot"]
    else:
        install = [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--upgrade",
            spec,
        ]
        if local:
            install.append("--force-reinstall")
    _run(
        runner,
        install,
        what="Installing AXPilot into the runtime",
        timeout_s=INSTALL_TIMEOUT_S,
        cwd=root.parent,
        log=commands,
    )

    commands.append((str(python), "-I", "-c", _VERSION_PROBE))
    installed = runtime_version(runner, python, cwd=root.parent)
    if installed is None:
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED,
            f"The runtime at {display_path(root)} cannot import axpilot after installation.",
            hint="Run `axpilot setup` again; if it keeps failing, delete that folder first.",
        )
    if not local and installed != version:
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED,
            f"The runtime has axpilot {installed}, but {version} was requested.",
            hint="Run `axpilot setup` again; if it keeps failing, delete the runtime folder first.",
        )
    _run(
        runner,
        [str(python), "-I", "-c", WARM_UP],
        what="Loading the AXPilot host in the runtime",
        timeout_s=WARM_UP_TIMEOUT_S,
        cwd=root.parent,
        log=commands,
    )
    return RuntimeInstall(root, python, installer, spec, installed, created, tuple(commands))
