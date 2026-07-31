#!/usr/bin/env python3
"""Build the compact camera pack the app uses to draw cones offline.

    python3 build_camera_pack.py --cameras ../cameras/cameras.json -o cameras-us.json.gz

The map layer normally fetches cameras from the routing server (or Overpass). Offline
that leaves the map blank over surveilled streets while the offline router is busy
avoiding those very cameras — the map and the route disagree, and the quiet direction
of the disagreement is the dangerous one.

This ships the same snapshot the routing graph and the .obf tags were built from, so an
offline map draws exactly what the offline router avoided. Only what the layer needs is
kept — position and direction — which turns 13.4 MB of Overpass JSON into ~1.1 MB gzipped
for all 120k US cameras, small enough to fetch alongside any map download.
"""
import argparse, gzip, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", default=os.path.join(HERE, "..", "cameras", "cameras.json"))
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    data = json.load(open(args.cameras))
    cams = []
    for el in data.get("elements", ()):
        lat, lon = el.get("lat"), el.get("lon")
        if lat is None or lon is None:
            continue
        tags = el.get("tags") or {}
        # `direction` dominates in the wild (3,864 vs 21 for camera:direction in one metro);
        # kept verbatim so the client reuses TramesCameraSource.parseDirections and both
        # ends agree on arcs, multi-head units and junk values.
        raw = tags.get("direction") or tags.get("camera:direction") or ""
        # 5 decimal places is ~1 m — far finer than a camera position is known, and it
        # halves the file versus full float repr.
        cams.append([round(lat, 5), round(lon, 5), raw])

    if not cams:
        sys.exit("no cameras found")
    payload = {"v": 1, "count": len(cams), "cams": cams}
    blob = json.dumps(payload, separators=(",", ":")).encode()
    with gzip.open(args.out, "wb", compresslevel=9) as f:
        f.write(blob)
    print(f"{len(cams)} cameras -> {args.out} "
          f"({os.path.getsize(args.out)/1048576:.1f} MB gzipped, {len(blob)/1048576:.1f} MB raw)")

if __name__ == "__main__":
    main()
