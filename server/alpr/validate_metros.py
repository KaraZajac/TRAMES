#!/usr/bin/env python3
"""
Validate ALPR avoidance across metros of very different camera density.

Delaware proved the mechanism on a 6-camera route. The open risk is the opposite
regime: in a metro with thousands of cameras, does avoidance still produce a sane
route, or does it either over-detour absurdly or give up and change nothing? Those
two failure modes look completely different in the numbers, and neither shows up in
a sparse test.

Reports, per route and avoidance strength:
  cameras passed / avoided, extra time, and minutes spent per camera avoided.

Usage:
    python3 validate_metros.py --url http://localhost:8989/route \
        --cones ../graphhopper/custom_areas/alpr.geojson
"""
import argparse
import json
import urllib.request

from shapely.geometry import LineString, shape
from shapely.strtree import STRtree

# (label, from lat,lon, to lat,lon) — a spread of densities, not just dense ones,
# so a regression that only bites in sparse areas still shows up.
ROUTES = [
    ("Atlanta metro (dense)",      (33.7490, -84.3880), (33.9526, -84.5499)),
    ("Atlanta -> Athens",          (33.7490, -84.3880), (33.9519, -83.3576)),
    ("Nashville metro",            (36.1627, -86.7816), (36.0331, -86.7828)),
    ("Wilmington -> Dover (DE)",   (39.7391, -75.5398), (38.9108, -75.4277)),
    ("Chicago metro",              (41.8781, -87.6298), (42.0451, -87.6877)),
    ("Phoenix metro",              (33.4484, -112.0740), (33.5806, -112.2374)),
]

STRENGTHS = ["0.3", "0.1", "0.05", "0.01"]


def route(url, frm, to, multiply=None, timeout=180):
    body = {
        "points": [[frm[1], frm[0]], [to[1], to[0]]],
        "profile": "car",
        "ch.disable": True,
        "points_encoded": False,
    }
    if multiply is not None:
        body["custom_model"] = {"priority": [{"if": "in_alpr", "multiply_by": multiply}]}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    if "paths" not in d:
        raise RuntimeError(d.get("message", "no path"))
    p = d["paths"][0]
    return p, LineString(p["points"]["coordinates"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8989/route")
    ap.add_argument("--cones", required=True)
    args = ap.parse_args()

    fc = json.load(open(args.cones))
    geom = shape(fc["features"][0]["geometry"])
    cones = list(getattr(geom, "geoms", [geom]))
    # A linear scan over ~300k cones per route would dominate the runtime; the
    # R-tree makes the exposure score cheap enough to sweep several strengths.
    tree = STRtree(cones)
    print(f"loaded {len(cones)} cone parts\n")

    def exposure(line):
        return sum(1 for i in tree.query(line) if cones[i].intersects(line))

    for label, frm, to in ROUTES:
        print(f"=== {label} ===")
        try:
            bp, bl = route(args.url, frm, to, None)
        except Exception as e:                            # noqa: BLE001
            print(f"  baseline FAILED: {e}\n")
            continue
        bhits = exposure(bl)
        bkm, bmin = bp["distance"] / 1000, bp["time"] / 60000
        print(f"  {'baseline':>8}  {bkm:7.2f} km  {bmin:6.1f} min  {bhits:5d} cameras")

        for m in STRENGTHS:
            try:
                p, l = route(args.url, frm, to, m)
            except Exception as e:                        # noqa: BLE001
                print(f"  {m:>8}  FAILED: {e}")
                continue
            hits = exposure(l)
            km, mins = p["distance"] / 1000, p["time"] / 60000
            avoided = bhits - hits
            extra = mins - bmin
            per = f"{extra/avoided:6.2f}" if avoided > 0 else "     -"
            pct = f"{100*avoided/bhits:5.1f}%" if bhits else "    -"
            print(f"  {m:>8}  {km:7.2f} km  {mins:6.1f} min  {hits:5d} cameras  "
                  f"avoided {avoided:4d} ({pct})  +{extra:5.1f} min  {per} min/cam")
        print()


if __name__ == "__main__":
    main()
