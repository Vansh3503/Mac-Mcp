"""Stand-ins for the Apple command-line tools that launcher/build.sh calls.

``test_build_script.py`` puts a wrapper for each tool first on ``PATH``; every wrapper runs
``python apple_tool_stubs.py <tool> <args...>``. The stubs do just enough for build.sh to run
end to end on any OS, and append each invocation to ``$STUB_LOG`` (JSON lines) for assertions.

Knobs (environment variables): ``STUB_UNAME_S`` (default ``Darwin``), ``STUB_ARCHS``
(``lipo -archs`` output), ``STUB_VERSION_OUTPUT`` (what the compiled "binary" prints for
``--version``; default: derived from main.swift).
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

BUNDLE_ID = "io.github.vansh3503.axpilot"
CDHASHES = {"arm64": "a" * 40, "x86_64": "b" * 40}


def log_call(tool: str, args: list[str]) -> None:
    if path := os.environ.get("STUB_LOG"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"tool": tool, "args": args, "cwd": os.getcwd()}) + "\n")


def option(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def uname(args: list[str]) -> int:
    print("arm64" if "-m" in args else os.environ.get("STUB_UNAME_S", "Darwin"))
    return 0


def swiftc(args: list[str]) -> int:
    if "--version" in args:
        print("swift-driver version: stub Apple Swift version 6.0 (stub)")
        return 0
    source = Path(args[-1])
    match = re.search(r'^let launcherVersion = "([^"]+)"', source.read_text(encoding="utf-8"), re.M)
    default_line = f"AXPilot launcher {match.group(1) if match else '?'}"
    version_line = os.environ.get("STUB_VERSION_OUTPUT") or default_line
    output = Path(option(args, "-o") or "a.out")
    output.write_text(f'#!/bin/sh\n[ "$1" = --version ] && echo "{version_line}"\n', encoding="utf-8")
    output.chmod(0o755)
    return 0


def lipo(args: list[str]) -> int:
    if "-archs" in args:
        print(os.environ.get("STUB_ARCHS", "x86_64 arm64"))
        return 0
    output = Path(option(args, "-output") or "")
    inputs = [a for a in args if a not in ("-create", "-output", str(output))]
    shutil.copy2(inputs[0], output)
    return 0


def xcrun(args: list[str]) -> int:
    if args[0] == "--find":
        print(f"/usr/bin/{args[1]}")
        return 0
    if "--show-sdk-path" in args:
        print("/stub/MacOSX.sdk")
        return 0
    if "--show-sdk-version" in args:
        print("15.0")
        return 0
    tool, rest = args[0], args[1:]
    return {"swiftc": swiftc, "lipo": lipo}[tool](rest)


def codesign(args: list[str]) -> int:
    target = Path(args[-1])
    if "--sign" in args:
        seal = target / "Contents" / "_CodeSignature" / "CodeResources"
        seal.parent.mkdir(parents=True, exist_ok=True)
        seal.write_text("<plist>stub seal</plist>\n", encoding="utf-8")
        return 0
    if "--verify" in args:
        print(f"{target}: valid on disk", file=sys.stderr)
        return 0
    if "-r-" in args:
        print(f"Executable={target}/Contents/MacOS/AXPilot", file=sys.stderr)
        print(f'# designated => cdhash H"{CDHASHES["arm64"]}"')
        return 0
    arch = option(args, "-a") or "arm64"
    print(
        "\n".join(
            [
                f"Executable={target}/Contents/MacOS/AXPilot",
                f"Identifier={BUNDLE_ID}",
                "Format=app bundle with Mach-O universal (x86_64 arm64)",
                f"CandidateCDHash sha256={CDHASHES[arch]}",
                f"CDHash={CDHASHES[arch]}",
                "Signature=adhoc",
                "TeamIdentifier=not set",
            ]
        ),
        file=sys.stderr,
    )
    return 0


def plutil(args: list[str]) -> int:
    try:
        with open(args[-1], "rb") as handle:
            plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return 1
    return 0


def plistbuddy(args: list[str]) -> int:
    key = args[args.index("-c") + 1].removeprefix("Print :")
    with open(args[-1], "rb") as handle:
        print(plistlib.load(handle)[key])
    return 0


def ditto(args: list[str]) -> int:
    source, destination = Path(args[-2]), Path(args[-1])
    with zipfile.ZipFile(destination, "w") as archive:
        for path in sorted(source.rglob("*")):
            archive.write(path, path.relative_to(source.parent))
    return 0


def sw_vers(args: list[str]) -> int:
    print("24A335" if "-buildVersion" in args else "15.0")
    return 0


def arch(args: list[str]) -> int:
    os.execvp(args[1], args[1:])  # noqa: S606 - stands in for `arch -x86_64 <cmd>`
    return 127


def shasum(args: list[str]) -> int:
    for name in args[2:]:  # shasum -a 256 FILE...
        print(f"{hashlib.sha256(Path(name).read_bytes()).hexdigest()}  {name}")
    return 0


def main(argv: list[str]) -> int:
    tool, args = argv[0], argv[1:]
    log_call(tool, args)
    handlers = {
        "uname": uname,
        "xcrun": xcrun,
        "codesign": codesign,
        "plutil": plutil,
        "PlistBuddy": plistbuddy,
        "ditto": ditto,
        "sw_vers": sw_vers,
        "xattr": lambda _args: 0,
        "arch": arch,
        "shasum": shasum,
    }
    return handlers[tool](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
