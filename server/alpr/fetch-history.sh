#!/usr/bin/env bash
# Rebuild the ALPR camera set as it was mapped on past dates, one state box at a time.
#
#   ./fetch-history.sh 2025-07-01 2026-01-01 ...    # -> history/<date>/{tile_*.json, alpr.geojson}
#
# Each date's cones get the area id alpr_YYYY_MM_DD, so several dates can be imported into
# one routing graph side by side and chosen per request (in_alpr_2025_07_01). Resumable:
# fetched state tiles are cached, so a rerun only asks for what is missing. Only
# overpass-api.de keeps the history this needs; expect about an hour per date.
set -uo pipefail
cd "$(dirname "$0")"
PY=../.venv/bin/python
BOXES=$(python3 -c 'import json; print(" ".join("--bbox " + ",".join(map(str, b)) for b in json.load(open("us_state_boxes.json"))["boxes"].values()))')
for D in "$@"; do
  mkdir -p "history/$D"
  echo "== $D  ($(date -u +%H:%M:%SZ))"
  "$PY" build_cones.py --date "${D}T00:00:00Z" $BOXES --cache "history/$D" \
        --area-id "alpr_${D//-/_}" -o "history/$D/alpr.geojson" 2>&1 | grep -v RuntimeWarning
done
