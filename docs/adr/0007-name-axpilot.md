# ADR-0007: Name: AXPilot

- **Status:** Proposed — pending confirmation of the name ([PLAN.md §17](../PLAN.md#17-remaining-open-questions), question 1). AXPilot is used as the working name throughout the code base; this ADR becomes *Accepted* when the PyPI name is reserved and the repository is renamed.
- **Date:** 2026-10-19
- **Deciders:** AXPilot maintainers
- **Source:** [PLAN.md §4, ADR-7](../PLAN.md#4-key-decisions-adrs)

## Context and problem statement

The project needs a name that is distinctive and searchable, free on the package registries we use,
and safe from trademark trouble. Apple's guidelines discourage using its trademarks ("Mac", "Cocoa",
"Tahoe", …) in product names, although descriptive use such as "an MCP server *for macOS*" is fine.
The repository is currently called `Mac-Mcp`.

## Decision drivers

- Says what it does, for people who know macOS automation.
- Available on PyPI (the package and CLI) and npm (defensively, for future TypeScript helpers).
- No Apple trademarks in the product name.
- Short enough for a CLI command.

## Decision

Use **AXPilot**: "AX" is the prefix of macOS's Accessibility API, the core of how AXPilot works, and
"pilot" says it drives your Mac. `axpilot` was available on both PyPI and npm when checked.

| Identifier | Value |
|---|---|
| Python package and CLI | `axpilot` |
| App | `AXPilot.app` |
| Bundle id | `io.github.vansh3503.axpilot` |
| Environment variable prefix | `AXPILOT_` |
| MCP Registry name | `io.github.vansh3503/axpilot` |
| Tagline | "AXPilot: the MCP server for macOS" |
| Repository | rename `Mac-Mcp` → `axpilot` (GitHub redirects the old URLs) |
| GitHub topics | `mcp`, `macos`, `computer-use`, `accessibility` |

## Consequences

Positive:

- A unique, searchable name with no Apple trademark in it.
- Consistent identifiers across the package, app, bundle id, registry and environment variables.

Negative:

- "AX" is jargon; the tagline has to carry the meaning for newcomers.
- The bundle id is part of every user's permission grant, so it must not change after the first
  release. The same holds for the MCP Registry namespace.
- Renaming the repository later updates URLs in docs, badges and `pyproject.toml`.

## Alternatives considered

- **Pomme** (`pomme-mcp`, French for "apple"): friendly, but a pun on Apple's name.
- **Dexter** (`dexter-mcp`, as in "dexterity"): memorable, but less specific to macOS accessibility.
- **Mac-MCP** (the repository's current name): descriptive, but uses an Apple trademark as the
  product name and is too generic to search for.

## Validation

- Before the first release: reserve `axpilot` on PyPI (and npm, defensively), re-check for
  conflicting trademarks, and confirm the GitHub namespace for the MCP Registry
  (`io.github.vansh3503` versus a future organization, PLAN §17 question 2).
- Then rename the repository and mark this ADR *Accepted*.
