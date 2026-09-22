#!/usr/bin/env bash
# Re-derive every number in the paper from a completed experiment: the headline analysis,
# the vendor-resolved and cone-radius re-scorings, their reports, and the figures.
#
#   ./scripts/run-analyses.sh            # after scripts/run-full.sh has finished
#
# Everything here is post-processing of out/results.csv and out/routes.jsonl.gz — no
# routing, no server — and it is what makes a data refresh a rerun rather than a
# re-derivation: the July 2026 numbers were produced by these same steps typed by hand,
# in an order that the paper's reproducibility table had to describe in prose.
# ~10-15 min, dominated by the two geometric re-scorings.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=/home/kara/Projects/TRAMES/server/.venv/bin/python
V=../server/alpr/variants                       # from server/alpr/build-variants.sh
A=../server/graphhopper/custom_areas/alpr.geojson

for f in out/results.csv out/routes.jsonl.gz; do
  [ -s "$f" ] || { echo "missing $f — run scripts/run-full.sh first" >&2; exit 1; }
done
for f in "$A" "$V"/alpr_{flock,other,untagged,r30,r45,r90}.geojson; do
  [ -s "$f" ] || { echo "missing $f — run server/alpr/build-variants.sh" >&2; exit 1; }
done

echo "== headline analysis (RQ1-RQ4)"
"$PY" scripts/analyze.py --results out/results.csv -o out/analysis.txt

echo "== vendor-resolved re-scoring (RQ5)"
"$PY" scripts/vendor_exposure.py --results out/results.csv --routes out/routes.jsonl.gz \
    --cones flock="$V/alpr_flock.geojson" --cones other="$V/alpr_other.geojson" \
    --cones untagged="$V/alpr_untagged.geojson" -o out/vendor.csv 2>&1 | tee out/vendor_rescore.log
"$PY" scripts/analyze.py --results out/vendor.csv -o out/analysis_vendor.txt

echo "== cone-radius sensitivity (section 7.3)"
"$PY" scripts/radius_sweep.py --routes out/routes.jsonl.gz \
    --cones 30="$V/alpr_r30.geojson" --cones 45="$V/alpr_r45.geojson" \
    --cones 60="$A" --cones 90="$V/alpr_r90.geojson" \
    -o out/radius_sweep.csv 2>&1 | tee out/radius_sweep.log
"$PY" scripts/analyze_radius.py --sweep out/radius_sweep.csv --results out/results.csv \
    -o out/analysis_radius.txt

echo "== figures"
"$PY" paper/make_figures.py

echo "done: out/analysis.txt out/analysis_vendor.txt out/analysis_radius.txt paper/figures/"
