#!/usr/bin/env python3
"""Spike S1 installer: place AXPilot.app the way ``axpilot setup`` will, and write runtime.json.

    install.py bundle  <built AXPilot.app> <destination .app>
    install.py manifest <runtime.json> --python <abs python> [--env K=V]... [--cwd DIR] -- <args>...

``bundle`` copies file contents and permission bits only, like pip/uv unpacking a wheel: no
extended attributes (so no com.apple.quarantine), no resource forks. It replaces an existing
destination only if that is an AXPilot bundle. ``manifest`` writes a version-1 runtime.json
(mode 0600) that the launcher accepts, validated the same way the launcher validates it.

Stdlib only, Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import sys
from pathlib import Path
from typing import Any

LAUNCHER_BUNDLE_ID = "io.github.vansh3503.axpilot"
MANIFEST_VERSION = 1


class InstallError(Exception):
    """A refusal with a message for the user."""


def bundle_id(app: Path) -> str | None:
    try:
        with (app / "Contents" / "Info.plist").open("rb") as handle:
            return plistlib.load(handle).get("CFBundleIdentifier")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def install_bundle(source: Path, destination: Path) -> Path:
    """Copy ``source`` to ``destination`` (contents and mode bits only); returns the destination."""
    if bundle_id(source) != LAUNCHER_BUNDLE_ID:
        raise InstallError(f"{source} is not an AXPilot bundle; build it with launcher/build.sh")
    if destination.suffix != ".app":
        raise InstallError(f"the destination must end in .app: {destination}")
    if destination.exists() and bundle_id(destination) != LAUNCHER_BUNDLE_ID:
        raise InstallError(f"refusing to replace {destination}: it is not an AXPilot bundle")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.installing")
    if staging.exists():
        shutil.rmtree(staging)
    # shutil.copy = file data + permission bits; no xattrs, flags or timestamps (what a wheel install does).
    shutil.copytree(source, staging, symlinks=True, copy_function=shutil.copy)
    if destination.exists():
        shutil.rmtree(destination)
    staging.rename(destination)
    return destination


def parse_env(pairs: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise InstallError(f"--env expects KEY=VALUE, got {pair!r}")
        env[key] = value
    return env


def build_manifest(python: str, args: list[str], env: dict[str, str], cwd: str | None) -> dict[str, Any]:
    """A runtime.json document, validated like the launcher validates it."""
    if not os.path.isabs(python) or not os.access(python, os.X_OK) or os.path.isdir(python):
        raise InstallError(f"python must be an absolute path to an executable: {python}")
    if cwd is not None and not os.path.isabs(cwd):
        raise InstallError(f"cwd must be an absolute path: {cwd}")
    if any(not key or "=" in key for key in env):
        raise InstallError("env keys must be non-empty and must not contain '='")
    return {"version": MANIFEST_VERSION, "python": python, "args": args, "env": env, "cwd": cwd}


def write_manifest(path: Path, manifest: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    os.chmod(path, 0o600)
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    bundle = commands.add_parser("bundle", help="copy AXPilot.app like a package manager would")
    bundle.add_argument("source", type=Path)
    bundle.add_argument("destination", type=Path)
    manifest = commands.add_parser("manifest", help="write a runtime.json for the launcher")
    manifest.add_argument("output", type=Path)
    manifest.add_argument("--python", required=True, help="absolute path of the interpreter to spawn")
    manifest.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    manifest.add_argument("--cwd")
    manifest.epilog = "Everything after -- becomes the interpreter's arguments."
    return parser.parse_args(argv)


def split_program_args(argv: list[str]) -> tuple[list[str], list[str]]:
    """``[options..., "--", program args...]`` -> (options, program args)."""
    if "--" not in argv:
        return argv, []
    index = argv.index("--")
    return argv[:index], argv[index + 1 :]


def main(argv: list[str] | None = None) -> int:
    options, program_args = split_program_args(list(sys.argv[1:] if argv is None else argv))
    args = parse_args(options)
    try:
        if args.command == "bundle":
            path = install_bundle(args.source, args.destination)
            print(f"installed {path}")
        else:
            document = build_manifest(args.python, program_args, parse_env(args.env), args.cwd)
            print(f"wrote {write_manifest(args.output, document)}")
    except InstallError as exc:
        print(f"install.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
