#!/usr/bin/env python3
"""
Do cameras simply follow the traffic? siting.py compares cameras with kilometres of road of the
same class. A busier road offers more to watch, and if the arterials of Black and Hispanic
neighbourhoods carried more traffic per kilometre than those of the same county's white
neighbourhoods, a camera placed wherever the traffic is would still produce the per-kilometre
gradient. This repeats the central comparison on the federal-aid road network, whose traffic is
counted, with the controls weighted by the traffic they carry (HPMS 2024 AADT via hpms_sample.py),
so that the rates are cameras per vehicle-kilometre. If placement followed traffic alone, the
per-vehicle-kilometre ratios would sit at 1.

    python siting_traffic.py --roads out/siting/roads.npz --hpms out/siting/hpms.npz \\
        --tiles ../server/alpr/region_cache --tables out/siting --report out/analysis_siting_traffic.txt

Cases: cameras within WATCH m of a counted section. Controls: hpms_sample.py's points, one per
200 m of counted road, weighted by length (road-km) or by length x AADT (vehicle-km a day).
Quartiles, strata, Mantel-Haenszel ratios and the county bootstrap are siting.py's. Two bases:
  - all counted roads: functional systems 1-5 (Interstate to major collector);
  - surface roads: systems 3-5, without the Interstates, freeways and expressways. Freeway
    traffic is mostly through traffic that few cameras watch, and freeways were driven through
    Black neighbourhoods, so counting it would lower those tracts' per-vehicle rate for a reason
    unrelated to where cameras are put.
and, within each, a version stratified also by functional class, so like roads are compared.
"""
import argparse
import csv
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import geo  # noqa: E402
import siting as S  # noqa: E402

FGROUP = np.array([-1, 0, 0, 1, 2, 3])        # functional system -> freeway | OPA | minor arterial | major collector
FGROUPS = ("freeway (F1-2)", "other principal arterial (F3)", "minor arterial (F4)", "major collector (F5)")
BANDS = (0, 2_000, 5_000, 10_000, 20_000, 40_000, np.inf)


def schemes(U, bt):
    """siting.py's quartile schemes and strata for a set of units."""
    attrs, pop = S.attributes(U)
    ucix = bt.index(U["county"])
    dens = np.where(U["aland"] > 0, pop / np.maximum(U["aland"] / 1e6, 1e-3), np.nan)
    ok = np.isfinite(dens) & (pop > 0)
    o = np.argsort(dens[ok]); cw = np.cumsum(pop[ok][o]); cw = cw / cw[-1]
    t1, t2 = dens[ok][o][np.searchsorted(cw, 1 / 3)], dens[ok][o][np.searchsorted(cw, 2 / 3)]
    band = np.where(dens <= t1, "lo", np.where(dens <= t2, "mid", "hi"))
    cband = np.char.add(np.char.add(U["county"].astype(str), ":"), band)
    grouping = {"national": None, "within county": U["county"], "county x density": cband}
    Q = {(a, sch): S.quartiles(attrs[a], pop, g) for a in S.LABEL for sch, g in grouping.items()}
    bidx = np.where(dens <= t1, 0, np.where(dens <= t2, 1, 2))
    ncty = len(bt.cty)
    strata = {"within county": (ucix, np.arange(ncty)),
              "county x density": (ucix * 3 + bidx, np.repeat(np.arange(ncty), 3))}
    return pop, ucix, Q, strata


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roads", required=True)
    ap.add_argument("--hpms", required=True)
    ap.add_argument("--tiles", required=True)
    ap.add_argument("--tables", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--levels", nargs="+", default=["tract", "bg"])
    ap.add_argument("--watch", type=float, default=S.WATCH)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    t0 = time.time()
    L = []

    def P(s=""):
        L.append(s); print(s, flush=True)

    cams, nulls, _ = S.load_points(args.roads, args.tiles)
    H = np.load(args.hpms)
    assert np.array_equal(H["cam_id"], cams["id"]), "hpms.npz was built from a different roads.npz"
    sp_km = float(H["spacing"]) / 1000
    pf = H["pt_f"].astype(np.int64)
    aadt = H["pt_aadt"].astype(float)
    w_km = np.full(len(pf), sp_km)
    w_vkt = aadt * sp_km / 1e6                     # million vehicle-km a day
    D = H["cam_d"]                                  # cameras x F1..F5, metres
    close = D <= args.watch
    cam_f = np.where(close.any(axis=1), np.argmax(close, axis=1) + 1, 0)   # highest class within WATCH
    cam_aadt = np.where(cam_f > 0, H["cam_aadt"][np.arange(len(cam_f)), np.maximum(cam_f, 1) - 1], np.nan)
    cam_fg = np.where(cam_f > 0, FGROUP[cam_f], -1)
    pt_fg = FGROUP[pf]
    base_c = {"all": close.any(axis=1), "surface": close[:, 2:].any(axis=1)}
    base_n = {"all": np.ones(len(pf), bool), "surface": pf >= 3}

    P("=" * 78)
    P("DO CAMERAS FOLLOW THE TRAFFIC? SITING PER VEHICLE-KILOMETRE ON THE FEDERAL-AID NETWORK")
    P("=" * 78)
    P(f"Traffic: {H['source']}; functional systems 1-5, one point per {1000 * sp_km:.0f} m "
      f"({len(pf):,} points, {w_km.sum():,.0f} km, {w_vkt.sum():,.0f} million vehicle-km a day).")
    P(f"A camera watches a counted road if one lies within {args.watch:.0f} m. Rates per vehicle-km weight each")
    P("road point by its AADT; Mantel-Haenszel ratios and county-bootstrap intervals as in siting.py.")

    UT = geo.load_units("tract")
    S.CACHE["dir"] = args.tables
    bt = S.Boot(np.unique(UT["county"]))
    levels = {}
    for level in args.levels:
        U = UT if level == "tract" else geo.load_units(level)
        S.CACHE["src"] = os.path.getmtime(args.roads)
        cu = S.cached(f"cam_{level}", S.assign, U, cams["lon"], cams["lat"])[0]
        S.CACHE["src"] = os.path.getmtime(args.hpms)
        nu = S.cached(f"hpms_{level}", S.assign, U, H["pt_lon"], H["pt_lat"], neighbours=False)[0]
        levels[level] = (U, cu, nu)
        P(f"  {level}: {int((cu >= 0).sum()):,} cameras and {int((nu >= 0).sum()):,} road points in units  "
          f"[{time.time() - t0:.0f}s]")

    # --- descriptive: which cameras stand on counted roads, and how busy those roads are -----------
    _, cu, nu = levels["tract"]
    inc, inn = cu >= 0, nu >= 0
    P("\nWHICH CAMERAS, AND HOW BUSY THEIR ROADS ARE")
    on = inc & (cam_f > 0)
    P(f"  {on.sum():,} of {inc.sum():,} cameras ({100 * on.sum() / inc.sum():.1f}%) watch a counted road "
      f"(within {args.watch:.0f} m of a system 1-5 section)")
    for g, name in enumerate(S.GROUPS):
        m = inc & (cams["group"] == g)
        P(f"    of the {m.sum():,} cameras siting.py puts on {name} OSM roads: {100 * (m & on).sum() / max(m.sum(), 1):5.1f}%")
    for g, name in enumerate(FGROUPS):
        P(f"    {name:32s} {int((on & (cam_fg == g)).sum()):7,} cameras   "
          f"{w_km[inn & (pt_fg == g)].sum():9,.0f} km   {w_vkt[inn & (pt_fg == g)].sum():6,.0f} M veh-km/day")

    def wmedian(x, w):
        o = np.argsort(x); c = np.cumsum(w[o]); return x[o][np.searchsorted(c, c[-1] / 2)]

    ca = cam_aadt[on]
    P(f"\n  AADT of the road a camera watches: median {np.median(ca):,.0f} (IQR {np.percentile(ca, 25):,.0f}-"
      f"{np.percentile(ca, 75):,.0f})")
    P(f"  AADT of the median kilometre of counted road: {wmedian(aadt[inn], w_km[inn]):,.0f}; "
      f"of the median vehicle-kilometre: {wmedian(aadt[inn], w_vkt[inn]):,.0f}")
    P("\n  Cameras by the traffic on their road (crude national rates; surface roads, F3-5):")
    P(f"  {'AADT':>16s} {'cameras':>8s} {'km':>9s} {'per 100 km':>11s} {'per M veh-km/day':>17s}")
    sc = on & (cam_f >= 3)
    sn = inn & (pf >= 3)
    for lo, hi in zip(BANDS[:-1], BANDS[1:]):
        mc = sc & (cam_aadt >= lo) & (cam_aadt < hi)
        mn = sn & (aadt >= lo) & (aadt < hi)
        km, vk = w_km[mn].sum(), w_vkt[mn].sum()
        lab = f"{lo:,.0f}-{hi:,.0f}" if np.isfinite(hi) else f"{lo:,.0f}+"
        P(f"  {lab:>16s} {int(mc.sum()):8,} {km:9,.0f} {100 * mc.sum() / km:11.2f} {mc.sum() / vk:17.2f}")

    # --- siting gradients per road-km and per vehicle-km --------------------------------------------
    csv_rows = []
    for level, (U, cu, nu) in levels.items():
        pop, ucix, Q, strata = schemes(U, bt)
        mc0, mn0 = (cu >= 0) & (cam_f > 0), nu >= 0
        P("\n" + "=" * 78)
        P(f"{ {'tract': 'TRACTS', 'bg': 'BLOCK GROUPS'}[level] }  [{time.time() - t0:.0f}s]")
        P("=" * 78)
        for base, bname in (("all", "all counted roads (F1-5)"), ("surface", "surface roads (F3-5)")):
            mc, mn = mc0 & base_c[base], mn0 & base_n[base]
            P(f"\n--- {bname}: {int(mc.sum()):,} cameras ---")
            S.rates_by_quartile(P, bt, ucix, strata, Q, pop, cu, mc, nu, mn, "road",
                                "cameras per 100 km of road", csv_rows, f"{level}:hpms-{base}:road",
                                null_w=w_km, scale=100.0, per_label="road-km")
            S.rates_by_quartile(P, bt, ucix, strata, Q, pop, cu, mc, nu, mn, "road",
                                "cameras per million vehicle-km a day", csv_rows, f"{level}:hpms-{base}:traffic",
                                null_w=w_vkt, scale=1.0, per_label="vehicle-km")

            # like roads with like: strata also split by functional class
            P("\n  stratified also by functional class (Mantel-Haenszel, vs Q1)")
            P(f"  {'':24s} {'scheme':16s} {'per':11s} {'Q2':>6s} {'Q3':>6s} {'Q4':>6s}   Q4/Q1 [95% CI]")
            for a in ("income", "black", "hispanic"):
                for sch in ("within county", "county x density"):
                    q = Q[(a, sch)].astype(int)
                    u2s, s2c = strata[sch]
                    cc, nn = np.flatnonzero(mc), np.flatnonzero(mn)
                    kc = u2s[cu[cc]] * 4 + cam_fg[cc]
                    kn = u2s[nu[nn]] * 4 + pt_fg[nn]
                    for per, w in (("road-km", w_km), ("vehicle-km", w_vkt)):
                        C = np.zeros((len(s2c) * 4, 5)); N = np.zeros((len(s2c) * 4, 5))
                        np.add.at(C, (kc, q[cu[cc]]), 1)
                        np.add.at(N, (kn, q[nu[nn]]), w[nn])
                        pts, reps = S.mh(C, N, np.repeat(s2c, 4), bt)
                        lo, hi = S.ci(reps[:, 2])
                        P(f"  {S.LABEL[a]:24s} {sch:16s} {per:11s} " + " ".join(f"{v:6.2f}" for v in pts)
                          + f"   {S.fmt_ratio(float(pts[2]), lo, hi)}")
                        csv_rows.append({"analysis": f"{level}:hpms-{base}-byclass:{'road' if per == 'road-km' else 'traffic'}",
                                         "attribute": a, "scheme": sch, "per": per, "q1": 1.0,
                                         **{f"q{i + 2}": round(float(v), 4) for i, v in enumerate(pts)},
                                         "q4_over_q1": round(float(pts[2]), 4), "lo": round(float(lo), 4),
                                         "hi": round(float(hi), 4)})

            # how much busier each quartile's roads are, like for like
            P("\n  traffic per kilometre of road (vehicle-km over road-km, Mantel-Haenszel by stratum x class, vs Q1)")
            for a in ("income", "black", "hispanic"):
                for sch in ("within county", "county x density"):
                    q = Q[(a, sch)].astype(int)
                    u2s, s2c = strata[sch]
                    nn = np.flatnonzero(mn)
                    kn = u2s[nu[nn]] * 4 + pt_fg[nn]
                    V = np.zeros((len(s2c) * 4, 5)); K = np.zeros((len(s2c) * 4, 5))
                    np.add.at(V, (kn, q[nu[nn]]), w_vkt[nn])
                    np.add.at(K, (kn, q[nu[nn]]), w_km[nn])
                    pts, reps = S.mh(V, K, np.repeat(s2c, 4), bt)
                    lo, hi = S.ci(reps[:, 2])
                    P(f"  {S.LABEL[a]:24s} {sch:16s} Q2..Q4 " + " ".join(f"{v:6.2f}" for v in pts)
                      + f"   {S.fmt_ratio(float(pts[2]), lo, hi)}")
                    csv_rows.append({"analysis": f"{level}:hpms-{base}:aadt-per-km", "attribute": a, "scheme": sch,
                                     "per": "vehicle-km per road-km", "q1": 1.0,
                                     **{f"q{i + 2}": round(float(v), 4) for i, v in enumerate(pts)},
                                     "q4_over_q1": round(float(pts[2]), 4), "lo": round(float(lo), 4),
                                     "hi": round(float(hi), 4)})

            if level == "tract":
                pol = mc & (cams["op"] == "police")
                S.rates_by_quartile(P, bt, ucix, strata, Q, pop, cu, pol, nu, mn, "road",
                                    f"police-operated cameras per million vehicle-km a day  (n={int(pol.sum()):,})",
                                    csv_rows, f"{level}:hpms-{base}:op=police:traffic",
                                    null_w=w_vkt, scale=1.0, per_label="vehicle-km")

    open(args.report, "w").write("\n".join(L) + "\n")
    out = os.path.join(args.tables, f"siting_traffic{args.tag}.csv")
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["analysis", "attribute", "scheme", "per", "q1", "q2", "q3", "q4",
                                           "q4_over_q1", "lo", "hi"])
        w.writeheader(); w.writerows(csv_rows)
    print(f"wrote {args.report} and {out}  [{time.time() - t0:.0f}s]")


if __name__ == "__main__":
    main()
