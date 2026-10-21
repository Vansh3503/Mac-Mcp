# AXPilot

**The MCP server for macOS.** AXPilot lets any AI agent see and operate the apps on your Mac,
through the macOS Accessibility API first and screenshots second, with safety controls that are on
by default.

> **Status: alpha.** The first alpha, 0.1.0a1, is on PyPI (verified on macOS 26 on Apple silicon).
> These docs describe the planned 1.0 behaviour and mark what is *planned*; see [Install](install.md).

## What it does

- **Perception.** A compact text snapshot of the accessibility tree gives every button, field, list
  and menu a short ref (`e1`, `e2`, …). Agents act on refs instead of guessing pixels, so text-only
  and local models work. Screenshots (with a `screenshot_id` for coordinate mapping) and Vision OCR
  cover apps with poor accessibility.
- **Actions.** Click, type, keyboard shortcuts, scrolling, dragging, setting values, menu-bar
  commands by path, apps, windows, files and URLs. Each action tries the most robust strategy
  first and returns a diff of what changed.
- **Safety.** Profiles and toolsets, app and window deny-lists, protection for password fields,
  confirmations for destructive actions, a rate limit, an audit log, yield-to-user, client pairing,
  a kill switch and an on-screen indicator (not yet verified on a Mac). **No telemetry.**
- **Any client, any framework.** Standard MCP over stdio or Streamable HTTP. Works
  with Claude Desktop, Claude Code, Cursor, VS Code, Codex CLI, Gemini CLI, the OpenAI Agents SDK,
  LangGraph, Pydantic AI and anything else that speaks MCP.

## How it works

```
 MCP client / agent framework
        │ spawn `axpilot stdio`
        ▼
 ┌────────────────┐  Unix socket   ┌─────────────────────────────────────┐
 │ axpilot bridge │───────────────▶│ AXPilot.app: tiny frozen launcher   │
 │ (no perms)     │                │   └─ Python host (child process)    │
 └────────────────┘                │       ├ MCP server                  │
                                   │       ├ Policy and safety           │
                                   │       └ Engine (Accessibility,      │
                                   │         input, capture, OCR)        │
                                   └─────────────────────────────────────┘
          macOS permissions are granted once, to AXPilot.app
```

macOS checks privacy permissions against the app responsible for a process. Because the Python host
runs as a child of the `AXPilot.app` launcher, you grant Accessibility **once, to AXPilot**, and
every MCP client can use it. The launcher is tiny and never changes between AXPilot releases, so the
grant survives updates. Spike S1 confirmed this design on macOS 26 on Apple silicon; see
[ADR-2](adr/0002-architecture-launcher-host-bridge.md). **Direct mode** (`axpilot stdio --direct`)
runs everything in one process instead.

## Where to go next

| I want to… | Read |
|---|---|
| Install or run it from source | [Install](install.md) |
| Understand the macOS permissions | [Permissions](permissions.md) |
| Connect Claude, Cursor, VS Code or another client | [Connect a client](clients.md) |
| Use it from my agent framework | [Agent frameworks](frameworks.md) |
| See every tool and its parameters | [Tools](tools.md) |
| Change profiles, deny-lists or limits | [Configuration](configuration.md) |
| Know what AXPilot protects against | [Security model](security.md) |
| Fix a problem | [Troubleshooting](troubleshooting.md) |
| Understand the design decisions | [Architecture decisions](adr/README.md) and the [project plan](PLAN.md) |

AXPilot is open source under the Apache-2.0 license and inspired by
[Windows-MCP](https://github.com/CursorTouch/Windows-MCP). Contributions are welcome: see
[CONTRIBUTING.md](https://github.com/vansh3503/Mac-Mcp/blob/main/CONTRIBUTING.md).
