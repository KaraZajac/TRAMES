#!/usr/bin/env python3
"""
Exposure on the merged map, scored three ways from the same routes.

    python3 merged_exposure.py --results out/results.csv --routes out/routes.jsonl.gz \\
        --osm-cones out/merged/alpr_osm.geojson \\
        --merged-cones ../server/graphhopper/custom_areas_merged/alpr_merged.geojson \\
        --discs out/merged/registry_discs.json \\
        --frame out/sampling_frame.json --draws out/sample_draws.csv \\
        [--merged-results out/merged/results.csv --merged-routes out/merged/routes.jsonl.gz] \\
        -o out/merged/exposure.csv --report out/analysis_merged_exposure.txt

Per commute, the cameras its route passes under three definitions of the camera set, all
counted as the routing counts them: cone parts after the union, so overlapping cones are one.

  osm       the parts of the mapped cameras' wedges alone (the paper's time series);
  merged    the parts of the merged set: those wedges plus a 60 m disc for every in-service
            plate reader in Flock's registry that the map lacks (cones_from_supermap.py) -
            what the routing avoided, and what run_history.py counted;
  directed  the same parts, except that a part the route enters only through registry discs
            counts only if it crosses one of them travelling the way that reader's name says
            it watches (northbound, ...). A disc watches both directions of every road it
            covers; a Falcon watches one. Names with no cardinal direction (inbound and
            outbound included) leave the disc counted as it is.

Each wedge part and disc is assigned to the merged part that holds it, so the directed count
is the merged count less the parts reached only against the named direction. The direction
of travel at a disc is the route's bearing where it passes nearest the reader.

With --merged-results and --merged-routes (run_history.py's output against alpr_merged), the
merged count is checked against the one the routing recorded, and the avoiding routes are
scored the same three ways. Commuter-weighted summaries as in analyze.py, with the stratified
bootstrap of wstats.py.
"""
import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "server", "alpr"))
from build_cones import cone_polygon  # noqa: E402
from routes_io import read_routes  # noqa: E402
from run_experiment import exposure, load_cones  # noqa: E402
from wstats import Design, ci  # noqa: E402

KEY = ("state", "h_tract", "w_tract")
HEADING = {"northbound": 0.0, "eastbound": 90.0, "southbound": 180.0, "westbound": 270.0}
TOL = 60.0          # a route within this many degrees of the named heading is travelling that way


def bearing_at(line, pt, step_m=8.0):
    """Bearing of travel (degrees from north) where the route passes nearest pt."""
    d = line.project(pt)
    kx, ky = 111320.0 * math.cos(math.radians(pt.y)), 111320.0
    step = step_m / ky                             # degrees of latitude ~ metres / 111320
    a = line.interpolate(max(d - step, 0.0)); b = line.interpolate(min(d + step, line.length))
    return math.degrees(math.atan2((b.x - a.x) * kx, (b.y - a.y) * ky)) % 360.0


def assign(tree, pieces):
    """Index of the merged part holding each piece (a wedge part or a disc), by an interior point."""
    pts = np.array([g.representative_point() for g in pieces])
    ix, part = tree.query(pts, predicate="intersects")
    out = np.full(len(pieces), -1)
    out[ix] = part                     # a point on a shared boundary may hit two parts; either will do
    miss = np.flatnonzero(out < 0)
    if len(miss):
        out[miss] = tree.nearest(pts[miss])
    return out, len(miss)


class Scorer:
    def __init__(self, osm_path, merged_path, discs_path):
        self.osm = load_cones(osm_path); self.otree = STRtree(self.osm)
        self.parts = load_cones(merged_path); self.mtree = STRtree(self.parts)
        D = json.load(open(discs_path)); self.meta = D["discs"]
        self.discs = [cone_polygon(m["lat"], m["lon"], 0.0, 360.0, D["radius_m"]) for m in self.meta]
        self.osm_of, self.disc_of = defaultdict(list), defaultdict(list)
        a, m1 = assign(self.mtree, self.osm)
        for i, p in enumerate(a):
            self.osm_of[p].append(i)
        b, m2 = assign(self.mtree, self.discs)
        for j, p in enumerate(b):
            self.disc_of[p].append(j)
        self.unassigned = m1 + m2

    def direction_ok(self, line, j):
        want = HEADING.get(self.meta[j]["watches"] or "")
        if want is None:
            return True
        b = bearing_at(line, Point(self.meta[j]["lon"], self.meta[j]["lat"]))
        return abs((b - want + 180.0) % 360.0 - 180.0) <= TOL

    def score(self, coords):
        line = LineString(coords)
        o = exposure(line, self.otree, self.osm)
        crossed = [m for m in self.mtree.query(line) if self.parts[m].intersects(line)]
        d = 0
        for m in crossed:
            if any(self.osm[i].intersects(line) for i in self.osm_of.get(m, ())):
                d += 1
                continue
            hit = [j for j in self.disc_of.get(m, ()) if self.discs[j].intersects(line)]
            # a part whose pieces the line misses is a seam of the union: it counts as merged does
            d += (not hit) or any(self.direction_ok(line, j) for j in hit)
        return o, len(crossed), d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--routes", required=True)
    ap.add_argument("--osm-cones", required=True)
    ap.add_argument("--merged-cones", required=True)
    ap.add_argument("--discs", required=True)
    ap.add_argument("--frame", required=True)
    ap.add_argument("--draws", required=True)
    ap.add_argument("--merged-results", default=None)
    ap.add_argument("--merged-routes", default=None)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    S = Scorer(args.osm_cones, args.merged_cones, args.discs)
    print(f"{len(S.osm):,} OSM cone parts, {len(S.discs):,} registry discs, {len(S.parts):,} merged parts "
          f"({S.unassigned} pieces placed by nearest part)", flush=True)

    rows = {tuple(r[k] for k in KEY): r for r in csv.DictReader(open(args.results, newline=""))}
    base = {}
    for n, rec in enumerate(read_routes(args.routes), 1):
        k = tuple(rec[x] for x in KEY)
        if k in rows:
            base[k] = S.score(rec["base"])
        if n % 10000 == 0:
            print(f"  {n:,} baselines scored", flush=True)
    print(f"{len(base):,} baselines scored", flush=True)

    avoid, check = {}, []
    if args.merged_results and args.merged_routes and os.path.exists(args.merged_routes):
        mrows = {tuple(r[k] for k in KEY): r for r in csv.DictReader(open(args.merged_results, newline=""))}
        check = [(base[k][1], int(r["base_cameras"])) for k, r in mrows.items() if k in base]
        for rec in read_routes(args.merged_routes):
            k = tuple(rec[x] for x in KEY)
            if k in base and "avoid" in rec:
                avoid[k] = S.score(rec["avoid"])
        for k, r in mrows.items():                     # a commute the merged map does not expose keeps its baseline
            if k in base and k not in avoid and int(r["base_cameras"]) == 0:
                avoid[k] = base[k]
        print(f"{len(avoid):,} avoiding routes scored", flush=True)

    out_rows = []
    for k, r0 in rows.items():
        if k not in base:
            continue
        r = dict(r0); b = base[k]
        r.update(base_osm=b[0], base_merged=b[1], base_directed=b[2])
        if k in avoid:
            a = avoid[k]; r.update(avoid_osm=a[0], avoid_merged=a[1], avoid_directed=a[2])
        out_rows.append(r)
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys())); w.writeheader(); w.writerows(out_rows)

    Dz = Design(out_rows, args.frame, args.draws)
    km = np.array([float(r["base_km"]) for r in out_rows]); ones = np.ones(Dz.n)
    L = []; P = L.append
    P("=" * 78); P("EXPOSURE ON THE MERGED MAP: the same routes, three camera sets"); P("=" * 78)
    P(f"{Dz.n:,} commutes, commuter-weighted. {len(S.osm):,} OSM wedge parts; {len(S.discs):,} registry discs "
      f"({sum(1 for m in S.meta if m['watches'] in HEADING):,} name a cardinal travel direction); "
      f"{len(S.parts):,} merged parts. Cameras are counted as cone parts after the union, as the routing counts them.")
    if check:
        bad = sum(1 for x, y in check if x != y)
        P(f"check: the merged count matches the routing's own on {len(check) - bad:,} of {len(check):,} baselines")
    P(f"\n{'camera set':10s} {'>=1 camera':>22s} {'mean cameras':>22s} {'median':>7s} {'>=5':>7s} {'per km':>8s}")
    X = {}
    for name in ("osm", "merged", "directed"):
        x = X[name] = np.array([float(r["base_" + name]) for r in out_rows])
        lo, hi = ci(Dz.boot_ratio((x >= 1).astype(float), ones)[:, 0]); mlo, mhi = ci(Dz.boot_ratio(x, ones)[:, 0])
        P(f"{name:10s} {100*Dz.share(x >= 1):6.1f}% [{100*lo:.1f}-{100*hi:.1f}]   {Dz.mean(x):6.2f} [{mlo:.2f}-{mhi:.2f}]   "
          f"{Dz.quantile(x, .5):5.0f}   {100*Dz.share(x >= 5):5.1f}%   {Dz.ratio(x, km):.4f}")
    P(f"\nnewly exposed by the registry (0 on the map, >=1 merged): {100*Dz.share((X['osm'] == 0) & (X['merged'] >= 1)):.1f}% "
      f"of commuters; encounters +{100*(Dz.mean(X['merged'])/Dz.mean(X['osm'])-1):.1f}%")
    rd = Dz.boot_ratio(X["directed"], ones)[:, 0] / Dz.boot_ratio(X["merged"], ones)[:, 0]
    lo, hi = ci(rd)
    P(f"directed against merged: mean encounters x{Dz.mean(X['directed'])/Dz.mean(X['merged']):.3f} [{lo:.3f}-{hi:.3f}]; "
      f"of the registry's additions, {100*(Dz.mean(X['merged'])-Dz.mean(X['directed']))/(Dz.mean(X['merged'])-Dz.mean(X['osm'])):.0f}% "
      f"are against the named direction")
    if avoid:
        P(f"\nAVOIDING ROUTES planned against the merged map ({len(avoid):,} scored)")
        for name in ("osm", "merged", "directed"):
            has = [r for r in out_rows if "avoid_" + name in r]
            Dd = Design(has, args.frame, args.draws)
            xb = np.array([float(r["base_" + name]) for r in has]); xa = np.array([float(r["avoid_" + name]) for r in has])
            lo, hi = ci(Dd.boot_ratio((xa == 0).astype(float), np.ones(Dd.n))[:, 0])
            P(f"  {name:10s} reduced to zero {100*Dd.share(xa == 0):5.1f}% [{100*lo:.1f}-{100*hi:.1f}]   "
              f"of exposed {100*Dd.share(xa == 0, xb >= 1):5.1f}%   residual {100*Dd.ratio(xa, xb):.1f}% of baseline")
    txt = "\n".join(L); print(txt); open(args.report, "w").write(txt + "\n")


if __name__ == "__main__":
    main()
