#!/usr/bin/env bash
# Serve the previously-imported TRAMES routing graph on localhost:8989.
#
# Binds to localhost only — GraphHopper has no auth and no rate limiting, so it
# must never face the internet directly. Caddy on api.blackflagintel.com fronts it
# when deployed.
set -euo pipefail
cd "$(dirname "$0")"

# 8g suits a state-sized graph. The continental graph with ALPR cones needs ~40g:
# 14 GB graph (RAM_STORE) + the 114k-part custom-area index + per-request working set.
# Too small and the server starts cleanly then returns HTTP 500 on every route once the
# heap is exhausted — see rebuild-with-cones.sh.
XMX="${TRAMES_XMX:-8g}"

[ -d data/graph-cache ] || { echo "no graph-cache — run ./import.sh first" >&2; exit 1; }

mkdir -p logs
exec java "-Xmx$XMX" -jar graphhopper-web-11.0.jar server config/trames.yml
