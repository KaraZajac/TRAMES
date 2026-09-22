#!/usr/bin/env bash
# Build every US state's ALPR-tagged .obf, detached, on this machine.
#
#   ./build-all-detached.sh              # all 51 regions, resumable
#   ./build-all-detached.sh delaware,rhode-island
#   tail -f .work/build-all.log
#
# Refuses to start while the GraphHopper server or the commute experiment is running:
# California and Texas each want a ~48 GB heap and the cone-graph server holds 40 GB,
# and the two together do not fit in this 62 GB box — the JVM does not fail cleanly when
# they collide, it swaps, and an hours-long build becomes a days-long one.
#
# JDK 21 explicitly: OsmAndMapCreator is validated on 21, and the system java is 25.
set -uo pipefail
cd "$(dirname "$0")"

STATES="${1:-all}"
MC="$(realpath "${TRAMES_MAPCREATOR:-.work/OsmAndMapCreator}")"
export JAVA_HOME="${JAVA_HOME:-$HOME/.local/jdk-21}"
export PATH="$JAVA_HOME/bin:$PATH"
export TRAMES_JAVA_OPTS="${TRAMES_JAVA_OPTS:--Xms2G -Xmx48G}"

[ -x "$JAVA_HOME/bin/java" ] || { echo "no JDK at $JAVA_HOME" >&2; exit 1; }
[ -f "$MC/utilities.sh" ] || { echo "no OsmAndMapCreator at $MC — unzip the nightly there" >&2; exit 1; }
python3 -c "import osmium, shapely" 2>/dev/null || { echo "python3 needs pyosmium + shapely" >&2; exit 1; }

if curl -s -o /dev/null http://localhost:8989/info 2>/dev/null; then
  echo "GraphHopper is serving on :8989 — stop it first (pkill -f 'graphhopper-web-.*jar server')" >&2; exit 1
fi
for pid in $(pgrep -f "run_experiment\.py|build_maps\.py" 2>/dev/null); do
  case "$(cat /proc/$pid/comm 2>/dev/null)" in
    python*) echo "PID $pid ($(tr -d '\0' < /proc/$pid/cmdline | cut -c1-60)) is running — refusing" >&2; exit 1 ;;
  esac
done

mkdir -p .work maps
echo "building states=$STATES with JAVA_OPTS='$TRAMES_JAVA_OPTS' (java $("$JAVA_HOME/bin/java" -version 2>&1 | head -1 | cut -d'"' -f2))"
setsid nohup python3 -u build_maps.py --mapcreator "$MC" --states "$STATES" \
  --out maps --work .work >> .work/build-all.log 2>&1 < /dev/null &
disown 2>/dev/null || true
sleep 3
for pid in $(pgrep -f "build_maps\.py" 2>/dev/null); do
  case "$(cat /proc/$pid/comm 2>/dev/null)" in python*) echo "running as PID $pid — tail -f $(pwd)/.work/build-all.log"; exit 0 ;; esac
done
echo "FAILED to start:" >&2; tail -10 .work/build-all.log >&2; exit 1
