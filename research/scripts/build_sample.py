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
import gzip
import os
import random
import sys
from collections import defaultdict

DATA = os.path.join(os.path.dirname(__file__), "..", "data")


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
    path = os.path.join(DATA, "lodes", f"{state}_od_main_JT00_2022.csv.gz")
    if not os.path.exists(path):
        return []
    flows = defaultdict(int)
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
            if s < min_workers or h == w:
                # Same-tract commutes are dropped: origin and destination centroids
                # coincide, so there is no route to measure.
                continue
            if h in centroids and w in centroids:
                flows[(h, w)] += s
    if not flows:
        return []
    pairs = list(flows.keys())
    weights = [flows[p] for p in pairs]
    total = sum(weights)
    picked = rng.choices(pairs, weights=weights, k=min(n, len(pairs) * 3))
    seen, out = set(), []
    for h, w in picked:
        if (h, w) in seen:
            continue
        seen.add((h, w))
        out.append({"state": state, "h_tract": h, "w_tract": w,
                    "workers": flows[(h, w)], "flow_share": flows[(h, w)] / total})
        if len(out) >= n:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", nargs="+", required=True)
    ap.add_argument("--per-state", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=20260725)
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

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

    rows = []
    for st in args.states:
        s = sample_commutes(st, args.per_state, centroids, rng)
        print(f"  {st}: {len(s)} commutes", flush=True)
        rows.extend(s)

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
