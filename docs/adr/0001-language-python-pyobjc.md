# ADR-0001: Language: Python + PyObjC, with a frozen native launcher

- **Status:** Accepted
- **Date:** 2026-10-19
- **Deciders:** AXPilot maintainers
- **Source:** [PLAN.md §4, ADR-1](../PLAN.md#4-key-decisions-adrs)

## Context and problem statement

AXPilot has to call native macOS APIs (the Accessibility API, CoreGraphics events and window lists,
ScreenCaptureKit, Vision, AppKit) and serve them over MCP to many clients. The implementation
language decides how fast we can build, who can contribute, how much of the code can be tested off
a Mac, and how hard the macOS permission problem becomes.

## Decision drivers

- First-class, maintained MCP SDK with support for the 2026-07-28 specification.
- Complete access to the macOS frameworks we need.
- A low barrier for contributors (agent builders mostly write Python or TypeScript).
- Most of the code (server, policy, perception) testable on Linux CI.
- Small native surface: native code is expensive to review, build and sign.

## Decision

- **Python 3.11+** (stdlib `tomllib`; `mcp` and PyObjC need 3.10+). Environments, locking and builds
  use **uv**.
- **MCP:** the official `mcp` SDK v2 (`MCPServer`), pinned `>=2.3,<3`. Standalone FastMCP is the
  fallback if we need its middleware.
- **macOS APIs:** PyObjC (`pyobjc-core` plus the `ApplicationServices`, `Quartz`, `Cocoa`,
  `ScreenCaptureKit` and `Vision` framework packages), installed only on macOS through platform
  markers. The few Carbon calls PyObjC covers poorly (`TISCopyCurrentKeyboardLayoutInputSource`,
  `UCKeyTranslate`) go through `ctypes`.
- **Other dependencies kept minimal:** `pydantic`, `anyio`, `typer`, `pillow`, `tomli-w`.
- **PyObjC is imported only in `axpilot.engine.macos` and `axpilot.host.ui`**, behind the `Engine`
  interface, so everything else imports and runs on Linux.
- **Native code is limited to the launcher** (`launcher/`, a small Swift program), built once per
  launcher version ([ADR-0002](0002-architecture-launcher-host-bridge.md),
  [ADR-0005](0005-signing-and-distribution.md)). If Accessibility traversal in Python proves too
  slow, an optional native accelerator can be added later.

## Consequences

Positive:

- The official SDK tracks the specification; we don't maintain protocol code.
- Contributors can work on most of the code base without a Mac; `FakeEngine` makes server and
  policy tests fast and deterministic.
- PyObjC exposes every framework we need with little glue.

Negative:

- Python-to-Objective-C bridging adds overhead to tree traversal (budgeted and measured in spike S2).
- macOS does not let a plain `python` hold privacy permissions reliably; this needs the launcher
  architecture of ADR-0002.
- PyObjC adds import time and wheel size on macOS.
- An AppKit main loop next to an asyncio event loop needs a clear threading model (PLAN §5.3).

## Alternatives considered

- **Swift** (as Peekaboo and mcp-server-macos-use do): the best native performance and a natural
  app identity, but a smaller contributor pool, a less mature MCP SDK at decision time, and no
  Linux-testable core.
- **TypeScript/Node** with native addons: popular with agent builders, but the Accessibility and
  capture calls would still need a native module, so two native toolchains instead of one.
- **Rust** (as cua-driver does): fast and safe, but a high barrier for contributors and still a
  bridge to Objective-C.
- **Contributing to MacOS-MCP** (Python + PyObjC): a valid option; we build our own because the
  launcher and safety architecture are fundamental changes rather than patches (PLAN §2).

## Validation

- **Spike S2** measures snapshot latency on heavy apps (Xcode, Chrome, Electron apps, large Finder
  folders, System Settings). Target: snapshot p50 under 400 ms on typical windows (PLAN §12). If the
  target can't be met with batching, pruning and caching, revisit with a native accelerator.
- **Spike S3** confirms input (AX actions, CGEvent, layout mapping through `ctypes`) works from Python.
- **Spike S6** confirms the `mcp` SDK v2 interoperates with the main clients and frameworks.
- CI enforces the PyObjC import boundary by running the full test suite on Linux.
