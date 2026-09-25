#!/usr/bin/env python3
"""
How complete is the crowdsourced camera map? An external check against Flock Safety's own
transparency portals, which many police departments publish and which state how many cameras the
department operates.

    python validate_coverage.py --portals data/validation/transparency_portals_2026-09-24.json \\
        --tiles ../server/alpr/region_cache -o out/analysis_coverage.txt --csv out/siting/coverage.csv

For each municipal police department (portal type PD) whose city matches exactly one incorporated
Census place in its state, the mapped Flock cameras inside the city limits are compared with the
count the department reports. The comparison is approximate in both directions: a department's
cameras can stand outside its limits (a shared corridor, a partner's land) and OSM counts every
Flock camera inside the limits, including those an HOA, a business or another agency owns. So a
ratio near 1 is consistent with a complete map and a ratio well below 1 marks under-mapping; the
question that matters for the paper is whether the ratio varies with who lives in the city.

Portal counts are aggregated by eyesonflock.com from transparency.flocksafety.com.
"""
import argparse
import csv
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


def norm(name):
    n = name.lower().strip()
    n = re.sub(r"^(city|town|village|borough|township) of ", "", n)
    n = n.replace("saint ", "st ").replace("st. ", "st ").replace("ste. ", "ste ")
    n = re.sub(r"[^a-z0-9 ]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def spearman(a, b):
    ra = np.argsort(np.argsort(a)); rb = np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--portals", required=True)
    ap.add_argument("--tiles", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--csv", required=True)
    args = ap.parse_args()

    data = json.load(open(args.portals))
    portals = [p for p in data["portals"] if p.get("type") == "PD" and p.get("total_cameras")
               and p.get("city") and p.get("state")]

    UP = geo.load_units("place")
    inc = np.char.startswith(UP["classfp"].astype(str), "C")
    UP = {k: v[inc] for k, v in UP.items()}
    st_abbr = np.array([geo.ABBR[s].upper() for s in UP["state"]])
    index = {}
    for i, (nm, st) in enumerate(zip(UP["name"], st_abbr)):
        index.setdefault((norm(nm), st), []).append(i)

    nodes = {}
    for p in glob.glob(os.path.join(args.tiles, "tile_*.json")):
        for e in json.load(open(p)).get("elements", ()):
            if e.get("type") == "node":
                nodes[e["id"]] = e
    flock = [e for e in nodes.values() if "flock" in (vendor_of(e.get("tags") or {}) or "").lower()]
    lon = np.array([e["lon"] for e in flock]); lat = np.array([e["lat"] for e in flock])
    unit = S.assign(UP, lon, lat, neighbours=False)[0]
    per_place = np.bincount(unit[unit >= 0], minlength=len(UP["geoid"]))

    pop = UP["pop_total"]
    safe = np.where(pop > 0, pop, 1)
    black = np.where(pop > 0, 100 * UP["nh_black"] / safe, np.nan)
    hisp = np.where(pop > 0, 100 * UP["hispanic"] / safe, np.nan)
    rows, unmatched, ambiguous = [], 0, 0
    for p in portals:
        hits = index.get((norm(p["city"]), p["state"].upper()), [])
        if not hits:
            unmatched += 1; continue
        if len(hits) > 1:
            ambiguous += 1; continue
        i = hits[0]
        rows.append({"city": UP["name"][i], "state": p["state"], "geoid": UP["geoid"][i],
                     "portal_cameras": int(p["total_cameras"]), "osm_flock_in_limits": int(per_place[i]),
                     "ratio": per_place[i] / int(p["total_cameras"]),
                     "population": pop[i], "median_income": UP["median_income"][i],
                     "pct_black": black[i], "pct_hispanic": hisp[i],
                     "portal_updated": (p.get("data_last_updated") or "")[:10]})

    L = []
    P = L.append
    P("=" * 78)
    P("COVERAGE CHECK: mapped Flock cameras vs the count each police department reports")
    P("=" * 78)
    P(f"{len(data['portals']):,} transparency portals; {len(portals):,} municipal police departments report a camera count;")
    P(f"{len(rows):,} matched to exactly one incorporated place ({unmatched} unmatched names, {ambiguous} ambiguous).")
    P(f"{len(flock):,} mapped Flock cameras in the snapshot.\n")
    r = np.array([x["ratio"] for x in rows])
    pc = np.array([x["portal_cameras"] for x in rows]); oc = np.array([x["osm_flock_in_limits"] for x in rows])
    P(f"  cameras reported by the matched departments: {pc.sum():,}; mapped inside their limits: {oc.sum():,} "
      f"(ratio of totals {oc.sum() / pc.sum():.2f})")
    P(f"  per city, mapped/reported: median {np.median(r):.2f}, IQR {np.percentile(r, 25):.2f}-{np.percentile(r, 75):.2f}")
    for lo, hi, lab in ((0, 0.25, "< 0.25 (badly under-mapped)"), (0.25, 0.5, "0.25-0.5"), (0.5, 0.8, "0.5-0.8"),
                        (0.8, 1.25, "0.8-1.25 (about complete)"), (1.25, 1e9, "> 1.25 (more mapped than the PD reports)")):
        m = (r >= lo) & (r < hi)
        P(f"    {lab:42s} {m.sum():5d} cities ({100 * m.mean():4.1f}%)")
    P("\n  Does coverage vary with who lives in the city? (Spearman rank correlation with mapped/reported,")
    P("  and median ratio by quartile of the attribute across matched cities)")
    for key, lab in (("median_income", "median household income"), ("pct_black", "% non-Hispanic Black"),
                     ("pct_hispanic", "% Hispanic"), ("population", "population")):
        x = np.array([row[key] for row in rows], float)
        ok = np.isfinite(x)
        rho = spearman(x[ok], r[ok])
        q = np.quantile(x[ok], [0.25, 0.5, 0.75])
        b = 1 + (x[ok] > q[0]).astype(int) + (x[ok] > q[1]) + (x[ok] > q[2])
        med = [np.median(r[ok][b == k]) for k in (1, 2, 3, 4)]
        # bootstrap the rank correlation over cities
        rng = np.random.default_rng(S.SEED)
        reps = []
        xi, ri = x[ok], r[ok]
        for _ in range(2000):
            j = rng.integers(0, len(xi), len(xi))
            reps.append(spearman(xi[j], ri[j]))
        lo_, hi_ = np.percentile(reps, [2.5, 97.5])
        P(f"    {lab:26s} rho {rho:+.3f} [{lo_:+.3f} to {hi_:+.3f}]   median ratio Q1..Q4: " + " ".join(f"{v:.2f}" for v in med))
    open(args.out, "w").write("\n".join(L) + "\n")
    print("\n".join(L))
    with open(args.csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)


if __name__ == "__main__":
    main()
