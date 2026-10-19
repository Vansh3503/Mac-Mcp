# AXPilot launcher (`AXPilot.app`)

A tiny, dependency-free Swift app bundle whose only job is to start the AXPilot Python host
**as its child**, so that macOS attributes privacy permissions (Accessibility, Screen Recording,
Automation, and later Calendars/Reminders/Contacts) to `AXPilot.app` instead of to whichever
Python, terminal or AI client happened to start it. Background: [PLAN.md](../docs/PLAN.md),
ADR-2 (architecture) and ADR-5 (signing and distribution).

| File | Purpose |
|---|---|
| [`main.swift`](main.swift) | The launcher (Foundation + Darwin only, under 250 lines) |
| [`Info.plist`](Info.plist) | Bundle id `io.github.vansh3503.axpilot`, `LSUIElement`, usage strings |
| [`build.sh`](build.sh) | Builds, ad-hoc signs, verifies and packages the universal2 bundle |
| [`CDHASH.md`](CDHASH.md) | Published cdhash and checksums of every released launcher version |
| [`tests/`](tests) | Behaviour tests for the built bundle (macOS) and the build script (any OS) |

## Why it exists

TCC, the macOS privacy system, checks a request against the **responsible process**: the app
that macOS considers accountable for the process making the call. A Python process spawned by
Claude Desktop is "Claude"; one started from Terminal is "Terminal". A grant to `python` itself
rarely applies, breaks whenever the interpreter changes, and missing usage strings in the
responsible app cause silent denials.

`AXPilot.app` fixes this. LaunchServices starts it (`open -g -a AXPilot`), which makes it its own
responsible process. It then starts the host with `posix_spawn()`, so the host inherits that
responsibility: the host's AX, CGEvent and ScreenCaptureKit calls are checked against
`AXPilot.app`. The user grants Accessibility once, to "AXPilot", and every MCP client works.
(`exec` would not work: it would replace the app's process image with Python's.) Spike S1
verifies this on every supported macOS version; see [`spikes/s1_launcher_tcc`](../spikes/s1_launcher_tcc).

## Why it is frozen

The launcher is signed **ad hoc** (`codesign --sign -`). That is free and involves no private key
that could leak, but it means the bundle's only identity is its **cdhash**, the hash of its code
directory. TCC stores the grant together with a code requirement of the form `cdhash H"…"`.

- Any change to the executable, or to a file the signature seals (such as `Info.plist`), produces
  a new cdhash. The old grant no longer matches, even if System Settings still shows "AXPilot"
  switched on. Every user then has to grant access again.
- Each architecture slice of the universal binary has its own code directory, so its own cdhash.
  On Apple silicon TCC pins the arm64 one, on Intel the x86_64 one. `build.sh` prints both.
- A rebuild of the *same source* with another Xcode version also produces a new cdhash. That is
  why releases embed the bundle that CI built once per launcher version, byte for byte, instead of
  rebuilding it.

So everything that can change lives in Python, and the launcher changes only for a deliberate,
versioned reason (`launcher-v2`, …). `axpilot doctor` compares the installed launcher's cdhash
with the published value, which detects both an outdated grant and tampering
(`LAUNCHER_CHANGED`). To clear a stale grant: `tccutil reset Accessibility io.github.vansh3503.axpilot`.

## Contract with the Python side

### runtime.json (schema version 1)

Written by `axpilot setup` to `~/Library/Application Support/AXPilot/runtime.json`:

```json
{"version": 1, "python": "/Users/me/Library/Application Support/AXPilot/runtime/bin/python",
 "args": ["-m", "axpilot.host"], "env": {"AXPILOT_PROFILE": "standard"}, "cwd": null}
```

| Key | Required | Meaning |
|---|---|---|
| `version` | yes | Must be `1`. Add fields freely (they are ignored); bump only for incompatible changes. |
| `python` | yes | Absolute path to an executable. It is `argv[0]` of the host. |
| `args` | yes | Arguments after `argv[0]`. |
| `env` | no | Variables set on top of the launcher's own environment (launchd's minimal one). |
| `cwd` | no | Absolute working directory; `null` or absent keeps the launcher's (`/` under LaunchServices). |

Where it is read from, first match wins: `--runtime <path>`, `$AXPILOT_HOME/runtime.json`,
`~/Library/Application Support/AXPilot/runtime.json`. The launcher re-reads it before every
(re)start, so `axpilot update` can switch interpreters without restarting the launcher. An
invalid file at startup makes the launcher exit with status 78 and log why.

### The host process

- Started with `posix_spawn`, never `exec`; same user, own process group (so a terminal's ^C
  reaches it only through the launcher).
- Environment: the launcher's, then `runtime.json`'s `env`, plus `AXPILOT_LAUNCHER_VERSION`
  (e.g. `1.0.0`) and `AXPILOT_LAUNCHER_BUNDLE` (the bundle path). The host can use these to know
  it runs under the launcher.
- stdin is `/dev/null`; stdout and stderr are appended to `launcher.log`; every other file
  descriptor is closed (`POSIX_SPAWN_CLOEXEC_DEFAULT`); all signal handlers are at their default
  and the signal mask is empty.
- The parent is the launcher. If the launcher dies (for example `kill -9`), the host is
  reparented to launchd; the host should watch `os.getppid()` and exit when it changes.

### Signals, restarts and exit codes

| Event | Launcher behaviour |
|---|---|
| SIGTERM or SIGINT | Forwarded to the host. When the host exits, the launcher exits with the host's status. If the host is still running 10 s later it gets SIGKILL. |
| SIGHUP | Forwarded only. If the host dies of it, it is restarted like after any failure. |
| Host exits 0 | The launcher exits 0. |
| Host exits 78 (`EX_CONFIG`) | The launcher exits 78 without restarting. Use it when a restart cannot help (another host already owns the socket, unsupported launcher version, …). |
| Host exits otherwise, or is killed by a signal | Restarted after 1, 2, 4, 8, 16 s. A sixth failure within 60 s makes the launcher log an error and exit 1. |

| Launcher exit status | Meaning |
|---|---|
| 0 | The host exited 0, or a stop signal arrived while no host was running |
| 1 | The host kept failing (or could not be spawned); see the log |
| 64 | `--runtime` given without a path |
| 78 | Invalid or missing `runtime.json`, or the host exited 78 |
| *n*, 128 + *signal* | After SIGTERM/SIGINT: the host's own exit status (128 + *signal* if a signal killed it) |

### Command line

```
AXPilot.app/Contents/MacOS/AXPilot [--runtime <path to runtime.json>] [--version]
```

Unknown arguments are ignored, because LaunchServices may add its own. Pass arguments through
LaunchServices with `open -g -a AXPilot --args --runtime /abs/path/runtime.json`. If AXPilot is
already running, `open` only activates it and the arguments are dropped.

Running the binary directly (not through `open`) works for debugging, but the launcher's parent is
then your shell, so TCC holds *your terminal* responsible; the launcher logs a warning about that.

### Logs

`~/Library/Logs/AXPilot/launcher.log` (or `$AXPILOT_LOG_DIR/launcher.log`), one timestamped line
per event, interleaved with the host's stdout/stderr (so a Python traceback from a failed start
is right there). At startup a log over 5 MB is moved to `launcher.log.1`. The host is expected to
keep its own logs in rotating files and write little to stderr.

## Building

Requirements: macOS 14 or later with Xcode or the Command Line Tools (`xcode-select --install`).

```sh
launcher/build.sh                        # output in launcher/build/ (git-ignored)
AXPILOT_LAUNCHER_OUT=/tmp/axp launcher/build.sh
```

The script checks that the version in `main.swift` matches `CFBundleShortVersionString`, compiles
`main.swift` for `arm64-apple-macos14` and `x86_64-apple-macos14` with `swiftc -O`, joins the
slices with `lipo`, assembles `AXPilot.app/Contents/{Info.plist,MacOS/AXPilot}`, signs it, verifies
it with `codesign --verify --strict`, runs `--version` (also under Rosetta when available), and
writes:

| Output | Contents |
|---|---|
| `AXPilot.app` | The signed bundle |
| `AXPilot.app.zip` | The bundle, zipped with `ditto` (the release asset) |
| `SHA256SUMS` | SHA-256 of every file in the bundle and of the zip |
| `BUILDINFO.txt` | cdhash per architecture, git commit and tree state, Swift and SDK versions |

Signing choices:

- **Ad hoc** (`--sign -`): free, no certificate, no key to protect ([ADR-5](../docs/PLAN.md#adr-5-signing--distribution-without-a-paid-apple-account), tier 1).
- **No hardened runtime.** It is required only for notarization, which an ad-hoc signature cannot
  get anyway, and under it TCC-attributed features would depend on entitlements (for example
  `com.apple.security.automation.apple-events`) that an un-notarized app gains nothing from.
  It returns with Developer ID signing (tier 3), together with the entitlements.
- **`--timestamp=none`**: ad-hoc signatures carry no secure timestamp, and this keeps signing
  fully offline.

## Testing

```sh
launcher/build.sh && uv run pytest launcher/tests -v
```

On macOS this runs the built launcher against a stub host and checks the whole contract above:
`--version`, manifest validation, exit-status propagation, the restart and backoff schedule, the
give-up rule, signal forwarding and SIGKILL escalation, the child's environment, file descriptors,
signal dispositions and process group, and the code signature. The suite needs about a minute.
On other systems only the build-script test runs (with stubbed Apple tools). The TCC behaviour
itself is verified by spike S1 on real Macs.

## How releases embed it

1. **`launcher.yml`** (manual, rare): runs `build.sh` on a macOS runner and publishes a
   `launcher-vN` GitHub release with `AXPilot.app.zip`, `SHA256SUMS` and `BUILDINFO.txt`, plus a
   build-provenance attestation. The maintainer records the values in [`CDHASH.md`](CDHASH.md).
2. **`release.yml`** (every `v*` tag): downloads the pinned `launcher-vN` asset, verifies its
   SHA-256 and both cdhashes against `CDHASH.md`, unpacks it into `src/axpilot/_launcher/`, and
   builds the `macosx_14_0_universal2` wheel. The launcher is never rebuilt here.
3. **`axpilot setup`** copies the bundle byte for byte to `~/Applications/AXPilot.app` and writes
   `runtime.json`. Package managers (pip, uv, pipx) do not set `com.apple.quarantine`, so
   Gatekeeper never assesses the un-notarized app; spike S1 verifies this per macOS version.
   Wheels cannot hold symlinks and the bundle has none; the executable bit of
   `Contents/MacOS/AXPilot` must survive installation (verify in S1).

## Changing the launcher

Only for a reason that cannot be handled in Python. Then:

1. Make the change, keep `main.swift` under 250 lines and dependency-free.
2. Bump `launcherVersion` in `main.swift` and `CFBundleShortVersionString` plus `CFBundleVersion`
   in `Info.plist`.
3. Run `launcher/build.sh` and `uv run pytest launcher/tests` on macOS, then spike S1 on every
   supported macOS version.
4. Publish `launcher-vN` through `launcher.yml`, record it in `CDHASH.md`, and note the one-time
   re-grant prominently in the changelog. `axpilot doctor` guides users through it.

## Security notes

- The launcher runs whatever `runtime.json` (or `--runtime`) names, with AXPilot's permissions.
  Both that file and the runtime venv are writable by the user, so any code running as that user
  can make the launcher run something else. That is not a new boundary (such code can already
  read and write your files), but it is why `axpilot setup` should write `runtime.json` with mode
  0600 in a 0700 directory and `doctor` should check it. See PLAN.md §7.1.
- No network access, no dependencies beyond Foundation and Darwin, no telemetry.
