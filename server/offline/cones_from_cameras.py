#!/usr/bin/env python3
"""Build alpr.geojson cones from a local cameras.json, no Overpass round-trip.

    python3 cones_from_cameras.py --cameras ../cameras/cameras.json \
            --bbox 38.40,-75.80,39.85,-75.03 -o delaware-alpr.geojson

build_cones.py fetches cameras live from Overpass to feed the online GraphHopper graph.
For the offline map pipeline we already have the exact camera snapshot the app serves
(server/cameras/cameras.json, 120,838 nodes), so this builds the same 60 m / 45° cones
straight from it — deterministic, offline, and identical geometry because it imports
build_cones.py's own `parse_directions` / `cone_polygon`.

An optional --bbox clips to one region (a state for a metro build); omit it for the
whole file (the entire US).
"""

import argparse
import json
import os
import sys

# Reuse the online pipeline's cone geometry verbatim — same DEFAULT_RADIUS_M (60) /
# DEFAULT_SPAN_DEG (45) as TramesGeometry, same multi-head/arc direction parsing.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "alpr"))
from build_cones import (  # noqa: E402
    parse_directions, cone_polygon, DIRECTION_KEYS, DEFAULT_RADIUS_M,
)
from shapely.geometry import mapping  # noqa: E402
from shapely.ops import unary_union   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--cameras", default=os.path.join(here, "..", "cameras", "cameras.json"))
    ap.add_argument("--bbox", help="south,west,north,east to clip to (optional)")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    box = None
    if args.bbox:
        s, w, n, e = (float(x) for x in args.bbox.split(","))
        box = (s, w, n, e)

    data = json.load(open(args.cameras))
    polys = []
    cams = kept = 0
    for el in data.get("elements", ()):
        lat, lon = el.get("lat"), el.get("lon")
        if lat is None or lon is None:
            continue
        if box and not (box[0] <= lat <= box[2] and box[1] <= lon <= box[3]):
            continue
        cams += 1
        tags = el.get("tags") or {}
        raw = next((tags[k] for k in DIRECTION_KEYS if tags.get(k)), None)
        dirs = parse_directions(raw) if raw else []
        if not dirs:
            # No direction: a single all-round cone would overstate it, and the online
            # pipeline drops these too. Skip — an untagged unit contributes no cone.
            continue
        for bearing, span in dirs:
            polys.append(cone_polygon(lat, lon, bearing, span, DEFAULT_RADIUS_M))
        kept += 1

    if not polys:
        sys.exit("no cones built (no directional cameras in range)")

    merged = unary_union(polys)
    fc = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"id": "alpr"},
            "geometry": mapping(merged),
        }],
    }
    json.dump(fc, open(args.out, "w"))
    parts = len(getattr(merged, "geoms", [merged]))
    print(f"{cams} cameras in range, {kept} directional -> {len(polys)} cones "
          f"-> {parts} union parts -> {args.out}")


if __name__ == "__main__":
    main()
