#!/usr/bin/env bash
# Route every sampled commute twice (unavoided / avoided) and record ALPR exposure.
#
#   ./scripts/run-full.sh          # ~5.4 h, detached, resumable
#   tail -f out/experiment.log
#
# Detached via setsid: this runs for hours and must not die with the shell that started
# it. An earlier attempt was a child of an interactive session, and when that session
# ended the run died at 10,500/17,904 — and because results were only accumulated in
# memory and written after the final row, all of them were lost. run_experiment.py now
# appends and flushes each record as it completes, and --resume skips whatever is
# already in the output, so a restart costs only the work still outstanding.
#
# 12 workers, not more. Measured on 300 commutes: 12 workers -> 324 s (56/min); 32
# workers took longer than 576 s for the same set. Past ~12, GIL contention in the
# shapely exposure test and load on the routing server outweigh the extra concurrency.
set -uo pipefail
cd "$(dirname "$0")/.."

PY=/home/kara/Projects/TRAMES/server/.venv/bin/python
OUT=out/results.csv
CONES=../server/graphhopper/custom_areas/alpr.geojson

# The routing server must be up; every commute is two HTTP routes against it.
if [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8989/info 2>/dev/null)" != "200" ]; then
  echo "routing server is not answering on :8989 — start it first:" >&2
  echo "  cd ../server/graphhopper && TRAMES_XMX=40g ./serve.sh" >&2
  exit 1
fi

for pid in $(pgrep -f "run_experiment\.py" 2>/dev/null); do
  case "$(cat /proc/$pid/comm 2>/dev/null)" in
    python*) echo "experiment already running (PID $pid)" >&2; exit 1 ;;
  esac
done

mkdir -p out
setsid nohup "$PY" -u scripts/run_experiment.py \
  --commutes out/commutes.csv \
  --cones "$CONES" \
  --workers 12 --resume --shuffle 20260725 \
  --geometry out/routes.jsonl.gz -o "$OUT" \
  >> out/experiment.log 2>&1 < /dev/null &

disown 2>/dev/null || true
sleep 4

PID=""
for p in $(pgrep -f "run_experiment\.py" 2>/dev/null); do
  case "$(cat /proc/$p/comm 2>/dev/null)" in python*) PID=$p; break ;; esac
done
if [ -z "$PID" ]; then
  echo "FAILED to start:" >&2; tail -20 out/experiment.log >&2; exit 1
fi
echo "experiment running as PID $PID (SID $(ps -o sid= -p "$PID" | tr -d ' '))"
echo "  watch: tail -f $(pwd)/out/experiment.log"
