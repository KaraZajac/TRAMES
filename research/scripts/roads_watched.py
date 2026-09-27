#!/usr/bin/env python3
"""
Which roads each device watches, from one pass over the road network.

    python3 roads_watched.py --pbf ../server/graphhopper/data/north-america.osm.pbf \\
        --supermap out/supermap/alpr_supermap.json -o out/supermap/roads_watched.json

A device with a surveyed bearing watches the roads its cone crosses: the same 60 m, 45-degree
sector build_cones.py hands the router, one per head. A device with no bearing — every
registry-only device, and the mapped nodes without a direction — gets a circle of the cone's
length, --omni-radius-m (60 m), in every direction: as far as a camera with a bearing is
assumed to see, with no claim about which way. A first pass used a 40 ft circle and left 13%
of the bearing-less plate readers touching no road at all, their nearest counted road a median
50 m away; cameras stand set back from the carriageways they watch, which is what the 60 m
cone length was chosen for in the first place. At 60 m, 0.4% touch none (435 of 98,401). Per device, every drivable way either shape
touches is listed with its class, name, ref and, where tagged, oneway, lanes and maxspeed.

Same streaming pattern as road_sample.py: ways batched, node locations in a file-backed index
on the project disk (~26 GB for North America; /tmp is RAM), the pass run in a capped scope:

    systemd-run --user --scope -p MemoryMax=44G -p MemorySwapMax=0 --quiet nice -n 10 \\
        python3 roads_watched.py ...

The cap must hold the index's page cache or the pass thrashes; that cache is reclaimable, so a
high cap is OOM-safe. About an hour for North America.
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np
import osmium
import shapely
from osmium.filter import EntityFilter, KeyFilter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "server", "alpr"))
from build_cones import DEFAULT_RADIUS_M, cone_polygon  # noqa: E402

ROADS = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
         "living_street", "service", "motorway_link", "trunk_link", "primary_link", "secondary_link",
         "tertiary_link", "road", "busway"}
WAY_TAGS = ("name", "ref", "oneway", "lanes", "maxspeed")


def disc(lat, lon, r_m, n=24):
    """A circle of r_m metres about the device, in the equirectangular frame cone_polygon uses."""
    m_lat = 111320.0
    m_lon = max(111320.0 * math.cos(math.radians(lat)), 1.0)
    th = np.linspace(0, 2 * math.pi, n, endpoint=False)
    return shapely.Polygon(np.column_stack([lon + r_m * np.sin(th) / m_lon, lat + r_m * np.cos(th) / m_lat]))


def device_shapes(devices, omni_m):
    shapes, kind = [], []
    for d in devices:
        b, s = d.get("bearings"), d.get("cone_spans_deg")
        if b:
            parts = [disc(d["lat"], d["lon"], DEFAULT_RADIUS_M) if sp >= 360 else
                     cone_polygon(d["lat"], d["lon"], br, sp, DEFAULT_RADIUS_M) for br, sp in zip(b, s)]
            g = shapely.union_all(parts) if len(parts) > 1 else parts[0]
            kind.append("cone")
        else:
            g = disc(d["lat"], d["lon"], omni_m)
            kind.append("omni")
        shapes.append(g)
    return shapes, kind


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--supermap", required=True)
    ap.add_argument("--omni-radius-m", type=float, default=DEFAULT_RADIUS_M,
                    help="circle radius for a device with no bearing (default: the cone length)")
    ap.add_argument("--omni-radius-ft", type=float, default=None, help="the same, in feet (overrides -m)")
    ap.add_argument("--batch", type=int, default=200_000)
    ap.add_argument("--index", default=None, help="file-backed node-location index (default beside -o; deleted after)")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()
    omni_m = args.omni_radius_ft * 0.3048 if args.omni_radius_ft is not None else args.omni_radius_m
    index = args.index or os.path.join(os.path.dirname(os.path.abspath(args.out)), "node_locations.idx")
    if os.path.exists(index):
        os.remove(index)

    t0 = time.time()
    devices = json.load(open(args.supermap))["devices"]
    dev = [{"id": d["id"], "lat": d["lat"], "lon": d["lon"], "bearings": d.get("bearings"),
            "cone_spans_deg": d.get("cone_spans_deg")} for d in devices]
    del devices
    shapes, kind = device_shapes(dev, omni_m)
    tree = shapely.STRtree(shapes)
    print(f"{len(dev):,} devices: {kind.count('cone'):,} with cones, {kind.count('omni'):,} with a "
          f"{omni_m:.0f} m circle ({time.time() - t0:.0f}s)", flush=True)

    hits = [dict() for _ in dev]                    # device -> way id -> tags
    stats = {"ways": 0, "batches": 0, "pairs": 0}

    def flush(batch):
        coords = [c for c, _, _ in batch]
        lens = np.fromiter((len(c) for c in coords), np.int64, len(coords))
        V = np.concatenate(coords)
        idx = np.repeat(np.arange(len(batch)), lens)
        lines = shapely.linestrings(V, indices=idx)
        q = tree.query(lines, predicate="intersects")
        for li, di in zip(q[0], q[1]):
            _, wid, tags = batch[li]
            hits[di][wid] = tags
        stats["pairs"] += q.shape[1]
        stats["batches"] += 1

    batch = []
    fp = (osmium.FileProcessor(args.pbf, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations(f"sparse_file_array,{index}")
          .with_filter(EntityFilter(osmium.osm.WAY))
          .with_filter(KeyFilter("highway")))
    for w in fp:
        hw = w.tags.get("highway")
        if hw not in ROADS or len(w.nodes) < 2:
            continue
        try:
            xy = np.array([(n.lon, n.lat) for n in w.nodes])
        except osmium.InvalidLocationError:
            continue
        tags = {"highway": hw}
        for k in WAY_TAGS:
            v = w.tags.get(k)
            if v:
                tags[k] = v
        batch.append((xy, w.id, tags))
        stats["ways"] += 1
        if len(batch) >= args.batch:
            flush(batch); batch = []
            if stats["batches"] % 25 == 0:
                print(f"  {stats['ways']:,} ways, {stats['pairs']:,} device-road pairs, "
                      f"{sum(1 for h in hits if h):,} devices touched  ({time.time() - t0:.0f}s)", flush=True)
    if batch:
        flush(batch)
    del fp
    if os.path.exists(index):
        os.remove(index)

    out = {}
    for d, h, k in zip(dev, hits, kind):
        if h:
            out[d["id"]] = [{"way": wid, **tags} for wid, tags in sorted(h.items())]
    n_cone = sum(1 for h, k in zip(hits, kind) if k == "cone" and h)
    n_omni = sum(1 for h, k in zip(hits, kind) if k == "omni" and h)
    with open(args.out, "w") as fh:
        json.dump({"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "pbf": os.path.basename(args.pbf),
                   "method": {"cone": f"{DEFAULT_RADIUS_M:.0f} m sector per surveyed head, as the router's",
                              "omni": f"{omni_m:.0f} m circle where no bearing is known: the cone's length, in every direction"},
                   "devices": out}, fh, separators=(",", ":"))
    print(f"{stats['ways']:,} drivable ways; devices touching a road: {n_cone:,} of {kind.count('cone'):,} with a cone, "
          f"{n_omni:,} of {kind.count('omni'):,} with a circle; {len(out):,} written -> {args.out} "
          f"({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
