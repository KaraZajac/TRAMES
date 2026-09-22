#!/usr/bin/env bash
# Publish built maps and the camera pack to the map host.
#
#   ./deploy.sh                  # everything in maps/ that is newer than what is hosted
#   ./deploy.sh --dry-run
#
# rsync, not scp, for three reasons that all matter with 23 GB of state maps:
#   -t keeps each file's mtime, which the host's trames-manifest.timer turns into the
#      `date` the app shows as the map's freshness (gen-manifest.sh reads stat %y);
#   it writes to a temporary dot-name and renames when complete, and the manifest
#      generator globs *-alpr.obf, so a half-uploaded map is never listed for download;
#   it resumes (--partial) and skips what is already identical, so an interrupted
#      overnight upload costs only what was left.
# No --delete: a state that failed to build keeps its previous map on the host rather
# than vanishing from the catalogue.
set -euo pipefail
cd "$(dirname "$0")"

HOST="${TRAMES_MAP_HOST:-astrophage}"
DEST="${TRAMES_MAP_DEST:-/srv/maps/}"
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

ls maps/*-alpr.obf >/dev/null 2>&1 || { echo "nothing in maps/ — run build-all-detached.sh first" >&2; exit 1; }
[ -s maps/cameras-us.json.gz ] || echo "note: maps/cameras-us.json.gz missing — build_camera_pack.py — deploying maps only" >&2

echo "== $(ls maps/*-alpr.obf | wc -l) maps, $(du -sh maps | cut -f1) -> $HOST:$DEST $DRY"
rsync -avt --partial --progress $DRY --include='*-alpr.obf' --include='cameras-us.json.gz' --exclude='*' \
      maps/ "$HOST:$DEST"
[ -n "$DRY" ] || echo "done — the host's manifest timer lists the new maps within 5 minutes"
