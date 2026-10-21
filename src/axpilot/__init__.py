"""AXPilot: an MCP server that lets AI agents see and operate macOS.

``from axpilot import Desktop`` (or ``AsyncDesktop``) is the automation SDK; it is imported on
first use, so ``import axpilot`` stays cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0a1"

SERVER_NAME = "axpilot"
BUNDLE_ID = "io.github.vansh3503.axpilot"
REGISTRY_NAME = "io.github.vansh3503/axpilot"

if TYPE_CHECKING:
    from axpilot.sdk import AsyncDesktop, Desktop

__all__ = ["BUNDLE_ID", "REGISTRY_NAME", "SERVER_NAME", "AsyncDesktop", "Desktop", "__version__"]

_LAZY = {"Desktop", "AsyncDesktop"}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from axpilot import sdk  # noqa: PLC0415 - imported on first use

        return getattr(sdk, name)
    raise AttributeError(f"module 'axpilot' has no attribute {name!r}")
