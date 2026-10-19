# Engineering quality bar: error handling and testing

These rules are **mandatory** for every module. Reviews and CI enforce them. When code and this
document disagree, fix the code or update this document in the same PR.

## Part A: Error handling

### A1. One error type crosses module boundaries
- Inside AXPilot, failures travel as `axpilot.errors.AXPilotError(code, message, hint=..., details=...)`.
- Engines, system integrations, policy and setup code **must not** let other exceptions escape their
  public functions. Wrap them at the boundary, preserving the cause:
  `raise AXPilotError(ErrorCode.X, "...") from exc`.
- The MCP tool guard is the last line of defense. Any non-`AXPilotError` that still reaches it becomes
  `ErrorCode.INTERNAL` with a short incident id (`err_<8 hex>`). The full traceback goes to stderr and
  `~/Library/Logs/AXPilot/axpilot.log`, **never** into the tool result.

### A2. Choose the most specific code
| Situation | Code |
|---|---|
| Bad/missing/conflicting arguments (e.g. both `ref` and `x/y`) | `INVALID_ARGUMENT` |
| App / window / display / menu / snapshot not found | `APP_NOT_FOUND` / `WINDOW_NOT_FOUND` / `DISPLAY_NOT_FOUND` / `MENU_NOT_FOUND` / `SNAPSHOT_NOT_FOUND` |
| UI element gone / changed | `STALE_REF` |
| Element exists but can't do that (disabled, unsupported action) | `ELEMENT_NOT_ACTIONABLE` |
| Missing macOS permission | `PERMISSION_ACCESSIBILITY` / `PERMISSION_SCREEN_RECORDING` / `PERMISSION_AUTOMATION` |
| Safety policy refused | `BLOCKED_BY_POLICY` (and `TOOL_DISABLED` for disabled tools) |
| Human declined a confirmation | `USER_DENIED` |
| Human is using the machine / kill switch | `USER_ACTIVE` / `PAUSED_BY_USER` |
| Operation exceeded its deadline | `TIMEOUT` |
| Another input action holds the lock | `BUSY` |
| External command failed (non-zero exit) | `COMMAND_FAILED` (stderr summarized in `details`) |
| Feature unavailable on this OS/app/version | `NOT_SUPPORTED` / `UNSUPPORTED_OS` |

### A3. Messages are for humans and models
- Say **what** failed and **which target** (app name, ref, window title, command name).
- Give a `hint` that names the next step (the tool to call, the setting to change, the permission to grant).
- Never include secrets, typed text, clipboard contents, tokens, or full file contents. Truncate
  external stderr to 500 chars.
- Stack traces never appear in tool results.

### A4. Validate at the edge
- Tool parameters use pydantic constraints (`Field(ge=…, le=…, max_length=…)`, `Literal[...]`
  enums), so malformed calls fail before any side effect.
- Semantic validation (mutually exclusive or required-together arguments, empty strings, coordinates
  outside every display) comes next, before touching the engine, and raises `INVALID_ARGUMENT`.
- Config is validated once at load (`axpilot.config.load_config`) with a clear message naming the bad key.

### A5. Every wait has a deadline
- Accessibility calls: per-app messaging timeout (`perception.ax_timeout_ms`).
- Tree capture: `timeout_s` budget (the result is marked `truncated` rather than failing when possible).
- Screen capture / OCR / app launch completion handlers: bounded `threading.Event.wait(timeout)`.
- Subprocesses: `timeout_s`, and on timeout the **whole process group is killed**; partial output is returned
  with `timed_out=True`.
- `wait_for` ≤ 60 s; input lock acquisition ≤ 5 s (`BUSY`); confirmations ≤ 120 s (then `USER_DENIED`).
- Async code never blocks the event loop. Blocking engine calls run in `anyio.to_thread.run_sync` with
  `abandon_on_cancel=True`, and client cancellation must release locks.

### A6. Retries and fallbacks
- Reads may retry **once** on transient AX errors (`kAXErrorCannotComplete`).
- **Input actions are never retried automatically** (double clicks or double typing are worse than errors).
- Strategy ladders (AX press → synthesized click, AX value → keystrokes → paste) record what they tried in
  the result (`strategy`, `attempts`). If every rung fails, raise `ELEMENT_NOT_ACTIONABLE` with `attempts`
  in `details`.

### A7. Cleanup is guaranteed
- `try/finally` (or context managers) for temp files, sockets, clipboard save/restore, event taps, opened
  menus (cancel on failure), held locks.
- Best-effort side channels (audit log, indicator overlay, notifications) never fail the tool. They log a
  warning once and continue.

### A8. Never swallow silently
- A bare `except:` / `except Exception: pass` is forbidden. When degrading gracefully, log at
  `warning`/`debug` with context, and surface the degradation in the result (`warnings: [...]`) when the
  model should know (e.g. "screenshot skipped: no Screen Recording permission").

### A9. CLI behaviour
- Exit codes: `0` ok · `1` operational error · `2` usage error · `3` permission missing · `4` unsupported OS.
- Errors print one human line plus the hint to stderr. `--json` prints `{"error": {...}}` instead.
- `KeyboardInterrupt` exits `130` cleanly, with no traceback.

## Part B: Testing

### B1. Test pyramid
| Layer | Scope | Runs on |
|---|---|---|
| Unit | pure functions/classes | Linux + macOS |
| Component | one module with fakes (`FakeEngine`, `FakeRunner`, stub clocks) | Linux + macOS |
| Contract | MCP in-process `mcp.Client(server)` against `FakeEngine`: schemas, results, errors | Linux + macOS |
| Integration | `axpilot stdio --engine fake` as a real subprocess; host socket + bridge end-to-end | Linux + macOS |
| macOS E2E | real `MacEngine` (`@pytest.mark.macos`, opt-in via `AXPILOT_MACOS_TESTS=1`) | Mac |

### B2. Error paths are first-class tests
- **Error matrix:** every tool has tests for its success path **and** each error code it can return
  (invalid args, not found, stale ref, policy block, permission, timeout, user denied, paused, busy…).
- **Fault injection:** `tests/support/faulty_engine.py` wraps any engine and makes chosen methods raise a
  given `AXPilotError`, a raw `RuntimeError`, or hang past the deadline. Tests prove each tool:
  1. returns `isError: true` with the right code (raw exceptions → `INTERNAL` with an incident id),
  2. never crashes the server (the next call succeeds),
  3. releases the input lock and records a failed audit event,
  4. leaks no traceback or secret into the result.
- Subprocess failures are tested via `FakeRunner` (non-zero exit, timeout, garbage output, missing binary).

### B3. Properties and invariants (hypothesis)
Key-combo parse/format round-trip; redaction never leaks redacted keys; AppleScript string escaping
can't break out of the literal; JSONC stripping preserves strings; config env coercion; snapshot
invariants (refs unique and dense, `elements ⊆ all_elements`, paging covers every element exactly once,
secure values never present).

### B4. Golden files
`tools/list` per profile × naming scheme (catches accidental API breaks) and the snapshot text format.
Golden files live in `tests/**/golden/` and are regenerated only deliberately (`AXPILOT_UPDATE_GOLDEN=1`).

### B5. Concurrency and cancellation
Parallel tool calls; input actions serialized; a cancelled call releases the lock; `BUSY` after the lock
timeout; stores are thread-safe.

### B6. Security tests
Policy bypass attempts: a coordinate click landing on a deny-listed app, typing into Terminal with scripting
off, a secure field, a deny-listed window title, an unpaired client, HTTP without a token or with a bad
Origin/Host, injection through AppleScript/mdfind/defaults arguments.

### B7. Hermetic and deterministic
- No network. No real home directory: `tmp_path` + `monkeypatch` for `HOME` and all `AXPILOT_*` path vars.
- Inject clocks; no `sleep` > 0.2 s in tests; no reliance on test order.
- Tests that need macOS are marked and skipped elsewhere, never silently passing.

### B8. Coverage gate
Branch coverage on, measured over everything except `engine/macos` and `host/ui`. CI fails below the
threshold in `[tool.coverage.report] fail_under` (target ≥ 90%). New code must not lower it.
