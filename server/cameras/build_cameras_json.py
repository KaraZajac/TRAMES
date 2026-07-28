#!/usr/bin/env python3
"""Merge the Overpass region_cache tiles into one compact cameras.json.

    python3 build_cameras_json.py [--cache ../alpr/region_cache] [--out cameras.json]

Input:  the raw Overpass responses cached by ../alpr/fetch-na-cones.sh — one JSON
        file per region, overlapping (the continental tile overlaps the southern
        one), each element carrying every OSM tag.
Output: one deduplicated element list holding only the fields the map layer
        actually renders. The client's parser reads exactly five tags —
        direction, camera:direction, operator, brand, manufacturer — so
        everything else is dead weight at serve time. 55 MB of cache becomes a
        ~15 MB file, which is what lets serve_cameras.py hold the whole
        continent in memory on a box with 3 GB free.

The output timestamp is the OLDEST osm3s.timestamp_osm_base across the input
tiles: the merged dataset is only as fresh as its stalest region, and the
serving layer republishes this stamp to the client.
"""

import argparse
import json
import os
import sys

KEPT_TAGS = ("direction", "camera:direction", "operator", "brand", "manufacturer")


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--cache", default=os.path.join(here, "..", "alpr", "region_cache"))
    ap.add_argument("--out", default=os.path.join(here, "cameras.json"))
    args = ap.parse_args()

    tiles = sorted(
        f for f in os.listdir(args.cache) if f.startswith("tile_") and f.endswith(".json")
    )
    if not tiles:
        sys.exit(f"no tile_*.json in {args.cache} — run ../alpr/fetch-na-cones.sh first")

    by_id = {}
    oldest_stamp = None
    for name in tiles:
        with open(os.path.join(args.cache, name)) as f:
            data = json.load(f)
        stamp = (data.get("osm3s") or {}).get("timestamp_osm_base")
        if stamp and (oldest_stamp is None or stamp < oldest_stamp):
            oldest_stamp = stamp
        kept = 0
        for el in data.get("elements", ()):
            if el.get("type") != "node":
                continue
            lat, lon = el.get("lat"), el.get("lon")
            if lat is None or lon is None:
                continue
            tags = el.get("tags") or {}
            slim = {k: tags[k] for k in KEPT_TAGS if tags.get(k)}
            by_id[el["id"]] = {"id": el["id"], "lat": lat, "lon": lon, "tags": slim}
            kept += 1
        print(f"  {name}: {kept} nodes")

    elements = sorted(by_id.values(), key=lambda e: e["id"])
    out = {
        "generator": "TRAMES build_cameras_json.py",
        "osm3s": {
            "timestamp_osm_base": oldest_stamp or "unknown",
            "copyright": (
                "The data included in this document is from www.openstreetmap.org. "
                "The data is made available under ODbL."
            ),
        },
        "elements": elements,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"{len(elements)} unique cameras -> {args.out} "
          f"({os.path.getsize(args.out) / 1e6:.1f} MB, base {oldest_stamp})")


if __name__ == "__main__":
    main()
