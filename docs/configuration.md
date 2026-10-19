# Configuration

AXPilot works without any configuration. To change its behaviour, use a TOML file, environment
variables, or command-line flags.

**Precedence**, highest first:

1. command-line flags (for example `axpilot stdio --profile minimal`);
2. environment variables `AXPILOT_<SECTION>_<KEY>` (for example `AXPILOT_TOOLS_PROFILE=minimal`);
3. the config file, `~/.config/axpilot/config.toml`;
4. built-in defaults (below).

The config file is validated when AXPilot starts: an unknown key or an out-of-range value is an
error that names the key. Environment variables use the section and key names in upper case; list
values are comma-separated (`AXPILOT_TOOLS_ENABLE=scripting,browser`).

The `axpilot config` command manages the file:

```sh
axpilot config path                          # where the file is
axpilot config show                          # the effective configuration
axpilot config get tools.profile
axpilot config set safety.confirm always
axpilot config edit                          # open it in $EDITOR
```

The host's menu-bar *Settings* window edits the same file *(planned, v0.2)*.

## Full example with defaults

```toml
[server]
http_enabled = false
http_host = "127.0.0.1"
http_port = 7788
allow_remote = false
# tls_cert = "/path/to/cert.pem"
# tls_key = "/path/to/key.pem"
engine_timeout_s = 20.0
input_lock_timeout_s = 5.0

[tools]
profile = "standard"      # minimal | standard | full | computer
naming = "default"        # default | windows-mcp
enable = []               # tool or toolset names, e.g. ["scripting", "browser"]
disable = []              # tool or toolset names, e.g. ["clipboard"]
prefix = ""               # e.g. "mac_" → mac_snapshot, mac_click, …

[safety]
confirm = "destructive"   # never | destructive | always
yield_to_user = true
user_idle_threshold_s = 1.0
indicator = true
deny_apps = [
  "com.apple.keychainaccess",
  "com.apple.Passwords",
  "com.1password.1password",
  "com.agilebits.onepassword7",
  "com.bitwarden.desktop",
  "com.lastpass.LastPass",
  "com.dashlane.dashlanephonefinal",
  "org.keepassxc.keepassxc",
]
deny_windows = [
  "com.apple.systempreferences::Privacy & Security",
  "com.apple.settings.PrivacySecurity.extension::Privacy & Security",
]
allow_apps = []           # non-empty: only these apps may be observed or controlled
allow_secure_fields = false
max_actions_per_minute = 120
require_pairing = true
confirm_timeout_s = 120.0

[capture]
max_edge = 1280
format = "jpeg"           # png | jpeg
quality = 80

[perception]
max_elements = 150
max_nodes = 4000
max_depth = 40
ax_timeout_ms = 1000
value_max_chars = 200
enhance_electron_chrome = true
settle_ms = 150
capture_timeout_s = 5.0

[scripting]
shell = "/bin/zsh"
shell_timeout_s = 30
output_max_chars = 20000

[audit]
enabled = true
log_args = "redacted"     # none | redacted | full
# path = "~/Library/Logs/AXPilot/audit.jsonl"
```

## `[server]`

| Key | Default | Meaning |
|---|---|---|
| `http_enabled` | `false` | Serve Streamable HTTP in addition to the local socket *(planned)* |
| `http_host` | `"127.0.0.1"` | Address to bind. Anything other than localhost also needs `allow_remote` |
| `http_port` | `7788` | Port (1–65535) |
| `allow_remote` | `false` | Allow non-localhost connections; requires `tls_cert` and `tls_key`. Prefer an SSH tunnel or Tailscale |
| `tls_cert`, `tls_key` | unset | PEM files for TLS |
| `engine_timeout_s` | `20.0` | Deadline for one accessibility or input call made by a tool (1–300 s); slower calls fail with `TIMEOUT` |
| `input_lock_timeout_s` | `5.0` | How long an input tool waits for another input action to finish before failing with `BUSY` (0.1–5 s) |

The HTTP bearer token is generated on first use and stored in
`~/Library/Application Support/AXPilot/http-token` (mode `0600`), not in this file. Keychain storage
is planned.

## `[tools]`

| Key | Default | Meaning |
|---|---|---|
| `profile` | `"standard"` | Starting tool set: `minimal`, `standard`, `full` or `computer` (see [Tools](tools.md#profiles)) |
| `naming` | `"default"` | `windows-mcp` exposes Windows-MCP's tool names and parameter shapes (see [tools](tools.md)) |
| `enable` | `[]` | Tool or toolset names added to the profile (`"scripting"`, `"browser"`, `"ocr"`, …) |
| `disable` | `[]` | Tool or toolset names removed from the profile. A name in both lists ends up disabled |
| `prefix` | `""` | Put before every tool name (`"mac_"` → `mac_snapshot`), for clients that mix several servers' tools without namespacing them. Letters, digits, `_` and `-`, at most 24 |

Unknown tool or toolset names are an error (with a "did you mean" suggestion).

## `[safety]`

| Key | Default | Meaning |
|---|---|---|
| `confirm` | `"destructive"` | When to ask the human before running a tool: `never`, `destructive` (shell, AppleScript, JS eval, force quit, `defaults` write, process kill) or `always` |
| `yield_to_user` | `true` | Pause input actions (`USER_ACTIVE`) while you are using the mouse or keyboard |
| `user_idle_threshold_s` | `1.0` | Seconds of hardware inactivity required before AXPilot sends input (0–30) |
| `indicator` | `true` | Show the on-screen "agent is controlling" indicator *(planned, v0.2)* |
| `deny_apps` | password managers, Keychain Access | Apps that can't be observed, controlled or captured: bundle ids or names, case-insensitive, `*` as a wildcard (`"com.agilebits.*"`) |
| `deny_windows` | System Settings → Privacy & Security | `"<bundle id>::<title substring>"` entries that block one window of an otherwise allowed app |
| `allow_apps` | `[]` | If non-empty, **only** these apps can be observed or controlled |
| `allow_secure_fields` | `false` | Allow typing into password fields. Their values are never returned either way |
| `max_actions_per_minute` | `120` | Input rate limit (1–10000); excess calls get `RATE_LIMITED` |
| `require_pairing` | `true` | Ask before a new client app may connect to the host; the answer is remembered |
| `confirm_timeout_s` | `120.0` | How long a confirmation waits for your answer (5–120 s); no answer counts as `USER_DENIED` |

Setting `deny_apps` or `deny_windows` **replaces** the default list. Copy the defaults from the example
above if you only want to add an entry.

Some rules are not configurable:

- Typing into terminal apps (Terminal, iTerm2, Ghostty, Warp, kitty, Alacritty, WezTerm, Hyper,
  Tabby) is blocked unless the `shell` tool is enabled (`enable = ["scripting"]` or `["shell"]`),
  because it amounts to shell access. Likewise `open` won't run files (`.command`, executables…)
  or open anything with a terminal app.
- macOS authentication dialogs are never operated; tools return `NEEDS_USER`.
- AXPilot's own windows (confirmations, settings) can be read but never controlled, so an agent
  can't approve its own requests. Reading AXPilot's `status` is never blocked.

## `[capture]`

| Key | Default | Meaning |
|---|---|---|
| `max_edge` | `1280` | Longest edge of returned screenshots, in pixels (256–8192) |
| `format` | `"jpeg"` | `png` or `jpeg` |
| `quality` | `80` | JPEG quality (10–100) |

## `[perception]`

| Key | Default | Meaning |
|---|---|---|
| `max_elements` | `150` | Elements per snapshot page (10–2000); use `offset` for more |
| `max_nodes` | `4000` | Accessibility nodes visited per snapshot before it is marked truncated |
| `max_depth` | `40` | Maximum tree depth visited |
| `ax_timeout_ms` | `1000` | Per-call Accessibility messaging timeout, so a hung app can't block AXPilot |
| `value_max_chars` | `200` | Element values are truncated to this length |
| `enhance_electron_chrome` | `true` | Ask Chrome and Electron apps to expose their full accessibility tree (`AXManualAccessibility`) |
| `settle_ms` | `150` | How long the UI must be quiet before an action's diff is taken |
| `capture_timeout_s` | `5.0` | Time budget for reading one window's accessibility tree (0.5–60 s); a snapshot that runs out is marked truncated |

## `[scripting]`

Applies only when the `scripting` toolset is enabled.

| Key | Default | Meaning |
|---|---|---|
| `shell` | `"/bin/zsh"` | Shell used by the `shell` tool (run as `<shell> -lc <command>`) |
| `shell_timeout_s` | `30` | Default command timeout (1–120 s) |
| `output_max_chars` | `20000` | Output beyond this is truncated |

## `[audit]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Append one JSON line per tool call to the audit log |
| `log_args` | `"redacted"` | `none`, `redacted` (typed text, values, scripts and commands become `<redacted N chars>`) or `full` |
| `path` | `~/Library/Logs/AXPilot/audit.jsonl` | Where the audit log is written (created with mode 0600) |

## Environment variables for paths

These redirect AXPilot's files, which is mostly useful for testing:

| Variable | Default |
|---|---|
| `AXPILOT_CONFIG_DIR` | `$XDG_CONFIG_HOME/axpilot`, else `~/.config/axpilot` |
| `AXPILOT_HOME` | `~/Library/Application Support/AXPilot` (runtime, `runtime.json`, paired clients, socket) |
| `AXPILOT_LOG_DIR` | `~/Library/Logs/AXPilot` |
| `AXPILOT_SOCKET` | `$AXPILOT_HOME/axpilot.sock` (or a path in `$TMPDIR` if that is too long for a Unix socket) |
| `AXPILOT_APP_PATH` | `~/Applications/AXPilot.app` |
