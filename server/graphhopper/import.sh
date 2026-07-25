#!/usr/bin/env bash
# Build the TRAMES routing graph from an OSM extract.
#
#   ./import.sh data/delaware.osm.pbf          # ~10 s
#   ./import.sh data/north-america.osm.pbf     # hours — see README
#
# Adding or removing a profile REQUIRES a fresh import: CH/LM preparations are
# baked into the graph cache, and GraphHopper will refuse to serve a profile it
# has no preparation for.
set -euo pipefail
cd "$(dirname "$0")"

PBF="${1:-data/delaware.osm.pbf}"
# LM preparation is the memory-hungry stage. 8g is plenty for a state; North America
# wants ~32g. Don't set this above roughly (available RAM - 8g): the JVM swapping
# during landmark computation turns an hours-long job into a days-long one.
XMX="${TRAMES_XMX:-8g}"

[ -f "$PBF" ] || { echo "no such extract: $PBF" >&2; exit 1; }

# Refuse to blow away a graph cache that a running server has mapped.
if pgrep -f "graphhopper-web-.*\.jar server" >/dev/null; then
  echo "a GraphHopper server is running — stop it first (it holds data/graph-cache)" >&2
  exit 1
fi

echo "==> importing $PBF (Xmx=$XMX)"
rm -rf data/graph-cache
java "-Xmx$XMX" \
  "-Ddw.graphhopper.datareader.file=$PBF" \
  -jar graphhopper-web-11.0.jar import config/trames.yml

echo "==> done. graph cache:"
du -sh data/graph-cache
