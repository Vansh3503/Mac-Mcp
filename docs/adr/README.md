# Architecture decision records

This folder records the significant decisions behind AXPilot: what we decided, why, what it costs,
and how we check that it still holds. The records follow a lightweight
[MADR](https://adr.github.io/madr/) format. They start from the ADRs in
[PLAN.md §4](../PLAN.md#4-key-decisions-adrs); the Phase 0 spikes can overturn them.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-language-python-pyobjc.md) | Language: Python + PyObjC, with a frozen native launcher | Accepted |
| [0002](0002-architecture-launcher-host-bridge.md) | Architecture: frozen launcher → Python host, plus a thin stdio bridge | Proposed (S1 passed on macOS 26 arm64; other versions pending) |
| [0003](0003-minimum-macos-14.md) | Minimum macOS 14 Sonoma | Accepted |
| [0004](0004-mcp-protocol-surface.md) | MCP protocol surface | Accepted |
| [0005](0005-signing-and-distribution.md) | Signing and distribution without a paid Apple account | Accepted |
| [0006](0006-privacy-no-telemetry.md) | Privacy: no telemetry | Accepted |
| [0007](0007-name-axpilot.md) | Name: AXPilot | Proposed (pending name confirmation) |

## Statuses

- **Proposed**: the current plan of record, but waiting on evidence (usually a spike) or a final
  call. Code may already follow it.
- **Accepted**: decided. Changing it needs a new ADR.
- **Superseded by NNNN** or **Deprecated**: kept for history, never deleted.

## Adding an ADR

1. Copy the template below into `NNNN-short-title.md`, using the next free number.
2. Open a pull request with status *Proposed*. Discussion happens on the pull request.
3. When it is merged as *Accepted*, add it to the table above and, if it replaces an older ADR,
   mark that one *Superseded by NNNN*.

```markdown
# ADR-NNNN: Title

- **Status:** Proposed
- **Date:** YYYY-MM-DD
- **Deciders:** AXPilot maintainers

## Context and problem statement

What forces are at play, and what question does this answer?

## Decision drivers

- …

## Decision

What we will do.

## Consequences

Positive and negative, including what becomes harder.

## Alternatives considered

Each option, and why it was not chosen.

## Validation

How we confirm the decision holds (spikes, tests, CI checks), and what would make us revisit it.
```
