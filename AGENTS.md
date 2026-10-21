# AGENTS.md

Guidance for AI coding agents working **on** this repository. (Agents that want to **use** AXPilot
to operate a Mac should read [skills/axpilot/SKILL.md](skills/axpilot/SKILL.md) instead.)

AXPilot is an open-source MCP server, written in Python, that lets AI agents see and operate macOS
through the Accessibility API. It is **pre-alpha** (Phase 0/1 of [docs/PLAN.md](docs/PLAN.md)).

## Read first

1. [docs/PLAN.md](docs/PLAN.md): product plan, architecture, tool surface, safety model, roadmap.
2. [docs/dev/CONTRACTS.md](docs/dev/CONTRACTS.md): the exact module APIs. Implement signatures
   **exactly** as written; when a contract must change, update `CONTRACTS.md` in the same change.
3. [docs/dev/QUALITY.md](docs/dev/QUALITY.md): mandatory error-handling and testing rules.
4. The foundation modules, which everything else builds on: `src/axpilot/{errors,models,paths,config}.py`
   and `src/axpilot/engine/base.py`.

## Architecture map

```
MCP client ──stdio──▶ bridge/ ──Unix socket──▶ host/ (child of AXPilot.app launcher)
                                                 └─ server/ ─▶ policy/ ─▶ engine/ ─▶ macOS
direct mode: `axpilot stdio --direct` runs server/ + engine/ in one process (no launcher, no socket)
```

| Path | Responsibility | PyObjC allowed? |
|---|---|---|
| `src/axpilot/errors.py` | `ErrorCode` (public API, never rename or reuse a code) and `AXPilotError` | No |
| `src/axpilot/models.py` | Platform-independent dataclasses (`Rect`, `AppInfo`, `RawNode`, `Capture`, …); coordinates are screen **points** | No |
| `src/axpilot/config.py`, `paths.py` | Config model and loader (flags > `AXPILOT_*` env > TOML > defaults); redirectable paths | No |
| `src/axpilot/clientnames.py` | Readable names of known MCP clients (`claude` → "Claude Code") for display only | No |
| `src/axpilot/engine/base.py` | The `Engine` ABC: every perception and actuation primitive | No |
| `src/axpilot/engine/fake.py` | `FakeEngine`: in-memory desktop from JSON fixtures, used by tests and `--engine fake` | No |
| `src/axpilot/engine/macos/` | `MacEngine`: AX, CGEvent, ScreenCaptureKit, Vision, AppKit | **Yes** |
| `src/axpilot/perception/` | Snapshots, pruning, refs, diffs, find, text format, snapshot/capture stores | No |
| `src/axpilot/input/` | Key-combo parsing, coordinate mapping | No |
| `src/axpilot/policy/` | Profiles/toolsets, allow/deny rules, confirmations, rate limit, audit, control (input lock, pause, yield-to-user) | No |
| `src/axpilot/system/` | Subprocess integrations (osascript, shell, mdfind, defaults, processes, browsers) via `CommandRunner` | No |
| `src/axpilot/server/` | MCP tools (pydantic schemas), `ToolSpec` registry, the shared tool guard | No |
| `src/axpilot/host/`, `bridge/` | Host daemon on a Unix socket; stdio ⇄ socket bridge | `host/ui` only |
| `src/axpilot/clients/` | Config writers for `axpilot connect` | No |
| `src/axpilot/cli.py` | The Typer CLI (`axpilot …`) | No |
| `launcher/` | Frozen Swift launcher `AXPilot.app` (changes force users to re-grant permissions) | n/a |
| `spikes/` | Phase 0 experiments; not part of the package | n/a |
| `tests/` | Mirrors `src/axpilot/` (`tests/perception/test_snapshot.py`, …) | macOS tests only |

The shared tool guard runs, in order: tool-enabled check → rate limit (input tools) → policy →
confirmation (destructive tools) → `control.input_turn()` (input tools) → engine call in a worker
thread → audit.

## Commands

```sh
uv sync                                   # install runtime + dev dependencies from uv.lock
uv run pytest -q                          # all tests (Linux and macOS)
uv run pytest tests/<package> -q          # one package
uv run pytest --cov --cov-branch          # with coverage
uv run ruff check <paths>                 # lint
uv run ruff format <paths>                # format
uv run pyright                            # type check
uv run axpilot stdio --engine fake        # run the server against the simulated desktop
AXPILOT_MACOS_TESTS=1 uv run pytest tests/engine -q   # real macOS engine tests (Mac only)
```

Run linters and formatters on the paths you changed. Don't reformat unrelated files.

## Rules

Code:

- Python ≥ 3.11, `from __future__ import annotations`, full type hints, docstrings on public API,
  small focused functions. Match the style of the foundation files. Ruff line length is 110.
- **Only `axpilot.engine.macos` and `axpilot.host.ui` may import PyObjC** (`objc`, `AppKit`,
  `Foundation`, `Quartz`, `ApplicationServices`, `ScreenCaptureKit`, `Vision`). Everything else must
  import and pass its tests on Linux.
- Failures cross module boundaries only as `AXPilotError(ErrorCode.X, "message", hint=...)`, raised
  `from` the original exception. Pick the most specific code (QUALITY.md §A2). Messages name the
  target and never contain secrets, typed text, clipboard contents or tokens.
- No network access and **no telemetry**, in code or tests.
- Don't add dependencies to `pyproject.toml` without the maintainer's agreement. Available at
  runtime: `mcp` (SDK v2, `MCPServer`), `pydantic`, `anyio`, `typer`, `tomli-w`, `pillow`, stdlib.
- Every wait has a deadline. Blocking engine calls run in `anyio.to_thread.run_sync(...,
  abandon_on_cancel=True)`. Input actions are **never retried automatically**.
- Never use `shell=True`, except in `system/shell.run_shell` (which runs `[shell, "-lc", command]`).
- No bare `except:` or silent `except Exception: pass`; log and surface degradations as warnings.

Public API (SemVer):

- Tool names, parameters, output schemas and error codes are public API. `tools/list` order is
  `TOOL_ORDER` in `policy/profiles.py`. Golden files in `tests/**/golden/` catch changes; regenerate
  them only deliberately with `AXPILOT_UPDATE_GOLDEN=1` and say so in the change description.
- Follow the "Adding a tool" checklist in [CONTRIBUTING.md](CONTRIBUTING.md#adding-a-tool).

Safety while you work:

- Don't drive the user's real desktop (`axpilot stdio --direct`, macOS tests) unless they asked you
  to. Use `--engine fake` and the test suite instead.
- Don't modify `launcher/` unless explicitly asked: any change to the launcher binary or its
  `Info.plist` changes its cdhash and forces every user to grant permissions again.
- Don't run `tccutil`, change System Settings, or edit `~/Library` on the user's behalf.
- Never commit secrets, tokens, or fixtures containing personal data.

## Testing

- Tests mirror the package path and use plain pytest; async tests use `@pytest.mark.anyio`. A test
  directory may have its own `conftest.py`; there is no root `tests/conftest.py`.
- Hermetic: use `tmp_path` and `monkeypatch` for `HOME` and the `AXPILOT_*` path variables
  (`AXPILOT_CONFIG_DIR`, `AXPILOT_HOME`, `AXPILOT_LOG_DIR`, `AXPILOT_SOCKET`, `AXPILOT_APP_PATH`).
  Inject clocks; no `sleep` over 0.2 s; no test-order dependence.
- Use the fakes: `FakeEngine` (assert on `engine.events`), `FakeRunner` for subprocesses, `AutoConfirmer`
  and `DenyConfirmer` for confirmations.
- Test error paths as thoroughly as success paths: every error code a tool can return, fault
  injection (raw exceptions become `INTERNAL`, the server survives, the input lock is released),
  and policy-bypass attempts.
- macOS-only tests are marked `macos` and skipped unless `AXPILOT_MACOS_TESTS=1` on a Mac; tests
  that change state also need `AXPILOT_MACOS_TESTS_MUTATE=1`. They must never pass silently elsewhere.

## Conventions

- [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) (`feat(server): …`,
  `fix(perception): …`), with `!` and a `BREAKING CHANGE:` footer for breaking changes.
- Update [CHANGELOG.md](CHANGELOG.md) (Unreleased) for user-visible changes and the docs in `docs/`
  for behaviour changes. Mark unimplemented features as *planned*.
- Don't put model identifiers in code, docs or examples; use a placeholder such as `"<your-model>"`.
