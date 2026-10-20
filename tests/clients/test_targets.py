from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from axpilot.clients import TARGETS, detect_installed, get_target, target_names
from axpilot.errors import AXPilotError, ErrorCode

EXPECTED_USER_PATHS = {
    "claude-desktop": "Library/Application Support/Claude/claude_desktop_config.json",
    "claude-code": ".claude.json",
    "cursor": ".cursor/mcp.json",
    "vscode": "Library/Application Support/Code/User/mcp.json",
    "windsurf": ".codeium/windsurf/mcp_config.json",
    "antigravity": ".gemini/antigravity-ide/mcp_config.json",
    "zed": ".config/zed/settings.json",
    "codex": ".codex/config.toml",
    "gemini": ".gemini/settings.json",
    "lm-studio": ".lmstudio/mcp.json",
    "goose": ".config/goose/config.yaml",
    "cline": "Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/"
    "cline_mcp_settings.json",
    "continue": ".continue/mcpServers/axpilot.yaml",
}

EXPECTED_PROJECT_PATHS = {
    "claude-code": ".mcp.json",
    "cursor": ".cursor/mcp.json",
    "vscode": ".vscode/mcp.json",
    "gemini": ".gemini/settings.json",
    "continue": ".continue/mcpServers/axpilot.yaml",
}

EXPECTED_FORMATS = {
    "claude-desktop": ("json", ("mcpServers",)),
    "claude-code": ("json", ("mcpServers",)),
    "cursor": ("json", ("mcpServers",)),
    "vscode": ("jsonc", ("servers",)),
    "windsurf": ("json", ("mcpServers",)),
    "antigravity": ("json", ("mcpServers",)),
    "zed": ("jsonc", ("context_servers",)),
    "codex": ("toml", ("mcp_servers",)),
    "gemini": ("jsonc", ("mcpServers",)),
    "lm-studio": ("json", ("mcpServers",)),
    "goose": ("manual", ("extensions",)),
    "cline": ("json", ("mcpServers",)),
    "continue": ("yaml-file", ("mcpServers",)),
}


def test_unique_targets_in_documented_order() -> None:
    assert target_names() == list(EXPECTED_USER_PATHS)
    assert len(set(target_names())) == 13
    aliases = [alias for target in TARGETS for alias in target.aliases]
    assert len(aliases) == len(set(aliases))
    assert not set(aliases) & set(target_names())


def test_no_target_is_marked_verified_yet() -> None:
    assert all(target.verified is False for target in TARGETS)


@pytest.mark.parametrize("name", list(EXPECTED_FORMATS))
def test_format_and_servers_key(name: str) -> None:
    target = get_target(name)
    assert (target.format, target.servers_key) == EXPECTED_FORMATS[name]
    assert target.display_name
    assert target.restart_hint


@pytest.mark.parametrize("name", list(EXPECTED_USER_PATHS))
def test_user_paths_follow_home_at_call_time(
    name: str, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = get_target(name)
    assert target.config_path("user") == home / EXPECTED_USER_PATHS[name]
    other = tmp_path / "other-home"
    monkeypatch.setenv("HOME", str(other))
    assert target.config_path("user") == other / EXPECTED_USER_PATHS[name]


@pytest.mark.parametrize("name", list(EXPECTED_USER_PATHS))
def test_project_scope(name: str, home: Path, tmp_path: Path) -> None:
    target = get_target(name)
    project = tmp_path / "project"
    if name in EXPECTED_PROJECT_PATHS:
        assert target.scopes == ("user", "project")
        assert target.config_path("project", project) == project / EXPECTED_PROJECT_PATHS[name]
        assert target.config_path("project") == tmp_path / EXPECTED_PROJECT_PATHS[name]  # cwd
    else:
        assert target.scopes == ("user",)
        with pytest.raises(AXPilotError) as info:
            target.config_path("project", project)
        assert info.value.code is ErrorCode.INVALID_ARGUMENT
        assert "--scope user" in (info.value.hint or "")


def test_unknown_scope_is_rejected(home: Path) -> None:
    with pytest.raises(AXPilotError) as info:
        get_target("cursor").config_path("global")  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    ("alias", "name"),
    [
        ("Claude", "claude-desktop"),
        ("claude_desktop", "claude-desktop"),
        ("VS Code", "vscode"),
        ("copilot", "vscode"),
        ("lmstudio", "lm-studio"),
        ("gemini-cli", "gemini"),
        ("openai-codex", "codex"),
        ("CURSOR", "cursor"),
        ("Devin", "windsurf"),
        ("antigravity-ide", "antigravity"),
    ],
)
def test_aliases(alias: str, name: str) -> None:
    assert get_target(alias).name == name


def test_unknown_client_suggests_closest_match() -> None:
    with pytest.raises(AXPilotError) as info:
        get_target("cursr")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert "Did you mean `cursor`?" in info.value.message
    assert "claude-desktop" in (info.value.hint or "")


def test_unknown_client_without_suggestion() -> None:
    with pytest.raises(AXPilotError) as info:
        get_target("notepad")
    assert "Did you mean" not in info.value.message


def test_nothing_detected_on_an_empty_home(home: Path) -> None:
    assert detect_installed() == []


def test_detect_by_config_folder(home: Path) -> None:
    (home / ".cursor").mkdir()
    (home / ".codex").mkdir()
    assert [t.name for t in detect_installed()] == ["cursor", "codex"]


def test_detect_by_config_file(home: Path) -> None:
    (home / ".claude.json").write_text("{}")
    assert [t.name for t in detect_installed()] == ["claude-code"]


def test_detect_by_app_bundle(home: Path, applications: Path) -> None:
    (applications / "Claude.app").mkdir()
    (home / "Applications" / "Zed.app").mkdir(parents=True)
    assert [t.name for t in detect_installed()] == ["claude-desktop", "zed"]


def test_detect_by_executable_on_path(
    home: Path, bin_dir: Path, make_executable: Callable[[Path], Path]
) -> None:
    make_executable(bin_dir / "gemini")
    make_executable(bin_dir / "goose")
    assert [t.name for t in detect_installed()] == ["gemini", "goose"]


def test_claude_cli_found_outside_path(home: Path, make_executable: Callable[[Path], Path]) -> None:
    target = get_target("claude-code")
    assert target.find_cli() is None
    cli = make_executable(home / ".claude" / "local" / "claude")
    assert target.find_cli() == cli
    assert target.detect()


def test_non_executable_cli_is_ignored(home: Path) -> None:
    cli = home / ".local" / "bin" / "claude"
    cli.parent.mkdir(parents=True)
    cli.write_text("not executable")
    cli.chmod(0o644)
    assert get_target("claude-code").find_cli() is None
