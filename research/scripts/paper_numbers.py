#!/usr/bin/env python3
"""
Figures the paper quotes that no other report prints, recomputed from their sources.

    ../server/.venv/bin/python scripts/paper_numbers.py -o out/analysis_paper_numbers.txt

Each section names the claim it backs. Inputs built by the pipeline and not redistributed: the
Overpass tiles of 2026-09-22 (server/alpr/region_cache) and 2026-07-24
(server/alpr/region_cache.2026-07-24), the monthly maps rebuilt from the edit history
(server/alpr/history_monthly) and the version list extracted from it
(server/alpr/osm-history/node_versions.jsonl), the registry's discs
(out/merged/registry_discs.json), the merged device list (out/supermap/alpr_supermap.json), the
map-alone graph's cone set (server/graphhopper/custom_areas/alpr.geojson), the stored routes
(out/routes.jsonl.gz) and the routing server's log of the main run
(server/graphhopper/logs/server-run.log). Tracked inputs: out/commutes.csv, sample_draws.csv,
sampling_frame.json, results.csv, merged/results.csv, vendor.csv, trend_by_state.csv and
by_state.csv. A section whose input is missing says so and is skipped.

Installations are placed in the 50 states and DC by the outline test the figures use
(trend.state_locator); a registry device by the state of the census tract the device list put
it in.
"""
import argparse
import csv
import glob
import gzip
import json
import os
import re
import sys
from collections import Counter, defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "..", "server")
ALPR = os.path.join(SERVER, "alpr")
OUT = os.path.join(ROOT, "out")
sys.path.insert(0, HERE)
sys.path.insert(0, ALPR)
from build_cones import DIRECTION_KEYS, parse_directions  # noqa: E402
from build_sample import ORIGINAL_15  # noqa: E402
from trend import FIPS, state_locator  # noqa: E402
from wstats import KEY, Design  # noqa: E402

VENDOR_KEYS = ("manufacturer", "surveillance:manufacturer", "brand", "surveillance:brand")
CLASSES = ("flock", "other", "untagged")
# what keeps an in-service registry reader out of the discs (cones_from_supermap.py)
EXCLUDE = {"shared_coordinate", "flock_internal", "possible_duplicate", "name_says_retired", "labelled_test"}
FRAME = os.path.join(OUT, "sampling_frame.json")
DRAWS = os.path.join(OUT, "sample_draws.csv")


def vendor(tags):
    """make_figures.py's rule: Flock if any vendor key names it, another maker if one names
    anything else, untagged if none is set."""
    v = next((tags[k] for k in VENDOR_KEYS if tags.get(k)), None)
    return "flock" if v and "flock" in v.lower() else ("other" if v else "untagged")


def heads(tags):
    """The bearings build_cones.py can parse: one cone per head; none means no cone."""
    raw = next((tags[k] for k in DIRECTION_KEYS if tags.get(k)), None)
    return len(parse_directions(raw)) if raw else 0


def nodes(tile_dir):
    """Every ALPR node in a directory of Overpass answers, merged by id."""
    seen = {}
    for p in sorted(glob.glob(os.path.join(tile_dir, "*.json"))):
        try:
            d = json.load(open(p))
        except ValueError:
            continue
        for e in d.get("elements", ()):
            if e.get("type") == "node" and e.get("lat") is not None:
                seen[e["id"]] = e
    return seen


class Located:
    """State of each node, cached by id and position: the monthly maps repeat most nodes."""
    def __init__(self):
        self.locate = state_locator()
        self.cache = {}

    def __call__(self, e):
        k = (e["id"], e["lat"], e["lon"])
        if k not in self.cache:
            self.cache[k] = self.locate(e["lat"], e["lon"])
        return self.cache[k]


def pct(a, b):
    return 100.0 * a / b if b else float("nan")


def avg_ranks(x):
    """Ranks with ties given their average rank, as Spearman's coefficient takes them."""
    x = np.asarray(x, float)
    order = np.argsort(x, kind="mergesort")
    r = np.empty(len(x))
    r[order] = np.arange(1, len(x) + 1)
    vals, inv, cnt = np.unique(x, return_inverse=True, return_counts=True)
    sums = np.zeros(len(vals))
    np.add.at(sums, inv, r)
    return (sums / cnt)[inv]


def spearman(a, b):
    return float(np.corrcoef(avg_ranks(a), avg_ranks(b))[0, 1])


def registry_devices(path):
    """Stream the device list (as cones_from_supermap.py does): one dict per device."""
    s = open(path).read()
    i = s.index('"devices":[') + len('"devices":[')
    dec = json.JSONDecoder()
    while s[i] != "]":
        d, i = dec.raw_decode(s, i)
        if s[i] == ",":
            i += 1
        yield d


def rows(path):
    return list(csv.DictReader(open(path, newline="")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", default=os.path.join(ALPR, "region_cache"))
    ap.add_argument("--july", default=os.path.join(ALPR, "region_cache.2026-07-24"))
    ap.add_argument("--monthly", default=os.path.join(ALPR, "history_monthly"))
    ap.add_argument("--versions", default=os.path.join(ALPR, "osm-history", "node_versions.jsonl"))
    ap.add_argument("--discs", default=os.path.join(OUT, "merged", "registry_discs.json"))
    ap.add_argument("--supermap", default=os.path.join(OUT, "supermap", "alpr_supermap.json"))
    ap.add_argument("--cones", default=os.path.join(SERVER, "graphhopper", "custom_areas", "alpr.geojson"),
                    help="the cone set the map-alone graph was imported with")
    ap.add_argument("--routes", default=os.path.join(OUT, "routes.jsonl.gz"))
    ap.add_argument("--server-log", default=os.path.join(SERVER, "graphhopper", "logs", "server-run.log"),
                    help="the routing server's log of the main run, for why commutes failed")
    ap.add_argument("-o", "--out", default=os.path.join(OUT, "analysis_paper_numbers.txt"))
    args = ap.parse_args()

    lines = []

    def P(s=""):
        print(s, flush=True)
        lines.append(s)

    def have(path, what):
        if os.path.exists(path):
            return True
        P(f"\n({what}: no {os.path.relpath(path, ROOT)}, so not computed)")
        return False

    loc = Located()
    P("FIGURES THE PAPER QUOTES THAT NO OTHER REPORT PRINTS  (scripts/paper_numbers.py)")
    P("(installations in the 50 states and DC unless a line says otherwise; inputs in the docstring)")

    # ------------------------------------------------------------------ the map today, by vendor
    now = nodes(args.now)
    us = {i: e for i, e in now.items() if loc(e)}
    cls = {i: vendor(e.get("tags") or {}) for i, e in us.items()}
    hd = {i: heads(e.get("tags") or {}) for i, e in us.items()}
    allc = Counter(cls.values())
    cone = Counter(c for i, c in cls.items() if hd[i] > 0)
    n_all, n_cone = sum(allc.values()), sum(cone.values())
    P(f"\nTHE MAP ON 2026-09-22, BY VENDOR  ({len(now):,} nodes in the Overpass regions; "
      f"{n_all:,} in the 50 states and DC)")
    for c in CLASSES:
        P(f"  {c:9s} all nodes {allc[c]:7,} ({pct(allc[c], n_all):4.1f}%)   "
          f"with a usable bearing {cone[c]:7,} ({pct(cone[c], n_cone):4.1f}%)")
    P(f"  {'total':9s} all nodes {n_all:7,}          with a usable bearing {n_cone:7,}")
    if have(args.discs, "the registry's share"):
        discs = json.load(open(args.discs))["discs"]
        reg = sum(1 for m in discs if (m.get("state") or "").lower() in FIPS)
        named = sum(1 for m in discs if m.get("watches"))
        P(f"  with the registry's {reg:,} readers in the 50 states and DC (of {len(discs):,} discs), Flock holds "
          f"{pct(allc['flock'] + reg, n_all + reg):.1f}% of all {n_all + reg:,} installations "
          f"({pct(cone['flock'] + reg, n_cone + reg):.1f}% of those that contribute a cone or disc)")
        P(f"  discs whose label names the traffic watched: {named:,} of {len(discs):,}")

    P("\nMULTI-HEAD UNITS  (more than one parseable bearing, among nodes with a usable bearing)")
    for c in CLASSES:
        multi = sum(1 for i, k in cls.items() if k == c and hd[i] > 1)
        P(f"  {c:9s} {multi:6,} of {cone[c]:7,} = {pct(multi, cone[c]):.1f}%")

    vrows = rows(os.path.join(OUT, "vendor.csv"))
    D = Design(vrows, FRAME, DRAWS)
    exp = {c: D.mean(np.array([float(r["base_" + c]) for r in vrows])) for c in CLASSES}
    tot = sum(exp.values())
    P("\nENCOUNTERS PER UNIT DEPLOYED, ON THE MAP ALONE  (out/vendor.csv, commuter-weighted)")
    reach = {}
    for c in CLASSES:
        reach[c] = (exp[c] / tot) / (cone[c] / n_cone)
        P(f"  {c:9s} share of exposure {pct(exp[c], tot):4.1f}%  share of cone-contributing installations "
          f"{pct(cone[c], n_cone):4.1f}%  encounters per unit, relative {reach[c]:.3f}")
    P(f"  other makers' cameras against Flock's, per unit deployed: {reach['other'] / reach['flock']:.2f} times")

    # ------------------------------------------------------------------ the map over time
    if have(args.monthly, "vendor shares over time"):
        P("\nVENDOR SHARES OF THE MAPPED NETWORK, MONTH BY MONTH  (server/alpr/history_monthly)")
        for dd in sorted(glob.glob(os.path.join(args.monthly, "*"))):
            if not os.path.isdir(dd):
                continue
            c = Counter(vendor(e.get("tags") or {}) for e in nodes(dd).values() if loc(e))
            n = sum(c.values())
            P(f"  {os.path.basename(dd)}  {n:7,}   flock {pct(c['flock'], n):4.1f}%   other {pct(c['other'], n):4.1f}%"
              f"   untagged {pct(c['untagged'], n):4.1f}%")
        P(f"  2026-09-22  {n_all:7,}   flock {pct(allc['flock'], n_all):4.1f}%   other {pct(allc['other'], n_all):4.1f}%"
          f"   untagged {pct(allc['untagged'], n_all):4.1f}%")

    if have(args.july, "cameras that left the map"):
        july = nodes(args.july)
        P("\nCAMERAS THAT LEFT THE MAP BETWEEN 2026-07-24 AND 2026-09-22  (a July node no longer an ALPR node)")
        for s in ("wi", "ia"):
            jn = {i for i, e in july.items() if loc(e) == s}
            sn = {i for i, e in us.items() if loc(e) == s}
            P(f"  {s.upper()}: {len(jn):,} in July, {len(sn):,} in September (net {len(sn) - len(jn):+,}); "
              f"{len(jn - sn):,} of July's left the map, {len(sn - jn):,} were added")

    dated_all = sum(1 for e in now.values() if (e.get("tags") or {}).get("start_date"))
    dated_us = sum(1 for e in us.values() if (e.get("tags") or {}).get("start_date"))
    P("\nINSTALLATION DATES  (OSM start_date, the date a mapper recorded the camera going up)")
    P(f"  {dated_all:,} of the {len(now):,} nodes in the Overpass regions; {dated_us:,} of the {n_all:,} "
      f"in the 50 states and DC")

    if have(args.versions, "the edit history's versions"):
        with open(args.versions) as f:
            nv = sum(1 for ln in f if ln.strip())
        P(f"\nTHE EDIT HISTORY: {nv:,} versions of ever-ALPR nodes  ({os.path.relpath(args.versions, ROOT)})")

    T = rows(os.path.join(OUT, "trend_by_state.csv"))
    today = {r["state"]: float(r["mean_cameras"]) for r in rows(os.path.join(OUT, "by_state.csv"))}
    P("\nSTATE RANKS: MEAN EXPOSURE ON AN EARLIER MAP AGAINST 2026-09-22's  (Spearman, tied ranks averaged)")
    for d in ("2024-01-01", "2025-01-01", "2026-01-01"):
        then = {r["state"]: float(r["mean_cameras"]) for r in T if r["date"] == d}
        S = sorted(set(then) & set(today))
        zeros = sum(1 for s in S if then[s] == 0)
        P(f"  {d}: rho = {spearman([then[s] for s in S], [today[s] for s in S]):.2f} over {len(S)} jurisdictions "
          f"({zeros} with no exposure then)")

    # ------------------------------------------------------------------ the registry
    if have(args.supermap, "the registry's counts"):
        c = Counter()
        for d in registry_devices(args.supermap):
            src, st = d["sources"], ((d.get("geo") or {}).get("state") or "").lower()
            inus = st in FIPS
            if "flock" in src:
                c["records"] += 1
                c["records_us"] += inus
            if src != ["flock"] or d.get("reads_plates") is not True:
                continue
            if d.get("status") == "planned":
                c["planned"] += 1
                c["planned_us"] += inus
            if d.get("status") != "in_service":
                continue
            c["unpaired"] += 1
            fl = set(d.get("flags") or ())
            if EXCLUDE & fl:
                c["dropped"] += 1
                c["dropped_shared"] += "shared_coordinate" in fl
                continue
            c["kept"] += 1
            c["kept_us"] += inus
            created = ((d.get("flock") or {}).get("created") or "")[:10]
            c["kept_bulk"] += created == "2024-03-26"
            c["kept_2025h2"] += "2025-07-01" <= created <= "2025-12-31"
        P("\nTHE REGISTRY  (out/supermap/alpr_supermap.json: its records in the 50 states, DC, Puerto Rico and the "
          "Virgin Islands)")
        P(f"  records {c['records']:,}, of which in the 50 states and DC {c['records_us']:,}")
        P(f"  in-service plate readers with no mapped node within pairing distance: {c['unpaired']:,}")
        P(f"    dropped {c['dropped']:,} (flagged {', '.join(sorted(EXCLUDE))}); "
          f"{c['dropped_shared']:,} of them at a shared coordinate")
        P(f"    kept as discs {c['kept']:,}: {c['kept_us']:,} in the 50 states and DC, "
          f"{c['kept'] - c['kept_us']:,} in Puerto Rico and the Virgin Islands")
        P(f"    of the kept, recorded on 2024-03-26 (the bulk import) {c['kept_bulk']:,}; "
          f"recorded in July-December 2025 {c['kept_2025h2']:,}")
        P(f"  plate readers with no mapped node, status planned: {c['planned']:,} "
          f"({c['planned_us']:,} in the 50 states and DC)")

    # ------------------------------------------------------------------ the sample
    com = rows(os.path.join(OUT, "commutes.csv"))
    draws = {tuple(r[k] for k in KEY): int(r["draws"]) for r in rows(DRAWS)}
    workers = np.array([float(r["workers"]) for r in com])
    P(f"\nTHE SAMPLE  (out/commutes.csv, out/sample_draws.csv)")
    P(f"  {len(com):,} distinct commutes from {sum(draws.values()):,} draws; they carry {workers.sum():,.0f} workers, "
      f"a median of {np.median(workers):.0f} and at most {workers.max():,.0f}")
    by = defaultdict(lambda: [0, 0, 0])            # draws, distinct, largest draw count
    for k, n in draws.items():
        b = by[k[0]]
        b[0] += n; b[1] += 1; b[2] = max(b[2], n)
    rep = {s: pct(b[0] - b[1], b[0]) for s, b in by.items()}
    big = [s for s in ORIGINAL_15]
    P(f"  repeat draws in the fifteen states of the earlier rounds: "
      f"{pct(sum(by[s][0] - by[s][1] for s in big), sum(by[s][0] for s in big)):.2f}%")
    for s in sorted(by, key=lambda s: -rep[s])[:8]:
        P(f"    {s.upper()}: {by[s][1]:,} distinct of {by[s][0]:,} draws, {rep[s]:.1f}% repeats, "
          f"one pair drawn {by[s][2]} times")
    s_max = max(by, key=lambda s: by[s][2])
    P(f"  the most-drawn pair: {by[s_max][2]} draws, in {s_max.upper()}")

    res = rows(os.path.join(OUT, "results.csv"))
    frame = json.load(open(FRAME))
    routed = Counter(r["state"] for r in res)
    sampled = Counter(r["state"] for r in com)
    drop = {s: pct(sampled[s] - routed[s], sampled[s]) for s in sampled}
    worst = sorted(drop, key=lambda s: -drop[s])[:5]
    W = sum(float(frame[s]["workers"]) for s in sampled)
    P(f"\nCOMMUTES THAT COULD NOT BE ROUTED  ({len(com) - len(res):,} of {len(com):,})")
    P("  by state, the five worst: " + ", ".join(f"{s.upper()} {drop[s]:.1f}%" for s in worst)
      + f"; median state {np.median(list(drop.values())):.1f}%")
    P(f"  those five hold {pct(sum(float(frame[s]['workers']) for s in worst), W):.1f}% of the commuters")
    if have(args.server_log, "why commutes failed"):
        why = Counter()
        for ln in open(args.server_log, errors="replace"):
            if "bad request" in ln:
                why["cannot snap a point to any road" if "Cannot find point" in ln else
                    "no connection between the points" if "Connection between locations not found" in ln else
                    "other"] += 1
        n = sum(why.values())
        P(f"  the routing server's refusals in the main run: {n:,}; " +
          "; ".join(f"{k} {v:,} ({pct(v, n):.0f}%)" for k, v in why.most_common()))

    wide, default = (os.path.join(os.path.dirname(args.server_log), f) for f in ("server-reroute.log", "server-check.log"))
    if have(wide, "the wider road search test") and have(default, "the wider road search test"):
        def answers(path):
            return [(round(float(m.group(1)), 1), m.group(2)) if m else None
                    for m in (re.search(r"distance0: ([0-9.]+).*?time0: ([^,]+)", ln)
                              for ln in open(path, errors="replace") if "POST /route" in ln)]
        a, b = answers(wide), answers(default)
        P(f"  a wider road search, tested on {len(a)} already-routed commutes on 2026-09-24: "
          f"{sum(1 for x, y in zip(a, b) if x != y)} routes differ from the default's "
          f"(server-reroute.log against server-check.log, the same requests)")

    P("\nWEIGHTING  (share passing >=1 camera in the fifteen states of the earlier rounds: each routed commute "
      "counted once, against commuter-weighted)")
    for name, path in (("map alone", os.path.join(OUT, "results.csv")),
                       ("merged map", os.path.join(OUT, "merged", "results.csv"))):
        R = rows(path)
        Dw = Design(R, FRAME, DRAWS)
        b = np.array([float(r["base_cameras"]) for r in R])
        m = np.isin(Dw.state, big)
        P(f"  {name:10s} unweighted {pct((b[m] >= 1).sum(), m.sum()):.1f}%   weighted {100 * Dw.share(b >= 1, m):.1f}%")

    # ------------------------------------------------------------------ stored geometry against the router
    if have(args.routes, "re-scoring against the router") and have(args.cones, "re-scoring against the router"):
        from shapely.geometry import LineString, shape
        from shapely.strtree import STRtree
        fc = json.load(open(args.cones))
        g = shape(fc["features"][0]["geometry"])
        parts = list(getattr(g, "geoms", [g]))
        tree = STRtree(parts)
        live = {tuple(r[k] for k in KEY): int(r["base_cameras"]) for r in res}
        rescored = {}
        with gzip.open(args.routes, "rt") as f:
            for ln in f:
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                k = tuple(rec[x] for x in KEY)
                if k in live and rec.get("base"):
                    rescored[k] = len(tree.query(LineString(rec["base"]), predicate="intersects"))
        P(f"\nSTORED GEOMETRY RE-SCORED AGAINST THE ROUTER'S OWN COUNT  ({len(parts):,} cone parts, "
          f"{len(rescored):,} stored routes)")
        for name, ks in (("fifteen states", [k for k in rescored if k[0] in big]), ("all", list(rescored))):
            dlt = np.array([rescored[k] - live[k] for k in ks])
            flips = sum(1 for k in ks if (rescored[k] > 0) != (live[k] > 0))
            tl, tr = sum(live[k] for k in ks), sum(rescored[k] for k in ks)
            P(f"  {name:14s} {len(ks):6,} commutes: {pct((dlt != 0).sum(), len(ks)):.2f}% differ "
              f"({pct((np.abs(dlt) == 1).sum(), len(ks)):.2f}% by one camera; {(dlt > 0).sum()} up, {(dlt < 0).sum()} down); "
              f"{flips} change whether they pass any; totals {tl:,} live, {tr:,} re-scored ({pct(tr - tl, tl):+.2f}%)")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
