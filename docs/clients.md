# Connect a client

Every MCP client connects the same way: it starts `axpilot stdio` and talks MCP over stdin and
stdout. `axpilot connect <client>` writes the right config
for you, keeps a backup of the old file, and prints what it changed:

```sh
axpilot connect --list                      # supported and detected clients
axpilot connect claude-desktop --dry-run    # show the change without writing it
axpilot connect cursor --scope project      # project config instead of user config, where supported
```

## From source (today)

Until AXPilot is on PyPI, point your client at a source checkout and run it in **direct mode**. Use
absolute paths: GUI apps such as Claude Desktop don't start from your shell and don't see your
`PATH`. Find the paths with `which uv` and, inside the clone, `pwd`.

The command is always:

```
/absolute/path/to/uv --directory /absolute/path/to/axpilot run axpilot stdio --direct
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "axpilot": {
      "command": "/Users/you/.local/bin/uv",
      "args": ["--directory", "/Users/you/src/axpilot", "run", "axpilot", "stdio", "--direct"]
    }
  }
}
```

**Claude Code**:

```sh
claude mcp add axpilot -- uv --directory /Users/you/src/axpilot run axpilot stdio --direct
```

**Cursor** (`~/.cursor/mcp.json`) and **VS Code** (`.vscode/mcp.json`): use the same `command` and
`args` in the formats shown below.

In direct mode the client app needs the Accessibility permission (Claude, Cursor, VS Code, or the
terminal running Claude Code); see [Permissions](permissions.md#direct-mode). Restart the client
after granting it. To experiment without touching your desktop, replace `--direct` with
`--engine fake`.

## After v0.1 is published

The snippets below are what `axpilot connect` will write. Where a client is started by a GUI app,
`connect` uses the absolute path of `axpilot` (for example `/Users/you/.local/bin/axpilot`).

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "axpilot": { "command": "/Users/you/.local/bin/axpilot", "args": ["stdio"] }
  }
}
```

Quit Claude completely (⌘Q) and reopen it. Its MCP logs are in `~/Library/Logs/Claude/`.

### Claude Code

```sh
claude mcp add axpilot -- axpilot stdio                 # this project
claude mcp add --scope user axpilot -- axpilot stdio    # all your projects
claude mcp list
```

### Cursor

`~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project):

```json
{ "mcpServers": { "axpilot": { "command": "axpilot", "args": ["stdio"] } } }
```

### VS Code (GitHub Copilot)

`.vscode/mcp.json` in a workspace, or your user configuration (Command Palette → *MCP: Open User
Configuration*). VS Code uses a `servers` key:

```json
{ "servers": { "axpilot": { "type": "stdio", "command": "axpilot", "args": ["stdio"] } } }
```

### OpenAI Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.axpilot]
command = "axpilot"
args = ["stdio"]
```

### Gemini CLI

`~/.gemini/settings.json` (or `.gemini/settings.json` in a project):

```json
{ "mcpServers": { "axpilot": { "command": "axpilot", "args": ["stdio"] } } }
```

### Other clients

These clients support MCP over stdio; `axpilot connect` will support them. Their config locations
and keys are verified per client before v0.3, so check each client's documentation in the meantime.

| Client | Where MCP servers are configured | Shape |
|---|---|---|
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` (as for Cursor) |
| Zed | Zed `settings.json` | `context_servers` entry with `command` and `args` |
| LM Studio | *Program → Install → Edit mcp.json* (`~/.lmstudio/mcp.json`) | `mcpServers` (as for Cursor) |
| Goose | `goose configure` → *Add Extension* → *Command-line Extension*, command `axpilot stdio` | extension entry in Goose's `config.yaml` |
| Cline | *MCP Servers* → *Configure* (`cline_mcp_settings.json`) | `mcpServers` (as for Cursor) |
| Continue | `.continue/mcpServers/*.yaml` or `config.yaml` | `mcpServers` list with `name`, `command`, `args` |

Any other client: use the generic JSON form,
`{"mcpServers": {"axpilot": {"command": "axpilot", "args": ["stdio"]}}}`, with an absolute path if
the client is a GUI app.

## Options

Pass options as extra arguments, or as `AXPILOT_*` environment variables in the client's `env`
block (see [Configuration](configuration.md)):

```json
{
  "mcpServers": {
    "axpilot": {
      "command": "axpilot",
      "args": ["stdio", "--profile", "minimal"],
      "env": { "AXPILOT_SAFETY_CONFIRM": "always" }
    }
  }
}
```

| Option | Use |
|---|---|
| `--profile minimal` | 10 tools, for small or local models with limited context |
| `--profile full` | Adds the `system`, `scripting` and `browser` toolsets (each still policy-gated) |
| `--profile computer` | A single `computer` tool for vision-first computer-use loops |
| `--naming windows-mcp` | Windows-MCP's tool names and parameter shapes (`Click(loc=[x, y])`, `Type(text, label=12)`, …) |

## Streamable HTTP

For frameworks and clients that prefer HTTP:

```sh
axpilot http --port 7788
```

The endpoint is `http://127.0.0.1:7788/mcp`. It binds to localhost only and requires a bearer
token, generated on first run and kept in your login keychain (item
`io.github.vansh3503.axpilot.http-token`; `--show-token` prints it, and
`security find-generic-password -s io.github.vansh3503.axpilot.http-token -w` reads it). If the
keychain can't be used, and on other systems, it is a file readable only by you,
`~/Library/Application Support/AXPilot/http-token` (see `token_store` in the
[configuration](configuration.md)). Requests with another `Host` header, or from a
web page whose `Origin` you did not allow with `--allow-origin`, are refused, and there are no CORS
headers. Remote access needs `--allow-remote` with TLS; an SSH tunnel or Tailscale is the safer
choice. Defaults come from the `[server]` section of the configuration.

## Several clients at once

Several clients can be connected at the same time. Observation tools run concurrently; input
actions are serialized (AXPilot has one "virtual hand"), and a client that waits too long for its
turn gets `BUSY`. In launcher mode, the first connection from a new client asks you to approve it:
*"Allow Cursor to see and control this Mac through AXPilot?"* (Allow or Deny). The answer is
remembered per client app in `~/Library/Application Support/AXPilot/paired-clients.json`; remove
an entry there to be asked again.
