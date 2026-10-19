# ADR-0003: Minimum macOS 14 Sonoma

- **Status:** Accepted
- **Date:** 2026-10-19
- **Deciders:** AXPilot maintainers
- **Source:** [PLAN.md §4, ADR-3](../PLAN.md#4-key-decisions-adrs)

## Context and problem statement

Every supported macOS version adds code paths and test cost. Screen capture is the sharpest edge:
ScreenCaptureKit's `SCScreenshotManager` (single-frame capture) needs macOS 14, while the older
CoreGraphics capture API is deprecated. macOS 13 no longer receives security updates. macOS 27
supports Apple silicon only, so Intel Macs top out at macOS 26.

Which macOS versions does AXPilot support?

## Decision drivers

- One modern capture path, no deprecated fallbacks.
- A reasonably uniform Accessibility baseline.
- Keep Intel Macs that can run a supported macOS.
- Test every supported version in CI.

## Decision

- **Minimum: macOS 14 Sonoma**, on Apple silicon and Intel.
- The launcher is a **universal2** binary (arm64 + x86_64); Python wheels resolve per architecture
  through uv and pip.
- Test matrix: GitHub-hosted `macos-14` and `macos-15` runners (plus `macos-26` once generally
  available), and manual or self-hosted macOS 27.
- On older systems AXPilot refuses to start the macOS engine with `UNSUPPORTED_OS`, and
  `axpilot setup` checks the version first.

## Consequences

Positive:

- A single capture implementation on ScreenCaptureKit.
- Fewer version checks in the engine, and a smaller test matrix.
- Intel Macs on macOS 14 to 26 are supported.

Negative:

- Macs stuck on macOS 13 or older are excluded.
- macOS 15 and later re-prompt periodically for Screen Recording, which the design absorbs by making
  Screen Recording optional (AX-first).

## Alternatives considered

- **macOS 13 Ventura:** reaches more Macs, but needs the deprecated CoreGraphics capture path and an
  end-of-life OS in the matrix.
- **macOS 15 Sequoia** (Peekaboo's minimum): fewer code paths still, but excludes many Macs that run
  macOS 14 without a strong technical reason.

## Validation

- CI runs the test suite on `macos-14` and `macos-15` for every pull request.
- Spike S4 measures ScreenCaptureKit through PyObjC on 14, 15, 26 and 27, including exclusion
  filters and the periodic prompt.
- Revisit when GitHub retires the `macos-14` runner image or when macOS 14 stops receiving security
  updates.
