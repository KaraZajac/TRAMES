#!/usr/bin/env python3
"""
The ALPR camera map as it stood at any instant, from node_dates.py's version file.

    python3 snapshot_from_versions.py --versions osm-history/node_versions.jsonl \\
        --dates 2024-04-01 2024-10-01 2025-04-01 --out history

Same semantics as history_from_planet.py and Overpass's [date:] — each node's latest version at
or before the instant, kept if visible, still tagged ALPR and inside the study's state boxes —
but read from the ~600,000 versions node_dates.py already extracted rather than from the 162 GB
dump, so a date costs seconds. Output per date: <out>/<date>/tile_planet.json, shaped like an
Overpass response, which build_cones.py (--cache) and trend.py read like any tile.
"""
import argparse
import datetime as dt
import json
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))


def is_alpr(tags):
    return tags.get("man_made") == "surveillance" and (tags.get("surveillance:type") or "").lower() == "alpr"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--versions", required=True)
    ap.add_argument("--dates", nargs="+", required=True, help="YYYY-MM-DD[THH:MM:SSZ], default 00:00:00Z")
    ap.add_argument("--out", required=True)
    ap.add_argument("--boxes", default=os.path.join(HERE, "us_state_boxes.json"))
    args = ap.parse_args()
    boxes = list(json.load(open(args.boxes))["boxes"].values())
    inside = lambda lat, lon: any(s <= lat <= n and w <= lon <= e for s, w, n, e in boxes)

    versions = defaultdict(list)
    with open(args.versions) as fh:
        for line in fh:
            nid, ts, ver, vis, lat, lon, tags = json.loads(line)
            versions[nid].append((ts, ver, vis, lat, lon, tags))
    for vs in versions.values():
        vs.sort(key=lambda x: (x[0], x[1]))

    for d in args.dates:
        stamp = d if "T" in d else d + "T00:00:00Z"
        els = []
        for nid, vs in versions.items():
            cur = None
            for v in vs:
                if v[0] <= stamp:                        # ISO-8601 Z strings compare as instants
                    cur = v
                else:
                    break
            if cur and cur[2] and cur[3] is not None and is_alpr(cur[5]) and inside(cur[3], cur[4]):
                els.append({"type": "node", "id": nid, "lat": round(cur[3], 7), "lon": round(cur[4], 7), "tags": cur[5]})
        els.sort(key=lambda e: e["id"])
        day = d[:10]
        os.makedirs(os.path.join(args.out, day), exist_ok=True)
        path = os.path.join(args.out, day, "tile_planet.json")
        with open(path, "w") as fh:
            json.dump({"generator": "TRAMES snapshot_from_versions.py", "source": os.path.basename(args.versions),
                       "osm3s": {"timestamp_osm_base": stamp,
                                 "copyright": "The data included in this document is from www.openstreetmap.org. "
                                              "The data is made available under ODbL."},
                       "elements": els}, fh)
        print(f"  {day}: {len(els):,} ALPR nodes -> {path}", flush=True)


if __name__ == "__main__":
    main()
