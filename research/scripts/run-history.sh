#!/usr/bin/env bash
# Route every commute against each past camera map — the avoidance half of the trend.
#
#   ./scripts/run-history.sh        # after run-full.sh and server/alpr/fetch-history.sh
#
# 1. Imports ONE routing graph (server/graphhopper/config/trames-history.yml) carrying every
#    fetched date's cones side by side, each as its own area alpr_YYYY_MM_DD — one ~2 h
#    import instead of one per date. Skipped when the imported areas already match.
# 2. Serves it on :8989 (the current-snapshot server must be stopped: two 40 GB servers do
#    not fit in 62 GB).
# 3. Runs run_history.py for each date — resumable; only commutes exposed on that date are
#    routed — and stops the server on exit.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=/home/kara/Projects/TRAMES/server/.venv/bin/python
GH=../server/graphhopper
H=../server/alpr/history

DATES=()
for d in "$H"/*/; do [ -s "$d/alpr.geojson" ] && DATES+=("$(basename "$d")"); done
[ ${#DATES[@]} -gt 0 ] || { echo "no historical camera maps — run server/alpr/fetch-history.sh" >&2; exit 1; }
if curl -s -o /dev/null http://localhost:8989/info; then
  echo "a routing server is already answering on :8989 — stop it first" >&2; exit 1
fi

mkdir -p "$GH/custom_areas_history"
changed=0
for d in "${DATES[@]}"; do
  dst="$GH/custom_areas_history/alpr_${d//-/_}.geojson"
  cmp -s "$H/$d/alpr.geojson" "$dst" 2>/dev/null || { cp "$H/$d/alpr.geojson" "$dst"; changed=1; }
done
if [ "$changed" = 1 ] || [ ! -s "$GH/data/graph-cache-history/properties" ]; then
  echo "== importing the history graph with ${#DATES[@]} dated areas (~2 h)  $(date -u +%H:%M:%SZ)"
  rm -rf "$GH/data/graph-cache-history"
  (cd "$GH" && java -Xmx32g "-Ddw.graphhopper.datareader.file=data/north-america.osm.pbf" \
        -jar graphhopper-web-11.0.jar import config/trames-history.yml > logs/history-import.log 2>&1) || true
  grep -q "flushed graph" "$GH/logs/history-import.log" \
    || { echo "import FAILED — see $GH/logs/history-import.log" >&2; tail -5 "$GH/logs/history-import.log" >&2; exit 1; }
  grep -h "areas available" "$GH/logs/history-import.log" | tail -1
fi

echo "== serving  $(date -u +%H:%M:%SZ)"
(cd "$GH" && setsid nohup java -Xmx40g -jar graphhopper-web-11.0.jar server config/trames-history.yml \
      > logs/history-server.log 2>&1 < /dev/null &)
trap 'pkill -f "graphhopper-web-11.0.jar server config/trames-history.yml" || true' EXIT
(cd "$GH" && ./healthcheck.sh 900)

for d in "${DATES[@]}"; do
  echo "== $d  $(date -u +%H:%M:%SZ)"
  "$PY" -u scripts/run_history.py --area "alpr_${d//-/_}" --cones "$H/$d/alpr.geojson" \
      --results out/results.csv --routes out/routes.jsonl.gz --workers 12 \
      -o "out/history/$d/results.csv" --geometry "out/history/$d/routes.jsonl.gz"
done
echo "== done  $(date -u +%H:%M:%SZ)"
