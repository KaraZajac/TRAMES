#!/usr/bin/env python3
"""
How complete is the crowdsourced camera map, measured against Flock's own inventory? Joshua
Michael published a table of 335,701 Flock device records as of December 2025, with type,
status and coordinates (flocksurveillance.org, data/cameras.tsv; Michael 2026). Its plate
readers in service are the population the OpenStreetMap map is trying to capture, so the share
of them with a mapped camera nearby is the map's recall, and whether that share varies with who
lives nearby is the coverage-bias question every demographic result in the paper depends on.

    python validate_inventory.py --inventory data/flocksurveillance/cameras.tsv \\
        --snapshot 2026-01-01=../server/alpr/history/2026-01-01 \\
        --snapshot 2026-09-22=../server/alpr/region_cache \\
        --roads out/siting/roads.npz --tables out/siting \\
        -o out/analysis_inventory.txt --csv out/siting/inventory.csv

A. Recall: inventory plate readers in service (the readsLicensePlates feature, status inService)
   with a mapped ALPR node within 25, 50 or 100 m, nationally and by state, on each map. The
   January 2026 map is the like-for-like comparison with a December 2025 inventory; the current
   map shows how many of those cameras have been mapped since.
B. Precision: mapped Flock nodes with an inventory record within 50 m.
C. Coverage bias: recall in the highest quartile of each tract attribute over the lowest,
   Mantel-Haenszel over county (and county x density) strata, county bootstrap.
D. Siting from the inventory: siting.py's all-roads contrast (cameras per road point, i.e. per
   kilometre of road) with the inventory's plate readers in place of the mapped cameras.
Matching is nearest-within, not one-to-one: one mapped node at an intersection can stand for
two inventory cameras on its poles, as a multi-head node does.
"""
import argparse
import csv
import glob
import json
import os
import sys

import numpy as np
import shapely

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "server", "alpr"))
import geo  # noqa: E402
import siting as S  # noqa: E402
from build_cones import vendor_of  # noqa: E402
from siting_traffic import schemes  # noqa: E402

DISTS = (25.0, 50.0, 100.0)


def load_osm(tile_dir):
    nodes = {}
    for p in glob.glob(os.path.join(tile_dir, "tile_*.json")):
        for e in json.load(open(p)).get("elements", ()):
            if e.get("type") == "node" and e.get("lat") is not None:
                nodes[e["id"]] = e
    lon = np.array([e["lon"] for e in nodes.values()])
    lat = np.array([e["lat"] for e in nodes.values()])
    flock = np.array(["flock" in (vendor_of(e.get("tags") or {}) or "").lower() for e in nodes.values()])
    return lon, lat, flock


def within(lon_a, lat_a, lon_b, lat_b, dist):
    """For each point of A: is there a point of B within dist metres (per Albers region)?"""
    hit = np.zeros(len(lon_a), bool)
    ra, rb = S.regions_of(lon_a, lat_a), S.regions_of(lon_b, lat_b)
    for i, reg in enumerate(S.REG):
        ia, ib = np.flatnonzero(ra == i), np.flatnonzero(rb == i)
        if not len(ia) or not len(ib):
            continue
        xa, ya = geo.albers(lon_a[ia], lat_a[ia], reg)
        xb, yb = geo.albers(lon_b[ib], lat_b[ib], reg)
        tree = shapely.STRtree(shapely.points(xb, yb))
        q, _ = tree.query(shapely.points(xa, ya), predicate="dwithin", distance=dist)
        hit[ia[np.unique(q)]] = True
    return hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--snapshot", action="append", required=True, metavar="DATE=TILE_DIR")
    ap.add_argument("--roads", required=True, help="siting's roads.npz (its cached road-point assignment is reused)")
    ap.add_argument("--tables", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--csv", required=True)
    args = ap.parse_args()
    L = []

    def P(s=""):
        L.append(s); print(s, flush=True)

    rows = list(csv.DictReader(open(args.inventory, newline=""), delimiter="\t"))
    lon_all = np.array([float(r["lon"]) for r in rows])
    lat_all = np.array([float(r["lat"]) for r in rows])
    lpr = np.array(["readsLicensePlates" in (r["features"] or "") for r in rows])
    status = np.array([r["status"] for r in rows])
    UT = geo.load_units("tract")
    tr_all = S.assign(UT, lon_all, lat_all, neighbours=False)[0]
    in_us = tr_all >= 0
    sel = lpr & (status == "inService") & in_us            # the population: plate readers in service
    lon, lat, tr = lon_all[sel], lat_all[sel], tr_all[sel]
    st = UT["state"][tr]

    P("=" * 78)
    P("COVERAGE AGAINST FLOCK'S OWN INVENTORY (Michael 2026; records as of December 2025)")
    P("=" * 78)
    P(f"{len(rows):,} device records; {int(lpr.sum()):,} read licence plates; in the 50 states + DC, "
      f"{int((lpr & in_us).sum()):,} plate readers, of which in service {int(sel.sum()):,}, planned "
      f"{int((lpr & in_us & (status == 'inPlanning')).sum()):,}, decommissioned "
      f"{int((lpr & in_us & (status == 'decommissioned')).sum()):,}.")

    snaps = []
    for spec in args.snapshot:
        date, _, tiles = spec.partition("=")
        olon, olat, oflock = load_osm(tiles)
        ous = S.assign(UT, olon, olat, neighbours=False)[0] >= 0
        snaps.append((date, olon[ous], olat[ous], oflock[ous]))

    P("\nA. RECALL: in-service inventory plate readers with a mapped ALPR node within d metres")
    recall = {}
    for date, olon, olat, oflock in snaps:
        P(f"\n  map of {date}: {len(olon):,} mapped ALPR nodes in the states, {int(oflock.sum()):,} tagged Flock")
        for d in DISTS:
            h_any = within(lon, lat, olon, olat, d)
            h_flk = within(lon, lat, olon[oflock], olat[oflock], d)
            recall[(date, d)] = h_any
            P(f"    within {d:4.0f} m: any mapped ALPR {100 * h_any.mean():5.1f}%   a Flock-tagged node {100 * h_flk.mean():5.1f}%")

    P("\nB. PRECISION: mapped Flock nodes with an inventory record within 50 m")
    for date, olon, olat, oflock in snaps:
        fl, fa = olon[oflock], olat[oflock]
        a = within(fl, fa, lon, lat, 50.0)
        b = within(fl, fa, lon_all[lpr & in_us], lat_all[lpr & in_us], 50.0)
        c = within(fl, fa, lon_all[in_us], lat_all[in_us], 50.0)
        P(f"  {date}: {len(fl):,} Flock nodes; near an in-service plate reader {100 * a.mean():.1f}%, any plate-reader "
          f"record {100 * b.mean():.1f}%, any Flock device record {100 * c.mean():.1f}%")

    P("\n   recall at 50 m by state (in-service plate readers; current map), lowest first")
    cur = snaps[-1][0]
    h = recall[(cur, 50.0)]
    out_rows = []
    for s_ in sorted(np.unique(st), key=lambda s_: h[st == s_].mean()):
        m = st == s_
        out_rows.append({"state": geo.ABBR[s_].upper(), "inventory_lpr_in_service": int(m.sum()),
                         **{f"recall50_{d_}": round(float(recall[(d_, 50.0)][m].mean()), 4) for d_, *_ in snaps}})
    line = []
    for r in out_rows:
        line.append(f"{r['state']} {100 * r[f'recall50_{cur}']:.0f}% ({r['inventory_lpr_in_service']:,})")
    for i in range(0, len(line), 8):
        P("    " + "   ".join(line[i:i + 8]))

    # --- C: coverage bias -------------------------------------------------------------------
    bt = S.Boot(np.unique(UT["county"]))
    pop, ucix, Q, strata = schemes(UT, bt)
    P("\nC. COVERAGE BIAS: recall at 50 m in the highest quartile of each tract attribute over the lowest")
    P("   (national: crude ratio; within county / county x density: Mantel-Haenszel; 95% CI resampling counties)")
    for date, *_ in snaps:
        hit = recall[(date, 50.0)].astype(float)
        P(f"\n  map of {date}")
        for a in ("black", "hispanic", "income"):
            parts = []
            for sch in ("national", "within county", "county x density"):
                q = Q[(a, sch)].astype(int)[tr]
                if sch == "national":
                    num = bt.table(ucix[tr], q, values=hit, ncols=5); den = bt.table(ucix[tr], q, ncols=5)
                    rn, rd = bt.reps(num), bt.reps(den)
                    pt = S.div(num.sum(0)[4], den.sum(0)[4]) / S.div(num.sum(0)[1], den.sum(0)[1])
                    lo, hi = S.ci(S.div(S.div(rn[:, 4], rd[:, 4]), S.div(rn[:, 1], rd[:, 1])))
                    r1, r4 = S.div(num.sum(0)[1], den.sum(0)[1]), S.div(num.sum(0)[4], den.sum(0)[4])
                    parts.append(f"national Q1 {100 * r1:.0f}% Q4 {100 * r4:.0f}% ratio {S.fmt_ratio(float(pt), lo, hi)}")
                else:
                    u2s, s2c = strata[sch]
                    C = np.zeros((len(s2c), 5)); N = np.zeros((len(s2c), 5))
                    np.add.at(C, (u2s[tr], q), hit); np.add.at(N, (u2s[tr], q), 1)
                    pts, reps = S.mh(C, N, s2c, bt)
                    lo, hi = S.ci(reps[:, 2])
                    parts.append(f"{sch} {S.fmt_ratio(float(pts[2]), lo, hi)}")
                    out_rows.append({"state": f"coverage:{date}:{a}:{sch}", "q4_over_q1": round(float(pts[2]), 4),
                                     "lo": round(float(lo), 4), "hi": round(float(hi), 4)})
            P(f"    {S.LABEL[a]:24s} " + "   ".join(parts))

    # --- D: siting from the inventory -------------------------------------------------------
    P("\nD. SITING FROM THE INVENTORY: in-service plate readers per kilometre of road (all classes), Q4/Q1")
    S.CACHE["dir"], S.CACHE["src"] = args.tables, os.path.getmtime(args.roads)
    R = np.load(args.roads)
    nu = S.cached("null_tract", S.assign, UT, R["null_lon"], R["null_lat"])[0]
    mn = nu >= 0
    S.per_km = float(R["spacing"]) / 1000
    S.rates_by_quartile(P, bt, ucix, strata, Q, pop, tr, np.ones(len(tr), bool), nu, mn, "road",
                        "inventory plate readers per 100 km of road (compare siting.py's all-roads row)",
                        [], "inventory:all:road")

    open(args.out, "w").write("\n".join(L) + "\n")
    keys = sorted({k for r in out_rows for k in r})
    with open(args.csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys); w.writeheader(); w.writerows(out_rows)


if __name__ == "__main__":
    main()
