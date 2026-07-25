#!/usr/bin/env bash
# Wait for the ALPR cone build to finish, verify its output, then re-import the North
# America graph with the cones baked in and bring the server back up.
#
#   ./rebuild-with-cones.sh            # run detached via the launcher below
#
# The verification gate matters: the import costs ~82 minutes, so an empty or truncated
# alpr.geojson must fail here rather than after an hour and a half of CPU. It is also
# the difference between "no cameras nearby" and "the data never loaded" — which look
# identical from a routing response and would make TRAMES confidently route you past
# cameras it simply doesn't know about.
set -uo pipefail
cd "$(dirname "$0")"

ALPR_DIR=../alpr
GEOJSON=custom_areas/alpr.geojson
PBF=data/north-america.osm.pbf
MIN_CAMERAS=1000          # continental scale; Delaware alone had 529

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

log "waiting for cone build to finish..."
while "$ALPR_DIR/proccheck.sh" >/dev/null 2>&1; do
  sleep 60
done
log "cone build no longer running"

# --- verification gate -------------------------------------------------------------
if ! grep -q "^wrote " "$ALPR_DIR/logs/na-cones.log" 2>/dev/null; then
  log "ABORT: cone build exited without writing output. Last lines:"
  tail -15 "$ALPR_DIR/logs/na-cones.log" 2>/dev/null
  exit 1
fi

CAMERAS=$(python3 - "$GEOJSON" <<'PY'
import json, sys
try:
    fc = json.load(open(sys.argv[1]))
    f = fc["features"][0]
    assert f.get("id") == "alpr", "feature id is not 'alpr'"
    g = f["geometry"]
    assert g["type"] in ("Polygon", "MultiPolygon"), g["type"]
    parts = len(g["coordinates"]) if g["type"] == "MultiPolygon" else 1
    print(f["properties"].get("cameras", 0))
except Exception as e:
    print("ERR:%s" % e)
PY
)
case "$CAMERAS" in
  ERR:*) log "ABORT: $GEOJSON failed validation — $CAMERAS"; exit 1 ;;
esac
if [ "$CAMERAS" -lt "$MIN_CAMERAS" ] 2>/dev/null; then
  log "ABORT: only $CAMERAS cameras in $GEOJSON (expected >= $MIN_CAMERAS)."
  log "       Refusing to spend ~82 min importing what looks like a partial fetch."
  exit 1
fi
if grep -q "coverage is INCOMPLETE" "$ALPR_DIR/logs/na-cones.log" 2>/dev/null; then
  log "NOTE: cone build reported INCOMPLETE coverage in some tiles — proceeding, but"
  log "      routing will be blind to cameras in those areas. See na-cones.log."
fi
log "verified: $CAMERAS cameras, $(du -h "$GEOJSON" | cut -f1) geojson"

# --- re-import ---------------------------------------------------------------------
log "stopping server..."
pkill -f "graphhopper-web-.*\.jar server" 2>/dev/null
sleep 5

log "starting import (this takes ~82 min)..."
rm -rf data/graph-cache
java -Xmx32g "-Ddw.graphhopper.datareader.file=$PBF" \
  -jar graphhopper-web-11.0.jar import config/trames.yml \
  > logs/na-import-cones.log 2>&1
RC=$?
if [ $RC -ne 0 ] || ! grep -q "flushed graph" logs/na-import-cones.log; then
  log "IMPORT FAILED (rc=$RC). Last lines:"
  tail -15 logs/na-import-cones.log
  exit 1
fi
log "import complete: $(du -sh data/graph-cache | cut -f1) graph cache"

# --- serve -------------------------------------------------------------------------
log "starting server..."
# 40g, NOT 16g. Serving the continental graph WITH cones needs the 14 GB RAM_STORE
# graph plus the 114k-part custom-area index plus per-request working set. At 16g the
# server starts fine, answers a couple of requests, then dies with
# java.lang.OutOfMemoryError: Java heap space — which surfaces as HTTP 500 per request,
# not as a startup failure, so it looks like a routing bug rather than a memory limit.
setsid nohup java -Xmx40g -jar graphhopper-web-11.0.jar server config/trames.yml \
  > logs/server-run.log 2>&1 < /dev/null &
sleep 5
if ./healthcheck.sh 900; then
  log "SERVER UP with continental ALPR cones"
else
  log "server failed to become ready"
  exit 1
fi
