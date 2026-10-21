"""Readable names for the apps and programs that run MCP clients.

AXPilot names a client after the app or program that started it: the outermost app bundle among
its parent processes, else the nearest program (see :func:`axpilot.bridge.launch.identify_parent`),
and in direct mode the app macOS charges permissions to. Most bundles already carry a readable
name ("Cursor", "Visual Studio Code"); some clients do not: Claude Code inside the Claude app is a
bundle named ``claude``, and command-line clients are bare programs (``codex``, ``gemini``). This
module maps those to the names users know, and leaves every other name as it is.

Only the shown name changes: pairing decisions stay keyed on the bundle id or the raw name, so a
client paired under its raw name is not asked again.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["KNOWN_PROGRAMS", "client_display_name"]

KNOWN_PROGRAMS: Mapping[str, str] = {
    "claude": "Claude Code",
    "codex": "Codex CLI",
    "gemini": "Gemini CLI",
    "goose": "Goose",
    "code": "VS Code",
    "code-insiders": "VS Code Insiders",
    "cursor": "Cursor",
    "windsurf": "Windsurf",
    "zed": "Zed",
    "lms": "LM Studio",
}
"""Raw program or bundle names (case-sensitive: the Claude desktop app's bundle is ``Claude``)
and the names to show for them."""


def client_display_name(name: str | None) -> str | None:
    """The readable name of a client app or program (``"claude"`` → ``"Claude Code"``); any other
    name unchanged, ``None`` stays ``None``. A trailing ``.app`` is ignored when matching."""
    if name is None:
        return None
    return KNOWN_PROGRAMS.get(name.removesuffix(".app"), name)
