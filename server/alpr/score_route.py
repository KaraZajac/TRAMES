#!/usr/bin/env python3
"""
Score a route by ALPR exposure: how many camera cones does this path actually cross?

Distance and time alone can't tell you whether an avoidance route *worked* — a longer
route is not necessarily a less-surveilled one. This is the metric the product is
actually selling ("avoided N cameras"), and the one to tune the berth slider against.

A camera counts as passed if the route's line geometry intersects its cone. That is
deliberately the same geometric test the router applied, so the score and the routing
agree with each other.

Usage:
    python3 score_route.py --url http://localhost:8999/route \
        --from 39.7391,-75.5398 --to 38.9108,-75.4277 \
        --cones ../graphhopper/custom_areas/alpr.geojson \
        --sweep 1.0,0.5,0.1,0.05,0.01
"""
import argparse
import json
import urllib.request

from shapely.geometry import LineString, shape


def route(url, frm, to, multiply=None):
    body = {
        "points": [[frm[1], frm[0]], [to[1], to[0]]],
        "profile": "car",
        "ch.disable": True,
        "points_encoded": False,
    }
    if multiply is not None:
        body["custom_model"] = {"priority": [{"if": "in_alpr", "multiply_by": str(multiply)}]}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    if "paths" not in d:
        raise RuntimeError(d.get("message", "no path"))
    p = d["paths"][0]
    return p, LineString(p["points"]["coordinates"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8999/route")
    ap.add_argument("--from", dest="frm", required=True, help="lat,lon")
    ap.add_argument("--to", required=True, help="lat,lon")
    ap.add_argument("--cones", required=True, help="alpr.geojson from build_cones.py")
    ap.add_argument("--sweep", default="1.0,0.5,0.2,0.1,0.05,0.01")
    args = ap.parse_args()

    frm = tuple(float(x) for x in args.frm.split(","))
    to = tuple(float(x) for x in args.to.split(","))

    fc = json.load(open(args.cones))
    geom = shape(fc["features"][0]["geometry"])
    cones = list(getattr(geom, "geoms", [geom]))
    print(f"loaded {len(cones)} cone parts from {args.cones}\n")

    base_p, base_line = route(args.url, frm, to, None)
    base_hits = sum(1 for c in cones if base_line.intersects(c))
    base_km = base_p["distance"] / 1000
    base_min = base_p["time"] / 60000

    print(f"{'multiply':>9}  {'dist_km':>8}  {'time_min':>8}  {'cameras':>7}  "
          f"{'avoided':>7}  {'extra_min':>9}  {'min/camera':>10}")
    print(f"{'baseline':>9}  {base_km:8.2f}  {base_min:8.1f}  {base_hits:7d}  "
          f"{'-':>7}  {'-':>9}  {'-':>10}")

    for m in [x.strip() for x in args.sweep.split(",")]:
        p, line = route(args.url, frm, to, m)
        hits = sum(1 for c in cones if line.intersects(c))
        km = p["distance"] / 1000
        mins = p["time"] / 60000
        avoided = base_hits - hits
        extra = mins - base_min
        per = (extra / avoided) if avoided > 0 else float("nan")
        per_s = f"{per:10.2f}" if avoided > 0 else f"{'-':>10}"
        print(f"{m:>9}  {km:8.2f}  {mins:8.1f}  {hits:7d}  {avoided:7d}  {extra:9.1f}  {per_s}")


if __name__ == "__main__":
    main()
