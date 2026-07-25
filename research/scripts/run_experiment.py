#!/usr/bin/env python3
"""
Route every sampled commute twice — unavoided and avoided — and measure ALPR exposure.

For each commute we record:
  baseline route  : distance, time, cameras whose field of view it crosses
  avoided route   : same, with the ALPR custom model applied
  cost of avoidance: extra time and distance
  cameras evaded  : baseline exposure minus avoided exposure

Exposure is a directional test, not a proximity test. A camera counts only if the route
passes through its field-of-view wedge; a camera watching the opposite carriageway does
not see you. Counting by proximity alone would inflate every exposure figure and
overstate the paper's central claim, so the stricter test is used throughout.
"""
import argparse
import csv
import gzip
import json
import math
import os
import random
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from shapely.geometry import LineString, shape
from shapely.strtree import STRtree

CAPTURE_M = 40.0
HALF_SPAN_DEG = 22.5


def load_cones(path):
    fc = json.load(open(path))
    geom = shape(fc["features"][0]["geometry"])
    return list(getattr(geom, "geoms", [geom]))


def route(url, frm, to, multiply, timeout=120):
    body = {
        "points": [[frm[1], frm[0]], [to[1], to[0]]],
        "profile": "car",
        "ch.disable": True,
        "points_encoded": False,
    }
    if multiply is not None:
        body["custom_model"] = {"priority": [{"if": "in_alpr", "multiply_by": multiply}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    if "paths" not in d:
        return None
    p = d["paths"][0]
    return p, LineString(p["points"]["coordinates"])


def exposure(line, tree, cones):
    """Cameras whose cone the route line intersects. R-tree first, exact test second."""
    return sum(1 for i in tree.query(line) if cones[i].intersects(line))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commutes", required=True)
    ap.add_argument("--cones", required=True)
    ap.add_argument("--url", default="http://localhost:8989/route")
    ap.add_argument("--multiply", default="0.01", help="avoidance strength (MAXIMUM)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true",
                    help="skip commutes already present in --out and append to it")
    ap.add_argument("--shuffle", type=int, default=0, metavar="SEED",
                    help="process commutes in seeded random order (0 = file order)")
    ap.add_argument("--geometry", default=None, metavar="PATH",
                    help="also write route polylines to a gzipped JSONL sidecar")
    args = ap.parse_args()

    print("loading cones...", flush=True)
    cones = load_cones(args.cones)
    tree = STRtree(cones)
    print(f"  {len(cones)} cone parts", flush=True)

    rows = list(csv.DictReader(open(args.commutes)))
    if args.limit:
        rows = rows[:args.limit]
    print(f"routing {len(rows)} commutes x2 with {args.workers} workers...", flush=True)

    out_lock = threading.Lock()
    done = [0]
    results = []

    # Resume support. This job routes ~36k times and takes many hours; an earlier run
    # died at 10,500/17,904 and lost every one of them, because results were held in
    # memory and written only after the final row. Now each record is appended and
    # flushed as it completes, and a restart skips whatever is already on disk.
    # (state, h_tract, w_tract) uniquely identifies a sampled commute.
    def key_of(r):
        return (r["state"], r["h_tract"], r["w_tract"])

    already = set()
    if args.resume and os.path.exists(args.out):
        with open(args.out, newline="") as fh:
            for row in csv.DictReader(fh):
                already.add(key_of(row))
        if already:
            print(f"resuming: {len(already)} commutes already in {args.out}", flush=True)
        rows = [r for r in rows if key_of(r) not in already]
        print(f"  {len(rows)} remaining", flush=True)
        if not rows:
            sys.exit("nothing left to do")

    # commutes.csv is grouped by state, and ThreadPoolExecutor.map preserves input order,
    # so in file order the results arrive one state at a time: an interrupted run leaves
    # some states complete and others absent, and no partial cut can support a national
    # estimate. Shuffling makes any prefix of the output a near-uniform sample of the
    # whole, which both survives an interruption gracefully and allows the analysis to be
    # exercised on representative data long before the run finishes. Seeded, so the
    # processing order is reproducible; it has no effect on the completed result set.
    if args.shuffle:
        random.Random(args.shuffle).shuffle(rows)
        print(f"shuffled processing order (seed {args.shuffle})", flush=True)

    def work(r):
        try:
            frm = (float(r["h_lat"]), float(r["h_lon"]))
            to = (float(r["w_lat"]), float(r["w_lon"]))
        except ValueError:
            return None, None
        try:
            base = route(args.url, frm, to, None)
            if base is None:
                return None, None
            bp, bl = base
            avoid = route(args.url, frm, to, args.multiply)
            if avoid is None:
                return None, None
            ap_, al = avoid
        except Exception:
            # A failed route is dropped rather than retried. Failures are dominated by
            # unroutable centroid pairs (islands, centroids landing off-network), which
            # are a property of the sample rather than transient, so retrying just
            # spends time to fail again.
            return None, None
        b_hits = exposure(bl, tree, cones)
        a_hits = exposure(al, tree, cones)
        # Route geometry is the expensive thing here: it costs two HTTP routes and hours
        # of wall-clock for the full sample, while every question one might later ask of
        # it (exposure to one vendor's cameras only, sensitivity to cone radius or span)
        # is pure post-processing. Discarding the polyline and keeping only the counts
        # makes each of those a full re-run. Keeping it makes them free.
        geom = None
        if args.geometry:
            geom = {
                "state": r["state"], "h_tract": r["h_tract"], "w_tract": r["w_tract"],
                # 5 decimals is ~1 m — well inside the routing's own precision, and it
                # roughly halves the file against full float repr.
                "base": [[round(x, 5), round(y, 5)] for x, y in bp["points"]["coordinates"]],
                "avoid": [[round(x, 5), round(y, 5)] for x, y in ap_["points"]["coordinates"]],
            }
        rec = dict(r)
        rec.update({
            "base_km": round(bp["distance"] / 1000, 4),
            "base_min": round(bp["time"] / 60000, 4),
            "base_cameras": b_hits,
            "avoid_km": round(ap_["distance"] / 1000, 4),
            "avoid_min": round(ap_["time"] / 60000, 4),
            "avoid_cameras": a_hits,
            "extra_km": round((ap_["distance"] - bp["distance"]) / 1000, 4),
            "extra_min": round((ap_["time"] - bp["time"]) / 60000, 4),
            "cameras_evaded": b_hits - a_hits,
        })
        return rec, geom

    # Append mode when resuming onto an existing file, otherwise start fresh.
    appending = bool(already)
    fh = open(args.out, "a" if appending else "w", newline="")
    writer = [None]
    if appending:
        with open(args.out, newline="") as probe:
            writer[0] = csv.DictWriter(fh, fieldnames=next(csv.reader(probe)))

    # Geometry sidecar, appended in the same order as the CSV. Gzipped because the
    # polylines are an order of magnitude larger than the counts they accompany.
    gz = gzip.open(args.geometry, "at") if args.geometry else None

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for rec, geom in ex.map(work, rows):
                with out_lock:
                    done[0] += 1
                    if rec:
                        if writer[0] is None:
                            writer[0] = csv.DictWriter(fh, fieldnames=list(rec.keys()))
                            writer[0].writeheader()
                        writer[0].writerow(rec)
                        fh.flush()          # survive a kill, not just a clean exit
                        if gz is not None and geom is not None:
                            gz.write(json.dumps(geom, separators=(",", ":")) + "\n")
                            gz.flush()
                        results.append(1)
                    if done[0] % 500 == 0:
                        print(f"  {done[0]}/{len(rows)} ({len(results)} ok)", flush=True)
    finally:
        fh.close()
        if gz is not None:
            gz.close()

    if not results and not appending:
        sys.exit("no routes succeeded")
    print(f"wrote {len(results)} new records to {args.out}", flush=True)
    print(f"wrote {len(results)} routed commutes -> {args.out}")
    print(f"  dropped {len(rows) - len(results)} unroutable "
          f"({100*(len(rows)-len(results))/len(rows):.1f}%)")


if __name__ == "__main__":
    main()
