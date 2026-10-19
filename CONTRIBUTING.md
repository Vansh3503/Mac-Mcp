# Contributing to AXPilot

Thanks for your interest in AXPilot! This guide covers the development setup, the project layout,
tests, and how changes get merged. AXPilot is pre-alpha, so the best contributions right now are
bug reports from real Macs, app-compatibility reports, spike results, tests and docs fixes.

- Questions and ideas: [GitHub Discussions](https://github.com/vansh3503/Mac-Mcp/discussions).
- Bugs, app-compatibility problems and feature requests: [issues](https://github.com/vansh3503/Mac-Mcp/issues/new/choose).
- **Security problems: never in a public issue.** See [SECURITY.md](SECURITY.md).
- AI coding agents: read [AGENTS.md](AGENTS.md) as well.

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

You need `git` and [uv](https://docs.astral.sh/uv/getting-started/installation/). uv installs the
right Python (the repo pins 3.12 in `.python-version`; 3.11 to 3.13 are supported) and every
dependency from `uv.lock`.

```sh
git clone https://github.com/vansh3503/Mac-Mcp.git axpilot
cd axpilot
uv sync                  # creates .venv with the runtime and dev dependencies
uv run pytest            # runs the test suite
```

### On Linux

Most of AXPilot runs and is tested on Linux: the MCP server, the policy layer, perception
(snapshots, refs, diffs), key parsing, the command-line integrations and the in-memory
`FakeEngine`. PyObjC is only installed on macOS (platform markers in `pyproject.toml`), so the
macOS engine is skipped. To try the server against the simulated desktop:

```sh
uv run axpilot stdio --engine fake
npx @modelcontextprotocol/inspector uv run axpilot stdio --engine fake   # optional, interactive UI
```

### On macOS

macOS 14 Sonoma or later. `uv sync` also installs PyObjC. To build the launcher you need the Xcode
Command Line Tools (`xcode-select --install`); nothing else needs them.

```sh
uv run axpilot stdio --direct     # runs the real engine in-process (direct mode)
```

### pre-commit (optional, recommended)

```sh
uvx pre-commit install            # runs ruff and basic hygiene checks on every commit
uvx pre-commit run --all-files
```

## Project layout

```
src/axpilot/
├─ errors.py  models.py  paths.py  config.py   # foundation: error codes, data types, paths, config
├─ engine/
│  ├─ base.py            # the Engine interface every backend implements
│  ├─ fake.py            # FakeEngine: an in-memory desktop for tests (fixtures/demo_desktop.json)
│  └─ macos/             # MacEngine: the only place (with host/ui) that imports PyObjC
├─ perception/           # snapshots, pruning, refs, diffs, find, text formatting, stores
├─ input/                # key-combo parsing, coordinate mapping
├─ policy/               # profiles, allow/deny rules, confirmations, rate limit, audit, control
├─ system/               # subprocess integrations: osascript, shell, mdfind, defaults, browsers
├─ server/               # MCP tools, schemas, the shared tool guard
├─ host/  bridge/        # host daemon (Unix socket) and the stdio bridge
├─ clients/              # config writers for `axpilot connect`
└─ cli.py                # the `axpilot` command
launcher/                # the frozen AXPilot.app launcher (Swift), see launcher/README.md
spikes/                  # Phase 0 experiments (S1–S6), each ending in an ADR
tests/                   # mirrors src/axpilot/ (tests/perception/test_snapshot.py, …)
docs/                    # user docs (MkDocs), ADRs, PLAN.md, docs/dev/ (internal contracts)
skills/axpilot/SKILL.md  # the Agent Skill shipped to users
packaging/               # MCP Registry server.json, install.sh
```

The internal module APIs are specified in [docs/dev/CONTRACTS.md](docs/dev/CONTRACTS.md) and the
error-handling and testing rules in [docs/dev/QUALITY.md](docs/dev/QUALITY.md). Read both before
changing code; a change to a contract updates `CONTRACTS.md` in the same pull request.

## Everyday commands

```sh
uv run ruff check .                 # lint (add --fix to apply safe fixes)
uv run ruff format src tests        # format (CI also checks Python code blocks in docs/*.md)
uv run pyright                      # type check (standard mode)
uv run pytest                       # all tests
uv run pytest tests/policy -q       # one package
uv run pytest --cov --cov-branch    # with branch coverage
```

CI runs the same commands (see [.github/workflows/ci.yml](.github/workflows/ci.yml)) on Linux with
Python 3.11, 3.12 and 3.13, and on macOS 14 and 15.

## Tests

Tests follow the pyramid in [QUALITY.md](docs/dev/QUALITY.md#b1-test-pyramid): unit, component (with
`FakeEngine`, `FakeRunner` and stub clocks), MCP contract (in-process client against `FakeEngine`),
integration (`axpilot stdio --engine fake` as a subprocess), and macOS end-to-end.

Rules that keep the suite reliable:

- Plain pytest; async tests use anyio's plugin (`@pytest.mark.anyio`).
- Hermetic: no network, no real home directory. Use `tmp_path` and `monkeypatch` for `HOME` and
  every `AXPILOT_*` path variable (`AXPILOT_CONFIG_DIR`, `AXPILOT_HOME`, `AXPILOT_LOG_DIR`,
  `AXPILOT_SOCKET`, `AXPILOT_APP_PATH`).
- Deterministic: inject clocks, no `sleep` longer than 0.2 s, no reliance on test order.
- Error paths are first-class: every tool is tested for success **and** for each error code it can
  return.
- Golden files (`tests/**/golden/`) are regenerated only on purpose:
  `AXPILOT_UPDATE_GOLDEN=1 uv run pytest <path>`. Review the diff; a changed `tools/list` golden file
  is a public API change.

### macOS-only tests

Tests that drive the real macOS engine are marked `macos` and **skipped unless you opt in**, so they
never pass silently anywhere else:

```sh
AXPILOT_MACOS_TESTS=1 uv run pytest tests/engine/test_macos_engine.py -q
AXPILOT_MACOS_TESTS=1 AXPILOT_MACOS_TESTS_MUTATE=1 uv run pytest tests/engine -q   # also tests that change state (clipboard)
```

They need Accessibility (and, for capture tests, Screen Recording) for the app that runs them,
which is your terminal in direct mode. The launcher has its own suite:
`launcher/build.sh && uv run pytest launcher/tests -v` (see [launcher/README.md](launcher/README.md)).

## Direct mode and macOS permissions

In direct mode, macOS attributes AXPilot's permission checks to the **responsible app**: the app that
started the process, not `python`. From Terminal that is Terminal; from VS Code's or Cursor's
integrated terminal it is VS Code or Cursor.

1. Open **System Settings → Privacy & Security → Accessibility**.
2. Enable your terminal app (click **+** if it isn't listed), then **quit and reopen it**. macOS
   applies a new grant to new processes only.
3. For screenshots, do the same under **Screen & System Audio Recording**. It is optional: snapshot
   tools work without it.
4. Check with `uv run axpilot doctor` *(planned; until it lands, run the macOS tests above)*.

Do not try to grant permissions to `python` itself. uv's interpreters are symlinks, and a grant to
an interpreter path breaks whenever the interpreter changes.

To start over, reset a grant. `tccutil` takes a service and a bundle id, and only that app's
decision is cleared:

```sh
tccutil reset Accessibility com.apple.Terminal              # your terminal (use its bundle id)
tccutil reset Accessibility io.github.vansh3503.axpilot     # the AXPilot launcher
tccutil reset ScreenCapture io.github.vansh3503.axpilot
```

Working on a test machine or a VM is a good idea: macOS tests click and type for real.

## Recording accessibility fixtures

Perception tests run on recorded AX trees, so a fixture from a real app is a very useful
contribution, especially for apps that behave oddly.

```sh
uv run axpilot debug dump-tree --app TextEdit > tests/fixtures/ax-trees/textedit-document.json   # planned command
```

The format is described in [CONTRACTS.md §1](docs/dev/CONTRACTS.md) (`serialize.py`). **Scrub
personal data** (document text, names, email addresses, file paths) before committing a fixture.

## Adding a tool

Tool names, parameters and output schemas are public API (SemVer, enforced by golden files), so
please open a feature request first to agree on the name and shape. Then:

- [ ] **Toolset and profiles:** add the name to `TOOLSETS` (and `PROFILES` if it belongs in one) in
      `src/axpilot/policy/profiles.py`. Position matters: it is the `tools/list` order.
- [ ] **Schema:** a pydantic input model with constraints (`Field(ge=…, le=…, max_length=…)`,
      `Literal[...]` enums) and an output model, so malformed calls fail before any side effect.
- [ ] **Registration:** a `@tool(...)` factory in a module under `src/axpilot/server/tools/` (listed in
      `TOOL_MODULES` in `src/axpilot/server/app.py`) with accurate annotations (`read_only`,
      `destructive`, `idempotent`, `open_world`, `input_action`; `confirm_when` when only some calls
      are destructive) and a description written for models: purpose, when to use it, key
      parameters, what comes back, common errors.
- [ ] **Windows-MCP naming:** a public name in `NAMES` in `src/axpilot/server/naming/windows_mcp.py`,
      plus an adapter in `MAPPINGS` when Windows-MCP's parameters differ.
- [ ] **Engine:** if it needs new platform capability, add a method to `Engine`
      (`src/axpilot/engine/base.py`), implement it in `FakeEngine` and `MacEngine`, and update
      `CONTRACTS.md`.
- [ ] **Policy:** decide which checks apply (`check_app` kind, secure targets, confirmation for
      destructive actions, the input lock and rate limit for input tools). Never bypass the shared
      tool guard.
- [ ] **Errors:** the most specific `ErrorCode`, a message naming the target and a hint naming the
      next step ([QUALITY.md §A2–A3](docs/dev/QUALITY.md)).
- [ ] **Tests:** the success path, every error code it can return, fault injection, policy-bypass
      attempts, and the regenerated `tools/list` golden files.
- [ ] **Docs:** regenerate [docs/tools.md](docs/tools.md) with `uv run python scripts/gen_tool_docs.py`,
      update the [Agent Skill](skills/axpilot/SKILL.md) if it changes how agents should work, and
      [CHANGELOG.md](CHANGELOG.md).

## Code style

- Python ≥ 3.11 with `from __future__ import annotations`, full type hints and docstrings on public
  API. Small, focused functions with clear names.
- `ruff` (line length 110) and `pyright` (standard mode) must pass.
- Only `axpilot.engine.macos` and `axpilot.host.ui` may import PyObjC. Everything else must import
  and pass its tests on Linux.
- Errors cross module boundaries only as `AXPilotError(ErrorCode.X, "message", hint=...)`.
- No network access and no telemetry. New runtime dependencies need a discussion first.

## Commits and pull requests

We use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/); release notes and
version bumps will be generated from them.

```
feat(server): add fill_form tool
fix(perception): keep refs stable when a list scrolls
docs: explain the Screen Recording monthly prompt
```

Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`. Common scopes:
`engine`, `perception`, `input`, `policy`, `system`, `server`, `host`, `bridge`, `cli`, `clients`,
`launcher`, `skill`, `packaging`. Mark breaking changes with `!` (`feat(server)!: …`) and a
`BREAKING CHANGE:` footer.

Pull requests:

- Keep them small and focused, and link the issue they address.
- Fill in the [pull request template](.github/PULL_REQUEST_TEMPLATE.md), including which macOS
  versions you tested on, if any.
- CI must be green. A maintainer may ask for a test on a real Mac for engine changes.
- Changes to the launcher (`launcher/`) force every user to re-grant permissions, so they need a
  maintainer's agreement before work starts (see [launcher/README.md](launcher/README.md#changing-the-launcher)).

**No DCO or CLA is required.** By submitting a contribution you agree that it is licensed under the
project's [Apache License 2.0](LICENSE) (section 5 of the license).

## Documentation

The docs site is built with MkDocs Material from `docs/` (`mkdocs.yml`). Preview it locally without
adding dependencies to the project:

```sh
uvx --with mkdocs-material mkdocs serve
```

Mark anything that is not implemented yet as *planned*; the docs must match what users can run.

## Code of Conduct

This project follows the [Contributor Covenant 2.1](CODE_OF_CONDUCT.md). Be kind, assume good
intent, and help newcomers.
