"""Install, verify and start the frozen ``AXPilot.app`` launcher.

Release wheels carry the launcher at ``axpilot/_launcher/AXPilot.app`` (built once per launcher
version, ad-hoc signed; see ``launcher/README.md``). ``axpilot setup`` copies it **byte for
byte** to :func:`axpilot.paths.launcher_app_path` (``~/Applications/AXPilot.app``): macOS pins
the user's Accessibility grant to the bundle's cdhash, so the copy must be exact, including the
executable bit. :func:`install_launcher` therefore verifies every file's SHA-256 and mode after
copying, and :func:`launcher_status` lets ``axpilot doctor`` re-check them later.

``_launcher/CDHASH`` (optional) records the published cdhash per architecture, one per line::

    arm64 0123abcd...
    x86_64 4567ef01...

Gatekeeper only assesses files carrying the ``com.apple.quarantine`` attribute, which package
managers do not set. :func:`quarantined_paths` reports it if it is there anyway.

Copying and hashing work on any OS (tests use a fake bundle on Linux); starting the app,
reading code signatures and login items are macOS features.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import logging
import os
import platform
import plistlib
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from axpilot import BUNDLE_ID, paths
from axpilot.clients.fileops import atomic_write, display_path
from axpilot.errors import AXPilotError, ErrorCode
from axpilot.system.runner import CommandRunner, ensure_success

__all__ = [
    "APP_BUNDLE_NAME",
    "LOGIN_AGENT_LABEL",
    "QUARANTINE_XATTR",
    "LauncherInstall",
    "LauncherStatus",
    "bundled_app",
    "bundled_dir",
    "current_arch",
    "executable_path",
    "expected_cdhashes",
    "file_sha256",
    "has_xattr",
    "install_launcher",
    "install_login_item",
    "installed_cdhash",
    "installed_version",
    "is_installed",
    "launch",
    "launcher_status",
    "login_agent_path",
    "login_agent_plist",
    "login_item_installed",
    "parse_cdhash_file",
    "quarantined_paths",
    "remove_login_item",
    "tree_manifest",
]

logger = logging.getLogger(__name__)

APP_BUNDLE_NAME = "AXPilot.app"
_EXECUTABLE = ("Contents", "MacOS", "AXPilot")
_INFO_PLIST = ("Contents", "Info.plist")
CDHASH_FILE = "CDHASH"
QUARANTINE_XATTR = "com.apple.quarantine"
LOGIN_AGENT_LABEL = f"{BUNDLE_ID}.login"

_CDHASH_RE = re.compile(r"[0-9a-f]{40,64}")
_CODESIGN_CDHASH_RE = re.compile(r"^CDHash=([0-9A-Fa-f]+)\s*$", re.MULTILINE)
_LAUNCH_TIMEOUT_S = 30.0
_CODESIGN_TIMEOUT_S = 30.0
_DARWIN_ENOATTR = 93  # errno for a missing extended attribute on macOS
_XATTR_NOFOLLOW = 0x0001

# --------------------------------------------------------------------------------------------
# Locations and contents


def bundled_dir() -> Path | None:
    """The package's ``_launcher`` folder, or ``None`` (development installs have none)."""
    candidate = Path(str(resources.files("axpilot") / "_launcher"))
    return candidate if candidate.is_dir() else None


def bundled_app() -> Path | None:
    """The ``AXPilot.app`` shipped inside this package, or ``None`` if the build has none."""
    folder = bundled_dir()
    if folder is None:
        return None
    app = folder / APP_BUNDLE_NAME
    return app if app.is_dir() else None


def executable_path(app: Path) -> Path:
    """The launcher binary inside a bundle (``Contents/MacOS/AXPilot``)."""
    return app.joinpath(*_EXECUTABLE)


def file_sha256(path: Path) -> str:
    """Hex SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_manifest(root: Path) -> dict[str, tuple[str, ...]]:
    """Describe every entry under ``root``: kind, permission bits and content hash/link target.

    Two bundles with equal manifests are byte-identical, with identical modes. Symlinks are
    recorded, never followed.
    """
    entries: dict[str, tuple[str, ...]] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        folder = Path(dirpath)
        entries[folder.relative_to(root).as_posix()] = ("dir", oct(stat.S_IMODE(folder.lstat().st_mode)))
        for name in (*dirnames, *filenames):
            path = folder / name
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                entries[rel] = ("link", os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                entries[rel] = ("file", oct(stat.S_IMODE(info.st_mode)), file_sha256(path))
            elif not stat.S_ISDIR(info.st_mode):
                entries[rel] = ("other", oct(stat.S_IMODE(info.st_mode)))
    return entries


def _bundle_version(app: Path) -> str | None:
    try:
        with app.joinpath(*_INFO_PLIST).open("rb") as fh:
            info: Any = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    version = info.get("CFBundleShortVersionString") if isinstance(info, dict) else None
    return version if isinstance(version, str) else None


def is_installed(app: Path | None = None) -> bool:
    """True if the launcher bundle exists with an executable ``Contents/MacOS/AXPilot``."""
    exe = executable_path(app or paths.launcher_app_path())
    return exe.is_file() and os.access(exe, os.X_OK)


def installed_version(app: Path | None = None) -> str | None:
    """``CFBundleShortVersionString`` of the installed launcher, or ``None``."""
    target = app or paths.launcher_app_path()
    return _bundle_version(target) if is_installed(target) else None


def parse_cdhash_file(text: str) -> dict[str, str]:
    """Parse a ``CDHASH`` file into ``{arch: cdhash}`` (lower-case hex).

    Lines are ``<arch> <hash>``, ``<arch>=<hash>`` or ``<arch>: <hash>``; a bare hash applies to
    every architecture (key ``"*"``). Blank lines and ``#`` comments are ignored. Raises
    ``INVALID_ARGUMENT`` for malformed lines.
    """
    hashes: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = re.split(r"\s*[=:]\s*|\s+", line, maxsplit=1)
        arch, value = ("*", parts[0]) if len(parts) == 1 else (parts[0], parts[1])
        value = value.strip().lower()
        if not _CDHASH_RE.fullmatch(value):
            raise AXPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Line {number} of the launcher's CDHASH file is not `<arch> <hex cdhash>`.",
                hint="Reinstall AXPilot; the package may be damaged.",
            )
        hashes[arch] = value
    return hashes


def expected_cdhashes(source_dir: Path | None = None) -> dict[str, str]:
    """The published cdhashes from ``_launcher/CDHASH`` (empty if the file is absent)."""
    folder = source_dir if source_dir is not None else bundled_dir()
    if folder is None:
        return {}
    try:
        text = (folder / CDHASH_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"Could not read the launcher's CDHASH file: {exc}.",
            hint="Reinstall AXPilot; the package may be damaged.",
        ) from exc
    return parse_cdhash_file(text)


def current_arch() -> str:
    """The architecture name codesign uses for this machine (``arm64`` or ``x86_64``)."""
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else machine


# --------------------------------------------------------------------------------------------
# Quarantine


def _darwin_has_xattr(path: Path, name: str) -> bool | None:  # pragma: no cover - macOS only
    libc = ctypes.CDLL(None, use_errno=True)
    getxattr = libc.getxattr
    getxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    getxattr.restype = ctypes.c_ssize_t
    size = getxattr(os.fsencode(path), name.encode(), None, 0, 0, _XATTR_NOFOLLOW)
    if size >= 0:
        return True
    return False if ctypes.get_errno() == _DARWIN_ENOATTR else None


def has_xattr(path: Path, name: str) -> bool | None:
    """True/False if ``path`` has/lacks the extended attribute ``name``; ``None`` if this
    platform or filesystem cannot tell. Symlinks are not followed."""
    getxattr = getattr(os, "getxattr", None)
    if getxattr is not None:
        try:
            getxattr(path, name, follow_symlinks=False)
        except OSError as exc:
            if exc.errno == errno.ENODATA:
                return False
            return None  # ENOTSUP (name outside a supported namespace), EACCES, …
        return True
    if sys.platform == "darwin":  # pragma: no cover - macOS only
        return _darwin_has_xattr(path, name)
    return None  # pragma: no cover - platforms without extended attributes


def quarantined_paths(app: Path | None = None) -> tuple[str, ...] | None:
    """Bundle-relative paths (``"."`` for the bundle) carrying ``com.apple.quarantine``.

    Returns ``None`` if the attribute cannot be checked here (for example on Linux).
    """
    root = app or paths.launcher_app_path()
    if has_xattr(root, QUARANTINE_XATTR) is None:
        return None
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        folder = Path(dirpath)
        for path in (folder, *(folder / name for name in (*dirnames, *filenames))):
            if path != folder and path.is_dir() and not path.is_symlink():
                continue  # directories are checked when os.walk visits them
            if has_xattr(path, QUARANTINE_XATTR):
                found.append(path.relative_to(root).as_posix())
    return tuple(sorted(set(found)))


# --------------------------------------------------------------------------------------------
# Install and verify


@dataclass(frozen=True, slots=True)
class LauncherInstall:
    """The outcome of :func:`install_launcher`."""

    path: Path
    changed: bool
    """False if an identical copy was already installed."""
    sha256: str
    """SHA-256 of the installed ``Contents/MacOS/AXPilot``."""
    version: str | None


@dataclass(frozen=True, slots=True)
class LauncherStatus:
    """The installed launcher compared with the bundled one (for ``axpilot doctor``)."""

    path: Path
    installed: bool
    version: str | None = None
    sha256: str | None = None
    bundled_path: Path | None = None
    bundled_version: str | None = None
    bundled_sha256: str | None = None
    identical: bool | None = None
    """Whether the whole installed bundle equals the bundled one (``None`` if unknown)."""
    quarantined: tuple[str, ...] | None = None
    expected_cdhash: dict[str, str] = field(default_factory=dict)

    @property
    def hash_matches(self) -> bool | None:
        """Whether the installed executable has the bundled executable's SHA-256."""
        if self.sha256 is None or self.bundled_sha256 is None:
            return None
        return self.sha256 == self.bundled_sha256


def _require_source(source: Path | None) -> Path:
    app = source if source is not None else bundled_app()
    if app is None:
        raise AXPilotError(
            ErrorCode.NOT_SUPPORTED,
            "This AXPilot build does not include the launcher app (development install).",
            hint="Use direct mode (`axpilot stdio --direct`), or build the launcher with "
            "`launcher/build.sh` and pass it to setup.",
        )
    if not is_installed(app):
        raise AXPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{display_path(app)} is not a launcher bundle "
            "(Contents/MacOS/AXPilot is missing or not executable).",
            hint="Reinstall AXPilot; the package may be damaged.",
        )
    return app


def _remove_tree(path: Path) -> None:
    """Delete a temporary folder; failures are logged, never raised (it is only clutter)."""
    if not path.exists() and not path.is_symlink():
        return
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        logger.warning("Could not remove temporary folder %s: %s", path, exc)


def install_launcher(*, source: Path | None = None, dest: Path | None = None) -> LauncherInstall:
    """Copy the launcher bundle byte for byte to ``dest`` (default ``~/Applications/AXPilot.app``).

    The copy is staged next to ``dest``, verified file by file (content and mode) and then moved
    into place, replacing an older copy. An identical existing copy is left untouched, so the
    user's permission grant is never disturbed without need.

    Raises ``NOT_SUPPORTED`` if no launcher is bundled, ``INVALID_ARGUMENT`` for a damaged source,
    ``INTERNAL`` if the copy does not verify and ``COMMAND_FAILED`` for I/O errors.
    """
    app = _require_source(source)
    target = dest or paths.launcher_app_path()
    wanted = tree_manifest(app)
    sha = file_sha256(executable_path(app))
    version = _bundle_version(app)
    if target.is_dir() and not target.is_symlink() and tree_manifest(target) == wanted:
        return LauncherInstall(target, False, sha, version)

    staging: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        staged = staging / target.name
        shutil.copytree(app, staged, symlinks=True)
        if tree_manifest(staged) != wanted:
            raise AXPilotError(
                ErrorCode.INTERNAL,
                f"The copy of the launcher in {display_path(target.parent)} differs from the original.",
                hint="Check free disk space and the folder's filesystem, then run `axpilot setup` again.",
            )
        previous = staging / f"{target.name}.previous"
        had_previous = target.exists() or target.is_symlink()
        if had_previous:
            os.replace(target, previous)
        try:
            os.replace(staged, target)
        except OSError:
            if had_previous:
                os.replace(previous, target)  # put the old copy back
            raise
    except OSError as exc:
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED,
            f"Could not install the launcher at {display_path(target)}: {exc.strerror or exc}.",
            hint="Quit AXPilot if it is running and check that you own the folder, then try again.",
            details={"path": str(target)},
        ) from exc
    finally:
        if staging is not None:
            _remove_tree(staging)
    return LauncherInstall(target, True, sha, version)


def launcher_status(*, app: Path | None = None, source: Path | None = None) -> LauncherStatus:
    """Inspect the installed launcher and compare it with the bundled copy (never raises for a
    missing or damaged installation; that is what the status reports)."""
    target = app or paths.launcher_app_path()
    bundled = source if source is not None else bundled_app()
    if bundled is not None and not is_installed(bundled):
        bundled = None
    bundled_sha = file_sha256(executable_path(bundled)) if bundled is not None else None
    bundled_version = _bundle_version(bundled) if bundled is not None else None
    expected = expected_cdhashes(bundled.parent if bundled is not None else None)
    if not is_installed(target):
        return LauncherStatus(
            target,
            False,
            bundled_path=bundled,
            bundled_version=bundled_version,
            bundled_sha256=bundled_sha,
            expected_cdhash=expected,
        )
    identical = tree_manifest(target) == tree_manifest(bundled) if bundled is not None else None
    return LauncherStatus(
        target,
        True,
        version=_bundle_version(target),
        sha256=file_sha256(executable_path(target)),
        bundled_path=bundled,
        bundled_version=bundled_version,
        bundled_sha256=bundled_sha,
        identical=identical,
        quarantined=quarantined_paths(target),
        expected_cdhash=expected,
    )


def installed_cdhash(runner: CommandRunner, app: Path | None = None, arch: str | None = None) -> str | None:
    """The cdhash ``codesign`` reports for the installed launcher (macOS; ``None`` if unknown)."""
    target = app or paths.launcher_app_path()
    argv = ["codesign", "-dvvv"]
    if arch:
        argv += ["-a", arch]
    result = runner.run([*argv, str(target)], timeout_s=_CODESIGN_TIMEOUT_S)
    if not result.ok:
        return None
    match = _CODESIGN_CDHASH_RE.search(f"{result.stderr}\n{result.stdout}")
    return match.group(1).lower() if match else None


# --------------------------------------------------------------------------------------------
# Start and login item (macOS)


def _require_macos(what: str, system: str | None) -> None:
    current = system or sys.platform
    if current != "darwin":
        raise AXPilotError(
            ErrorCode.UNSUPPORTED_OS,
            f"{what} needs macOS; this system is {current}.",
            hint="On other systems use `axpilot stdio --engine fake` to try AXPilot.",
        )


def launch(
    runner: CommandRunner,
    *,
    app: Path | None = None,
    args: Sequence[str] = (),
    system: str | None = None,
) -> None:
    """Start the launcher in the background through LaunchServices (``open -g -a``).

    LaunchServices makes the app its own "responsible process", which is what lets macOS
    attribute permissions to it. Raises ``UNSUPPORTED_OS`` off macOS and ``HOST_UNAVAILABLE`` if
    the launcher is not installed.
    """
    _require_macos("Starting AXPilot.app", system)
    target = app or paths.launcher_app_path()
    if not is_installed(target):
        raise AXPilotError(
            ErrorCode.HOST_UNAVAILABLE,
            f"The AXPilot launcher is not installed at {display_path(target)}.",
            hint="Run `axpilot setup`.",
        )
    argv = ["open", "-g", "-a", str(target)]
    if args:
        argv += ["--args", *args]
    ensure_success(
        runner.run(argv, timeout_s=_LAUNCH_TIMEOUT_S),
        what="Starting AXPilot.app",
        hint="Open ~/Applications/AXPilot.app from Finder to see why it does not start.",
        timeout_s=_LAUNCH_TIMEOUT_S,
    )


def login_agent_path() -> Path:
    """The LaunchAgent that starts AXPilot at login (``~/Library/LaunchAgents/<label>.plist``)."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LOGIN_AGENT_LABEL}.plist"


def login_agent_plist(app: Path | None = None) -> dict[str, Any]:
    """The LaunchAgent definition: run ``open -g -a <app>`` once per GUI login.

    Going through ``open`` (LaunchServices) keeps the app its own responsible process, exactly
    as when the user opens it.
    """
    target = app or paths.launcher_app_path()
    return {
        "Label": LOGIN_AGENT_LABEL,
        "ProgramArguments": ["/usr/bin/open", "-g", "-a", str(target)],
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",
        "AssociatedBundleIdentifiers": [BUNDLE_ID],
    }


def install_login_item(app: Path | None = None) -> tuple[Path, bool]:
    """Write the login LaunchAgent. Returns ``(path, changed)``; it takes effect at next login."""
    path = login_agent_path()
    data = plistlib.dumps(login_agent_plist(app))
    try:
        unchanged = path.read_bytes() == data
    except FileNotFoundError:
        unchanged = False
    except OSError as exc:
        logger.debug("Could not read %s, rewriting it: %s", path, exc)
        unchanged = False
    if unchanged:
        return path, False
    atomic_write(path, data, mode=0o644)
    return path, True


def remove_login_item() -> bool:
    """Delete the login LaunchAgent. Returns True if one was removed."""
    path = login_agent_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED,
            f"Could not remove {display_path(path)}: {exc.strerror or exc}.",
            hint="Delete the file yourself.",
        ) from exc
    return True


def login_item_installed() -> bool:
    """True if the login LaunchAgent exists."""
    return login_agent_path().is_file()
