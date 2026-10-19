from __future__ import annotations

import json
import os
import stat
import tomllib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from axpilot.clients import (
    ClientTarget,
    ConnectResult,
    connect,
    disconnect,
    get_target,
    inspect_entry,
    merge_entry,
    render_snippet,
)
from axpilot.clients import fileops as fileops_module
from axpilot.errors import AXPilotError, ErrorCode

COMMAND = "/opt/axpilot/bin/axpilot"
ARGS = ["stdio"]
PLAIN = {"command": COMMAND, "args": ARGS}
TYPED = {"type": "stdio", "command": COMMAND, "args": ARGS}

JSON_SHAPES: dict[str, dict[str, Any]] = {
    "claude-desktop": {"mcpServers": {"axpilot": PLAIN}},
    "claude-code": {"mcpServers": {"axpilot": TYPED}},
    "cursor": {"mcpServers": {"axpilot": PLAIN}},
    "vscode": {"servers": {"axpilot": TYPED}},
    "windsurf": {"mcpServers": {"axpilot": PLAIN}},
    "zed": {"context_servers": {"axpilot": PLAIN}},
    "gemini": {"mcpServers": {"axpilot": PLAIN}},
    "lm-studio": {"mcpServers": {"axpilot": PLAIN}},
    "cline": {"mcpServers": {"axpilot": PLAIN}},
}
FILE_TARGETS = [*JSON_SHAPES, "codex", "continue"]

CONTINUE_FILE = (
    "# Written by `axpilot connect continue`. AXPilot rewrites this file when you connect again;\n"
    "# delete it to disconnect AXPilot from Continue.\n"
    'name: "AXPilot"\n'
    'version: "0.0.1"\n'
    'schema: "v1"\n'
    "mcpServers:\n"
    '  - name: "axpilot"\n'
    '    type: "stdio"\n'
    f'    command: "{COMMAND}"\n'
    "    args:\n"
    '      - "stdio"\n'
)


def add(name: str | ClientTarget, **kwargs: Any) -> ConnectResult:
    """``connect`` with this module's default command and arguments."""
    kwargs.setdefault("command", COMMAND)
    kwargs.setdefault("args", ARGS)
    return connect(name, **kwargs)


def config_file(name: str) -> Path:
    return get_target(name).config_path("user")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".bak-*"))


# --------------------------------------------------------------------------------------------
# Shapes


@pytest.mark.parametrize("name", list(JSON_SHAPES))
def test_connect_writes_each_clients_json_shape(name: str, home: Path, clock: Callable[[], datetime]) -> None:
    result = add(name, clock=clock)
    path = config_file(name)
    assert result.changed
    assert result.path == path
    assert result.backup_path is None
    assert result.target == name
    assert not result.manual
    assert read_json(path) == JSON_SHAPES[name]
    assert json.loads(result.snippet) == JSON_SHAPES[name]
    assert "Added axpilot to" in result.message


def test_connect_codex_writes_toml(home: Path) -> None:
    result = add("codex", env={"AXPILOT_TOOLS_PROFILE": "minimal"})
    data = tomllib.loads(config_file("codex").read_text())
    expected = {**PLAIN, "env": {"AXPILOT_TOOLS_PROFILE": "minimal"}}
    assert data == {"mcp_servers": {"axpilot": expected}}
    assert tomllib.loads(result.snippet) == data


def test_connect_continue_writes_its_own_yaml_block(home: Path) -> None:
    result = add("continue")
    path = home / ".continue" / "mcpServers" / "axpilot.yaml"
    assert path.read_text() == CONTINUE_FILE
    assert result.snippet == CONTINUE_FILE


def test_continue_env_is_written(home: Path) -> None:
    add("continue", env={"A": "1"})
    text = config_file("continue").read_text()
    assert '    env:\n      A: "1"\n' in text


def test_goose_is_manual_and_writes_nothing(home: Path) -> None:
    result = add("goose")
    assert result.manual
    assert not result.changed
    assert not config_file("goose").exists()
    assert result.snippet == (
        "extensions:\n"
        "  axpilot:\n"
        '    name: "axpilot"\n'
        '    type: "stdio"\n'
        f'    cmd: "{COMMAND}"\n'
        "    args:\n"
        '      - "stdio"\n'
        "    enabled: true\n"
        "    timeout: 300\n"
        "    envs: {}\n"
    )
    assert "goose configure" in result.message


def test_goose_mentions_an_existing_extension(home: Path) -> None:
    path = config_file("goose")
    path.parent.mkdir(parents=True)
    path.write_text(
        "GOOSE_PROVIDER: openai\nextensions:\n  developer:\n    enabled: true\n  axpilot:\n    cmd: x\n"
    )
    result = add("goose")
    assert "already lists" in result.message
    assert inspect_entry("goose").configured is True


def test_goose_disconnect(home: Path) -> None:
    assert "has no axpilot entry" in disconnect("goose").message
    path = config_file("goose")
    path.parent.mkdir(parents=True)
    path.write_text("extensions:\n  axpilot:\n    cmd: x\n")
    result = disconnect("goose")
    assert result.manual
    assert not result.changed
    assert "Remove the `axpilot` entry" in result.message
    assert path.read_text() == "extensions:\n  axpilot:\n    cmd: x\n"


# --------------------------------------------------------------------------------------------
# Idempotency, backups, dry runs


@pytest.mark.parametrize("name", FILE_TARGETS)
def test_second_connect_changes_nothing(name: str, home: Path, clock: Callable[[], datetime]) -> None:
    add(name, clock=clock)
    path = config_file(name)
    before = path.read_bytes()
    os.utime(path, (1_000_000, 1_000_000))
    result = add(name, clock=clock)
    assert not result.changed
    assert result.backup_path is None
    assert "already has this axpilot entry" in result.message
    assert path.read_bytes() == before
    assert path.stat().st_mtime == 1_000_000
    assert backups(path) == []


@pytest.mark.parametrize("name", FILE_TARGETS)
def test_changing_an_existing_file_takes_a_backup(
    name: str, home: Path, clock: Callable[[], datetime]
) -> None:
    add(name, command="/old/axpilot", clock=clock)
    path = config_file(name)
    old = path.read_bytes()
    result = add(name, clock=clock)
    assert result.changed
    assert result.backup_path == path.with_name(path.name + ".bak-20261019-093015")
    assert result.backup == result.backup_path
    assert result.backup_path is not None
    assert result.backup_path.read_bytes() == old
    assert "Updated axpilot in" in result.message


@pytest.mark.parametrize("name", FILE_TARGETS)
def test_dry_run_writes_nothing(name: str, home: Path) -> None:
    result = add(name, dry_run=True)
    path = config_file(name)
    assert result.changed
    assert result.dry_run
    assert not path.exists()
    assert result.preview is not None
    assert COMMAND in result.preview
    assert result.message.startswith("Would add axpilot to")


def test_dry_run_on_existing_file_keeps_it(home: Path) -> None:
    path = config_file("cursor")
    path.parent.mkdir(parents=True)
    path.write_text('{"mcpServers": {"other": {"command": "x"}}}')
    result = add("cursor", dry_run=True)
    assert result.changed
    assert path.read_text() == '{"mcpServers": {"other": {"command": "x"}}}'
    assert backups(path) == []
    assert result.preview is not None
    assert json.loads(result.preview)["mcpServers"] == {"other": {"command": "x"}, "axpilot": PLAIN}


def test_dry_run_when_already_configured_reports_no_change(home: Path) -> None:
    add("cursor")
    result = add("cursor", dry_run=True)
    assert not result.changed
    assert result.preview is None


# --------------------------------------------------------------------------------------------
# Never touching other entries


def test_other_servers_settings_order_and_indent_are_kept(home: Path, clock: Callable[[], datetime]) -> None:
    path = config_file("claude-desktop")
    path.parent.mkdir(parents=True)
    original = {
        "globalShortcut": "Cmd+Space",
        "mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "server-filesystem", "/tmp"]}},
        "preferences": {"theme": "dark"},
    }
    path.write_text(json.dumps(original, indent=4))
    add("claude-desktop", clock=clock)
    text = path.read_text()
    data = json.loads(text)
    assert list(data) == ["globalShortcut", "mcpServers", "preferences"]
    assert list(data["mcpServers"]) == ["filesystem", "axpilot"]
    assert data["mcpServers"]["filesystem"] == original["mcpServers"]["filesystem"]
    assert data["preferences"] == {"theme": "dark"}
    assert '\n    "globalShortcut"' in text
    assert text.endswith("}\n")


def test_unknown_keys_of_our_entry_are_kept_and_transport_keys_dropped(home: Path) -> None:
    path = config_file("cline")
    path.parent.mkdir(parents=True)
    old_entry = {"url": "http://localhost:7788/mcp", "autoApprove": ["snapshot"], "disabled": False}
    path.write_text(json.dumps({"mcpServers": {"axpilot": old_entry}}))
    add("cline")
    entry = read_json(path)["mcpServers"]["axpilot"]
    assert entry == {"autoApprove": ["snapshot"], "disabled": False, "command": COMMAND, "args": ARGS}


def test_env_none_keeps_existing_env_and_empty_env_removes_it(home: Path) -> None:
    add("cursor", env={"AXPILOT_TOOLS_PROFILE": "full"})
    path = config_file("cursor")
    assert read_json(path)["mcpServers"]["axpilot"]["env"] == {"AXPILOT_TOOLS_PROFILE": "full"}
    assert not add("cursor").changed  # env=None keeps it
    assert add("cursor", env={"A": "1"}).changed
    assert read_json(path)["mcpServers"]["axpilot"]["env"] == {"A": "1"}
    assert add("cursor", env={}).changed
    assert "env" not in read_json(path)["mcpServers"]["axpilot"]


def test_file_mode_and_line_endings_are_preserved(home: Path) -> None:
    path = config_file("cursor")
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{\r\n  "mcpServers": {}\r\n}\r\n')
    path.chmod(0o600)
    add("cursor")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_empty_file_counts_as_empty_config(home: Path) -> None:
    path = config_file("windsurf")
    path.parent.mkdir(parents=True)
    path.write_text("  \n")
    result = add("windsurf")
    assert result.changed
    assert read_json(path) == JSON_SHAPES["windsurf"]


def test_symlinked_config_is_updated_in_place(home: Path) -> None:
    real = home / "dotfiles" / "mcp.json"
    real.parent.mkdir()
    real.write_text("{}")
    link = config_file("cursor")
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    add("cursor")
    assert link.is_symlink()
    assert read_json(real) == JSON_SHAPES["cursor"]


def test_failed_write_leaves_the_original(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = config_file("cursor")
    path.parent.mkdir(parents=True)
    path.write_text('{"mcpServers": {}}')

    def no_space(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fileops_module.os, "replace", no_space)
    with pytest.raises(AXPilotError) as info:
        add("cursor")
    assert info.value.code is ErrorCode.COMMAND_FAILED
    assert path.read_text() == '{"mcpServers": {}}'
    assert sorted(p.name for p in path.parent.iterdir() if not p.name.startswith("mcp.json.bak-")) == [
        "mcp.json"
    ]


# --------------------------------------------------------------------------------------------
# Invalid files


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ('{"mcpServers": {', "not valid JSON"),
        ("[1, 2]", "JSON object at the top level"),
        ('{"mcpServers": []}', '"mcpServers"'),
        ('{"mcpServers": "x"}', "is not an object"),
    ],
)
def test_invalid_json_is_reported_and_left_alone(home: Path, content: str, fragment: str) -> None:
    path = config_file("cursor")
    path.parent.mkdir(parents=True)
    path.write_text(content)
    with pytest.raises(AXPilotError) as info:
        add("cursor")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert fragment in info.value.message
    assert path.read_text() == content
    assert backups(path) == []


def test_nested_servers_key_must_be_object(home: Path) -> None:
    path = config_file("zed")
    path.parent.mkdir(parents=True)
    path.write_text('{"context_servers": 3}')
    with pytest.raises(AXPilotError, match="context_servers"):
        add("zed")


def test_invalid_toml_is_reported(home: Path) -> None:
    path = config_file("codex")
    path.parent.mkdir(parents=True)
    path.write_text("model = = 1\n")
    with pytest.raises(AXPilotError) as info:
        add("codex")
    assert "not valid TOML" in info.value.message


# --------------------------------------------------------------------------------------------
# JSONC


VSCODE_WITH_COMMENTS = """{
  // My servers
  "servers": {
    "github": { "type": "http", "url": "https://api.githubcopilot.com/mcp/" }, /* keep */
  },
  "inputs": [],
}
"""


def write_vscode(home: Path, text: str) -> Path:
    path = config_file("vscode")
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_jsonc_with_comments_is_not_rewritten(home: Path) -> None:
    path = write_vscode(home, VSCODE_WITH_COMMENTS)
    result = add("vscode")
    assert result.manual
    assert not result.changed
    assert path.read_text() == VSCODE_WITH_COMMENTS
    assert json.loads(result.snippet) == JSON_SHAPES["vscode"]
    assert "--force" in result.message
    assert "comments" in result.message


def test_jsonc_with_comments_force_rewrites_and_warns(home: Path, clock: Callable[[], datetime]) -> None:
    path = write_vscode(home, VSCODE_WITH_COMMENTS)
    result = add("vscode", force=True, clock=clock)
    assert result.changed
    assert result.warnings
    assert "comments" in result.warnings[0]
    data = read_json(path)
    assert data["servers"]["github"]["type"] == "http"
    assert data["servers"]["axpilot"] == TYPED
    assert data["inputs"] == []
    assert result.backup_path is not None
    assert result.backup_path.read_text() == VSCODE_WITH_COMMENTS


def test_jsonc_without_comments_but_trailing_commas_is_rewritten(home: Path) -> None:
    path = write_vscode(home, '{"servers": {"a": {"command": "x",},},}')
    result = add("vscode")
    assert result.changed
    assert not result.warnings
    assert read_json(path)["servers"] == {"a": {"command": "x"}, "axpilot": TYPED}


def test_jsonc_with_comments_already_configured_is_unchanged(home: Path) -> None:
    text = '{\n  // ours\n  "servers": {"axpilot": ' + json.dumps(TYPED) + "}\n}\n"
    path = write_vscode(home, text)
    result = add("vscode")
    assert not result.changed
    assert not result.manual
    assert path.read_text() == text


def test_zed_settings_template_with_comments(home: Path) -> None:
    path = config_file("zed")
    path.parent.mkdir(parents=True)
    text = (
        "// Zed settings\n//\n// For information on how to configure Zed, see the docs.\n"
        '{\n  "theme": "One Dark"\n}\n'
    )
    path.write_text(text)
    result = add("zed")
    assert result.manual
    assert path.read_text() == text
    forced = add("zed", force=True)
    assert forced.changed
    assert read_json(path) == {"theme": "One Dark", **JSON_SHAPES["zed"]}


# --------------------------------------------------------------------------------------------
# TOML


CODEX_CONFIG = """# Codex settings
model = "o4-mini"  # fast

[mcp_servers.docs]
command = "docs-server"
args = ["--port", "1"]

[profiles.work]
model = "o3"
"""


def write_codex(home: Path, text: str = CODEX_CONFIG) -> Path:
    path = config_file("codex")
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_toml_append_keeps_comments_and_other_tables(home: Path) -> None:
    path = write_codex(home)
    result = add("codex")
    text = path.read_text()
    assert text.startswith(CODEX_CONFIG.rstrip())
    assert not result.warnings
    data = tomllib.loads(text)
    assert data["mcp_servers"] == {
        "docs": {"command": "docs-server", "args": ["--port", "1"]},
        "axpilot": PLAIN,
    }
    assert data["profiles"] == {"work": {"model": "o3"}}
    assert data["model"] == "o4-mini"


def test_toml_update_rewrites_only_our_table(home: Path) -> None:
    path = write_codex(home)
    add("codex", command="/old/axpilot", env={"A": "1"})
    result = add("codex")
    text = path.read_text()
    assert "# Codex settings" in text
    assert "# fast" in text
    assert not result.warnings
    assert tomllib.loads(text)["mcp_servers"]["axpilot"] == {**PLAIN, "env": {"A": "1"}}
    assert text.count("[mcp_servers.axpilot]") == 1


def test_toml_inline_entry_falls_back_to_full_rewrite_with_warning(
    home: Path, clock: Callable[[], datetime]
) -> None:
    original = '# comment\n[mcp_servers]\naxpilot = { command = "/old", args = [] }\n'
    path = write_codex(home, original)
    result = add("codex", clock=clock)
    assert result.changed
    assert result.warnings
    assert "comments" in result.warnings[0]
    assert tomllib.loads(path.read_text()) == {"mcp_servers": {"axpilot": PLAIN}}
    assert result.backup_path is not None
    assert result.backup_path.read_text() == original


def test_toml_keeps_unknown_keys_of_our_entry(home: Path) -> None:
    path = write_codex(home, '[mcp_servers.axpilot]\ncommand = "/old"\nstartup_timeout_sec = 30\n')
    add("codex")
    entry = tomllib.loads(path.read_text())["mcp_servers"]["axpilot"]
    assert entry == {"command": COMMAND, "startup_timeout_sec": 30, "args": ARGS}


def test_toml_disconnect_keeps_everything_else(home: Path) -> None:
    path = write_codex(home)
    add("codex")
    result = disconnect("codex")
    assert result.changed
    assert not result.warnings
    assert path.read_text() == CODEX_CONFIG


def test_toml_disconnect_of_only_server_leaves_no_empty_table(home: Path) -> None:
    path = write_codex(home, 'model = "x"\n')
    add("codex")
    disconnect("codex")
    assert path.read_text() == 'model = "x"\n'


# --------------------------------------------------------------------------------------------
# disconnect


def test_disconnect_removes_only_our_entry(home: Path, clock: Callable[[], datetime]) -> None:
    path = config_file("claude-desktop")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "o"}, "axpilot": PLAIN}}))
    result = disconnect("claude-desktop", clock=clock)
    assert result.changed
    assert result.backup_path is not None
    assert read_json(path) == {"mcpServers": {"other": {"command": "o"}}}
    assert "Removed axpilot from Claude Desktop" in result.message


@pytest.mark.parametrize("name", FILE_TARGETS)
def test_disconnect_without_entry_changes_nothing(name: str, home: Path) -> None:
    result = disconnect(name)
    assert not result.changed
    assert "has no axpilot entry" in result.message
    assert not config_file(name).exists()


def test_disconnect_with_other_servers_only(home: Path) -> None:
    path = config_file("gemini")
    path.parent.mkdir(parents=True)
    path.write_text('{"mcpServers": {"other": {}}}')
    assert not disconnect("gemini").changed
    assert path.read_text() == '{"mcpServers": {"other": {}}}'


def test_disconnect_jsonc_with_comments_needs_force(home: Path) -> None:
    text = '{\n  // ours\n  "servers": {"axpilot": ' + json.dumps(TYPED) + "}\n}\n"
    path = write_vscode(home, text)
    blocked = disconnect("vscode")
    assert blocked.manual
    assert not blocked.changed
    assert "Remove the axpilot entry" in blocked.message
    assert json.loads(blocked.snippet) == {"servers": {"axpilot": TYPED}}
    assert path.read_text() == text
    forced = disconnect("vscode", force=True)
    assert forced.changed
    assert read_json(path) == {"servers": {}}


def test_disconnect_continue_deletes_file_with_backup(home: Path, clock: Callable[[], datetime]) -> None:
    add("continue")
    path = config_file("continue")
    result = disconnect("continue", clock=clock)
    assert result.changed
    assert not path.exists()
    assert result.backup_path is not None
    assert result.backup_path.read_text() == CONTINUE_FILE


def test_disconnect_dry_run(home: Path) -> None:
    add("cursor")
    before = config_file("cursor").read_text()
    result = disconnect("cursor", dry_run=True)
    assert result.changed
    assert result.dry_run
    assert result.message.startswith("Would remove axpilot from")
    assert config_file("cursor").read_text() == before


# --------------------------------------------------------------------------------------------
# Claude Code through its CLI


def claude_json(home: Path, servers: dict[str, Any]) -> Path:
    path = home / ".claude.json"
    path.write_text(json.dumps({"numStartups": 3, "mcpServers": servers}))
    return path


def test_claude_code_uses_cli_when_available(home: Path, claude_cli: Path, runner: Any) -> None:
    result = add("claude-code", runner=runner)
    assert result.changed
    assert runner.calls == [
        [str(claude_cli), "mcp", "add", "--scope", "user", "axpilot", "--", COMMAND, "stdio"]
    ]
    assert result.commands == (tuple(runner.calls[0]),)
    assert result.snippet == f"claude mcp add --scope user axpilot -- {COMMAND} stdio\n"
    assert result.backup_path is None
    assert not (home / ".claude.json").exists()  # the CLI owns the file
    assert "claude mcp add" in result.message


def test_claude_code_cli_env_flags_follow_the_name(home: Path, claude_cli: Path, runner: Any) -> None:
    add("claude-code", runner=runner, env={"AXPILOT_TOOLS_PROFILE": "minimal", "B": "x y"})
    assert runner.calls[0][:10] == [
        str(claude_cli),
        "mcp",
        "add",
        "--scope",
        "user",
        "axpilot",
        "-e",
        "AXPILOT_TOOLS_PROFILE=minimal",
        "-e",
        "B=x y",
    ]
    assert runner.calls[0][10:] == ["--", COMMAND, "stdio"]


def test_claude_code_cli_is_idempotent(home: Path, claude_cli: Path, runner: Any) -> None:
    claude_json(home, {"axpilot": {"type": "stdio", "command": COMMAND, "args": ARGS, "env": {}}})
    result = add("claude-code", runner=runner)
    assert not result.changed
    assert runner.calls == []


def test_claude_code_cli_replaces_a_different_entry(home: Path, claude_cli: Path, runner: Any) -> None:
    claude_json(home, {"axpilot": {"type": "stdio", "command": "/old", "args": [], "env": {"A": "1"}}})
    result = add("claude-code", runner=runner)
    assert result.changed
    assert runner.calls[0] == [str(claude_cli), "mcp", "remove", "--scope", "user", "axpilot"]
    assert runner.calls[1] == [
        *[str(claude_cli), "mcp", "add", "--scope", "user", "axpilot"],
        *["-e", "A=1", "--", COMMAND, "stdio"],
    ]
    assert result.message.startswith("Updated axpilot in Claude Code")


def test_claude_code_cli_dry_run_runs_nothing(home: Path, claude_cli: Path, runner: Any) -> None:
    result = add("claude-code", runner=runner, dry_run=True)
    assert result.changed
    assert result.dry_run
    assert runner.calls == []
    assert len(result.commands) == 1
    assert result.message.startswith("Would run: claude mcp add")


def test_claude_code_cli_failure(home: Path, claude_cli: Path, runner: Any) -> None:
    runner.responses[(str(claude_cli), "mcp", "add")] = (1, "MCP server axpilot already exists")
    with pytest.raises(AXPilotError) as info:
        add("claude-code", runner=runner)
    assert info.value.code is ErrorCode.COMMAND_FAILED
    assert "already exists" in info.value.message
    assert "claude mcp add" in (info.value.hint or "")


def test_claude_code_without_cli_edits_the_file(home: Path, claude_cli: Path, runner: Any) -> None:
    path = claude_json(home, {})
    result = add("claude-code", runner=runner, use_cli=False)
    assert runner.calls == []
    assert result.changed
    assert result.backup_path is not None
    assert read_json(path) == {"numStartups": 3, "mcpServers": {"axpilot": TYPED}}


def test_claude_code_project_scope_writes_mcp_json(
    home: Path, claude_cli: Path, runner: Any, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    result = add("claude-code", scope="project", project_dir=project, runner=runner)
    assert runner.calls == []
    assert result.path == project / ".mcp.json"
    assert read_json(project / ".mcp.json") == {"mcpServers": {"axpilot": TYPED}}


def test_claude_code_cli_disconnect(home: Path, claude_cli: Path, runner: Any) -> None:
    assert not disconnect("claude-code", runner=runner).changed
    assert runner.calls == []
    claude_json(home, {"axpilot": TYPED})
    result = disconnect("claude-code", runner=runner)
    assert result.changed
    assert runner.calls == [[str(claude_cli), "mcp", "remove", "--scope", "user", "axpilot"]]
    assert result.snippet == "claude mcp remove --scope user axpilot\n"


def test_claude_code_cli_with_broken_claude_json(home: Path, claude_cli: Path, runner: Any) -> None:
    (home / ".claude.json").write_text("{broken")
    with pytest.raises(AXPilotError) as info:
        add("claude-code", runner=runner)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert runner.calls == []


# --------------------------------------------------------------------------------------------
# Scopes, names, validation


@pytest.mark.parametrize("name", ["cursor", "vscode", "gemini", "continue"])
def test_project_scope_writes_inside_the_project(name: str, home: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    result = add(name, scope="project", project_dir=project)
    assert result.path.is_relative_to(project)
    assert result.path.exists()
    assert not result.warnings


def test_project_scope_warns_about_home_paths(home: Path, tmp_path: Path) -> None:
    result = add(
        "cursor", scope="project", project_dir=tmp_path / "p", command=str(home / ".local/bin/axpilot")
    )
    assert result.warnings
    assert "inside your home folder" in result.warnings[0]


def test_unsupported_project_scope(home: Path, tmp_path: Path) -> None:
    with pytest.raises(AXPilotError) as info:
        add("claude-desktop", scope="project", project_dir=tmp_path)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_custom_server_name(home: Path) -> None:
    add("cursor", server_name="axpilot-fake", args=["stdio", "--engine", "fake"])
    servers = read_json(config_file("cursor"))["mcpServers"]
    assert servers == {"axpilot-fake": {"command": COMMAND, "args": ["stdio", "--engine", "fake"]}}
    add("continue", server_name="axpilot-fake")
    path = home / ".continue" / "mcpServers" / "axpilot-fake.yaml"
    assert 'name: "axpilot-fake"' in path.read_text()
    assert inspect_entry("cursor", server_name="axpilot-fake").configured
    assert not inspect_entry("cursor").configured


@pytest.mark.parametrize(
    "kwargs",
    [
        {"command": ""},
        {"command": "   "},
        {"command": "/bin/ax\0pilot"},
        {"args": "stdio"},
        {"args": ["stdio", 3]},
        {"env": {"BAD-NAME": "x"}},
        {"env": {"A": 1}},
        {"server_name": "has space"},
        {"server_name": ""},
        {"server_name": "-leading"},
    ],
)
def test_invalid_arguments(home: Path, kwargs: dict[str, Any]) -> None:
    with pytest.raises(AXPilotError) as info:
        add("cursor", **kwargs)
    assert info.value.code is ErrorCode.INVALID_ARGUMENT
    assert not config_file("cursor").exists()


def test_unknown_client_name(home: Path) -> None:
    with pytest.raises(AXPilotError) as info:
        add("cursorr")
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


def test_target_objects_are_accepted(home: Path) -> None:
    assert add(get_target("lm-studio")).changed


# --------------------------------------------------------------------------------------------
# inspect_entry, merge_entry, render_snippet


def test_inspect_entry_states(home: Path) -> None:
    missing = inspect_entry("cursor")
    assert (missing.exists, missing.configured, missing.entry) == (False, False, None)
    add("cursor")
    present = inspect_entry("cursor")
    assert present.configured is True
    assert present.entry == PLAIN
    assert present.command == COMMAND
    config_file("windsurf").parent.mkdir(parents=True)
    config_file("windsurf").write_text("{oops")
    broken = inspect_entry("windsurf")
    assert broken.configured is None
    assert broken.error
    assert "not valid JSON" in broken.error


def test_inspect_entry_toml_and_yaml(home: Path) -> None:
    add("codex")
    assert inspect_entry("codex").command == COMMAND
    add("continue")
    status = inspect_entry("continue")
    assert status.configured is True
    assert status.command == COMMAND


def test_inspect_entry_for_goose_without_extension(home: Path) -> None:
    path = config_file("goose")
    path.parent.mkdir(parents=True)
    path.write_text("extensions:\n  developer:\n    axpilot: nested-not-a-key\n")
    assert inspect_entry("goose").configured is False


def test_merge_entry() -> None:
    existing = {"disabled": True, "command": "old", "url": "http://x", "env": {"A": "1"}}
    new = {"command": "new", "args": ["stdio"]}
    assert merge_entry(existing, new, replace_env=False) == {
        "disabled": True,
        "command": "new",
        "env": {"A": "1"},
        "args": ["stdio"],
    }
    assert merge_entry(existing, new, replace_env=True) == {
        "disabled": True,
        "command": "new",
        "args": ["stdio"],
    }
    assert merge_entry("not a dict", new, replace_env=False) == new
    assert merge_entry(None, new, replace_env=False) == new


def test_render_snippet_formats() -> None:
    cursor = get_target("cursor")
    assert json.loads(render_snippet(cursor, PLAIN)) == {"mcpServers": {"axpilot": PLAIN}}
    codex = get_target("codex")
    assert tomllib.loads(render_snippet(codex, PLAIN)) == {"mcp_servers": {"axpilot": PLAIN}}
    goose = get_target("goose")
    assert render_snippet(goose, goose.entry(COMMAND, ARGS)).startswith("extensions:\n  axpilot:\n")
