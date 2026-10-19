# ADR-0006: Privacy: no telemetry

- **Status:** Accepted
- **Date:** 2026-10-19
- **Deciders:** AXPilot maintainers
- **Source:** [PLAN.md §4, ADR-6](../PLAN.md#4-key-decisions-adrs)

## Context and problem statement

AXPilot runs on personal machines and sees whatever is on screen: messages, documents, browsing,
names. Some comparable tools collect usage telemetry by default. Telemetry would help prioritize
work, but any data leaving the machine, even "anonymous" usage counts, is a trust and security
liability for a tool with this level of access.

Does AXPilot collect telemetry?

## Decision drivers

- Users must be able to trust AXPilot with full access to their Mac.
- Organizations should be able to adopt it without a data-protection review of a remote service.
- A smaller attack surface: no network client in the server.

## Decision

- **No telemetry, ever by default.** AXPilot makes no network requests of its own: no usage
  statistics, no update checks, no crash uploads. Tools that the user enables to reach the network
  (opening a URL, the browser toolset, shell commands) act only when called.
- Logs, the audit log, snapshots and screenshots stay on the machine (`~/Library/Logs/AXPilot/` and
  memory) and go only to the MCP client that requested them.
- **Opt-in crash reports** may be added later, only with explicit consent per report, and only
  through a new ADR.

## Consequences

Positive:

- A clear, verifiable privacy promise ("no network calls in code or tests" is a project rule).
- No backend to run, secure or pay for.

Negative:

- No usage data: priorities come from issues, discussions, app-compatibility reports and published
  evals instead.
- Bug reports depend on users sharing `axpilot doctor` output and logs manually.

## Alternatives considered

- **Opt-out telemetry:** the most data, but violates the trust model.
- **Opt-in anonymous usage statistics:** better, but still a network client, a backend, and a
  standing temptation to widen collection. Not needed for v1.
- **Automatic crash reporting:** crash reports can contain screen content and arguments. Possible
  later as opt-in, per report.

## Validation

- Code review and the contributor rules ([CONTRACTS.md](../dev/CONTRACTS.md), AGENTS.md) forbid
  network access and telemetry; tests are hermetic and run without network access.
- Any proposal that sends data off the machine requires a new ADR that supersedes this one.
