"""The real macOS engine (PyObjC).

``MacEngine`` is imported lazily so that the pure helpers in this package
(:mod:`axpilot.engine.macos.helpers`) can be imported and unit-tested on any OS. Importing
``MacEngine`` itself loads PyObjC and only works on macOS; use
:func:`axpilot.engine.create_engine`, which checks the platform first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axpilot.engine.macos.engine import MacEngine

__all__ = ["MacEngine"]


def __getattr__(name: str) -> Any:
    if name == "MacEngine":
        from axpilot.engine.macos.engine import MacEngine

        return MacEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
