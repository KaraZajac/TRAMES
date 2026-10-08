#!/usr/bin/env bash
# Route every commute against the merged map: the map's wedges plus a 60 m disc for every
# in-service plate reader in Flock's registry that the map lacks. The paper's current figures.
#
#   ./scripts/run-merged.sh     # after run-full.sh and build_supermap.py; then run-merged-analyses.sh
#
# 1. Rebuilds the map's wedges from the cached Overpass tiles (server/alpr/region_cache) and adds
#    the registry's discs (cones_from_supermap.py), as one area, alpr_merged.
# 2. Imports a graph of its own (config/trames-merged.yml, data/graph-cache-merged; ~2 h) unless
#    one exists, and serves it on :8989 (no other routing server may be running).
# 3. Runs run_history.py against it - resumable; only commutes the merged map exposes are routed -
#    twice, so that requests cut short by an interruption are repeated; stops the server on exit.
#
# A run takes most of a day. Launch it as a user service rather than from a terminal: the kernel
# kills the largest process when memory runs out, and if that process lives in a terminal's
# cgroup, systemd stops the terminal with it.
#
#   systemd-run --user --unit=trames-merged --collect --working-directory="$PWD" -E PATH \
#     -p MemoryMax=48G -p MemorySwapMax=0 -p OOMPolicy=continue \
#     systemd-inhibit --what=sleep:idle --mode=block ./scripts/run-merged.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=/home/kara/Projects/TRAMES/server/.venv/bin/python
GH=../server/graphhopper
AREAS=$GH/custom_areas_merged
mkdir -p out/merged "$AREAS"
if curl -s -o /dev/null http://localhost:8989/info; then
  echo "a routing server is already answering on :8989 — stop it first" >&2; exit 1
fi

if [ ! -s out/merged/alpr_osm.geojson ]; then
  echo "== the map's wedges  $(date -u +%H:%M:%SZ)"
  (cd ../server/alpr && "$PY" build_cones.py --bbox 24,-125,50,-66 --bbox 51,-170,72,-129 \
      --bbox 18,-161,22,-154 --bbox 42,-141,72,-52 --bbox 14,-118,33,-86 --cache region_cache \
      --area-id alpr -o ../../research/out/merged/alpr_osm.geojson)
fi
if [ ! -s "$AREAS/alpr_merged.geojson" ]; then
  echo "== the registry's discs  $(date -u +%H:%M:%SZ)"
  "$PY" -u scripts/cones_from_supermap.py --osm-cones out/merged/alpr_osm.geojson \
      --supermap out/supermap/alpr_supermap.json --area-id alpr_merged \
      -o "$AREAS/alpr_merged.geojson" --discs out/merged/registry_discs.json
fi
# the import indexes every GeoJSON in the directory: only the merged area may be there
[ "$(ls "$AREAS" | wc -l)" -eq 1 ] || { echo "$AREAS must hold only alpr_merged.geojson" >&2; exit 1; }

if [ ! -s "$GH/data/graph-cache-merged/properties" ]; then
  echo "== importing the merged graph (~2 h)  $(date -u +%H:%M:%SZ)"
  rm -rf "$GH/data/graph-cache-merged"
  (cd "$GH" && java -Xmx40g "-Ddw.graphhopper.datareader.file=data/north-america.osm.pbf" \
        -jar graphhopper-web-11.0.jar import config/trames-merged.yml > logs/merged-import.log 2>&1) || true
  grep -q "flushed graph" "$GH/logs/merged-import.log" \
    || { echo "import FAILED — see $GH/logs/merged-import.log" >&2; tail -5 "$GH/logs/merged-import.log" >&2; exit 1; }
fi

echo "== serving  $(date -u +%H:%M:%SZ)"
(cd "$GH" && setsid nohup java -Xmx40g -jar graphhopper-web-11.0.jar server config/trames-merged.yml \
      > logs/merged-server.log 2>&1 < /dev/null &)
trap 'pkill -f "graphhopper-web-11.0.jar server config/trames-merged.yml" || true' EXIT
(cd "$GH" && ./healthcheck.sh 900)

for pass in 1 2; do
  echo "== routing, pass $pass  $(date -u +%H:%M:%SZ)"
  "$PY" -u scripts/run_history.py --area alpr_merged --cones "$AREAS/alpr_merged.geojson" \
      --results out/results.csv --routes out/routes.jsonl.gz --workers 12 \
      -o out/merged/results.csv --geometry out/merged/routes.jsonl.gz
done
echo "== done: $(($(wc -l < out/merged/results.csv) - 1)) commutes  $(date -u +%H:%M:%SZ)"
