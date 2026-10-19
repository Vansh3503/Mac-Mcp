"""A tiny stdio MCP server that stands in for ``axpilot stdio --engine fake`` in the S6 tests.

``--no-annotations`` drops the annotations of the ``press`` tool so tests can see the interop
script report it.
"""

from __future__ import annotations

import sys

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel


class Status(BaseModel):
    ok: bool
    version: str


def build(annotate_press: bool = True) -> MCPServer:
    server = MCPServer(name="stub-axpilot", version="0.0.1", instructions="Stub server for the S6 tests.")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    def status() -> Status:
        """Report the stub's status."""
        return Status(ok=True, version="0.0.1")

    press_annotations = (
        ToolAnnotations(read_only_hint=False, destructive_hint=False) if annotate_press else None
    )

    @server.tool(annotations=press_annotations)
    def press(key: str) -> str:
        """Pretend to press a key."""
        return f"pressed {key}"

    return server


if __name__ == "__main__":
    build(annotate_press="--no-annotations" not in sys.argv).run("stdio")
