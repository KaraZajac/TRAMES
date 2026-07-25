#!/usr/bin/env bash
# Launch a long import fully detached from the calling shell/session.
#
#   ./import-detached.sh data/north-america.osm.pbf
#   tail -f logs/import-run.log
#
# Why: a continental import runs for hours. Started as a child of a shell that a
# session manager may reap, it dies with that parent — which is exactly how the
# first North America attempt was lost, mid-way through subnetwork marking, with
# no OOM and no error in the log. setsid puts it in its own session and process
# group so nothing upstream can take it down by association.
set -uo pipefail
cd "$(dirname "$0")"

PBF="${1:-data/north-america.osm.pbf}"
XMX="${TRAMES_XMX:-32g}"

[ -f "$PBF" ] || { echo "no such extract: $PBF" >&2; exit 1; }

if pgrep -f "graphhopper-web-.*\.jar (import|server)" >/dev/null; then
  echo "a GraphHopper import or server is already running — refusing to start" >&2
  exit 1
fi

mkdir -p logs
rm -rf data/graph-cache

echo "launching detached import of $PBF (Xmx=$XMX)"
setsid nohup java "-Xmx$XMX" \
  "-Ddw.graphhopper.datareader.file=$PBF" \
  -jar graphhopper-web-11.0.jar import config/trames.yml \
  > logs/import-run.log 2>&1 < /dev/null &

disown 2>/dev/null || true
sleep 3
PID=$(pgrep -f "graphhopper-web-.*\.jar import" | head -1)
if [ -n "$PID" ]; then
  echo "running as PID $PID (survives this shell)"
  echo "watch: tail -f $(pwd)/logs/import-run.log"
else
  echo "FAILED to start — see logs/import-run.log" >&2
  tail -20 logs/import-run.log >&2
  exit 1
fi
