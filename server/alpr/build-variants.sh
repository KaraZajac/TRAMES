#!/usr/bin/env bash
# Build the study's cone-set VARIANTS from the cached Overpass tiles — no refetch.
#
#   ./build-variants.sh               # after fetch-na-cones.sh has populated region_cache/
#
# Writes to variants/ (gitignored), deliberately NOT into ../graphhopper/custom_areas/:
# GraphHopper indexes every geojson in that directory at import, so leaving seven cone
# sets there would bake seven custom areas into the graph and roughly septuple the
# custom-area index the 40 GB serving heap has to hold. Only alpr.geojson belongs in
# custom_areas/. These sets are consumed by research/scripts/vendor_exposure.py and
# radius_sweep.py, which re-score saved route geometry locally and never touch the graph.
#
# Same five regional boxes as fetch-na-cones.sh, so --cache hits every tile and the
# variants are cut from exactly the snapshot the main cones came from.
set -euo pipefail
cd "$(dirname "$0")"

PY=../.venv/bin/python
CACHE="${TRAMES_CACHE:-region_cache}"
OUT="${TRAMES_VARIANTS:-variants}"
BOXES=(--bbox 24,-125,50,-66 --bbox 51,-170,72,-129 --bbox 18,-161,22,-154
       --bbox 42,-141,72,-52 --bbox 14,-118,33,-86)

[ -x "$PY" ] || { echo "no venv python at $PY" >&2; exit 1; }
[ -d "$CACHE" ] || { echo "no $CACHE — run ./fetch-na-cones.sh first" >&2; exit 1; }
mkdir -p "$OUT"

build() {  # name, then extra build_cones.py flags
  local name=$1; shift
  echo "== $name"
  "$PY" build_cones.py "${BOXES[@]}" --cache "$CACHE" --area-id "$name" \
        -o "$OUT/alpr_$name.geojson" "$@" 2>&1 | grep -v RuntimeWarning | grep -E "cameras kept|unioned|wrote|skipped"
}

# Vendor split (paper RQ5). Untagged cameras are their OWN set, held out of both vendor
# sets: an untagged camera is likelier an unlabelled Flock unit than a non-Flock one, so
# --require-vendor keeps "other" from silently absorbing them.
build flock    --vendor flock
build other    --vendor-exclude flock --require-vendor
build untagged --untagged-only

# Cone-radius sensitivity sweep (paper section 7.3); 60 m is the main alpr.geojson.
build r30 --radius 30
build r45 --radius 45
build r90 --radius 90

echo "done -> $OUT/"; ls -la "$OUT"
