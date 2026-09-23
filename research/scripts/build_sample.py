#!/usr/bin/env python3
"""
Build a commute-weighted origin-destination sample for the ALPR exposure study.

Sampling design matters more than sample size here. Uniform random points on a map
would over-represent empty rural space, where nobody commutes and no cameras exist,
and would understate exposure badly. Instead we sample real home-work pairs from the
Census LEHD LODES origin-destination file, with each pair's probability proportional to
the number of workers actually making that trip. The resulting distribution is what an
average commuter experiences, not what an average square kilometre contains.

Outputs one row per sampled commute with origin/destination tract centroids and the
origin tract's demographics attached, ready for routing.
"""
import argparse
import csv
import glob
import gzip
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict

DATA = os.path.join(os.path.dirname(__file__), "..", "data")

# `--states all` in a FIXED order, and the order is load-bearing: the whole sample comes
# from one seeded random stream consumed state by state, so a state's draw depends on
# every state before it. The 15 states of the original study come first, in their
# original order, which makes the 2026-07 sample an exact prefix of the 51-state one —
# those commutes keep their results, and only the 36 states appended after them are new.
ORIGINAL_15 = ["ga", "tx", "ca", "fl", "il", "ny", "pa", "oh", "nc", "az", "wa", "co", "tn", "mo", "va"]
ALL_51 = ORIGINAL_15 + sorted(
    set("al ak az ar ca co ct de dc fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne "
        "nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy".split())
    - set(ORIGINAL_15))


def lodes_file(state):
    """The state's od_main file on disk, latest year first. LODES has not published 2022
    for every state (Michigan stops at 2021, Alaska at 2016); data/fetch.sh falls back to
    the latest year it can get, and this picks up whatever it fetched."""
    files = sorted(glob.glob(os.path.join(DATA, "lodes", f"{state}_od_main_JT00_*.csv.gz")))
    return files[-1] if files else None


def load_tract_centroids():
    """GEOID -> (lat, lon) from the Census Gazetteer."""
    path = os.path.join(DATA, "2024_Gaz_tracts_national.txt")
    out = {}
    with open(path, encoding="latin-1") as fh:
        r = csv.DictReader(fh, delimiter="\t")
        for row in r:
            k = {c.strip(): c for c in row}
            try:
                geoid = row[k["GEOID"]].strip()
                lat = float(row[k["INTPTLAT"]].strip())
                lon = float(row[k["INTPTLONG"]].strip())
            except (KeyError, ValueError):
                continue
            out[geoid] = (lat, lon)
    return out


def load_acs(filename, cols):
    """
    GEOID -> {name: value} from an ACS table-based summary file.

    GEO_ID looks like 1400000US13121001100; the 1400000US prefix denotes summary level
    140 (census tract), so filtering on it is how we drop state/county/national rows.
    """
    path = os.path.join(DATA, filename)
    out = {}
    with open(path, encoding="latin-1") as fh:
        header = fh.readline().rstrip("\n").split("|")
        idx = {c: i for i, c in enumerate(header)}
        want = {name: idx[c] for name, c in cols.items() if c in idx}
        if len(want) != len(cols):
            missing = set(cols.values()) - set(header)
            sys.exit(f"{filename}: missing columns {missing}")
        for line in fh:
            parts = line.rstrip("\n").split("|")
            gid = parts[0]
            if not gid.startswith("1400000US"):
                continue
            rec = {}
            for name, i in want.items():
                try:
                    rec[name] = float(parts[i])
                except (ValueError, IndexError):
                    rec[name] = None
            out[gid[len("1400000US"):]] = rec
    return out


def sample_commutes(state, n, centroids, rng, min_workers=1):
    """
    Draw n home-work pairs from LODES, weighted by worker count.

    LODES is block-level; we aggregate to tract because that is the finest resolution at
    which ACS demographics are reliable, and because block centroids would imply a
    spatial precision the routing does not have.
    """
    path = lodes_file(state)
    if path is None:
        return [], None
    # Connecticut replaced its counties with planning regions in 2022, and every CT tract
    # GEOID changed with them (09001xxxxxx -> 09110xxxxxx). LODES8 still names tracts by
    # the old counties while the 2024 Gazetteer and ACS 2022 use the new ones, so without
    # this nothing matches and Connecticut silently drops out of the sample. The tracts
    # were reassigned, not redrawn: each 6-digit tract code maps to exactly one new GEOID
    # (all 879 in the 2022 flows do), so the crosswalk is that lookup.
    recode = {}
    if state == "ct":
        by_code = defaultdict(list)
        for g in centroids:
            if g.startswith("09"):
                by_code[g[5:]].append(g)
        recode = {code: gs[0] for code, gs in by_code.items() if len(gs) == 1}
    flows = defaultdict(int)
    raw_pairs = unmatched = 0
    with gzip.open(path, "rt") as fh:
        r = csv.reader(fh)
        header = next(r)
        try:
            i_h, i_w, i_s = header.index("h_geocode"), header.index("w_geocode"), header.index("S000")
        except ValueError:
            return []
        for row in r:
            try:
                # block geocode -> tract = first 11 chars
                h, w, s = row[i_h][:11], row[i_w][:11], int(row[i_s])
            except (ValueError, IndexError):
                continue
            if recode:
                h, w = recode.get(h[5:], h), recode.get(w[5:], w)
            if s < min_workers or h == w:
                # Same-tract commutes are dropped: origin and destination centroids
                # coincide, so there is no route to measure.
                continue
            raw_pairs += 1
            if h in centroids and w in centroids:
                flows[(h, w)] += s
            else:
                unmatched += 1
    if not flows:
        return [], None
    pairs = list(flows.keys())
    weights = [flows[p] for p in pairs]
    total = sum(weights)
    picked = rng.choices(pairs, weights=weights, k=min(n, len(pairs) * 3))
    # A pair drawn more than once is routed once — the route would be identical — but it
    # stands for every draw, so the draw count is kept as its analysis weight. Keeping one
    # copy with weight 1 would under-represent exactly the heaviest flows, and in a small
    # state that is not a rounding matter: Wyoming's 1,200 draws hold 824 distinct pairs.
    draws = Counter(picked)
    seen, out = set(), []
    for h, w in picked:
        if (h, w) in seen:
            continue
        seen.add((h, w))
        out.append({"state": state, "h_tract": h, "w_tract": w,
                    "workers": flows[(h, w)], "flow_share": flows[(h, w)] / total,
                    "draws": draws[(h, w)]})
        if len(out) >= n:
            break
    # The sampling frame, recorded for weighting. Every state gets the same n, so a
    # national estimate must weight each state's commutes by how many workers that
    # state's frame holds; otherwise Wyoming counts as much as California.
    frame = {"workers": total, "pairs": len(pairs), "lodes_year": int(re.search(r"_(\d{4})\.csv", path).group(1)),
             "block_pairs_unmatched_to_tracts": unmatched, "block_pairs": raw_pairs}
    return out, frame


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", nargs="+", required=True,
                    help="state codes, or 'all' for the 51 in the fixed study order")
    ap.add_argument("--per-state", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=20260725)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--frame", default=None,
                    help="write per-state sampling-frame totals (the weights) as JSON here")
    ap.add_argument("--draws", default=None,
                    help="write each sampled pair's draw count (its within-state weight) as CSV here")
    args = ap.parse_args()
    if args.states == ["all"]:
        args.states = ALL_51

    rng = random.Random(args.seed)          # fixed seed: the sample is reproducible

    print("loading tract centroids...", flush=True)
    centroids = load_tract_centroids()
    print(f"  {len(centroids)} tracts", flush=True)

    print("loading ACS...", flush=True)
    inc = load_acs("acs_b19013.dat", {"median_income": "B19013_E001"})
    race = load_acs("acs_b03002.dat", {
        "pop_total": "B03002_E001",
        "nh_white": "B03002_E003",
        "nh_black": "B03002_E004",
        "hispanic": "B03002_E012",
    })
    print(f"  income {len(inc)} tracts, race {len(race)} tracts", flush=True)

    rows, frames = [], {}
    for st in args.states:
        s, frame = sample_commutes(st, args.per_state, centroids, rng)
        if frame:
            frames[st] = frame
            miss = frame["block_pairs_unmatched_to_tracts"] / max(frame["block_pairs"], 1)
            note = f"  LODES {frame['lodes_year']}" if frame["lodes_year"] != 2022 else ""
            note += f"  !! {100*miss:.1f}% of flows name tracts the Gazetteer lacks" if miss > 0.01 else ""
        else:
            note = "  !! no LODES file"
        print(f"  {st}: {len(s)} commutes{note}", flush=True)
        rows.extend(s)
    if args.frame:
        with open(args.frame, "w") as fh:
            json.dump(frames, fh, indent=1, sort_keys=True)
    if args.draws:
        with open(args.draws, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["state", "h_tract", "w_tract", "draws"])
            for r in rows:
                w.writerow([r["state"], r["h_tract"], r["w_tract"], r["draws"]])

    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["state", "h_tract", "w_tract", "workers", "flow_share",
                    "h_lat", "h_lon", "w_lat", "w_lon",
                    "median_income", "pop_total", "nh_white", "nh_black", "hispanic"])
        for r in rows:
            hl = centroids[r["h_tract"]]
            wl = centroids[r["w_tract"]]
            i = inc.get(r["h_tract"], {})
            rc = race.get(r["h_tract"], {})
            w.writerow([r["state"], r["h_tract"], r["w_tract"], r["workers"],
                        f"{r['flow_share']:.10f}",
                        hl[0], hl[1], wl[0], wl[1],
                        i.get("median_income", ""), rc.get("pop_total", ""),
                        rc.get("nh_white", ""), rc.get("nh_black", ""), rc.get("hispanic", "")])
    print(f"wrote {len(rows)} commutes -> {args.out}")


if __name__ == "__main__":
    main()
