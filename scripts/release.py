"""Release checks and helpers for ``.github/workflows/release.yml``; run ``check`` before tagging.

    python scripts/release.py check v0.1.0                # everything a release needs, nothing built yet
    python scripts/release.py notes 0.1.0                 # the CHANGELOG section, as release notes
    python scripts/release.py verify-launcher --zip AXPilot.app.zip --app path/AXPilot.app
    python scripts/release.py write-cdhash src/axpilot/_launcher
    python scripts/release.py server-json 0.1.0           # set the versions in packaging/server.json

``check`` prints ``key=value`` lines for ``$GITHUB_OUTPUT`` (``version``, ``prerelease``,
``launcher_asset``). Every command exits 1 with one line per problem on stderr. Standard library
only, so it runs before the project's dependencies are installed.

A release embeds exactly the launcher pinned by the newest row of ``launcher/CDHASH.md``: users'
permission grants are tied to its cdhash, so ``check`` fails closed while that row is pending.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT_FILE = Path("src/axpilot/__init__.py")
CHANGELOG = Path("CHANGELOG.md")
CDHASH_TABLE = Path("launcher/CDHASH.md")
SERVER_JSON = Path("packaging/server.json")
"""Paths relative to the repository root."""

ARCHS = ("arm64", "x86_64")
CDHASH_FILE = "CDHASH"
"""The file ``axpilot setup`` and ``axpilot doctor`` read the published cdhashes from."""
LAUNCHER_BINARY = Path("Contents") / "MacOS" / "AXPilot"
REGISTRY_DESCRIPTION_MAX = 100

TAG_RE = re.compile(r"v(?P<version>\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)")
VERSION_RE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
HEADING_RE = re.compile(r"^## \[(?P<version>[^\]]+)\](?P<rest>.*)$", re.MULTILINE)
DATE_RE = re.compile(r" - \d{4}-\d{2}-\d{2}")
ASSET_RE = re.compile(r"launcher-v\d+")
CDHASH_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{7,40}")
CODESIGN_CDHASH_RE = re.compile(r"^CDHash=([0-9A-Fa-f]+)\s*$", re.MULTILINE)


class ReleaseError(Exception):
    """A check failed; the message says what to fix."""


@dataclass(frozen=True)
class LauncherPin:
    """One row of ``launcher/CDHASH.md``."""

    launcher: str
    asset: str
    cdhashes: dict[str, str]
    zip_sha256: str
    binary_sha256: str
    commit: str


# =========================================================================== reading the repository
def version_from_tag(tag: str) -> str:
    """``v1.2.3`` → ``1.2.3`` (also ``a``/``b``/``rc`` pre-releases)."""
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise ReleaseError(f"Tag {tag!r} is not v<major>.<minor>.<patch>[a|b|rc<n>].")
    return match["version"]


def is_prerelease(version: str) -> bool:
    return re.search(r"(a|b|rc)\d+$", version) is not None


def package_version(init_text: str) -> str:
    """The ``__version__`` string of ``src/axpilot/__init__.py``."""
    match = VERSION_RE.search(init_text)
    if match is None:
        raise ReleaseError('src/axpilot/__init__.py has no `__version__ = "..."` line.')
    return match.group(1)


def changelog_section(text: str, version: str) -> str:
    """The body of ``## [version] - YYYY-MM-DD`` in the changelog, without the heading."""
    headings = list(HEADING_RE.finditer(text))
    for index, heading in enumerate(headings):
        if heading["version"] != version:
            continue
        if not DATE_RE.fullmatch(heading["rest"]):
            raise ReleaseError(f"CHANGELOG.md: write the heading as `## [{version}] - YYYY-MM-DD`.")
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[heading.end() : end]
        body = re.split(r"^\[[^\]]+\]: ", body, maxsplit=1, flags=re.MULTILINE)[0].strip()
        if not body:
            raise ReleaseError(f"CHANGELOG.md: the [{version}] section is empty.")
        return body
    raise ReleaseError(f"CHANGELOG.md has no [{version}] section; move the Unreleased entries into one.")


def parse_pins(text: str) -> list[LauncherPin]:
    """The rows of the table in ``launcher/CDHASH.md``, oldest first."""
    rows = [line for line in text.splitlines() if line.startswith("|")]
    if len(rows) < 2 or not rows[0].lstrip("| ").startswith("Launcher"):
        raise ReleaseError("launcher/CDHASH.md has no table starting with a `Launcher` column.")
    header = _cells(rows[0])
    pins: list[LauncherPin] = []
    for row in rows[2:]:
        cells = dict(zip(header, _cells(row), strict=False))
        asset = ASSET_RE.search(cells.get("Release asset", ""))
        pins.append(
            LauncherPin(
                launcher=cells.get("Launcher", ""),
                asset=asset.group(0) if asset else "",
                cdhashes={arch: cells.get(f"cdhash ({arch})", "").lower() for arch in ARCHS},
                zip_sha256=cells.get("SHA-256 of AXPilot.app.zip", "").lower(),
                binary_sha256=cells.get("SHA-256 of Contents/MacOS/AXPilot", "").lower(),
                commit=cells.get("Git commit", "").lower(),
            )
        )
    if not pins:
        raise ReleaseError("launcher/CDHASH.md has no launcher rows.")
    return pins


def _cells(row: str) -> list[str]:
    """The cells of a Markdown table row, without code backticks."""
    return [cell.replace("`", "").strip() for cell in row.strip().strip("|").split("|")]


def pin_problems(pin: LauncherPin) -> list[str]:
    """Why ``pin`` can't be embedded yet (empty when it can)."""
    where = f"launcher/CDHASH.md, launcher {pin.launcher}"
    problems: list[str] = []
    if not pin.asset:
        problems.append(f"{where}: the release asset is not `launcher-vN`.")
    for arch, value in pin.cdhashes.items():
        if not CDHASH_RE.fullmatch(value):
            problems.append(f"{where}: the {arch} cdhash is {value or 'empty'!r}, not 40 hex digits.")
    for label, value in (("AXPilot.app.zip", pin.zip_sha256), ("Contents/MacOS/AXPilot", pin.binary_sha256)):
        if not SHA256_RE.fullmatch(value):
            problems.append(f"{where}: the SHA-256 of {label} is {value or 'empty'!r}, not 64 hex digits.")
    if not COMMIT_RE.fullmatch(pin.commit):
        problems.append(f"{where}: the git commit is {pin.commit or 'empty'!r}.")
    return problems


def server_json_problems(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"packaging/server.json is not valid JSON: {exc}."]
    problems: list[str] = []
    if len(data.get("description", "")) > REGISTRY_DESCRIPTION_MAX:
        problems.append(f"packaging/server.json: description is over {REGISTRY_DESCRIPTION_MAX} characters.")
    if not any(package.get("registryType") == "pypi" for package in data.get("packages", [])):
        problems.append("packaging/server.json lists no PyPI package.")
    return problems


def set_server_json_version(text: str, version: str) -> str:
    """``server.json`` with its own and its PyPI package's ``version`` set to ``version``."""
    data = json.loads(text)
    data["version"] = version
    for package in data.get("packages", []):
        if package.get("registryType") == "pypi":
            package["version"] = version
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def cdhash_file_text(pin: LauncherPin) -> str:
    """The ``_launcher/CDHASH`` file for ``pin`` (one ``<arch> <cdhash>`` line per architecture)."""
    return "".join(f"{arch} {pin.cdhashes[arch]}\n" for arch in ARCHS)


# =========================================================================== checks
def check(tag: str, root: Path | None = None) -> tuple[dict[str, str], list[str]]:
    """Everything a release of ``tag`` needs before anything is built: ``(outputs, problems)``.
    ``root`` defaults to this repository."""
    root = root if root is not None else ROOT
    problems: list[str] = []
    outputs: dict[str, str] = {}
    try:
        version = version_from_tag(tag)
    except ReleaseError as exc:
        return outputs, [str(exc)]
    outputs.update(version=version, prerelease=str(is_prerelease(version)).lower())
    try:
        found = package_version((root / INIT_FILE).read_text(encoding="utf-8"))
        if found != version:
            problems.append(f"The tag says {version} but src/axpilot/__init__.py says {found}.")
    except ReleaseError as exc:
        problems.append(str(exc))
    try:
        changelog_section((root / CHANGELOG).read_text(encoding="utf-8"), version)
    except ReleaseError as exc:
        problems.append(str(exc))
    try:
        pin = parse_pins((root / CDHASH_TABLE).read_text(encoding="utf-8"))[-1]
        problems.extend(pin_problems(pin))
        outputs["launcher_asset"] = pin.asset
    except ReleaseError as exc:
        problems.append(str(exc))
    problems.extend(server_json_problems((root / SERVER_JSON).read_text(encoding="utf-8")))
    return outputs, problems


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


Run = Callable[[Sequence[str]], str]


def codesign_output(argv: Sequence[str]) -> str:
    """``codesign -d`` prints to stderr; return both streams."""
    done = subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=60)
    return done.stdout + done.stderr


def verify_launcher(
    pin: LauncherPin,
    *,
    zip_path: Path | None = None,
    app: Path | None = None,
    cdhash: bool = False,
    run: Run = codesign_output,
) -> list[str]:
    """Compare a downloaded launcher (the zip, the extracted bundle) with ``pin``. ``cdhash``
    also reads each architecture's cdhash with ``codesign`` (macOS only)."""
    problems: list[str] = []
    if zip_path is not None and sha256_of(zip_path) != pin.zip_sha256:
        problems.append(f"{zip_path.name}: SHA-256 differs from the pinned launcher {pin.launcher}.")
    if app is not None:
        binary = app / LAUNCHER_BINARY
        if not binary.is_file():
            problems.append(f"{app}: no {LAUNCHER_BINARY} in the bundle.")
        elif sha256_of(binary) != pin.binary_sha256:
            problems.append(f"{binary}: SHA-256 differs from the pinned launcher {pin.launcher}.")
        for arch in ARCHS if cdhash else ():
            match = CODESIGN_CDHASH_RE.search(run(["codesign", "-dvvv", "-a", arch, str(app)]))
            actual = match.group(1).lower() if match else "unreadable"
            if actual != pin.cdhashes[arch]:
                problems.append(f"{app}: {arch} cdhash is {actual}, pinned {pin.cdhashes[arch]}.")
    return problems


# =========================================================================== command line
def _current_pin() -> LauncherPin:
    pin = parse_pins((ROOT / CDHASH_TABLE).read_text(encoding="utf-8"))[-1]
    problems = pin_problems(pin)
    if problems:
        raise ReleaseError("\n".join(problems))
    return pin


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release checks and helpers for release.yml.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="check everything a release needs").add_argument("tag")
    commands.add_parser("notes", help="print the CHANGELOG section of a version").add_argument("version")
    verify = commands.add_parser("verify-launcher", help="compare a launcher with the pinned row")
    verify.add_argument("--zip", type=Path, dest="zip_path")
    verify.add_argument("--app", type=Path)
    verify.add_argument("--cdhash", action="store_true", help="also compare cdhashes (macOS, codesign)")
    commands.add_parser("write-cdhash", help="write _launcher/CDHASH").add_argument("folder", type=Path)
    commands.add_parser("server-json", help="set the versions in server.json").add_argument("version")
    args = parser.parse_args(argv)

    try:
        if args.command == "check":
            outputs, problems = check(args.tag)
            if problems:
                raise ReleaseError("\n".join(problems))
            print("\n".join(f"{key}={value}" for key, value in outputs.items()))
        elif args.command == "notes":
            print(changelog_section((ROOT / CHANGELOG).read_text(encoding="utf-8"), args.version))
        elif args.command == "verify-launcher":
            if args.zip_path is None and args.app is None:
                raise ReleaseError("Pass --zip, --app or both.")
            problems = verify_launcher(
                _current_pin(), zip_path=args.zip_path, app=args.app, cdhash=args.cdhash
            )
            if problems:
                raise ReleaseError("\n".join(problems))
        elif args.command == "write-cdhash":
            args.folder.mkdir(parents=True, exist_ok=True)
            (args.folder / CDHASH_FILE).write_text(cdhash_file_text(_current_pin()), encoding="utf-8")
        elif args.command == "server-json":
            text = set_server_json_version((ROOT / SERVER_JSON).read_text(encoding="utf-8"), args.version)
            (ROOT / SERVER_JSON).write_text(text, encoding="utf-8")
    except (ReleaseError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
