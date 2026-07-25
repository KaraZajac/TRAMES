#!/usr/bin/env bash
# Build continental ALPR cones from live OSM, detached.
#
#   ./fetch-na-cones.sh
#   tail -f logs/na-cones.log
#
# REGIONS, not tiles. Overpass answers the entire continental US — 120k+ cameras,
# ~44 MB — in a single ~2 minute query. An earlier version swept North America as a
# 4-degree grid of 450 tiles; 86% of those requests hit empty ocean and the run was
# on track for ~2.5 hours to return the same data. Big regional boxes that follow
# where land actually is do it in about ten minutes.
#
# Boxes overlap at borders on purpose (e.g. the US/Canada line) so no camera falls
# through a seam; build_cones.py merges by OSM id, so duplicates collapse.
#
# Not using DeFlock's bundled cameras-us.json.gz: that snapshot was committed
# 2026-04-12 and carries 61,768 cameras against 120,757 live — it is missing ~49% of
# them, including real Flock units watching traffic. Their live hourly PMTiles feed
# is Cloudflare-gated (HTTP 403), the same wall OVERWATCH hit on cdn.deflock.me.
# Overpass is the authoritative current source.
set -uo pipefail
cd "$(dirname "$0")"

PY=../.venv/bin/python
OUT="${TRAMES_OUT:-../graphhopper/custom_areas/alpr.geojson}"
CACHE="${TRAMES_CACHE:-region_cache}"

[ -x "$PY" ] || { echo "no venv python at $PY" >&2; exit 1; }

if ./proccheck.sh >/dev/null; then
  echo "a cone build is already running (PID $(./proccheck.sh)) — refusing to start a second" >&2
  exit 1
fi

mkdir -p "$CACHE" logs

setsid nohup "$PY" -u build_cones.py \
  --bbox 24,-125,50,-66     `# continental US`        \
  --bbox 51,-170,72,-129    `# Alaska`                \
  --bbox 18,-161,22,-154    `# Hawaii`                \
  --bbox 42,-141,72,-52     `# Canada`                \
  --bbox 14,-118,33,-86     `# Mexico`                \
  --cache "$CACHE" \
  -o "$OUT" \
  > logs/na-cones.log 2>&1 < /dev/null &

disown 2>/dev/null || true
sleep 4

PID=$(./proccheck.sh)
if [ -z "$PID" ]; then
  echo "FAILED to start:" >&2
  tail -20 logs/na-cones.log >&2
  exit 1
fi
SID=$(ps -o sid= -p "$PID" | tr -d ' ')
echo "cone build running as PID $PID (SID $SID)"
[ "$PID" = "$SID" ] && echo "  session-isolated: yes" || echo "  WARNING: not session-isolated"
echo "  watch: tail -f $(pwd)/logs/na-cones.log"
