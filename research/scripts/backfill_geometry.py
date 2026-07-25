#!/usr/bin/env python3
"""
Re-route commutes that have results but no saved geometry, and append their polylines.

The experiment began before run_experiment.py persisted route geometry, so the first
2,537 rows of results.csv have counts but no polyline. RQ5 (per-vendor exposure) and the
cone-radius sweep of paper section 7.3 both work by re-scoring saved geometry, so those
rows would silently drop out of every geometry-based analysis — producing a result over a
subset while appearing to cover the whole sample.

This routes only the missing keys and writes to the geometry sidecar alone; results.csv is
never touched. Requests are built by importing run_experiment.route, so the geometry is
constructed identically to the main run rather than by a parallel implementation that
could drift.

    python3 backfill_geometry.py --results out/results.csv --geometry out/routes.jsonl.gz
"""
import argparse
import gzip
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from routes_io import read_routes          # noqa: E402
from run_experiment import route           # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--geometry", required=True)
    ap.add_argument("--url", default="http://localhost:8989/route")
    ap.add_argument("--multiply", default="0.01")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import csv
    rows = list(csv.DictReader(open(args.results)))
    have = set()
    if os.path.exists(args.geometry):
        have = {(r["state"], r["h_tract"], r["w_tract"])
                for r in read_routes(args.geometry)}

    missing = [r for r in rows
               if (r["state"], r["h_tract"], r["w_tract"]) not in have]
    print(f"results {len(rows)}, geometry {len(have)}, missing {len(missing)}")
    if args.dry_run or not missing:
        return

    lock = threading.Lock()
    gz = gzip.open(args.geometry, "at")
    done = [0, 0]

    def work(r):
        try:
            frm = (float(r["h_lat"]), float(r["h_lon"]))
            to = (float(r["w_lat"]), float(r["w_lon"]))
            base = route(args.url, frm, to, None)
            avoid = route(args.url, frm, to, args.multiply)
        except Exception:
            return None
        if base is None or avoid is None:
            return None
        bp, _ = base
        ap_, _ = avoid
        return {
            "state": r["state"], "h_tract": r["h_tract"], "w_tract": r["w_tract"],
            "base": [[round(x, 5), round(y, 5)] for x, y in bp["points"]["coordinates"]],
            "avoid": [[round(x, 5), round(y, 5)] for x, y in ap_["points"]["coordinates"]],
        }

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for geom in ex.map(work, missing):
                with lock:
                    done[0] += 1
                    if geom:
                        gz.write(json.dumps(geom, separators=(",", ":")) + "\n")
                        gz.flush()
                        done[1] += 1
                    if done[0] % 250 == 0:
                        print(f"  {done[0]}/{len(missing)} ({done[1]} ok)", flush=True)
    finally:
        gz.close()

    print(f"backfilled {done[1]}/{len(missing)}")
    if done[1] < len(missing):
        # A route that succeeded during the main run can fail here only transiently, so
        # unlike the main run these are worth a second pass — rerun the script.
        print(f"  {len(missing)-done[1]} still missing; re-run to retry")


if __name__ == "__main__":
    main()
