# Security policy

AXPilot gives AI agents control of a real Mac, so we take security reports seriously and appreciate
your help. This page explains how to report a vulnerability, what we consider in scope, and the
threat model the project is designed against.

## Supported versions

AXPilot is **alpha** (0.1.0a1, a pre-release on PyPI) and has no stable release yet. Until 1.0, only the latest pre-release and
the `main` branch receive security fixes.

| Version | Supported |
|---|---|
| `main` | Yes |
| Latest `0.x` pre-release (now 0.1.0a1) | Yes |
| Older `0.x` pre-releases | No, please upgrade |

After 1.0 we will support the latest minor release of the current major version, and announce
end-of-support dates for older lines in the [changelog](CHANGELOG.md).

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for a security problem.**

Report it privately through GitHub's private vulnerability reporting:
[**Report a vulnerability**](https://github.com/vansh3503/Mac-Mcp/security/advisories/new)
(repository → *Security* → *Advisories* → *Report a vulnerability*).

Please include, as far as you can:

- the AXPilot version or commit, macOS version and architecture, and the MCP client or framework;
- the configuration involved (profile, enabled toolsets, safety settings) and whether you ran in
  launcher mode or direct mode (`--direct`);
- step-by-step reproduction, a proof of concept, and the impact you observed;
- any suggested fix or mitigation.

Redact personal data from logs and screenshots. `axpilot doctor` output and the relevant lines of
`~/Library/Logs/AXPilot/audit.jsonl` are usually helpful.

### What to expect

This is a small, volunteer-run project, so these are goals rather than guarantees:

- acknowledgement within **7 days**;
- an initial assessment (confirmed, needs more information, or not a vulnerability) within
  **14 days**;
- a fix or mitigation for confirmed issues as fast as severity warrants, coordinated with you
  through the advisory, followed by a published GitHub Security Advisory (with a CVE when
  appropriate) that credits you unless you prefer otherwise.

Please give us a reasonable time to ship a fix before public disclosure; we aim to disclose within
**90 days** of the report, and sooner once a fix is released.

### Safe harbor

We will not pursue or support legal action against good-faith research that follows this policy:
test only on machines and accounts you own or are authorized to test, avoid privacy violations and
data destruction, and do not degrade other people's systems. If in doubt, ask in the advisory first.

## Threat model (summary)

The full model is in [docs/security.md](docs/security.md) and [PLAN.md §7](docs/PLAN.md#7-security-and-safety-model).
AXPilot runs locally, as the logged-in user, with the macOS permissions that user granted it.

| Threat | Main mitigations |
|---|---|
| **Prompt injection from screen content.** Text in a web page, email or document tries to steer the agent. | Risky toolsets (`scripting`, `browser`) off by default; confirmations for destructive actions; app and window deny-lists; the Terminal rule; rate limits; audit log; documentation that screen content is untrusted. |
| **Confused deputy / local TCC bypass.** Another local process uses AXPilot's permissions through its socket or HTTP port. | Unix socket with mode `0600` and same-uid peer checks; client pairing (*"Allow «Claude» to control this Mac?"*); HTTP bearer token stored in the Keychain. Pairing raises the bar but is **not** a hard boundary against malware running as the same user. |
| **Runaway agent.** An agent loops or acts too fast. | Rate limit (120 input actions/min), kill switch (⌃⌥⌘.), yield-to-user, confirmations, preferring Trash over deletion. |
| **Data exfiltration.** Reading secrets through the UI or the clipboard. | Secure-field values are never returned; deny-listed apps are excluded from snapshots and screenshots; clipboard reads are audited and can be disabled. |
| **Network exposure.** The optional HTTP transport is reached by other machines or web pages. | Binds to `127.0.0.1`; requires a token; checks `Origin` and `Host`; no CORS. Remote access needs `--allow-remote` plus TLS (SSH or Tailscale are recommended instead). |
| **Supply chain and launcher integrity.** A tampered package or launcher. | PyPI trusted publishing with attestations, a locked dependency set (`uv.lock`), Dependabot, and a published cdhash per launcher version that `axpilot doctor` verifies. The launcher is ad-hoc signed, so there is no signing key to leak. |

Several of these controls (pairing, the kill switch, the HTTP transport, trusted publishing) are
planned for the v0.2 and v0.3 milestones; [docs/security.md](docs/security.md) tracks what is
implemented today.

## Scope

**In scope**, for example:

- bypassing a safety control in its default configuration: deny-lists (including via coordinate
  clicks or keyboard shortcuts), the Terminal rule, secure-field protection, confirmations, rate
  limits, the kill switch, yield-to-user, disabled tools or toolsets;
- secure-field values, tokens or other secrets appearing in tool results, logs or the audit log
  (for example a redaction failure);
- connecting to the host socket or HTTP transport without pairing, the token, or the same uid;
  `Origin`/`Host` check bypasses; DNS rebinding;
- command or script injection through tool arguments (AppleScript, `mdfind`, `defaults`, shell
  quoting) beyond what an enabled `scripting` toolset intends to allow;
- code execution or privilege problems in `axpilot setup`, `axpilot connect`, `packaging/install.sh`,
  the launcher or its `runtime.json` handling (including unsafe file permissions);
- tampering with the launcher that `axpilot doctor` fails to detect;
- vulnerabilities in our release pipeline or GitHub Actions workflows.

**Out of scope / please don't report:**

- actions an agent takes **within** the permissions you deliberately enabled, for example running
  shell commands after you enabled `scripting` with confirmations set to `never`;
- prompt injection that only succeeds after the user disabled the relevant safeguards, or that does
  not cross any control listed above (models can be persuaded; the controls are what we defend);
- attacks that require malware already running as the same user with full access to the account
  (documented as a non-boundary above), or physical access to an unlocked Mac;
- vulnerabilities in macOS itself, TCC, or third-party apps: please report those to Apple or the
  vendor (we are happy to help coordinate and to add mitigations);
- apps that expose sensitive data through their own accessibility tree, unless AXPilot's
  documented protections should have hidden it;
- vulnerabilities in dependencies without a demonstrated impact on AXPilot (Dependabot tracks
  these; a normal issue or pull request is fine);
- automated scanner output without a proof of concept, missing security headers on the docs site,
  social engineering, or denial of service against your own machine.

Not sure whether something is in scope? Report it privately anyway.

## Our commitments

- **No telemetry**, ever by default (see [ADR-6](docs/adr/0006-privacy-no-telemetry.md)).
- Security-relevant defaults are documented in [docs/security.md](docs/security.md) and changing
  them is called out in the changelog.
- Fixed vulnerabilities are disclosed in GitHub Security Advisories.
