"""Facts about macOS app bundles read straight from disk (no PyObjC, so they work anywhere)."""

from __future__ import annotations

import plistlib
from pathlib import Path, PurePosixPath

__all__ = ["bundle_id_of", "enclosing_app"]


def bundle_id_of(app: Path) -> str | None:
    """``CFBundleIdentifier`` from an ``.app`` bundle's ``Contents/Info.plist`` (``None`` if it
    cannot be read): a bundle's identity, whatever its folder is called."""
    try:
        with (app / "Contents" / "Info.plist").open("rb") as file:
            info = plistlib.load(file)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    bundle_id = info.get("CFBundleIdentifier") if isinstance(info, dict) else None
    return bundle_id if isinstance(bundle_id, str) and bundle_id.strip() else None


def enclosing_app(executable: str) -> Path | None:
    """The outermost ``.app`` bundle an executable path lies in
    (``/Applications/Claude.app`` for ``/Applications/Claude.app/Contents/MacOS/Claude``), or
    ``None``. The outermost one is the app the user knows: helper apps live inside it."""
    parts = PurePosixPath(executable).parts
    for index, part in enumerate(parts):
        if part.endswith(".app") and len(part) > len(".app"):
            return Path(*parts[: index + 1])
    return None
