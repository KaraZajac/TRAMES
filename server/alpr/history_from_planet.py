#!/usr/bin/env python3
"""
Rebuild the ALPR camera map as it stood on past dates from OpenStreetMap's full-history dump.

    python3 history_from_planet.py --history osm-history/history-latest.osm.pbf \\
        --dates 2024-01-01 2024-07-01 2025-01-01 2026-01-01 --out history

The same question fetch-history.sh asks Overpass — which nodes were mapped as ALPRs on date
D, with the tags they had that day — answered locally. Overpass's history queries are heavy,
and after a night of them overpass-api.de began refusing this machine's connections; the
dump (planet.openstreetmap.org full history, ~163 GB) puts no load on any shared service.

Two passes over the dump:
  1. every node version tagged man_made=surveillance + surveillance:type=ALPR (any case)
     gives the ids of every node that was EVER an ALPR;
  2. every version of those ids, including deletions and retaggings — which carry no ALPR
     tags and so escape pass 1 — so that the map on date D is each node's latest version at
     or before D, kept only if that version is visible and still tagged ALPR.
Those are Overpass's [date:] semantics; a date both sources cover is compared node for node.

Only nodes inside the study's state boxes (us_state_boxes.json) are written, as with
fetch-history.sh. Output per date: <out>/<date>/tile_planet.json, shaped like an Overpass
response (osm3s timestamp = the date), which build_cones.py and trend.py read like any tile.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import defaultdict

import osmium
from osmium.filter import IdFilter, TagFilter

HERE = os.path.dirname(os.path.abspath(__file__))


def is_alpr(tags):
    return tags.get("man_made") == "surveillance" and (tags.get("surveillance:type") or "").lower() == "alpr"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", required=True, help="an OSM full-history file (.osh.pbf / .osh / .osm.pbf)")
    ap.add_argument("--dates", nargs="+", required=True, help="YYYY-MM-DD, each read at 00:00:00Z")
    ap.add_argument("--out", required=True, help="directory to hold <date>/tile_planet.json")
    ap.add_argument("--boxes", default=os.path.join(HERE, "us_state_boxes.json"))
    args = ap.parse_args()

    boxes = list(json.load(open(args.boxes))["boxes"].values())
    inside = lambda lat, lon: any(s <= lat <= n and w <= lon <= e for s, w, n, e in boxes)

    t0 = time.time()
    ids = set()
    for n in osmium.FileProcessor(args.history, osmium.osm.NODE).with_filter(TagFilter(("man_made", "surveillance"))):
        if (n.tags.get("surveillance:type") or "").lower() == "alpr":
            ids.add(n.id)
    print(f"pass 1: {len(ids):,} nodes were ALPRs at some point ({time.time()-t0:.0f}s)", flush=True)

    t1 = time.time()
    versions = defaultdict(list)
    for n in osmium.FileProcessor(args.history, osmium.osm.NODE).with_filter(IdFilter(ids)):
        loc = n.location
        versions[n.id].append((n.timestamp, n.version, bool(n.visible),
                               loc.lat if loc.valid() else None, loc.lon if loc.valid() else None,
                               {t.k: t.v for t in n.tags}))
    nv = sum(len(v) for v in versions.values())
    print(f"pass 2: {nv:,} versions of those nodes ({time.time()-t1:.0f}s)", flush=True)
    for v in versions.values():
        v.sort(key=lambda x: (x[0], x[1]))

    for d in args.dates:
        at = dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc)
        els = []
        for nid, vs in versions.items():
            cur = None
            for v in vs:
                if v[0] <= at:
                    cur = v
                else:
                    break
            if cur and cur[2] and cur[3] is not None and is_alpr(cur[5]) and inside(cur[3], cur[4]):
                els.append({"type": "node", "id": nid, "lat": round(cur[3], 7), "lon": round(cur[4], 7), "tags": cur[5]})
        els.sort(key=lambda e: e["id"])
        os.makedirs(os.path.join(args.out, d), exist_ok=True)
        path = os.path.join(args.out, d, "tile_planet.json")
        with open(path, "w") as fh:
            json.dump({"generator": "TRAMES history_from_planet.py", "source": os.path.basename(args.history),
                       "osm3s": {"timestamp_osm_base": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "copyright": "The data included in this document is from www.openstreetmap.org. "
                                              "The data is made available under ODbL."},
                       "elements": els}, fh)
        print(f"  {d}: {len(els):,} ALPR nodes -> {path}", flush=True)


if __name__ == "__main__":
    main()
