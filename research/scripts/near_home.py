#!/usr/bin/env python3
"""
Where along the commute the cameras are — inside the home tract, at its edge, within 2 km of
home, near work, or in between — by who lives in the home tract.

The perimeter hypothesis (cameras at the entrances and exits of marginalized neighbourhoods rather
than inside them) predicts that residents of high-minority tracts pass more cameras at their home
tract's edge and within a couple of kilometres of home than their county neighbours do, whether
or not their total is higher. The commute analysis measures only the total; this splits it.

    python near_home.py --results out/results.csv --routes out/routes.jsonl.gz \\
        --cones ../server/graphhopper/custom_areas/alpr.geojson \\
        --frame out/sampling_frame.json --draws out/sample_draws.csv -o out/analysis_near_home.txt

A camera is located by its cone's representative point; "edge" is within 50 m of the tract
boundary on either side; distance from home and work is measured along the route. Categories are
exclusive and taken in the order listed, so a camera at the home tract's edge is not also counted
as within 2 km of home. Counts are of cone parts, as everywhere in the study.
"""
import argparse
import csv
import os
import sys
import time

import numpy as np
import shapely
from shapely.geometry import LineString
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import geo  # noqa: E402
from routes_io import read_routes  # noqa: E402
from run_experiment import load_cones  # noqa: E402
from trend import quartile_masks  # noqa: E402
from wstats import Design, ci  # noqa: E402

KEY = ("state", "h_tract", "w_tract")
EDGE, RING = 50.0, 2000.0
CATS = ("home tract interior", "home tract edge", "within 2 km of home", "in between",
        "within 2 km of work", "work tract edge", "work tract interior")


def regions_of(lon, lat):
    r = np.full(len(lon), "conus", object)
    r[(lat > 51) & ((lon < -129) | (lon > 170))] = "ak"
    r[(lat > 18) & (lat < 23) & (lon > -161) & (lon < -154)] = "hi"
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--routes", required=True)
    ap.add_argument("--cones", required=True)
    ap.add_argument("--frame", required=True)
    ap.add_argument("--draws", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()
    t0 = time.time()

    rows = list(csv.DictReader(open(args.results, newline="")))
    at = {tuple(r[k] for k in KEY): i for i, r in enumerate(rows)}
    UT = geo.load_units("tract")
    gidx = {g: i for i, g in enumerate(UT["geoid"])}
    geoms = UT["geom"]
    cones = load_cones(args.cones)
    tree = STRtree(cones)
    rp = shapely.get_coordinates(shapely.point_on_surface(np.array(cones, dtype=object)))
    creg = regions_of(rp[:, 0], rp[:, 1])
    cx, cy = np.zeros(len(rp)), np.zeros(len(rp))
    for reg in ("conus", "ak", "hi"):
        m = creg == reg
        if m.any():
            cx[m], cy[m] = geo.albers(rp[m, 0], rp[m, 1], reg)
    print(f"{len(rows):,} commutes, {len(cones):,} cone parts, {len(geoms):,} tracts  [{time.time() - t0:.0f}s]", flush=True)

    counts = np.zeros((len(rows), len(CATS)))
    missing_tract = 0
    for n, rec in enumerate(read_routes(args.routes), 1):
        i = at.get(tuple(rec[k] for k in KEY))
        if i is None:
            continue
        line = LineString(rec["base"])
        hits = [j for j in tree.query(line) if cones[j].intersects(line)]
        if not hits:
            continue
        r = rows[i]
        reg = geo.region(geo.FIPS[r["state"]])
        xy = np.asarray(rec["base"], float)
        mx, my = geo.albers(xy[:, 0], xy[:, 1], reg)
        lm = LineString(np.column_stack([mx, my]))
        pts = shapely.points(cx[hits], cy[hits])
        s = shapely.line_locate_point(lm, pts)
        e = lm.length - s
        hu, wu = gidx.get(r["h_tract"]), gidx.get(r["w_tract"])
        if hu is None or wu is None:
            missing_tract += 1
            continue
        in_h, d_h = shapely.contains(geoms[hu], pts), shapely.distance(geoms[hu].boundary, pts)
        in_w, d_w = shapely.contains(geoms[wu], pts), shapely.distance(geoms[wu].boundary, pts)
        cat = np.full(len(hits), 3)                               # in between
        cat[e <= RING] = 4
        cat[s <= RING] = 2
        cat[in_w & (d_w > EDGE)] = 6
        cat[d_w <= EDGE] = 5
        cat[in_h & (d_h > EDGE)] = 0
        cat[d_h <= EDGE] = 1
        np.add.at(counts[i], cat, 1)
        if n % 10000 == 0:
            print(f"  {n:,} routes  [{time.time() - t0:.0f}s]", flush=True)

    D = Design(rows, args.frame, args.draws)
    num = lambda k: np.array([float(r[k]) if r.get(k) not in ("", None) else np.nan for r in rows])
    pop = num("pop_total")
    pct = lambda k: np.where(pop > 0, 100 * num(k) / np.where(pop > 0, pop, 1), np.nan)
    county = np.array([r["h_tract"][:5] for r in rows])
    # Urban form: within a county, high-minority and poorer tracts are often the dense core, where
    # every road carries more cameras. Ranking inside county x density tercile removes that.
    dens_t = UT["pop_total"] / np.maximum(UT["aland"] / 1e6, 1e-3)
    dens = np.array([dens_t[gidx[r["h_tract"]]] if r["h_tract"] in gidx else np.nan for r in rows])
    cuts = np.nanpercentile(dens, [100 / 3, 200 / 3])
    band = np.where(np.isnan(dens), "x", np.where(dens <= cuts[0], "lo", np.where(dens <= cuts[1], "mid", "hi")))
    county_band = np.char.add(np.char.add(county, ":"), band)
    attrs = {"black": pct("nh_black"), "hispanic": pct("hispanic"), "income": num("median_income")}
    label = {"black": "% non-Hispanic Black", "hispanic": "% Hispanic", "income": "median household income"}
    total = counts.sum(axis=1)
    ones = np.ones(D.n)

    L = []; P = L.append
    P("=" * 78)
    P("WHERE ALONG THE COMMUTE: cameras passed by location relative to home and work")
    P("=" * 78)
    P(f"{D.n:,} commutes, commuter-weighted. Categories are exclusive, in this order: home tract")
    P(f"interior, home tract edge (<= {EDGE:.0f} m of its boundary, either side), within {RING / 1000:.0f} km of home along")
    P("the route, work tract interior and edge, within 2 km of work, in between.")
    if missing_tract:
        P(f"({missing_tract} commutes with a tract missing from TIGER 2022 are left uncategorized)")
    P(f"\n  {'':26s} {'mean':>6s} {'share':>6s}")
    for c, name in enumerate(CATS):
        P(f"  {name:26s} {D.mean(counts[:, c]):6.3f} {100 * D.mean(counts[:, c]) / D.mean(total):5.1f}%")
    P(f"  {'all':26s} {D.mean(total):6.3f}")

    near = counts[:, 1] + counts[:, 2]
    groups = [("home tract edge", counts[:, 1]), ("home edge + 2 km ring", near),
              ("home tract interior", counts[:, 0]), ("everything else", total - near - counts[:, 0]),
              ("all cameras", total)]
    out_rows = []
    for a in ("black", "hispanic", "income"):
        band_ranked = np.any(quartile_masks(D, attrs[a], county_band), axis=0)
        for scope, cty in (("within county", county), ("within county x density tercile", county_band),
                           ("within county, same commutes as the density ranking", county), ("national", None)):
            masks = quartile_masks(D, attrs[a], cty)
            if scope.startswith("within county, same"):
                masks = [m & band_ranked for m in masks]
            cover = np.any(masks, axis=0)
            P(f"\n--- {label[a]}, quartiles {scope} (Q4 = highest) ---")
            P(f"  ranked: {int(cover.sum()):,} commutes, {100 * D.weight_share(cover):.0f}% of commuters")
            P(f"  {'cameras passed':26s} {'Q1':>7s} {'Q2':>7s} {'Q3':>7s} {'Q4':>7s}   Q4/Q1 [95% CI]")
            for name, x in groups:
                means = [D.mean(x, m) for m in masks]
                reps = D.boot_ratio(x, ones, masks)
                lo, hi = ci(reps[:, 3] / np.maximum(reps[:, 0], 1e-12))
                rat = means[3] / max(means[0], 1e-12)
                star = "" if lo <= 1 <= hi else "  *"
                P(f"  {name:26s} " + " ".join(f"{v:7.3f}" for v in means) + f"   {rat:5.2f}x [{lo:.2f}-{hi:.2f}]{star}")
                out_rows.append({"attribute": a, "scope": scope, "segment": name,
                                 **{f"q{k + 1}": round(v, 4) for k, v in enumerate(means)},
                                 "q4_over_q1": round(rat, 4), "lo": round(lo, 4), "hi": round(hi, 4)})
    P(f"\n[{time.time() - t0:.0f}s]")
    txt = "\n".join(L)
    print(txt)
    open(args.out, "w").write(txt + "\n")
    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(out_rows[0]))
            w.writeheader(); w.writerows(out_rows)


if __name__ == "__main__":
    main()
