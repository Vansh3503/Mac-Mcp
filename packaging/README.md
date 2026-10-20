# Packaging

Distribution metadata and installers for AXPilot. Background:
[PLAN.md §8](../docs/PLAN.md#8-installation-and-client-integration) (install channels),
[§13](../docs/PLAN.md#13-cicd-and-release-engineering) (release engineering) and
[ADR-0005](../docs/adr/0005-signing-and-distribution.md) (why package managers only).

> **Status:** nothing here is live yet. AXPilot is not yet published on PyPI or in the MCP Registry,
> and the installer URL is not hosted yet. These files are ready for the first release (v0.1) and
> the public preview (v0.3).

| File | Purpose |
|---|---|
| [`server.json`](server.json) | MCP Registry entry (`io.github.vansh3503/axpilot`) |
| [`install.sh`](install.sh) | One-line installer (`curl … \| sh`) |
| `mcpb/` | *Planned (v0.3):* Claude Desktop extension bundle; see the [feasibility note](../docs/dev/mcpb-feasibility.md) |
| `homebrew/` | *Planned:* Homebrew tap formula, after verifying brew leaves the launcher bundle untouched |

## `server.json`: MCP Registry

The [MCP Registry](https://github.com/modelcontextprotocol/registry) lists AXPilot as a PyPI package
that clients run with `uvx axpilot stdio`.

- **Schema:** `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`.
  Re-check it against the registry's current schema before each publish; the registry rejects
  entries that don't validate (for example, `description` is limited to 100 characters).
- **Name and ownership:** `io.github.vansh3503/axpilot`. The registry verifies a PyPI package by
  looking for `mcp-name: io.github.vansh3503/axpilot` in the package's README on PyPI, which is why
  `README.md` carries the HTML comment `<!-- mcp-name: io.github.vansh3503/axpilot -->`. Never remove
  it. CI's build job checks that it survives into the wheel metadata.
- **Namespace:** `io.github.<user>` names are authorized by GitHub login (or GitHub Actions OIDC) as
  that user. The repository owner is shown as `Vansh3503` on GitHub; confirm that the namespace the
  registry grants matches the lowercase `io.github.vansh3503` used here (and in
  `axpilot.REGISTRY_NAME`) before the first publish.
- **Versions:** both `version` fields must equal the version published on PyPI (no ranges, no
  `latest`). They read `0.1.0` as a placeholder; the release workflow sets them from the tag.
- **Arguments and environment:** the package argument `stdio` selects the stdio transport. The
  optional `AXPILOT_TOOLS_PROFILE` and `AXPILOT_SAFETY_CONFIRM` variables map onto
  `[tools] profile` and `[safety] confirm` (see [configuration](../docs/configuration.md)).

Publishing (the `mcp-registry` job of `release.yml`, after the PyPI upload):

```sh
mcp-publisher login github-oidc     # in GitHub Actions, with id-token: write
mcp-publisher publish               # reads ./server.json
```

## `install.sh`: one-line installer

Planned to be served from the docs site:

```sh
curl -LsSf https://vansh3503.github.io/axpilot/install.sh | sh
```

What it does, in order: checks macOS 14+ and that it isn't running as root; prints the plan and asks
for confirmation (reading the answer from the terminal, since stdin is the script); installs uv with
[Astral's official installer](https://docs.astral.sh/uv/getting-started/installation/) only if uv is
missing (downloaded to a temporary file first, so a broken connection can't run half a script);
runs `uv tool install axpilot`, or `uv tool upgrade axpilot` if it is already installed; then runs
`axpilot setup` when a terminal is attached. It never uses `sudo` and is safe to run again.

Options: `--yes`, `--no-setup`, `--dry-run`, `--help`; through a pipe, `sh -s -- --yes`.
`AXPILOT_PACKAGE` overrides the package spec (for example `axpilot==0.1.0`), which is handy for
testing a pre-release.

Rules for changes:

- POSIX `sh` only (macOS runs `/bin/sh` as bash 3.2 in POSIX mode; test with `dash` too). No
  `local`, no `[[ ]]`, no arrays.
- Everything happens inside functions and `main "$@"` is the last line, so a truncated download
  runs nothing.
- Print every command before running it, and keep the script short enough to read before piping it
  to a shell.
- Check it with `sh -n packaging/install.sh && dash -n packaging/install.sh` and, if available,
  `shellcheck packaging/install.sh`. On a Mac, run `packaging/install.sh --dry-run`.

## Release flow

The workflows exist; none has run yet, because no launcher has been published.

1. **`launcher.yml`** (manual, rare): builds `AXPilot.app` on a macOS runner, runs the launcher
   tests, attests the build and publishes a `launcher-vN` release with `AXPilot.app.zip`,
   `SHA256SUMS` and `BUILDINFO.txt`. The maintainer records those values in a new row of
   `launcher/CDHASH.md`.
2. **`release.yml`** (tag `v*` on `main`):
   - `check`: `scripts/release.py check` (tag = `__version__`, a dated CHANGELOG section, a valid
     `server.json`, and a filled launcher row; it fails while the row is pending);
   - `build` (macOS): the sdist from sources, then the pinned launcher downloaded, its SHA-256s and
     both cdhashes verified, unpacked into `src/axpilot/_launcher/` with a `CDHASH` file, and the
     `macosx_14_0_universal2` wheel; a clean install of that wheel must carry the identical,
     executable, validly signed launcher;
   - `pypi`: trusted publishing with attestations (environment `pypi`);
   - `github-release`: the CHANGELOG section as release notes;
   - `mcp-registry` (not for pre-releases): `server.json` at the release version, published with
     `mcp-publisher login github-oidc`;
   - `docs` (not for pre-releases): `docs.yml` deploys the site to GitHub Pages with `install.sh` at
     its root.
3. The Homebrew formula is not automated: it comes only after a Mac shows that brew leaves the
   launcher bundle untouched.

Before tagging, bump `__version__`, move the Unreleased changelog entries under
`## [X.Y.Z] - YYYY-MM-DD`, and run `python3 scripts/release.py check vX.Y.Z`.

One-time setup by the maintainer: a [trusted publisher](https://docs.pypi.org/trusted-publishers/)
on PyPI for this repository (workflow `release.yml`, environment `pypi`), and GitHub Pages with
source "GitHub Actions" (a private repository needs a paid plan for Pages, or goes public first).
