#!/usr/bin/env python3
"""
The merged cone set: OpenStreetMap's wedges, plus a 60 m disc for every in-service plate reader
that Flock's own registry holds and the map does not.

    python3 cones_from_supermap.py --osm-cones out/merged/alpr_osm.geojson \\
        --supermap out/supermap/alpr_supermap.json --area-id alpr_merged \\
        -o ../server/graphhopper/custom_areas_merged/alpr_merged.geojson \\
        --discs out/merged/registry_discs.json

Which registry devices: those with no mapped node within pairing distance (a device with one is
already in the OSM wedges, with that node's bearing), that read plates, are in service, and are
not flagged as a site address (shared_coordinate: scores of devices at one building's
coordinate), as Flock's own factory or validation units, as a probable duplicate, or as retired
by name. None of them has a bearing - the registry's rotationAngle is not one - so each gets
the device map's rule: a disc of the cone's length, 60 m, in every direction.

The discs file lists each disc with the traffic its name says it watches (northbound, ...),
which merged_exposure.py uses for a direction-aware scoring of the same routes.
"""
import argparse
import json
import os
import sys
import time

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "server", "alpr"))
from build_cones import DEFAULT_RADIUS_M, cone_polygon  # noqa: E402

EXCLUDE = {"shared_coordinate", "flock_internal", "possible_duplicate", "name_says_retired", "labelled_test"}


def osm_parts(path):
    data = json.load(open(path))
    feats = data["features"] if data.get("type") == "FeatureCollection" else [data]
    parts = []
    for f in feats:
        g = shape(f["geometry"] if "geometry" in f else f)
        parts.extend(g.geoms if g.geom_type == "MultiPolygon" else [g])
    return parts


def registry_readers(path):
    """Stream the device list; yield the registry-only in-service plate readers to model."""
    s = open(path).read()
    i = s.index('"devices":[') + len('"devices":[')
    dec = json.JSONDecoder()
    n = 0
    while s[i] != "]":
        d, i = dec.raw_decode(s, i)
        if s[i] == ",":
            i += 1
        n += 1
        if d["sources"] != ["flock"] or d.get("reads_plates") is not True or d.get("status") != "in_service":
            continue
        if EXCLUDE & set(d.get("flags") or ()):
            continue
        yield d
    print(f"  read {n:,} devices", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm-cones", required=True, help="build_cones.py output for the OSM nodes (any area id)")
    ap.add_argument("--supermap", required=True)
    ap.add_argument("--area-id", default="alpr_merged")
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--discs", required=True, help="JSON list of the registry discs with their travel-direction hints")
    args = ap.parse_args()

    t0 = time.time()
    parts = osm_parts(args.osm_cones)
    print(f"OSM cone set: {len(parts):,} union parts ({time.time() - t0:.0f}s)", flush=True)

    discs, meta = [], []
    for d in registry_readers(args.supermap):
        discs.append(cone_polygon(d["lat"], d["lon"], 0.0, 360.0, args.radius))
        meta.append({"id": d["id"], "lat": d["lat"], "lon": d["lon"], "state": d["geo"].get("state"),
                     "watches": d.get("watches_travel_direction"), "label": d.get("label")})
    print(f"registry readers given a {args.radius:.0f} m disc: {len(discs):,}, of which "
          f"{sum(1 for m in meta if m['watches']):,} name the traffic they watch ({time.time() - t0:.0f}s)", flush=True)

    merged = unary_union(parts + discs)
    if not merged.is_valid:
        merged = merged.buffer(0)
    n_parts = len(getattr(merged, "geoms", [merged]))
    print(f"union: {n_parts:,} parts, valid={merged.is_valid} ({time.time() - t0:.0f}s)", flush=True)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "id": args.area_id,
        "properties": {"id": args.area_id, "osm_union_parts": len(parts), "registry_discs": len(discs),
                       "radius_m": args.radius},
        "geometry": mapping(merged)}]}
    json.dump(fc, open(args.out, "w"))
    os.makedirs(os.path.dirname(os.path.abspath(args.discs)), exist_ok=True)
    json.dump({"radius_m": args.radius, "discs": meta}, open(args.discs, "w"), separators=(",", ":"))
    print(f"wrote {args.out} ({os.path.getsize(args.out) / 1e6:.0f} MB) and {args.discs}")


if __name__ == "__main__":
    main()
