#!/usr/bin/env python3
"""
Re-score already-routed commutes against per-vendor camera sets (paper RQ5).

Flock Safety is 82.5% of mapped North American ALPR and sells subscriptions to municipal
police departments and homeowners' associations, so its siting follows local purchasing
decisions. The remaining vendors (Motorola, Genetec, Leonardo, Axis) skew toward
departments of transportation and tolling — highways and toll points. Those are different
deployment logics and should leave different marks on who gets watched: one tracks who
*buys* surveillance, the other tracks road infrastructure.

This reads the route geometry saved by run_experiment.py and recomputes exposure against
each vendor's cones separately. No routing happens: the polylines already exist, so the
whole question costs geometry tests rather than another full run.

    python3 vendor_exposure.py --results out/results.csv --routes out/routes.jsonl.gz \
        --cones flock=../server/graphhopper/custom_areas/alpr_flock.geojson \
        --cones other=../server/graphhopper/custom_areas/alpr_other.geojson \
        -o out/vendor.csv
"""
import argparse
import csv
import json
import os
import sys

from shapely.geometry import LineString, shape
from shapely.strtree import STRtree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from routes_io import read_routes  # noqa: E402


def load_cone_set(path):
    fc = json.load(open(path))
    geom = shape(fc["features"][0]["geometry"])
    parts = list(getattr(geom, "geoms", [geom]))
    props = fc["features"][0].get("properties", {})
    return parts, STRtree(parts), props.get("cameras")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--routes", required=True)
    ap.add_argument("--cones", action="append", required=True, metavar="NAME=PATH",
                    help="repeatable; e.g. flock=.../alpr_flock.geojson")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    sets = {}
    for spec in args.cones:
        name, _, path = spec.partition("=")
        parts, tree, ncam = load_cone_set(path)
        sets[name] = (parts, tree)
        print(f"{name}: {len(parts)} cone parts ({ncam} cameras)", flush=True)

    # Demographics and the all-vendor counts already computed by the experiment.
    base_rows = {}
    with open(args.results, newline="") as fh:
        for r in csv.DictReader(fh):
            base_rows[(r["state"], r["h_tract"], r["w_tract"])] = r

    fieldnames = None
    out = open(args.out, "w", newline="")
    writer = None
    n = matched = 0

    for rec in read_routes(args.routes):
        n += 1
        key = (rec["state"], rec["h_tract"], rec["w_tract"])
        row = base_rows.get(key)
        if row is None:
            # Geometry without a results row should not happen; both are written under
            # the same lock in the same iteration. Counted rather than crashed so a
            # partial file can still be analysed.
            continue
        matched += 1
        rec_out = dict(row)
        for which in ("base", "avoid"):
            line = LineString(rec[which])
            for name, (parts, tree) in sets.items():
                hits = sum(1 for i in tree.query(line) if parts[i].intersects(line))
                rec_out[f"{which}_{name}"] = hits
        if writer is None:
            fieldnames = list(rec_out.keys())
            writer = csv.DictWriter(out, fieldnames=fieldnames)
            writer.writeheader()
        writer.writerow(rec_out)
        if n % 2000 == 0:
            print(f"  {n} routes scored", flush=True)

    out.close()
    print(f"scored {matched}/{n} routes -> {args.out}")
    if matched < n:
        print(f"  {n - matched} geometry records had no matching results row")


if __name__ == "__main__":
    main()
