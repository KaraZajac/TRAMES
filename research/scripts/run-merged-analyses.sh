#!/usr/bin/env bash
# The merged-map numbers: analyze.py on the merged routing, the paired weighted comparison with
# the OSM-only run, the three-way exposure scoring with the avoiding routes, then the paper's
# figures and tables (which draw the merged map by default).
#
#   ./scripts/run-merged-analyses.sh        # after out/chain_merged.sh has written out/merged/results.csv
set -euo pipefail
cd "$(dirname "$0")/.."
PY=/home/kara/Projects/TRAMES/server/.venv/bin/python
DESIGN=(--frame out/sampling_frame.json --draws out/sample_draws.csv)
[ -s out/merged/results.csv ] || { echo "no out/merged/results.csv" >&2; exit 1; }
n=$(($(wc -l < out/merged/results.csv) - 1)); [ "$n" -eq 56131 ] || { echo "merged results hold $n of 56131 commutes" >&2; exit 1; }
log() { echo "$(date -u +%H:%M:%SZ) $*"; }

log "headline analysis on the merged map"
"$PY" scripts/analyze.py --results out/merged/results.csv "${DESIGN[@]}" --by-state out/merged/by_state.csv -o out/analysis_merged.txt > /dev/null

log "paired comparison, map alone -> merged map, same commutes, weighted"
"$PY" scripts/compare_merged.py --a out/results.csv --b out/merged/results.csv "${DESIGN[@]}" \
    --by-state-a out/by_state.csv --by-state-b out/merged/by_state.csv \
    -o out/merged/compare.csv --report out/analysis_compare_merged.txt > /dev/null

log "exposure three ways, with the avoiding routes"
"$PY" scripts/merged_exposure.py --results out/results.csv --routes out/routes.jsonl.gz \
    --osm-cones out/merged/alpr_osm.geojson \
    --merged-cones ../server/graphhopper/custom_areas_merged/alpr_merged.geojson \
    --discs out/merged/registry_discs.json "${DESIGN[@]}" \
    --merged-results out/merged/results.csv --merged-routes out/merged/routes.jsonl.gz \
    -o out/merged/exposure.csv --report out/analysis_merged_exposure.txt > /dev/null

log "figures (merged map; the map-alone versions as fig_*_osm), then tables"
"$PY" paper/make_figures.py
"$PY" paper/make_figures.py --osm-only
"$PY" paper/make_tables.py
"$PY" paper/make_tables.py --osm-only
log "done: out/analysis_merged.txt out/analysis_compare_merged.txt out/analysis_merged_exposure.txt out/merged/by_state.csv"
