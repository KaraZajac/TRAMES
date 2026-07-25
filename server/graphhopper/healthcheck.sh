#!/usr/bin/env bash
# Wait for the TRAMES routing server to become ready, then report what it's serving.
#
#   ./healthcheck.sh          # wait up to 120 s
#   ./healthcheck.sh 600      # wait up to 600 s (big graphs load slowly)
#
# Probes /info, NOT /route. A /route probe needs coordinates that snap to a real road,
# and a point that lands in water returns PointNotFoundException forever — which makes
# an unbounded "wait until 200" loop spin indefinitely instead of failing. /info is
# coordinate-free and returns 200 exactly when the graph is loaded and serving.
set -uo pipefail
cd "$(dirname "$0")"

DEADLINE="${1:-120}"
URL="http://localhost:8989/info"

echo "waiting up to ${DEADLINE}s for $URL ..."
ELAPSED=0
while [ "$ELAPSED" -lt "$DEADLINE" ]; do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null)" = "200" ]; then
    curl -s "$URL" | python3 -c '
import json,sys
d=json.load(sys.stdin)
bb=[round(x,3) for x in d.get("bbox",[])]
print("READY  graphhopper %s" % d.get("version"))
print("       profiles: %s" % ", ".join(p["name"] for p in d.get("profiles",[])))
print("       bbox:     %s" % bb)
'
    exit 0
  fi
  sleep 2
  ELAPSED=$((ELAPSED+2))
done

echo "NOT READY after ${DEADLINE}s" >&2
echo "last 5 log lines:" >&2
tail -5 logs/graphhopper.log 2>/dev/null >&2
exit 1
