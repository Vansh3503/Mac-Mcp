# Install

> **Status:** AXPilot is in alpha: 0.1.0a1 is on PyPI, and the from-source path below works too.
> Both are verified on macOS 26 on Apple silicon; macOS 14 and 15 and Intel Macs are not tested
> yet. Channels that come later are marked *planned*.

## Requirements

- **macOS 14 Sonoma or later**, on Apple silicon or Intel ([ADR-3](adr/0003-minimum-macos-14.md)).
  macOS 27 runs on Apple silicon only.
- [uv](https://docs.astral.sh/uv/) (it installs a suitable Python for you; AXPilot needs Python 3.11+).
- From source: the Xcode Command Line Tools (`xcode-select --install`), to build the launcher.
- For development only: any OS. On Linux, AXPilot runs against a simulated desktop (`--engine fake`).

## Run from source (works today)

```sh
git clone https://github.com/vansh3503/Mac-Mcp.git axpilot
cd axpilot
uv sync
```

**Try the simulated desktop** (any OS, no permissions needed). It serves the same tools against an
in-memory desktop with Finder, TextEdit, Notes, Terminal, System Settings and a few more apps:

```sh
uv run axpilot stdio --engine fake
```

**Drive your real Mac** (launcher mode, recommended):

```sh
launcher/build.sh                      # builds launcher/build/AXPilot.app
uv run axpilot setup --dev-source .    # installs ~/Applications/AXPilot.app and its runtime
uv run axpilot connect claude-desktop  # or claude-code, cursor, vscode, … (see --list)
uv run axpilot doctor                  # checks the installation
```

Restart the client. The first time it connects, AXPilot asks whether to allow it; the first tool
call makes macOS ask for Accessibility for **AXPilot** ([Permissions](permissions.md)).

**Or direct mode**, without the launcher:

```sh
uv run axpilot stdio --direct
```

`axpilot stdio` speaks MCP over stdin and stdout, so it waits silently for a client. Connect a
client as described in [Connect a client](clients.md#from-a-source-checkout), or explore the tools in the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```sh
npx @modelcontextprotocol/inspector uv run axpilot stdio --engine fake
```

In direct mode, macOS attributes permissions to the app that started AXPilot (your terminal, or
the MCP client). Grant **Accessibility** to that app; see [Permissions](permissions.md#direct-mode).

## Install channels

| Channel | Command | Status |
|---|---|---|
| **uv** (recommended) | `uv tool install axpilot` then `axpilot setup` | **Alpha** (0.1.0a1) |
| pipx | `pipx install axpilot` then `axpilot setup` | **Alpha** (0.1.0a1) |
| One-line installer | `install.sh` piped to `sh` ([below](#one-line-installer-planned)) | Planned (v0.3) |
| uvx (try without installing) | `uvx axpilot stdio` | **Alpha** (0.1.0a1) |
| MCP Registry | `io.github.vansh3503/axpilot` | Planned (v0.3) |
| Claude Desktop extension (`.mcpb`) | One-click bundle | Under evaluation (v0.3); ships only if reliable |
| Homebrew tap (formula) | `brew install vansh3503/tap/axpilot` | Planned, after verifying the launcher survives the keg unmodified |
| Source | `git clone` → `uv sync` → `launcher/build.sh` → `uv run axpilot setup --dev-source .` | **Works today** |

Why package managers and not a DMG? The launcher app is ad-hoc signed (free, no Apple Developer
account). Package managers don't mark downloaded files with `com.apple.quarantine`, so Gatekeeper
never blocks it; a browser download would. See [ADR-5](adr/0005-signing-and-distribution.md).

## Install with uv

```sh
uv tool install axpilot
axpilot setup
axpilot connect claude-desktop      # or cursor, vscode, claude-code, … (axpilot connect --list)
```

`uv tool install` puts `axpilot` in `~/.local/bin`. If your shell can't find it, run
`uv tool update-shell` and open a new terminal.

### What `axpilot setup` does

1. Check that you run macOS 14 or later, and your architecture.
2. Create a **dedicated runtime** in `~/Library/Application Support/AXPilot/runtime/` (a uv virtual
   environment with a pinned `axpilot`), so the host never depends on uvx caches or project venvs.
3. Copy the **launcher** to `~/Applications/AXPilot.app`, byte for byte, verify its cdhash, and
   write `runtime.json` (which tells the launcher how to start the host).
4. Optionally register a login item (`--login-item`) and open the app (`--launch`).

It prints a report of every step. macOS asks for Accessibility the first time a tool needs it; see
[Permissions](permissions.md). Add AXPilot to your MCP clients with `axpilot connect`
([Connect a client](clients.md)).

## One-line installer *(planned)*

```sh
curl -LsSf https://vansh3503.github.io/axpilot/install.sh | sh
```

The script ([`packaging/install.sh`](https://github.com/vansh3503/Mac-Mcp/blob/main/packaging/install.sh))
is short; please read it before piping it to a shell. It:

1. checks that it runs on macOS 14 or later, as a normal user (it never uses `sudo`);
2. prints what it is about to do and asks for confirmation;
3. installs uv with [Astral's official installer](https://docs.astral.sh/uv/getting-started/installation/)
   if uv is missing;
4. runs `uv tool install axpilot` (or `uv tool upgrade axpilot` if it is already installed);
5. runs `axpilot setup`.

Running it again is safe. Options: `--yes` (don't ask), `--no-setup` (skip step 5), `--dry-run`
(print the commands only). Pass them through the pipe with `sh -s -- --yes`.

## Update and uninstall

```sh
uv tool upgrade axpilot   # or pipx upgrade axpilot
axpilot update        # upgrades the runtime; the launcher is untouched, so no re-grant is needed
axpilot uninstall     # quits AXPilot, then removes the login item, the runtime and its manifest;
                      # keeps config and logs
                      # (--remove-launcher also deletes AXPilot.app, which means granting permissions again)
uv tool uninstall axpilot
```

A host that is already running keeps the previous version until AXPilot restarts: the launcher
reads the runtime only when it starts the host. In a terminal, `axpilot update` offers to restart
AXPilot (connected MCP clients lose their session: restart them, or reconnect the server). Otherwise
it says how, and `axpilot doctor` warns while the host still runs the previous version.

When a release ships a new **launcher** version (rare, announced in the changelog), macOS asks you
to grant Accessibility once more. `axpilot doctor` detects this and walks you through it.

## Check your installation

```sh
axpilot doctor
```

`doctor` reports the macOS version, permissions, the launcher's cdhash, the runtime, the socket,
connected clients and versions, and tells you how to fix anything that is wrong. See
[Troubleshooting](troubleshooting.md).
