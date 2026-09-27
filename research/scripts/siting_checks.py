#!/usr/bin/env python3
"""
Supporting checks quoted in the siting section of the paper, kept here so that each number has
a script and a saved output. Everything reuses siting.py's cached point-to-unit assignments.

    python siting_checks.py --roads out/siting/roads.npz --hpms out/siting/hpms.npz \\
        --tiles ../server/alpr/region_cache --tables out/siting -o out/analysis_siting_checks.txt

  A. Majority tracts. The plainest within-county comparison: majority-Black (or majority-
     Hispanic) tracts against majority-white tracts of the same county, in the counties that
     have both; Mantel-Haenszel rate ratio of cameras per road point of the same class,
     stratified by county, county bootstrap.
  B. Hampton Roads. The one region with an authoritative list of Flock locations (614, unsealed
     by a federal court in 2025, analysed by Keener et al.): how many Flock cameras our map
     holds there, and cameras per tract in majority-Black against majority-white tracts, the
     statistic that study reports.
  C. Freeways. Share of the counted (HPMS) road network that is Interstate or other freeway
     (functional system 1-2), by length and by traffic, in the highest quartile of each
     attribute over the lowest, Mantel-Haenszel over county x density strata.
  D. Who operates each vendor's cameras, and on what roads they stand.
  E. Business-operated cameras: who they are, and how they are tagged.
"""
import argparse
import glob
import json
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "server", "alpr"))
import geo  # noqa: E402
import siting as S  # noqa: E402
from build_cones import vendor_of  # noqa: E402
from siting_traffic import schemes  # noqa: E402

HAMPTON_ROADS = {"51550": "Chesapeake", "51650": "Hampton", "51700": "Newport News", "51710": "Norfolk",
                 "51735": "Poquoson", "51740": "Portsmouth", "51800": "Suffolk", "51810": "Virginia Beach",
                 "51830": "Williamsburg", "51620": "Franklin", "51073": "Gloucester", "51093": "Isle of Wight",
                 "51095": "James City", "51115": "Mathews", "51175": "Southampton", "51181": "Surry",
                 "51199": "York"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roads", required=True)
    ap.add_argument("--hpms", required=True)
    ap.add_argument("--tiles", required=True)
    ap.add_argument("--tables", required=True)
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()
    L = []

    def P(s=""):
        L.append(s); print(s, flush=True)

    cams, nulls, _ = S.load_points(args.roads, args.tiles)
    UT = geo.load_units("tract")
    S.CACHE["dir"], S.CACHE["src"] = args.tables, os.path.getmtime(args.roads)
    cu = S.cached("cam_tract", S.assign, UT, cams["lon"], cams["lat"])[0]
    nu = S.cached("null_tract", S.assign, UT, nulls["lon"], nulls["lat"])[0]
    bt = S.Boot(np.unique(UT["county"]))
    ucix = bt.index(UT["county"])
    pop = UT["pop_total"]
    share = lambda k: np.where(pop > 0, UT[k] / np.maximum(pop, 1), np.nan)
    pb, pw, ph = share("nh_black"), share("nh_white"), share("hispanic")
    cg, ng = cams["group"], nulls["group"]

    P("=" * 78); P("SUPPORTING CHECKS FOR THE SITING SECTION"); P("=" * 78)

    # --- A ---------------------------------------------------------------------------------
    P("\nA. MAJORITY TRACTS, WITHIN COUNTY (Mantel-Haenszel over counties that have both kinds;")
    P("   cameras per road point of the same class; 95% CI resampling counties within states)")

    def majority(kind_a, kind_b, group, label):
        t = np.zeros(len(pop), np.int64); t[kind_a] = 1; t[kind_b] = 2
        both = (np.bincount(ucix[t == 1], minlength=len(bt.cty)) > 0) & (np.bincount(ucix[t == 2], minlength=len(bt.cty)) > 0)
        okc = (cu >= 0) & ((cg == group) if group is not None else (cg >= 0))
        okn = (nu >= 0) & ((ng == group) if group is not None else np.ones(len(nu), bool))
        okc &= both[ucix[np.maximum(cu, 0)]] & (t[np.maximum(cu, 0)] > 0)
        okn &= both[ucix[np.maximum(nu, 0)]] & (t[np.maximum(nu, 0)] > 0)
        num = bt.table(ucix[cu[okc]], t[cu[okc]], ncols=3); den = bt.table(ucix[nu[okn]], t[nu[okn]], ncols=3)
        a, b, n1, n2 = num[:, 1], num[:, 2], den[:, 1], den[:, 2]
        N = n1 + n2
        ac, bc = a * n2 / np.maximum(N, 1), b * n1 / np.maximum(N, 1)
        pt = ac.sum() / bc.sum()
        lo, hi = S.ci(S.div(bt.W @ ac, bt.W @ bc))
        P(f"  {label:56s} {S.fmt_ratio(pt, lo, hi)}  ({int(both.sum())} counties; cameras {int(a.sum()):,} vs {int(b.sum()):,})")

    for g, gl in ((None, "all classes"), (0, "arterial"), (1, "residential")):
        majority(pb > 0.5, pw > 0.5, g, f"majority-Black vs majority-white tracts, {gl}")
    for g, gl in ((None, "all classes"), (0, "arterial"), (1, "residential")):
        majority(ph > 0.5, pw > 0.5, g, f"majority-Hispanic vs majority-white tracts, {gl}")

    # --- B ---------------------------------------------------------------------------------
    P("\nB. HAMPTON ROADS (17 Virginia localities)")
    m = np.isin(UT["county"], list(HAMPTON_ROADS))
    U = {k: v[m] for k, v in UT.items()}
    nodes = {}
    for p in glob.glob(os.path.join(args.tiles, "tile_*.json")):
        for e in json.load(open(p)).get("elements", ()):
            if e.get("type") == "node":
                nodes[e["id"]] = e
    ids = np.array(list(nodes))
    lon = np.array([nodes[i]["lon"] for i in ids]); lat = np.array([nodes[i]["lat"] for i in ids])
    box = (lat > 36.4) & (lat < 37.8) & (lon > -77.5) & (lon < -75.5)
    u = S.assign(U, lon[box], lat[box], neighbours=False)[0]
    flock = np.array(["flock" in (vendor_of(nodes[i].get("tags") or {}) or "").lower() for i in ids[box]])
    inside = u >= 0
    P(f"  {len(U['geoid'])} tracts; {int(inside.sum()):,} mapped ALPRs inside, {int((inside & flock).sum()):,} of them Flock"
      f"  (the court list: 614 Flock)")
    hp = U["pop_total"]
    hb = np.where(hp > 0, U["nh_black"] / np.maximum(hp, 1), np.nan)
    hw = np.where(hp > 0, U["nh_white"] / np.maximum(hp, 1), np.nan)
    for lab, sel in (("Flock", inside & flock), ("all ALPR", inside)):
        x = np.bincount(u[sel], minlength=len(U["geoid"]))
        mb, mw = hb > 0.5, hw > 0.5
        P(f"  {lab:8s} per tract: majority-Black {x[mb].mean():.2f} ({int(mb.sum())} tracts), majority-white "
          f"{x[mw].mean():.2f} ({int(mw.sum())} tracts), ratio {x[mb].mean() / x[mw].mean():.2f}")

    # --- C ---------------------------------------------------------------------------------
    P("\nC. FREEWAYS ON THE COUNTED NETWORK (HPMS functional system 1-2), Q4 over Q1, Mantel-Haenszel")
    P("   over strata: ratio of the freeway share of counted road length, and of counted traffic")
    H = np.load(args.hpms)
    S.CACHE["src"] = os.path.getmtime(args.hpms)
    hu = S.cached("hpms_tract", S.assign, UT, H["pt_lon"], H["pt_lat"], neighbours=False)[0]
    pf = H["pt_f"].astype(int); aadt = H["pt_aadt"].astype(float)
    fw = (pf <= 2).astype(float)
    _, _, Q, strata = schemes(UT, bt)
    ix = np.flatnonzero(hu >= 0)
    for a in ("black", "hispanic", "income"):
        for sch in ("within county", "county x density"):
            q = Q[(a, sch)].astype(int); u2s, s2c = strata[sch]
            out = []
            for numw, denw in ((fw[ix], np.ones(len(ix))), (fw[ix] * aadt[ix], aadt[ix])):
                C = np.zeros((len(s2c), 5)); N = np.zeros((len(s2c), 5))
                np.add.at(C, (u2s[hu[ix]], q[hu[ix]]), numw); np.add.at(N, (u2s[hu[ix]], q[hu[ix]]), denw)
                pts, reps = S.mh(C, N, s2c, bt)
                lo, hi = S.ci(reps[:, 2])
                out.append(S.fmt_ratio(float(pts[2]), lo, hi))
            crude = []
            for qq in (1, 4):
                k = ix[q[hu[ix]] == qq]
                crude.append(f"Q{qq} {100 * fw[k].mean():.1f}% of km, {100 * (fw[k] * aadt[k]).sum() / aadt[k].sum():.1f}% of traffic")
            P(f"  {S.LABEL[a]:24s} {sch:16s} length {out[0]}   traffic {out[1]}   ({'; '.join(crude)})")

    # --- D ---------------------------------------------------------------------------------
    P("\nD. OPERATORS AND ROADS BY VENDOR (cameras in the 50 states + DC)")
    inc = cu >= 0
    close = H["cam_d"] <= S.WATCH
    for v in ("flock", "other", "untagged"):
        mv = inc & (cams["vendor"] == v)
        known = mv & ~np.isin(cams["op"], ["untagged", "Flock (customer unknown)"])
        ops, cnt = np.unique(cams["op"][known], return_counts=True)
        cls = cams["cls"][mv]
        P(f"  {v}: {int(mv.sum()):,} cameras; operator identifiable for {int(known.sum()):,} ({100 * known.sum() / mv.sum():.1f}%): "
          + ", ".join(f"{o} {100 * c / known.sum():.1f}%" for o, c in sorted(zip(ops, cnt), key=lambda z: -z[1])))
        P(f"      OSM road watched: motorway {100 * (cls == 0).mean():.1f}%, trunk {100 * (cls == 1).mean():.1f}%, "
          f"primary {100 * (cls == 2).mean():.1f}%, secondary {100 * (cls == 3).mean():.1f}%, tertiary {100 * (cls == 4).mean():.1f}%;"
          f" within {S.WATCH:.0f} m of an HPMS freeway (F1-2) {100 * close[mv][:, :2].any(1).mean():.1f}%, "
          f"of a freeway or principal arterial (F1-3) {100 * close[mv][:, :3].any(1).mean():.1f}%")
    tags = {}
    for p in glob.glob(os.path.join(args.tiles, "tile_*.json")):
        for e in json.load(open(p)).get("elements", ()):
            if e.get("type") == "node":
                tags[e["id"]] = e.get("tags") or {}
    vn = np.array([(vendor_of(tags.get(int(i), {})) or "").strip().lower() for i in cams["id"]])
    mo = inc & (cams["vendor"] == "other")
    names, cnt = np.unique(vn[mo], return_counts=True)
    o = np.argsort(-cnt)[:8]
    P("  other vendors, by name: " + ", ".join(f"{names[i]} {cnt[i]:,}" for i in o))

    # --- E ---------------------------------------------------------------------------------
    P("\nE. BUSINESS-OPERATED CAMERAS")
    mb_ = inc & (cams["op"] == "business")
    opn = np.array([" ".join(x for x in (tags.get(int(i), {}).get("operator"), tags.get(int(i), {}).get("owner")) if x).strip()
                    for i in cams["id"][mb_]])
    canon = np.array([re.sub(r"[^a-z ]", "", s.lower()).replace("the ", "").strip() for s in opn])
    names, cnt = np.unique(canon, return_counts=True)
    o = np.argsort(-cnt)[:6]
    P(f"  {int(mb_.sum()):,} cameras; operators: " + ", ".join(f"{names[i]} {cnt[i]:,}" for i in o))
    zones, zc = np.unique(cams["zone"][mb_], return_counts=True)
    oz = np.argsort(-zc)[:5]
    P("  surveillance:zone tags: " + ", ".join(f"{zones[i] or '(none)'} {zc[i]:,}" for i in oz))

    # --- F ---------------------------------------------------------------------------------
    P("\nF. HOW FAR FROM PARITY: share of the 2,000 county-bootstrap replicates of each headline")
    P("   siting contrast (Q4/Q1, tracts) that fall on the far side of 1; 0 means p < 0.001")
    P(f"  {'contrast':62s} {'scheme':16s} {'ratio':>6s}  {'beyond 1':>9s}")
    pop_, ucix_, Q_, strata_ = schemes(UT, bt)
    mc_all = (cu >= 0) & (cg >= 0)
    mn_all = nu >= 0
    police = mc_all & (cams["op"] == "police")
    pt_fg = np.array([-1, 0, 0, 1, 2, 3])[pf]
    close_ = H["cam_d"] <= S.WATCH
    cam_f = np.where(close_.any(axis=1), np.argmax(close_, axis=1) + 1, 0)
    cam_fg = np.where(cam_f > 0, np.array([-1, 0, 0, 1, 2, 3])[cam_f], -1)
    w_vkt = aadt * float(H["spacing"]) / 1e9
    rows = [("arterial cameras per km of arterial", mc_all & (cg == 0), mn_all & (ng == 0), None, None),
            ("residential cameras per km of residential street", mc_all & (cg == 1), mn_all & (ng == 1), None, None),
            ("police-operated cameras per km of road", police, mn_all, None, None),
            ("counted roads, per vehicle-km, by class", (cu >= 0) & (cam_f > 0), hu >= 0, w_vkt, "hpms")]
    for label, cm, nm, w, kind in rows:
        for a in ("black", "hispanic", "income"):
            for sch in ("within county", "county x density"):
                q = Q_[(a, sch)].astype(int); u2s, s2c = strata_[sch]
                cc = np.flatnonzero(cm)
                if kind == "hpms":
                    nn = np.flatnonzero(nm)
                    kc = u2s[cu[cc]] * 4 + cam_fg[cc]; kn = u2s[hu[nn]] * 4 + pt_fg[nn]
                    C = np.zeros((len(s2c) * 4, 5)); N = np.zeros((len(s2c) * 4, 5))
                    np.add.at(C, (kc, q[cu[cc]]), 1); np.add.at(N, (kn, q[hu[nn]]), w[nn])
                    pts, reps = S.mh(C, N, np.repeat(s2c, 4), bt)
                else:
                    nn = np.flatnonzero(nm)
                    C = np.zeros((len(s2c), 5)); N = np.zeros((len(s2c), 5))
                    np.add.at(C, (u2s[cu[cc]], q[cu[cc]]), 1); np.add.at(N, (u2s[nu[nn]], q[nu[nn]]), 1)
                    pts, reps = S.mh(C, N, s2c, bt)
                r = reps[:, 2]; r = r[np.isfinite(r)]
                beyond = (r <= 1).mean() if pts[2] > 1 else (r >= 1).mean()
                short = {"black": "% Black", "hispanic": "% Hispanic", "income": "income"}[a]
                P(f"  {label + ', ' + short:62s} {sch:16s} {pts[2]:6.2f}  {100 * beyond:8.2f}%")

    open(args.out, "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
