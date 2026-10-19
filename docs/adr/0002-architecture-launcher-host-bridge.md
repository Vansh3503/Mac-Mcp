# ADR-0002: Architecture: frozen launcher → Python host, plus a thin stdio bridge

- **Status:** Proposed — pending spike S1
- **Date:** 2026-10-19
- **Deciders:** AXPilot maintainers
- **Source:** [PLAN.md §4, ADR-2](../PLAN.md#4-key-decisions-adrs) and [§5](../PLAN.md#5-architecture)

## Context and problem statement

macOS checks Accessibility, Screen Recording and Automation against the **responsible process**:
the app macOS holds accountable for a process, usually the app that launched it. When an MCP client
spawns a Python server, the responsible app is the client (Claude, Cursor, VS Code) or the terminal.
So users must grant permissions to every client separately, grants to `python` rarely apply and
break when the interpreter changes (uv's interpreters are symlinks), and a client whose bundle lacks
a usage string can cause silent denials. These identity problems account for most of the known
issues of comparable Python-based macOS MCP servers (PLAN §2).

How do we make one permission grant work for every client, and survive updates?

## Decision drivers

- Grant once, for every client, and keep the grant across AXPilot and Python updates.
- No paid Apple Developer account for v1 ([ADR-0005](0005-signing-and-distribution.md)).
- Keep all logic in Python ([ADR-0001](0001-language-python-pyobjc.md)); keep native code minimal and rarely changed.
- Several clients at once; clients that know nothing beyond "spawn a stdio server".

## Decision

1. **`AXPilot.app`**, a tiny, frozen, ad-hoc-signed launcher app (`LSUIElement`, with usage
   strings in `Info.plist`), is started through LaunchServices (`open -g -a AXPilot`, or a login
   item). That makes it its own responsible process.
2. The launcher reads `runtime.json` and **spawns** (`posix_spawn`, not `exec`) the Python host,
   `<runtime>/bin/python -m axpilot.host`. As its child, the host's responsible process is
   `AXPilot.app`, so the grants made to AXPilot apply to the host's Accessibility, input and capture
   calls. The launcher forwards signals and restarts the host with backoff
   ([launcher/README.md](https://github.com/vansh3503/Mac-Mcp/blob/main/launcher/README.md)).
3. The **host** runs the engine, the policy layer, the menu-bar UI and the MCP server. It listens on
   a per-user **Unix socket** (mode `0600`, same-uid checks) and optionally on localhost Streamable
   HTTP.
4. MCP clients spawn **`axpilot stdio`**, a bridge that pipes stdio to the socket and starts the app
   if needed. The bridge needs no permissions. Its first message identifies the client for pairing.
5. **Direct mode** (`axpilot stdio --direct`) runs the host in-process. Permissions then attach to
   the launching app. It is meant for development, CI, and users who prefer it.

## Consequences

Positive:

- One grant, to "AXPilot", works for every client and survives Python and AXPilot updates, because
  the launcher's bytes don't change.
- The bridge is trivial and permission-free; clients only need stdio.
- One host serves several clients, which allows a single "virtual hand", global rate limits and one
  place for confirmations and the kill switch.

Negative:

- More moving parts: launcher, host lifecycle, socket, bridge, discovery and auto-start.
- A local socket that exposes AXPilot's permissions creates a **confused-deputy** risk, which
  requires client pairing (PLAN §7.1).
- The design relies on how TCC attributes child processes, which Apple could change in a macOS
  update. We test every macOS beta.
- Any change to the launcher forces users to grant permissions again, so it must stay frozen.

## Alternatives considered

- **Direct mode only.** Simplest, but users grant permissions per client, and grants to
  interpreters break. Kept as an option for development.
- **(a) Self-contained app with embedded Python** (PyInstaller or Briefcase), so AX calls run inside
  the bundle's own process. Large bundles; with ad-hoc signing every AXPilot update changes the
  cdhash and forces a re-grant. **First fallback** if S1 fails.
- **(b) Native helper** that performs the AX, input and capture calls (the hybrid model), with
  Python driving it over IPC. Works regardless of child-process attribution, but moves much more
  code into native land. **Second fallback.**
- **`exec` instead of `spawn`**: replaces the app's process image with Python's, losing the identity.
- **A standalone driver daemon** that holds the grants with a CLI proxying to it (as cua-driver
  does): essentially this design, with more native code.

## Validation

**Spike S1** ([`spikes/s1_launcher_tcc`](https://github.com/vansh3503/Mac-Mcp/tree/main/spikes/s1_launcher_tcc),
scenario matrix A–E) decides go/no-go at the end of Phase 0, on macOS 14, 15, 26 and 27:

- A: a child of the ad-hoc-signed `AXPilot.app` is attributed to AXPilot and inherits its
  Accessibility and Screen Recording grants;
- B: the grant survives interpreter changes, Python upgrades and a recreated runtime venv, and is
  lost when the launcher changes (negative control);
- C: it works when the bridge is started by Claude Desktop, Claude Code, Cursor, VS Code and a terminal;
- D: installs through uv and pipx carry no quarantine attribute and trigger no Gatekeeper prompt;
- E: launcher behaviour (restarts, signals, logging) on a real system.

If S1 fails on a supported macOS version, we switch to fallback (a), then (b), and supersede this
ADR. After acceptance, the release checklist re-runs S1 on each new macOS beta.
