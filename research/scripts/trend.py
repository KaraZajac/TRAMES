#!/usr/bin/env python3
"""
Trend analysis: the same commutes, the same roads, against camera maps from different dates.

    python3 trend.py --results out/results.csv --routes out/routes.jsonl.gz \\
        --frame out/sampling_frame.json --draws out/sample_draws.csv \\
        --snapshot 2025-07-01=../server/alpr/history/2025-07-01 \\
        --snapshot 2026-09-22=../server/alpr/region_cache:../server/graphhopper/custom_areas/alpr.geojson \\
        ...  -o out/trend.csv --by-state out/trend_by_state.csv \\
        --cameras-by-state out/cameras_by_state.csv --report out/analysis_trend.txt

A --snapshot is DATE=DIR[:CONES], where DIR holds that date's Overpass tiles (tile_*.json)
and CONES its cone GeoJSON (default DIR/alpr.geojson). If out/history/DATE/results.csv exists
(run_history.py), avoidance is read from it; exposure never needs routing — the unavoided route
does not depend on the cameras, so each commute's saved route is re-scored against the date's
cones.

Per date it also re-asks analyze.py's demographic contrasts (national and within-county Q4/Q1
by income, Black and Hispanic share), and it follows each routed date's exposed commutes to the
later maps (<out>_cohorts.csv): across dates the exposed population changes, so only a fixed
set of trips shows whether the same commutes got harder to clean.

What this measures, stated plainly: OSM records when a camera was MAPPED, not installed
(installation dates are on 47 of 142,991 nodes), so a date's map is what a study run that day
would have seen. The trend is how measured exposure — and the avoidance picture — evolved as
volunteers filled in the map, on a fixed road network and a fixed sample.
"""
import argparse
import csv
import glob
import json
import os
import sys
from multiprocessing import Pool

import numpy as np
from shapely.geometry import LineString, Point, shape
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from routes_io import read_routes  # noqa: E402
from run_experiment import exposure, load_cones  # noqa: E402
from wstats import Design, ci  # noqa: E402

KEY = ("state", "h_tract", "w_tract")
ROOT = os.path.dirname(HERE)
CODES = {"Alabama": "al", "Alaska": "ak", "Arizona": "az", "Arkansas": "ar", "California": "ca", "Colorado": "co",
         "Connecticut": "ct", "Delaware": "de", "District of Columbia": "dc", "Florida": "fl", "Georgia": "ga",
         "Hawaii": "hi", "Idaho": "id", "Illinois": "il", "Indiana": "in", "Iowa": "ia", "Kansas": "ks",
         "Kentucky": "ky", "Louisiana": "la", "Maine": "me", "Maryland": "md", "Massachusetts": "ma",
         "Michigan": "mi", "Minnesota": "mn", "Mississippi": "ms", "Missouri": "mo", "Montana": "mt",
         "Nebraska": "ne", "Nevada": "nv", "New Hampshire": "nh", "New Jersey": "nj", "New Mexico": "nm",
         "New York": "ny", "North Carolina": "nc", "North Dakota": "nd", "Ohio": "oh", "Oklahoma": "ok",
         "Oregon": "or", "Pennsylvania": "pa", "Rhode Island": "ri", "South Carolina": "sc", "South Dakota": "sd",
         "Tennessee": "tn", "Texas": "tx", "Utah": "ut", "Vermont": "vt", "Virginia": "va", "Washington": "wa",
         "West Virginia": "wv", "Wisconsin": "wi", "Wyoming": "wy"}


def state_locator():
    """Point -> state code, from the boundaries make_figures.py draws (simplified, so a
    point that falls just outside every polygon — a coastal camera — takes the nearest
    state within ~20 km; beyond that it is Canada or Mexico and belongs to no state)."""
    fc = json.load(open(os.path.join(ROOT, "paper", "figures", "us-states.json")))
    polys, codes = [], []
    for f in fc["features"]:
        c = CODES.get(f["properties"]["name"])
        if c:
            polys.append(shape(f["geometry"])); codes.append(c)
    tree = STRtree(polys)

    def locate(lat, lon):
        p = Point(lon, lat)
        for i in tree.query(p):
            if polys[i].contains(p):
                return codes[i]
        i = tree.nearest(p)
        return codes[i] if polys[i].distance(p) < 0.2 else None
    return locate


def cameras_in(tile_dir):
    nodes = {}
    for p in glob.glob(os.path.join(tile_dir, "tile_*.json")):
        for e in json.load(open(p)).get("elements", ()):
            if e.get("type") == "node":
                nodes[e["id"]] = (e["lat"], e["lon"])
    return nodes


def quartile_masks(D, x, county=None, min_n=40):
    """Commuter-weighted quartiles of a tract attribute as four masks: national cut-points, or
    ranked inside each county with >= min_n commutes — exactly analyze.py's wcontrast() and
    wwithin(). Tracts do not move between map dates, so one set of masks serves every date."""
    valid = ~np.isnan(x)
    b = np.zeros(D.n, int)
    if county is None:
        groups = [np.flatnonzero(valid)]
    else:
        by = {}
        for i in np.flatnonzero(valid):
            by.setdefault(county[i], []).append(i)
        groups = [np.array(ix) for ix in by.values() if len(ix) >= min_n]
    for ix in groups:
        m = np.zeros(D.n, bool); m[ix] = True
        qs = [D.quantile(x, q, m) for q in (0.25, 0.5, 0.75)]
        xi = x[ix]
        b[ix] = 1 + (xi > qs[0]).astype(int) + (xi > qs[1]) + (xi > qs[2])
    return [b == q for q in (1, 2, 3, 4)]


def rescore(job):
    """Worker: this date's exposure for every saved baseline route."""
    label, cones_path, routes_path, keys = job
    cones = load_cones(cones_path)
    tree = STRtree(cones)
    out = {}
    for rec in read_routes(routes_path):
        k = tuple(rec[x] for x in KEY)
        if k in keys:
            out[k] = exposure(LineString(rec["base"]), tree, cones)
    return label, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--routes", required=True)
    ap.add_argument("--frame", required=True)
    ap.add_argument("--draws", required=True)
    ap.add_argument("--snapshot", action="append", required=True, metavar="DATE=DIR[:CONES]")
    ap.add_argument("--history-dir", default=os.path.join(ROOT, "out", "history"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--by-state", required=True)
    ap.add_argument("--cameras-by-state", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    snaps = []
    for spec in args.snapshot:
        date, _, rest = spec.partition("=")
        tiles, _, cones = rest.partition(":")
        snaps.append((date, tiles, cones or os.path.join(tiles, "alpr.geojson")))
    snaps.sort()

    rows = list(csv.DictReader(open(args.results, newline="")))
    keys = {tuple(r[k] for k in KEY) for r in rows}
    D = Design(rows, args.frame, args.draws)
    km = np.array([float(r["base_km"]) for r in rows])
    ones = np.ones(D.n)

    print(f"re-scoring {len(rows)} baseline routes against {len(snaps)} camera maps ...", flush=True)
    with Pool(min(args.workers, len(snaps))) as pool:
        scored = dict(pool.map(rescore, [(d, c, args.routes, keys) for d, _, c in snaps]))

    locate = state_locator()
    pops = {}
    for line in open(os.path.join(ROOT, "data", "acs_b03002.dat"), encoding="latin-1"):
        p = line.split("|")
        if p[0].startswith("0400000US"):
            pops[p[0][9:]] = int(p[1])
    fips = {"al": "01", "ak": "02", "az": "04", "ar": "05", "ca": "06", "co": "08", "ct": "09", "de": "10",
            "dc": "11", "fl": "12", "ga": "13", "hi": "15", "id": "16", "il": "17", "in": "18", "ia": "19",
            "ks": "20", "ky": "21", "la": "22", "me": "23", "md": "24", "ma": "25", "mi": "26", "mn": "27",
            "ms": "28", "mo": "29", "mt": "30", "ne": "31", "nv": "32", "nh": "33", "nj": "34", "nm": "35",
            "ny": "36", "nc": "37", "nd": "38", "oh": "39", "ok": "40", "or": "41", "pa": "42", "ri": "44",
            "sc": "45", "sd": "46", "tn": "47", "tx": "48", "ut": "49", "vt": "50", "va": "51", "wa": "53",
            "wv": "54", "wi": "55", "wy": "56"}

    # The demographic contrasts of analyze.py (RQ3/RQ4), re-asked on every date's map: if the
    # national gradients were siting, they should not depend on how complete the map was.
    num = lambda k: np.array([float(r[k]) if r.get(k) not in ("", None) else np.nan for r in rows])
    pop = num("pop_total")
    pct = lambda k: np.where(pop > 0, 100 * num(k) / np.where(pop > 0, pop, 1), np.nan)
    county = np.array([r["h_tract"][:5] for r in rows])
    attrs = {"income": num("median_income"), "black": pct("nh_black"), "hispanic": pct("hispanic")}
    qmasks = {(a, scope): quartile_masks(D, x, county if scope == "within" else None)
              for a, x in attrs.items() for scope in ("national", "within")}

    L = []; P = L.append
    P("=" * 78); P("TREND: THE SAME COMMUTES AGAINST CAMERA MAPS FROM DIFFERENT DATES"); P("=" * 78)
    P(f"\n{D.n} commutes, {len(D.states)} states, commuter-weighted; routes and roads held fixed.")
    P("A date's map is what was MAPPED by then, not what was installed.\n")
    nat, bystate, camrows, avoid_tabs = [], [], [], {}
    for date, tiles, cones in snaps:
        cams = cameras_in(tiles)
        per_state = {}
        for lat, lon in cams.values():
            s = locate(lat, lon)
            if s:
                per_state[s] = per_state.get(s, 0) + 1
        us = sum(per_state.values())
        for s in sorted(fips):
            camrows.append({"date": date, "state": s, "cameras": per_state.get(s, 0),
                            "per_100k": round(100000 * per_state.get(s, 0) / pops[fips[s]], 2)})
        bc = np.array([scored[date][tuple(r[k] for k in KEY)] for r in rows], float)
        rec = {"date": date, "cameras_us": us,
               "pct_ge1": 100 * D.share(bc >= 1), "mean_cameras": D.mean(bc),
               "median_cameras": D.quantile(bc, .5), "pct_ge5": 100 * D.share(bc >= 5),
               "cameras_per_km": D.ratio(bc, km)}
        lo, hi = ci(D.boot_ratio((bc >= 1).astype(float), ones)[:, 0])
        rec["pct_ge1_lo"], rec["pct_ge1_hi"] = 100 * lo, 100 * hi
        lo, hi = ci(D.boot_ratio(bc, ones)[:, 0])
        rec["mean_cameras_lo"], rec["mean_cameras_hi"] = lo, hi
        # Highest quartile's mean exposure over the lowest's, with the interval on the ratio
        # itself from shared bootstrap replicates, as in analyze.py.
        for (a, scope), masks in qmasks.items():
            reps = D.boot_ratio(bc, ones, masks)
            k = f"{a}_{scope}"
            rec[k] = D.mean(bc, masks[3]) / max(D.mean(bc, masks[0]), 1e-12)
            rec[k + "_lo"], rec[k + "_hi"] = ci(reps[:, 3] / np.maximum(reps[:, 0], 1e-12))

        hist = os.path.join(args.history_dir, date, "results.csv")
        avoid_src = hist if os.path.exists(hist) else (args.results if date == snaps[-1][0] else None)
        if avoid_src:
            hrows = list(csv.DictReader(open(avoid_src, newline="")))
            avoid_tabs[date] = hrows
            H = Design(hrows, args.frame, args.draws)
            f = lambda k: np.array([float(x[k]) for x in hrows])
            ac, em, ev, bmin, hb = f("avoid_cameras"), f("extra_min"), f("cameras_evaded"), f("base_min"), f("base_cameras")
            rec.update({"avoidance_source": os.path.relpath(avoid_src, ROOT), "avoid_n": H.n,
                        "pct_zero_after": 100 * H.share(ac == 0),
                        "pct_zero_after_given_exposed": 100 * H.share(ac == 0, hb >= 1),
                        "median_extra_min": H.quantile(em, .5), "mean_extra_min": H.mean(em),
                        "median_extra_min_given_exposed": H.quantile(em, .5, hb >= 1),
                        "median_overhead_pct": H.quantile(np.where(bmin > 0, 100 * em / np.maximum(bmin, 1e-9), 0), .5),
                        "median_min_per_camera": H.quantile(np.where(ev > 0, em / np.maximum(ev, 1), 0), .5, ev > 0),
                        "residual_pct_of_baseline": 100 * H.ratio(ac, hb)})
            lo, hi = ci(H.boot_ratio((ac == 0).astype(float), np.ones(H.n))[:, 0])
            rec["pct_zero_after_lo"], rec["pct_zero_after_hi"] = 100 * lo, 100 * hi
            lo, hi = ci(H.boot_quantile(em, .5))
            rec["median_extra_min_lo"], rec["median_extra_min_hi"] = lo, hi
            # The same, for the commuters who had something to avoid on this date — the
            # comparison across dates that the all-commuter figures blur with exposure.
            hx = hb >= 1
            lo, hi = ci(H.boot_ratio((ac == 0).astype(float), np.ones(H.n), [hx])[:, 0])
            rec["pct_zero_after_given_exposed_lo"], rec["pct_zero_after_given_exposed_hi"] = 100 * lo, 100 * hi
            lo, hi = ci(H.boot_quantile(em, .5, hx))
            rec["median_extra_min_given_exposed_lo"], rec["median_extra_min_given_exposed_hi"] = lo, hi
            per = np.where(ev > 0, em / np.maximum(ev, 1), 0)
            lo, hi = ci(H.boot_quantile(per, .5, ev > 0))
            rec["median_min_per_camera_lo"], rec["median_min_per_camera_hi"] = lo, hi
        nat.append(rec)
        for s in D.states:
            m = D.state == s
            bystate.append({"date": date, "state": s, "pct_ge1": round(100 * D.share(bc >= 1, m), 2),
                            "mean_cameras": round(D.mean(bc, m), 3), "cameras": per_state.get(s, 0)})
        P(f"--- {date}: {us:,} mapped cameras in the 50 states + DC ---")
        P(f"  commutes passing >=1 camera   {rec['pct_ge1']:5.1f}% [{rec['pct_ge1_lo']:.1f}–{rec['pct_ge1_hi']:.1f}]")
        P(f"  mean cameras passed           {rec['mean_cameras']:5.2f} [{rec['mean_cameras_lo']:.2f}–{rec['mean_cameras_hi']:.2f}]"
          f"   median {rec['median_cameras']:.0f}   >=5: {rec['pct_ge5']:.1f}%   per km {rec['cameras_per_km']:.4f}")
        for a in attrs:
            n, w = f"{a}_national", f"{a}_within"
            P(f"  {a:9s} Q4/Q1 national {rec[n]:5.2f}x [{rec[n + '_lo']:.2f}–{rec[n + '_hi']:.2f}]"
              f"   within-county {rec[w]:5.2f}x [{rec[w + '_lo']:.2f}–{rec[w + '_hi']:.2f}]")
        if avoid_src:
            P(f"  reduced to ZERO cameras        {rec['pct_zero_after']:5.1f}% [{rec['pct_zero_after_lo']:.1f}–{rec['pct_zero_after_hi']:.1f}]"
              f"   (of exposed commutes: {rec['pct_zero_after_given_exposed']:.1f}% "
              f"[{rec['pct_zero_after_given_exposed_lo']:.1f}–{rec['pct_zero_after_given_exposed_hi']:.1f}])")
            P(f"  median extra time              {rec['median_extra_min']:5.2f} min [{rec['median_extra_min_lo']:.2f}–{rec['median_extra_min_hi']:.2f}]"
              f"   (of exposed: {rec['median_extra_min_given_exposed']:.2f} "
              f"[{rec['median_extra_min_given_exposed_lo']:.2f}–{rec['median_extra_min_given_exposed_hi']:.2f}])"
              f"   overhead {rec['median_overhead_pct']:.2f}%")
            P(f"  minutes per camera evaded      {rec['median_min_per_camera']:5.2f} "
              f"[{rec['median_min_per_camera_lo']:.2f}–{rec['median_min_per_camera_hi']:.2f}]"
              f"   residual {rec['residual_pct_of_baseline']:.1f}% of baseline")
        else:
            P("  (no avoidance run for this date)")
        P("")

    # Cohorts: the commutes exposed on one date, followed to every later routed date. Across
    # dates the exposed population changes — the map reached new, often costlier places — so
    # only a fixed set of trips shows whether the same commutes got harder to clean.
    cohort_rows = []
    routed = [d for d, _, _ in snaps if d in avoid_tabs]
    if len(routed) > 1:
        tabs = {d: {tuple(r[k] for k in KEY): r for r in avoid_tabs[d]} for d in routed}
        common = sorted(set.intersection(*(set(t) for t in tabs.values())))
        C = Design([tabs[routed[-1]][k] for k in common], args.frame, args.draws)
        col = lambda d, c: np.array([float(tabs[d][k][c]) for k in common])
        P("-" * 78); P("COHORTS: the commutes exposed on one date, followed to later maps"); P("-" * 78)
        for d0 in routed[:-1]:
            S = col(d0, "base_cameras") >= 1
            P(f"\n  exposed on {d0}: {int(S.sum())} commutes, {100 * C.weight_share(S):.1f}% of commuters")
            P(f"  {'map date':12s} {'mean cams':>9s} {'reach zero':>18s} {'med +min':>9s} {'min per camera':>20s}")
            for d in routed[routed.index(d0):]:
                bc_, ac_, em_, ev_ = (col(d, c) for c in ("base_cameras", "avoid_cameras", "extra_min", "cameras_evaded"))
                per = np.where(ev_ > 0, em_ / np.maximum(ev_, 1), 0)
                zlo, zhi = ci(C.boot_ratio((ac_ == 0).astype(float), np.ones(C.n), [S])[:, 0])
                plo, phi = ci(C.boot_quantile(per, .5, S & (ev_ > 0)))
                rec = {"exposed_on": d0, "date": d, "n": int(S.sum()), "commuter_share": 100 * C.weight_share(S),
                       "mean_cameras": C.mean(bc_, S),
                       "pct_zero_after": 100 * C.share(ac_ == 0, S), "pct_zero_after_lo": 100 * zlo,
                       "pct_zero_after_hi": 100 * zhi, "median_extra_min": C.quantile(em_, .5, S),
                       "median_min_per_camera": C.quantile(per, .5, S & (ev_ > 0)),
                       "median_min_per_camera_lo": plo, "median_min_per_camera_hi": phi}
                cohort_rows.append(rec)
                P(f"  {d:12s} {rec['mean_cameras']:9.2f} {rec['pct_zero_after']:5.1f}% [{100*zlo:.1f}–{100*zhi:.1f}]"
                  f" {rec['median_extra_min']:9.2f} {rec['median_min_per_camera']:8.2f} [{plo:.2f}–{phi:.2f}]")
        P("")

    cohort_path = os.path.splitext(args.out)[0] + "_cohorts.csv"
    for path, data in ((args.out, nat), (args.by_state, bystate), (args.cameras_by_state, camrows),
                       (cohort_path, cohort_rows)):
        if not data:
            continue
        fields = []
        for r in data:
            fields += [k for k in r if k not in fields]
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for r in data:
                w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
    txt = "\n".join(L)
    print(txt)
    open(args.report, "w").write(txt + "\n")


if __name__ == "__main__":
    main()
