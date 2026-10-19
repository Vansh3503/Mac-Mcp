#!/usr/bin/env bash
# Spike S1: does a child of the ad-hoc-signed AXPilot.app inherit its TCC grants?
#
#   spikes/s1_launcher_tcc/run_s1.sh [options]
#
# Builds the launcher, installs it to ~/Applications/AXPilot.app with a plain file copy (as pip/uv
# would, so no quarantine), writes a runtime.json that runs probe.py instead of the host, starts
# AXPilot through LaunchServices (`open -g -a`), waits for the probe's report and prints it.
#
# Options:
#   --label NAME          scenario name for the report (default: launcher)
#   --prompt              let the probe call AXIsProcessTrustedWithOptions(prompt: true)
#   --request-screen      let the probe call CGRequestScreenCaptureAccess()
#   --wait-for-trust S    let the probe poll AXIsProcessTrusted() for up to S seconds
#   --python PATH         interpreter for the probe (default: python3 on PATH, resolved to a path)
#   --app-dir DIR         install into DIR/AXPilot.app (default: ~/Applications)
#   --skip-build          reuse launcher/build/AXPilot.app
#   --skip-install        reuse the installed bundle as is (tests "unchanged bytes keep the grant")
#   --direct              control run: start the probe directly from this terminal, no launcher
#   --timeout S           seconds to wait for the report (default: 60, plus --wait-for-trust)
#
# See README.md and MATRIX.md in this directory for the scenarios and how to record results.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PROBE="$HERE/probe.py"
RESULTS="$HERE/results"

label=launcher
probe_flags=()
python_bin=""
app_dir="$HOME/Applications"
skip_build=0
skip_install=0
direct=0
timeout=60
wait_for_trust=0

die() { printf 'run_s1.sh: error: %s\n' "$*" >&2; exit 1; }
step() { printf '==> %s\n' "$*"; }
need_value() { [[ $# -ge 2 && -n "$2" ]] || die "$1 needs a value"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label) need_value "$@"; label="$2"; shift 2 ;;
    --prompt) probe_flags+=(--prompt); shift ;;
    --request-screen) probe_flags+=(--request-screen); shift ;;
    --wait-for-trust) need_value "$@"; wait_for_trust="$2"; probe_flags+=(--wait-for-trust "$2"); shift 2 ;;
    --python) need_value "$@"; python_bin="$2"; shift 2 ;;
    --app-dir) need_value "$@"; app_dir="$2"; shift 2 ;;
    --skip-build) skip_build=1; shift ;;
    --skip-install) skip_install=1; shift ;;
    --direct) direct=1; shift ;;
    --timeout) need_value "$@"; timeout="$2"; shift 2 ;;
    -h | --help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[[ "$(uname -s)" == Darwin ]] || die "spike S1 runs on macOS"
[[ "$label" =~ ^[A-Za-z0-9._-]+$ ]] || die "--label may only contain letters, digits, '.', '_' and '-'"
[[ "$timeout" =~ ^[0-9]+$ ]] || die "--timeout must be a whole number of seconds"
[[ "$wait_for_trust" =~ ^[0-9]+$ ]] || die "--wait-for-trust must be a whole number of seconds"
if [[ -z "$python_bin" ]]; then
  python_bin="$(command -v python3 || true)"
  [[ -n "$python_bin" ]] || die "python3 not found; pass --python /absolute/path/to/python3"
fi
[[ "$python_bin" == /* && -x "$python_bin" ]] || die "--python must be an absolute path to an executable"
mkdir -p "$RESULTS"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"

# Probe flags may be empty; "${probe_flags[@]}" on an empty array fails under `set -u` in bash 3.2.
probe_args=("$PROBE")
if [[ ${#probe_flags[@]} -gt 0 ]]; then probe_args+=("${probe_flags[@]}"); fi

if [[ "$direct" == 1 ]]; then
  report="$RESULTS/$stamp-direct-$label.json"
  step "Control run: probe started directly by $(ps -o comm= -p $PPID 2>/dev/null || echo 'this shell')"
  "$python_bin" "${probe_args[@]}" --label "direct-$label" --out-file "$report"
  exit 0
fi

app="$app_dir/AXPilot.app"
manifest="$RESULTS/$stamp-$label-runtime.json"
report="$RESULTS/$stamp-$label.json"
log_file="$HOME/Library/Logs/AXPilot/launcher.log"

if [[ "$skip_build" == 0 ]]; then
  step "Building the launcher"
  "$REPO/launcher/build.sh"
fi

step "Stopping any running AXPilot"
pkill -x AXPilot 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
  pgrep -x AXPilot >/dev/null 2>&1 || break
  sleep 0.5
done
pgrep -x AXPilot >/dev/null 2>&1 && die "AXPilot is still running; quit it first (pkill -x AXPilot)"

if [[ "$skip_install" == 0 ]]; then
  step "Installing to $app (plain copy, as a package manager would)"
  "$python_bin" "$HERE/install.py" bundle "$REPO/launcher/build/AXPilot.app" "$app"
fi
[[ -d "$app" ]] || die "$app does not exist (drop --skip-install)"
if xattr -p com.apple.quarantine "$app" >/dev/null 2>&1; then
  echo "    NOTE: $app carries com.apple.quarantine; Gatekeeper may block it"
fi

step "Writing $manifest"
"$python_bin" "$HERE/install.py" manifest "$manifest" --python "$python_bin" --env "AXPILOT_SPIKE=s1" -- \
  "${probe_args[@]}" --label "$label" --out-file "$report"

log_lines_before=0
[[ -f "$log_file" ]] && log_lines_before="$(wc -l <"$log_file" | tr -d ' ')"

step "Starting AXPilot through LaunchServices: open -g -a $app"
open -g -a "$app" --args --runtime "$manifest"

deadline=$((SECONDS + timeout + wait_for_trust))
until [[ -s "$report" ]]; do
  if ((SECONDS > deadline)); then
    echo "No report after $((timeout + wait_for_trust)) s. Launcher log:" >&2
    [[ -f "$log_file" ]] && tail -n +"$((log_lines_before + 1))" "$log_file" >&2
    exit 1
  fi
  sleep 1
done
sleep 1  # let the probe finish writing and the launcher log its exit

step "Launcher log for this run ($log_file)"
[[ -f "$log_file" ]] && tail -n +"$((log_lines_before + 1))" "$log_file" | sed 's/^/    /'
if pgrep -x AXPilot >/dev/null 2>&1; then
  echo "    NOTE: AXPilot is still running; expected it to exit after the probe" >&2
fi

step "Report: $report"
"$python_bin" - "$report" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
summary = report["summary"]
print(f"    verdict: {summary['verdict']}")
for key, value in summary.items():
    if key not in ("verdict", "reasons"):
        print(f"    {key}: {value}")
for reason in summary["reasons"]:
    print(f"    reason: {reason}")
PY
