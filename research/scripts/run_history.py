#!/usr/bin/env python3
"""
Route the study's commutes against a PAST camera snapshot, for the trend analysis.

    python3 run_history.py --area alpr_2025_07_01 \\
        --cones ../server/alpr/history/2025-07-01/alpr.geojson \\
        --results out/results.csv --routes out/routes.jsonl.gz \\
        -o out/history/2025-07-01/results.csv

Nothing about the unavoided route depends on the cameras. On the same road extract it is
identical to the metre in every snapshot (17,580 of 17,580 between July and September), so it
is not requested again: each commute's saved route is re-scored against the past date's cones.
Only commutes that pass at least one of THAT date's cameras need an avoiding route, and only
that one route — a route crossing no cone is already the avoiding route (verified on all 3,741
such commutes in the 15-state run). On a sparse 2024 snapshot that is a few percent of the
sample.

Avoiding routes are requested against the date's own area (`in_<area>`) from a routing graph
that carries every historical snapshot side by side, so one import serves all dates.

Rows use run_experiment.py's results schema, so analyze.py reads them unchanged: base_* are the
saved route scored against this date's cameras; avoid_* the new route, or the baseline where
there was nothing to avoid. Resumable: commutes already in the output are skipped.
"""
import argparse
import csv
import gzip
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from shapely.geometry import LineString
from shapely.strtree import STRtree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from routes_io import read_routes  # noqa: E402
from run_experiment import exposure, load_cones, route  # noqa: E402

KEY = ("state", "h_tract", "w_tract")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", required=True, help="the date's custom-area id in the graph, e.g. alpr_2025_07_01")
    ap.add_argument("--cones", required=True, help="the date's cone GeoJSON (for scoring)")
    ap.add_argument("--results", required=True, help="results.csv of the current-snapshot run (baselines)")
    ap.add_argument("--routes", required=True, help="its route-geometry sidecar")
    ap.add_argument("--url", default="http://localhost:8989/route")
    ap.add_argument("--multiply", default="0.01")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--geometry", default=None, help="write the avoiding routes that were requested here")
    args = ap.parse_args()

    cones = load_cones(args.cones)
    tree = STRtree(cones)
    print(f"{args.area}: {len(cones)} cone parts", flush=True)

    base = {tuple(r[k] for k in KEY): r for r in csv.DictReader(open(args.results, newline=""))}
    done = set()
    if os.path.exists(args.out):
        done = {tuple(r[k] for k in KEY) for r in csv.DictReader(open(args.out, newline=""))}
        print(f"resuming: {len(done)} commutes already in {args.out}", flush=True)

    # Score every saved baseline against this date's cameras; that alone settles every
    # commute that passes none of them.
    todo, rows = [], []
    for rec in read_routes(args.routes):
        k = tuple(rec[x] for x in KEY)
        r = base.get(k)
        if r is None or k in done:
            continue
        hits = exposure(LineString(rec["base"]), tree, cones)
        if hits == 0:
            rows.append(dict(r, base_cameras=0, avoid_km=r["base_km"], avoid_min=r["base_min"],
                             avoid_cameras=0, extra_km=0.0, extra_min=0.0, cameras_evaded=0))
        else:
            todo.append((r, hits))
    print(f"  {len(rows) + len(todo)} commutes to settle: {len(rows)} pass no camera on this date, "
          f"{len(todo)} need an avoiding route", flush=True)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    fh = open(args.out, "a" if done else "w", newline="")
    fields = list(next(iter(base.values())).keys())
    w = csv.DictWriter(fh, fieldnames=fields)
    if not done:
        w.writeheader()
    for r in rows:
        w.writerow(r)
    fh.flush()
    gz = gzip.open(args.geometry, "at") if args.geometry else None
    lock, n_ok, n_fail = threading.Lock(), [0], [0]

    def work(item):
        r, hits = item
        try:
            got = route(args.url, (float(r["h_lat"]), float(r["h_lon"])),
                        (float(r["w_lat"]), float(r["w_lon"])), args.multiply, area=args.area)
        except Exception:
            return None
        if got is None:
            return None
        p, line = got
        a_hits = exposure(line, tree, cones)
        rec = dict(r, base_cameras=hits,
                   avoid_km=round(p["distance"] / 1000, 4), avoid_min=round(p["time"] / 60000, 4),
                   avoid_cameras=a_hits,
                   extra_km=round(p["distance"] / 1000 - float(r["base_km"]), 4),
                   extra_min=round(p["time"] / 60000 - float(r["base_min"]), 4),
                   cameras_evaded=hits - a_hits)
        geom = {k: r[k] for k in KEY}
        geom["avoid"] = [[round(x, 5), round(y, 5)] for x, y in p["points"]["coordinates"]]
        return rec, geom

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for i, res in enumerate(ex.map(work, todo), 1):
                with lock:
                    if res is None:
                        n_fail[0] += 1
                    else:
                        w.writerow(res[0]); fh.flush(); n_ok[0] += 1
                        if gz is not None:
                            gz.write(json.dumps(res[1], separators=(",", ":")) + "\n"); gz.flush()
                    if i % 500 == 0:
                        print(f"  {i}/{len(todo)} avoiding routes ({n_fail[0]} failed)", flush=True)
    finally:
        fh.close()
        if gz is not None:
            gz.close()
    print(f"wrote {len(rows) + n_ok[0]} commutes -> {args.out} "
          f"({len(rows)} without cameras, {n_ok[0]} routed, {n_fail[0]} routing failures)", flush=True)


if __name__ == "__main__":
    main()
