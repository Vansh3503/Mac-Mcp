"""Engines: the layer that actually talks to macOS (or pretends to, in tests)."""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from axpilot.engine.base import Engine
from axpilot.errors import AXPilotError, ErrorCode

__all__ = ["Engine", "create_engine"]


def create_engine(kind: str = "auto", *, fixture: Path | None = None, **options: object) -> Engine:
    """Create an engine.

    ``kind`` is ``"macos"``, ``"fake"`` or ``"auto"`` (macOS on darwin, otherwise an error).
    ``fixture`` selects the desktop fixture for the fake engine (default: the bundled demo).
    Extra ``options`` are passed to the engine constructor.
    """
    if kind == "auto":
        kind = "macos" if sys.platform == "darwin" else "unsupported"
    if kind == "fake":
        from axpilot.engine.fake import FakeEngine  # noqa: PLC0415 - optional import

        return FakeEngine.from_fixture(fixture) if fixture else FakeEngine.demo()
    if kind == "macos":
        if sys.platform != "darwin":
            raise AXPilotError(ErrorCode.UNSUPPORTED_OS, "The macOS engine only runs on macOS.")
        _require_supported_macos()
        try:
            from axpilot.engine.macos import MacEngine  # noqa: PLC0415 - darwin-only import
        except ImportError as exc:  # pragma: no cover - depends on PyObjC install
            raise AXPilotError(
                ErrorCode.ENGINE_UNAVAILABLE,
                f"PyObjC frameworks are missing: {exc}",
                hint="Reinstall AXPilot on macOS: `uv tool install --reinstall axpilot`.",
            ) from exc
        return MacEngine(**options)  # type: ignore[arg-type]
    raise AXPilotError(
        ErrorCode.UNSUPPORTED_OS,
        f"AXPilot controls macOS; this platform is {sys.platform!r}.",
        hint="Use `--engine fake` to try the server with a simulated desktop.",
    )


def _require_supported_macos() -> None:
    """Raise ``UNSUPPORTED_OS`` below macOS 14 (ADR-0003: older systems lack the capture APIs)."""
    from axpilot.setup import MIN_MACOS, parse_version  # noqa: PLC0415 - only needed on macOS

    release = platform.mac_ver()[0]
    if release and parse_version(release) < MIN_MACOS:
        raise AXPilotError(
            ErrorCode.UNSUPPORTED_OS,
            f"AXPilot needs macOS {MIN_MACOS[0]} Sonoma or later; this Mac runs macOS {release}.",
            hint="Update macOS, or use `--engine fake` to try the server with a simulated desktop.",
        )
