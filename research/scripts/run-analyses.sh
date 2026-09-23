#!/usr/bin/env bash
# Re-derive every number in the paper from a completed experiment: the commuter-weighted
# headline analysis and per-state table, the vendor-resolved and cone-radius re-scorings,
# the 2024-2026 trend, and the figures.
#
#   ./scripts/run-analyses.sh        # after run-full.sh (and, for the trend's avoidance
#                                    # columns, run_history.py for each date)
#
# Everything here is post-processing of out/results.csv and out/routes.jsonl.gz — no
# routing, no server. National figures are weighted by each state's commuters (see
# wstats.py): every state was sampled at the same size, so an unweighted pool would count
# Wyoming as much as California. ~40 min, dominated by the geometric re-scorings.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=/home/kara/Projects/TRAMES/server/.venv/bin/python
V=../server/alpr/variants                       # from server/alpr/build-variants.sh
A=../server/graphhopper/custom_areas/alpr.geojson
H=../server/alpr/history                        # from server/alpr/fetch-history.sh
DESIGN=(--frame out/sampling_frame.json --draws out/sample_draws.csv)

for f in out/results.csv out/routes.jsonl.gz out/sampling_frame.json out/sample_draws.csv; do
  [ -s "$f" ] || { echo "missing $f" >&2; exit 1; }
done
for f in "$A" "$V"/alpr_{flock,other,untagged,r30,r45,r90}.geojson; do
  [ -s "$f" ] || { echo "missing $f — run server/alpr/build-variants.sh" >&2; exit 1; }
done

echo "== headline analysis (commuter-weighted RQ1-RQ4) and the per-state table"
"$PY" scripts/analyze.py --results out/results.csv "${DESIGN[@]}" --by-state out/by_state.csv -o out/analysis.txt > /dev/null

echo "== vendor-resolved re-scoring (RQ5)"
"$PY" scripts/vendor_exposure.py --results out/results.csv --routes out/routes.jsonl.gz \
    --cones flock="$V/alpr_flock.geojson" --cones other="$V/alpr_other.geojson" \
    --cones untagged="$V/alpr_untagged.geojson" -o out/vendor.csv > out/vendor_rescore.log 2>&1
"$PY" scripts/analyze.py --results out/vendor.csv "${DESIGN[@]}" -o out/analysis_vendor.txt > /dev/null

echo "== cone-radius sensitivity"
"$PY" scripts/radius_sweep.py --routes out/routes.jsonl.gz \
    --cones 30="$V/alpr_r30.geojson" --cones 45="$V/alpr_r45.geojson" \
    --cones 60="$A" --cones 90="$V/alpr_r90.geojson" -o out/radius_sweep.csv > out/radius_sweep.log 2>&1
"$PY" scripts/analyze_radius.py --sweep out/radius_sweep.csv --results out/results.csv "${DESIGN[@]}" \
    -o out/analysis_radius.txt > /dev/null

echo "== trend across camera-map dates"
SNAPS=()
for d in "$H"/*/; do
  [ -s "$d/alpr.geojson" ] && SNAPS+=(--snapshot "$(basename "$d")=${d%/}")
done
SNAPS+=(--snapshot 2026-07-24=../server/alpr/region_cache.2026-07-24:../server/graphhopper/custom_areas.2026-07-24/alpr.geojson)
SNAPS+=(--snapshot 2026-09-22=../server/alpr/region_cache:"$A")
"$PY" scripts/trend.py --results out/results.csv --routes out/routes.jsonl.gz "${DESIGN[@]}" "${SNAPS[@]}" \
    -o out/trend.csv --by-state out/trend_by_state.csv --cameras-by-state out/cameras_by_state.csv \
    --report out/analysis_trend.txt > /dev/null

echo "== figures"
"$PY" paper/make_figures.py

echo "done: out/analysis*.txt out/by_state.csv out/trend*.csv paper/figures/"
