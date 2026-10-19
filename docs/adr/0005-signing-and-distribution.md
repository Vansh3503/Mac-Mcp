# ADR-0005: Signing and distribution without a paid Apple account

- **Status:** Accepted
- **Date:** 2026-10-19
- **Deciders:** AXPilot maintainers
- **Source:** [PLAN.md §4, ADR-5](../PLAN.md#4-key-decisions-adrs)

## Context and problem statement

The launcher app ([ADR-0002](0002-architecture-launcher-host-bridge.md)) must be code-signed for
TCC to identify it. Developer ID signing and notarization need the Apple Developer Program
($99 per year); Apple's fee waiver covers nonprofits, accredited education and government only.

Two macOS facts shape the options:

- TCC pins an **ad-hoc-signed** app to its exact **cdhash**: any change to the binary or a sealed
  file (such as `Info.plist`) invalidates existing grants. A certificate-signed app is pinned to a
  designated requirement that survives rebuilds with the same certificate.
- **Gatekeeper only assesses quarantined files.** Browsers set `com.apple.quarantine`; command-line
  downloaders such as curl don't, and package managers very likely don't (to be verified).

How do we ship a launcher that users can grant once, for free, without Gatekeeper warnings?

## Decision drivers

- Free for the project and for users.
- Grants that survive AXPilot releases.
- No signing key that could leak and let an attacker inherit users' grants.
- No "unidentified developer" prompts in the supported install paths.

## Decision

Adopt **tier 1, the frozen ad-hoc launcher**, for v1:

- The launcher is built **once per launcher version** in CI (`launcher.yml`), signed ad hoc
  (`codesign --sign -`), and published as a `launcher-vN` release asset with its SHA-256 and the
  cdhash of each architecture slice, recorded in `launcher/CDHASH.md`.
- Every AXPilot wheel embeds that exact bundle, byte for byte (the wheel is tagged
  `macosx_14_0_universal2`). `release.yml` verifies the checksum and cdhash before building.
  `axpilot setup` copies it to `~/Applications/AXPilot.app`.
- **Distribution rule:** the launcher reaches users only through **package managers**
  (uv, pip, pipx; a Homebrew formula once verified), never as a browser download, DMG or cask.
- `axpilot doctor` compares the installed launcher's cdhash with the published value, which detects
  both an outdated grant (`LAUNCHER_CHANGED`) and tampering.
- Move to **tier 3** (Developer ID + notarization) when sponsorship funds it.

| Tier | How | Cost | Verdict |
|---|---|---|---|
| 1. Frozen ad-hoc launcher | Built once per launcher version, embedded byte-identical | Free | **v1 default** |
| 2. Self-signed project certificate | Designated requirement survives rebuilds | Free | Rejected: a leaked key lets an attacker sign a binary that silently inherits users' grants; Gatekeeper still prompts for downloads |
| 3. Developer ID + notarization | Apple-issued certificate | $99/yr | Later, when funded; enables DMG, cask and auto-update; switching identity needs a one-time re-grant |

## Consequences

Positive:

- Free, with no private key to protect or leak.
- Grants persist across all AXPilot releases that keep the same launcher version.
- The published cdhash doubles as an integrity check.

Negative:

- A new launcher version forces a one-time re-grant for every user, so the launcher must stay
  minimal and change rarely (announced in the changelog, guided by `doctor`).
- No browser-download distribution, DMG or Homebrew cask in v1.
- Wheels become platform-specific because they embed the launcher.
- The Homebrew formula must be checked not to modify or re-sign the bundle in the keg.

## Alternatives considered

See tiers 2 and 3 above. Rebuilding the launcher in every release was also rejected: a rebuild of the
same source with another toolchain produces a new cdhash, so every release would force a re-grant.

## Validation

- **Spike S1, section D:** no quarantine and no Gatekeeper prompt for installs through uv and pipx
  (and later Homebrew); the negative control shows a browser download is quarantined.
- **Spike S1, B6:** a changed launcher loses the grant, confirming cdhash pinning (and what `doctor`
  must detect).
- `release.yml` refuses to embed a launcher whose checksum or cdhash differs from `launcher/CDHASH.md`.
- Revisit if a package manager starts quarantining files, or when funding for tier 3 exists.
