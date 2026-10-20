"""The MCP clients ``axpilot connect`` supports, described as data.

Config locations and entry shapes follow each client's documentation for macOS. None of them is
marked ``verified`` yet: that flag is set per client once the path and shape have been checked
against a real installation on a Mac (PLAN.md §8.4, Phase 3).
"""

from __future__ import annotations

import difflib

from axpilot.clients.base import (
    ClientTarget,
    continue_document,
    continue_server,
    goose_extension,
    typed_stdio_entry,
)
from axpilot.errors import AXPilotError, ErrorCode

__all__ = ["TARGETS", "detect_installed", "get_target", "target_names"]

_APP_SUPPORT = ("Library", "Application Support")
_VSCODE_USER = (*_APP_SUPPORT, "Code", "User")

CLAUDE_DESKTOP = ClientTarget(
    name="claude-desktop",
    display_name="Claude Desktop",
    format="json",
    servers_key=("mcpServers",),
    user_path=(*_APP_SUPPORT, "Claude", "claude_desktop_config.json"),
    detect_paths=((*_APP_SUPPORT, "Claude"),),
    app_names=("Claude.app",),
    notes="Claude Desktop starts servers without your shell's PATH, so use an absolute command. "
    "Its MCP logs are in ~/Library/Logs/Claude/.",
    restart_hint="Quit Claude completely (⌘Q) and open it again.",
    aliases=("claude", "claude-app"),
)

CLAUDE_CODE = ClientTarget(
    name="claude-code",
    display_name="Claude Code",
    format="json",
    servers_key=("mcpServers",),
    user_path=(".claude.json",),
    project_path=(".mcp.json",),
    build_entry=typed_stdio_entry,
    detect_paths=((".claude",),),
    executables=("claude",),
    cli="claude",
    cli_paths=((".claude", "local", "claude"), (".local", "bin", "claude")),
    notes="For the user scope AXPilot runs `claude mcp add --scope user` when the claude CLI is "
    "installed, and edits ~/.claude.json directly otherwise. The project scope writes .mcp.json, "
    "which Claude Code asks each person to approve.",
    restart_hint="Start a new Claude Code session (or run /mcp) to load the server.",
    aliases=("claudecode",),
)

CURSOR = ClientTarget(
    name="cursor",
    display_name="Cursor",
    format="json",
    servers_key=("mcpServers",),
    user_path=(".cursor", "mcp.json"),
    project_path=(".cursor", "mcp.json"),
    detect_paths=((".cursor",),),
    app_names=("Cursor.app",),
    executables=("cursor",),
    restart_hint="Open Cursor Settings → MCP and check that axpilot is enabled.",
)

VSCODE = ClientTarget(
    name="vscode",
    display_name="VS Code (GitHub Copilot)",
    format="jsonc",
    servers_key=("servers",),
    user_path=(*_VSCODE_USER, "mcp.json"),
    project_path=(".vscode", "mcp.json"),
    build_entry=typed_stdio_entry,
    detect_paths=(_VSCODE_USER,),
    app_names=("Visual Studio Code.app",),
    executables=("code",),
    notes="VS Code reads MCP servers from the `servers` key of mcp.json, which may contain comments.",
    restart_hint="Start axpilot from the MCP Servers view (or Copilot Chat's tools menu).",
    aliases=("code", "vs-code", "copilot", "github-copilot"),
)

WINDSURF = ClientTarget(
    name="windsurf",
    display_name="Devin (Windsurf)",
    format="json",
    servers_key=("mcpServers",),
    user_path=(".codeium", "windsurf", "mcp_config.json"),
    detect_paths=((".codeium", "windsurf"),),
    app_names=("Devin.app", "Windsurf.app"),
    executables=("windsurf",),
    notes="Cognition's desktop app Devin is Windsurf renamed (bundle com.exafunction.windsurf 3.6) and "
    "keeps Windsurf's ~/.codeium/windsurf/mcp_config.json.",
    restart_hint="Refresh the MCP servers in the Cascade panel.",
    aliases=("devin", "devin-desktop"),
)

ANTIGRAVITY = ClientTarget(
    name="antigravity",
    display_name="Antigravity",
    format="json",
    servers_key=("mcpServers",),
    user_path=(".gemini", "antigravity-ide", "mcp_config.json"),
    detect_paths=((".gemini", "antigravity-ide"),),
    app_names=("Antigravity IDE.app",),
    notes="Google Antigravity IDE (2.5) reads mcp_config.json in its data folder ~/.gemini/antigravity-ide.",
    restart_hint="Refresh the MCP servers in Antigravity's agent panel, or restart Antigravity.",
    aliases=("antigravity-ide", "google-antigravity"),
)

ZED = ClientTarget(
    name="zed",
    display_name="Zed",
    format="jsonc",
    servers_key=("context_servers",),
    user_path=(".config", "zed", "settings.json"),
    detect_paths=((".config", "zed"),),
    app_names=("Zed.app",),
    executables=("zed",),
    notes="Zed's settings.json usually contains comments, so AXPilot prints the snippet to paste "
    "instead of rewriting the file (unless you force it). Older Zed releases used a nested "
    '`"command": {"path": ..., "args": [...]}` form.',
    restart_hint="Zed reloads settings automatically; check the Agent panel's settings.",
)

CODEX = ClientTarget(
    name="codex",
    display_name="OpenAI Codex CLI",
    format="toml",
    servers_key=("mcp_servers",),
    user_path=(".codex", "config.toml"),
    detect_paths=((".codex",),),
    executables=("codex",),
    notes="Adding AXPilot keeps the rest of config.toml as it is. Updating or removing an "
    "existing entry may rewrite the file without its comments (a backup is kept).",
    restart_hint="Start a new Codex session.",
    aliases=("codex-cli", "openai-codex"),
)

GEMINI = ClientTarget(
    name="gemini",
    display_name="Gemini CLI",
    format="jsonc",
    servers_key=("mcpServers",),
    user_path=(".gemini", "settings.json"),
    project_path=(".gemini", "settings.json"),
    detect_paths=((".gemini",),),
    executables=("gemini",),
    restart_hint="Start a new Gemini CLI session (`/mcp` lists the servers).",
    aliases=("gemini-cli",),
)

LM_STUDIO = ClientTarget(
    name="lm-studio",
    display_name="LM Studio",
    format="json",
    servers_key=("mcpServers",),
    user_path=(".lmstudio", "mcp.json"),
    detect_paths=((".lmstudio",),),
    app_names=("LM Studio.app",),
    executables=("lms",),
    notes="The same file opens from LM Studio's Program tab → Install → Edit mcp.json.",
    restart_hint="LM Studio reloads mcp.json automatically.",
    aliases=("lmstudio",),
)

GOOSE = ClientTarget(
    name="goose",
    display_name="Goose",
    format="manual",
    servers_key=("extensions",),
    user_path=(".config", "goose", "config.yaml"),
    build_entry=goose_extension,
    detect_paths=((".config", "goose"),),
    app_names=("Goose.app",),
    executables=("goose",),
    notes="Goose keeps extensions in config.yaml, which AXPilot does not rewrite. Paste the "
    "snippet under `extensions:`, or run `goose configure` → Add Extension → Command-line Extension.",
    restart_hint="Start a new Goose session.",
)

CLINE = ClientTarget(
    name="cline",
    display_name="Cline",
    format="json",
    servers_key=("mcpServers",),
    user_path=(
        *_VSCODE_USER,
        "globalStorage",
        "saoudrizwan.claude-dev",
        "settings",
        "cline_mcp_settings.json",
    ),
    detect_paths=((*_VSCODE_USER, "globalStorage", "saoudrizwan.claude-dev"),),
    notes="Cline (the VS Code extension) keeps its MCP settings in VS Code's global storage.",
    restart_hint="Open Cline's MCP Servers view to check that axpilot is connected.",
)

CONTINUE = ClientTarget(
    name="continue",
    display_name="Continue",
    format="yaml-file",
    servers_key=("mcpServers",),
    user_path=(".continue", "mcpServers", "axpilot.yaml"),
    project_path=(".continue", "mcpServers", "axpilot.yaml"),
    build_entry=continue_server,
    document=continue_document,
    detect_paths=((".continue",),),
    notes="AXPilot writes its own block file; delete it to disconnect.",
    restart_hint="Reload Continue (or the editor window) to pick up the new block.",
    aliases=("continue-dev",),
)

TARGETS: tuple[ClientTarget, ...] = (
    CLAUDE_DESKTOP,
    CLAUDE_CODE,
    CURSOR,
    VSCODE,
    WINDSURF,
    ANTIGRAVITY,
    ZED,
    CODEX,
    GEMINI,
    LM_STUDIO,
    GOOSE,
    CLINE,
    CONTINUE,
)
"""Every supported client, in the order ``axpilot connect --list`` shows them."""


def _normalize(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace(" ", "-")


def target_names() -> list[str]:
    """The slugs of all supported clients."""
    return [target.name for target in TARGETS]


def get_target(name: str) -> ClientTarget:
    """Look a client up by slug or alias (case-insensitive; ``_`` and spaces count as ``-``).

    Raises ``INVALID_ARGUMENT`` naming the closest match and listing the supported clients.
    """
    wanted = _normalize(name)
    for target in TARGETS:
        if wanted == target.name or wanted in target.aliases:
            return target
    known = target_names()
    candidates = known + [alias for target in TARGETS for alias in target.aliases]
    close = difflib.get_close_matches(wanted, candidates, n=1, cutoff=0.6)
    suggestion = ""
    if close:
        match = next(t for t in TARGETS if close[0] == t.name or close[0] in t.aliases)
        suggestion = f" Did you mean `{match.name}`?"
    raise AXPilotError(
        ErrorCode.INVALID_ARGUMENT,
        f"Unknown MCP client {name!r}.{suggestion}",
        hint="Supported clients: " + ", ".join(known) + ".",
    )


def detect_installed() -> list[ClientTarget]:
    """The supported clients that appear to be installed, in :data:`TARGETS` order."""
    return [target for target in TARGETS if target.detect()]
