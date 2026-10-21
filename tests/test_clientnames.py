"""Readable names of known MCP clients (#21)."""

from __future__ import annotations

import pytest

from axpilot.clientnames import KNOWN_PROGRAMS, client_display_name


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("claude", "Claude Code"),  # Claude Code's bundle inside the Claude app, or the CLI
        ("claude.app", "Claude Code"),
        ("Claude", "Claude"),  # the Claude desktop app keeps its own name
        ("codex", "Codex CLI"),
        ("gemini", "Gemini CLI"),
        ("Cursor", "Cursor"),
        ("Visual Studio Code", "Visual Studio Code"),
        ("node", "node"),  # unknown: unchanged
        ("", ""),
        (None, None),
    ],
)
def test_client_display_name(raw: str | None, shown: str | None) -> None:
    assert client_display_name(raw) == shown


def test_every_known_name_is_readable() -> None:
    for raw, shown in KNOWN_PROGRAMS.items():
        assert raw == raw.lower()
        assert shown != raw
        assert shown[0].isupper()
