# Claude Desktop extension (`.mcpb`): feasibility

Issue #10 asks whether AXPilot can ship as an MCP Bundle, so Claude Desktop users install it from
one file. Checked against the format's specification on 2026-10-20; nothing here has run on a Mac.

**Verdict:** feasible as a thin bundle that runs AXPilot's bridge, and worth building only after
spike S1 passes and the first PyPI release exists. It would replace `axpilot connect claude-desktop`
(one command today), not `axpilot setup`, which still has to run once.

## The format

An MCP Bundle (formerly DXT) is a zip with a `manifest.json`. Since manifest version 0.4 the
`uv` server type ships only a `pyproject.toml` and an entry point; the host app installs the
dependencies with uv, so the bundle stays small and holds no virtual environment. `compatibility`
can restrict the bundle to `darwin` and to a Python version. Sources: the
[manifest specification](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md) and its
[`hello-world-uv` example](https://github.com/modelcontextprotocol/mcpb/tree/main/examples/hello-world-uv).

## How AXPilot would use it

- **The bundle must not run the server itself.** Claude Desktop starts a bundle's server as its
  own child process, so macOS attributes that process's Accessibility and Screen Recording use to
  Claude Desktop. That is direct mode: the user would grant Claude Desktop control of the Mac, which
  the security model advises against for everyday use.
- **So the bundle runs the bridge**, exactly like the configuration `axpilot connect` writes: its
  `pyproject.toml` depends on `axpilot==<version>` and its entry point runs `axpilot stdio`. The
  bridge starts the host under `AXPilot.app` (`open -g -a`, not a child of Claude Desktop) and relays
  over the socket; pairing asks the user once to allow Claude.
- **The launcher never travels inside the bundle.** It comes from the PyPI wheel uv installs, which
  package managers don't quarantine, and `axpilot setup` copies it byte for byte. Whether Claude
  Desktop quarantines files it unpacks from a bundle therefore doesn't matter.

Sketch (not built):

```json
{
  "manifest_version": "0.4",
  "name": "axpilot",
  "display_name": "AXPilot",
  "version": "<axpilot version>",
  "description": "Lets Claude see and operate macOS apps through the Accessibility API, safely.",
  "author": { "name": "AXPilot contributors" },
  "server": {
    "type": "uv",
    "entry_point": "src/server.py",
    "mcp_config": { "command": "uv", "args": ["run", "--directory", "${__dirname}", "src/server.py"] }
  },
  "compatibility": { "platforms": ["darwin"], "runtimes": { "python": ">=3.11" } }
}
```

`src/server.py` would only call `axpilot.cli.main(["stdio"])`.

## What blocks it today

1. **First run.** Without `AXPilot.app`, the bridge exits with `HOST_UNAVAILABLE` ("Run `axpilot
   setup` once") before the MCP handshake, which Claude Desktop shows as a server that failed to
   start. A bundle needs the bridge to complete the handshake and return that message from every
   tool call instead, or a first-run path that installs the launcher non-interactively and lets
   macOS ask for permissions on first use.
2. **No release yet.** The bundle pins an `axpilot` version from PyPI.
3. **Unknowns that need a Mac with Claude Desktop:** the minimum Claude Desktop version that
   supports manifest 0.4 and the `uv` type; that `open -g -a` and the pairing dialog work when the
   bridge is started by Claude Desktop; and how Claude Desktop reports a bundle server's errors.

## Next steps

After S1 and the first release: make the bridge answer the handshake when the host is unavailable
(item 1), build `packaging/mcpb/` with `mcpb validate` and `mcpb pack`, and test the install on
macOS 14 and 15. Until then, `axpilot connect claude-desktop` remains the documented path.
