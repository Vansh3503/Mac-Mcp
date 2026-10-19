# Changelog

All notable changes to AXPilot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Tool names, parameters,
output schemas and error codes are public API: breaking changes to them are called out under
**Changed** or **Removed** with a migration note. The launcher (`AXPilot.app`) has its own version
line; a new launcher version requires a one-time permission re-grant, which is always flagged here.

## [Unreleased]

Pre-alpha. Nothing is published on PyPI yet.

### Added

- Project plan ([docs/PLAN.md](docs/PLAN.md)) and architecture decision records for language,
  architecture, minimum macOS, MCP surface, signing and distribution, privacy, and the name
  ([docs/adr/](docs/adr/README.md)).
- Package foundation: stable error codes with hints (`axpilot.errors`), platform-independent data
  types (`axpilot.models`), configuration with TOML file, `AXPILOT_<SECTION>_<KEY>` environment
  variables and CLI overrides (`axpilot.config`), and redirectable filesystem locations
  (`axpilot.paths`).
- The `Engine` interface, the in-memory `FakeEngine` with a demo desktop fixture for tests and
  `--engine fake`, and the PyObjC-based macOS engine.
- Perception: accessibility snapshots with refs, pruning, paging, diffs, fuzzy element search,
  the compact text format, and snapshot and capture stores.
- Stable element refs: a ref is unique per session, never reused for another element, and kept
  by its element across snapshots of a window, so refs held from earlier snapshots keep working
  after actions re-snapshot the UI. A ref whose element is gone, or a list row that scrolled
  away, gives `STALE_REF` instead of hitting another element. Diffs pair equal refs first.
- Navigation tools: `explore` (a map of an app: windows, menus with item counts, toolbar, tabs,
  sidebar, scroll areas, dialogs, focus and its compatibility profile), `find` (fuzzy search that
  can scroll a list or page until the element appears), `scroll` (by lines or pages, to the top or
  bottom, or an element into view; reports `moved` and `at_end`) and `read` (the whole content of a
  long list, table, text view or web page, joined page by page without repeats, with `complete`).
  Scrolling measures the app's line height, applies the input policy and lock, and every loop is
  bounded, returning partial results with a warning instead of failing.
- Input helpers: key-combo parsing (`cmd+shift+t`, `⌘⇧T`, `Command-Shift-T`) and screenshot-to-screen
  coordinate mapping.
- Policy layer: profiles and toolsets, app and window deny-lists, the Terminal rule, secure-field
  protection, confirmations, rate limiting, an audit log with argument redaction, and input
  serialization with yield-to-user and pause.
- Command-line integrations: a subprocess runner with timeouts, AppleScript/JXA, shell, Spotlight,
  Shortcuts, `defaults`, processes, system information and browser tab scripting.
- The frozen launcher source (`launcher/`), its build script and tests, and harnesses for
  spikes S1–S6.
- Documentation site skeleton, contributor guide, security policy, code of conduct, AGENTS.md,
  issue and pull request templates, CI workflow, Dependabot and pre-commit configuration, the
  Agent Skill (`skills/axpilot/SKILL.md`), MCP Registry metadata and the installer script
  (`packaging/`).

[Unreleased]: https://github.com/vansh3503/Mac-Mcp/commits/main
