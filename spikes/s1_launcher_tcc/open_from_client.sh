#!/usr/bin/env bash
# Spike S1, scenario C: start AXPilot.app the way the stdio bridge will, from inside an MCP client.
#
# Configure this script as an MCP server command in the client under test, for example in
# Claude Desktop's claude_desktop_config.json:
#
#   {"mcpServers": {"s1-probe": {
#      "command": "/ABSOLUTE/PATH/spikes/s1_launcher_tcc/open_from_client.sh",
#      "args": ["claude-desktop"],
#      "env": {"S1_PYTHON": "/ABSOLUTE/PATH/TO/python3"}}}}
#
# The client will report this "server" as failed (it does not speak MCP). That is expected: what
# matters is what happens when a process the client spawned runs `open -g -a AXPilot`. It runs the
# probe twice and writes both reports to results/:
#   <stamp>-<client>-direct.json    probe as the client's child (control: the client is responsible)
#   <stamp>-<client>-launcher.json  probe under AXPilot.app (treatment: AXPilot should be responsible)
#
# Prerequisite: run_s1.sh has installed ~/Applications/AXPilot.app at least once.
set -euo pipefail
exec 1>&2  # never write to stdout: the client expects MCP messages there

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS="$HERE/results"
client="${1:-client}"
python_bin="${S1_PYTHON:-/usr/bin/python3}"
app="${S1_APP:-$HOME/Applications/AXPilot.app}"

[[ "$client" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "invalid client label: $client"; exit 1; }
[[ -x "$python_bin" ]] || { echo "S1_PYTHON is not executable: $python_bin"; exit 1; }
[[ -d "$app" ]] || { echo "$app is missing; run run_s1.sh first"; exit 1; }
mkdir -p "$RESULTS"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"

echo "s1: control run as a child of $(ps -o comm= -p $PPID 2>/dev/null || echo '?')"
"$python_bin" "$HERE/probe.py" --label "$client-direct" --out-file "$RESULTS/$stamp-$client-direct.json" \
  --no-app-support-copy || true

if pgrep -x AXPilot >/dev/null 2>&1; then
  echo "s1: AXPilot is already running; quit it (pkill -x AXPilot) so that --args are not dropped"
  exit 1
fi
manifest="$RESULTS/$stamp-$client-runtime.json"
"$python_bin" "$HERE/install.py" manifest "$manifest" --python "$python_bin" --env "AXPILOT_SPIKE=s1" -- \
  "$HERE/probe.py" --label "$client-launcher" --out-file "$RESULTS/$stamp-$client-launcher.json"
echo "s1: treatment run through LaunchServices"
open -g -a "$app" --args --runtime "$manifest"
echo "s1: done; reports will appear in $RESULTS"
