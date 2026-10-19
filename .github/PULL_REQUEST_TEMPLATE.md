<!--
Thanks for contributing to AXPilot! Please read CONTRIBUTING.md first.
Use a Conventional Commits title, e.g. "feat(server): add fill_form tool" or "fix(perception): ...".
Security fixes: report privately first (SECURITY.md) instead of opening a public pull request.
-->

## Summary

<!-- What does this change and why? Link the issue it addresses. -->

Closes #

## Type of change

- [ ] Bug fix
- [ ] New feature or tool
- [ ] Breaking change to the public API (tool names, parameters, output schemas, error codes, config keys)
- [ ] Documentation
- [ ] Tests, CI, build or packaging
- [ ] Refactor or performance

## How it was tested

<!-- Commands you ran and what you checked. For engine or input changes, say which macOS versions and apps. -->

- [ ] `uv run pytest` passes
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass
- [ ] `uv run pyright` passes
- [ ] Tested on a real Mac (macOS version, Apple silicon or Intel): <!-- e.g. macOS 15.4, Apple silicon -->
- [ ] macOS-only tests run with `AXPILOT_MACOS_TESTS=1` (if the change touches `engine/macos`)

## Checklist

- [ ] Tests cover the success path **and** the error paths (each error code the change can return).
- [ ] Follows [CONTRACTS.md](https://github.com/vansh3503/Mac-Mcp/blob/main/docs/dev/CONTRACTS.md); if a contract changed, `CONTRACTS.md` is updated in this PR.
- [ ] PyObjC is imported only in `engine/macos` or `host/ui`; no network access or telemetry added.
- [ ] Errors are raised as `AXPilotError` with the most specific code, a message naming the target, and a hint.
- [ ] `tools/list` golden files regenerated deliberately (`AXPILOT_UPDATE_GOLDEN=1`) if tools changed, and the diff is explained below.
- [ ] Docs updated (`docs/`, `skills/axpilot/SKILL.md` if agent behaviour changes); unimplemented features are marked *planned*.
- [ ] `CHANGELOG.md` updated under **Unreleased** for user-visible changes.
- [ ] No change to `launcher/` (or a maintainer agreed to it: it forces users to re-grant permissions).

## Safety and security considerations

<!-- Does this change what an agent can do, read or reach? New permissions? Could prompt injection exploit it? Write "None" if not applicable. -->

## Screenshots or recordings

<!-- Optional, for UI or behaviour changes. Remove personal data. -->
