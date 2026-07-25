#!/usr/bin/env python3
"""
Sensitivity of the study's results to the assumed camera cone radius (paper section 7.3).

The 60 m radius was chosen by reasoning — a pole set back from the kerb needs a cone long
enough to reach across the carriageway it watches — and never swept. Exposure counts
obviously scale with it. The question is whether the paper's conclusions do.

Two different questions are answered here, and they are not the same:

  1. EXPOSURE SENSITIVITY. Re-score the unavoided routes at each radius. This says how
     much the measured exposure depends on the assumption.

  2. AVOIDANCE ROBUSTNESS. The avoided routes were planned against 60 m cones baked into
     the routing graph. Re-scoring *those* routes at a larger radius asks a sharper
     question: if the true field of view is wider than we assumed, does a route planned
     on the assumption still evade? A result that holds only at exactly the radius it was
     optimised for would be an artefact, not a finding.

Only exposure is recomputed. Re-planning at another radius would need a fresh graph import
(~82 min) plus a full re-route (~7 h) per radius, so what this cannot answer is whether a
router given 90 m cones would find *different* clean routes. It very likely would, and in
that sense the robustness figures below are a lower bound on achievable avoidance.

    python3 radius_sweep.py --routes out/routes.jsonl.gz \
        --cones 30=../server/.../alpr_r30.geojson 60=../server/.../alpr.geojson \
        -o out/radius_sweep.csv
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


def score_one_radius(routes_path, cone_path):
    """{(state,h,w): (base_hits, avoid_hits)} for a single cone set."""
    fc = json.load(open(cone_path))
    geom = shape(fc["features"][0]["geometry"])
    parts = list(getattr(geom, "geoms", [geom]))
    tree = STRtree(parts)
    ncam = fc["features"][0].get("properties", {}).get("cameras")
    print(f"    {len(parts)} cone parts ({ncam} cameras)", flush=True)

    out = {}
    for n, rec in enumerate(read_routes(routes_path), 1):
        key = (rec["state"], rec["h_tract"], rec["w_tract"])
        hits = []
        for which in ("base", "avoid"):
            line = LineString(rec[which])
            hits.append(sum(1 for i in tree.query(line) if parts[i].intersects(line)))
        out[key] = tuple(hits)
        if n % 5000 == 0:
            print(f"    {n} routes", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--routes", required=True)
    ap.add_argument("--cones", action="append", required=True, metavar="RADIUS=PATH")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    specs = []
    for spec in args.cones:
        r, _, path = spec.partition("=")
        specs.append((int(r), path))
    specs.sort()

    # One cone set resident at a time. Holding four simultaneously is several GB of
    # shapely geometry for no benefit; re-reading the 75 MB sidecar per radius is cheap.
    scored = {}
    for r, path in specs:
        print(f"  radius {r} m", flush=True)
        scored[r] = score_one_radius(args.routes, path)

    keys = set.intersection(*(set(v) for v in scored.values()))
    print(f"  {len(keys)} routes scored at every radius", flush=True)

    radii = [r for r, _ in specs]
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["state", "h_tract", "w_tract"]
                   + [f"base_r{r}" for r in radii]
                   + [f"avoid_r{r}" for r in radii])
        for k in sorted(keys):
            w.writerow(list(k)
                       + [scored[r][k][0] for r in radii]
                       + [scored[r][k][1] for r in radii])
    print(f"wrote {len(keys)} rows -> {args.out}")


if __name__ == "__main__":
    main()
