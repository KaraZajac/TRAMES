#!/usr/bin/env python3
"""
Paired comparison of two experiment runs over the same commutes — what a camera-data
refresh changed, commute by commute.

    python3 compare_snapshots.py --a out/2026-07/results.csv --b out/results.csv \
        --label-a 2026-07-24 --label-b 2026-09-22 -o out/compare_snapshots.txt

Two runs of run_experiment.py on the same sample and the same road extract differ only
in the camera snapshot baked into the graph. Comparing their marginal distributions would
say whether the headline numbers moved; joining on the commute key says *which* commutes
moved and by how much, with the noise of the sample itself cancelled out. The paired view
is also the integrity check: the unavoided route never sees the cameras, so its distance
and time must be identical between runs to the decimal, and any drift there is a fault in
the pipeline, not a fact about surveillance.

Bootstraps and seeds come from analyze.py so intervals here mean what they mean there.
"""
import argparse
import csv
import os
import statistics as st
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import boot_ci, describe  # noqa: E402

KEY = ("state", "h_tract", "w_tract")


def load(path):
    rows = {}
    for r in csv.DictReader(open(path, newline="")):
        rows[tuple(r[k] for k in KEY)] = r
    return rows


def pct(n, d):
    return 100.0 * n / d if d else float("nan")


def share(xs, pred):
    return pct(sum(1 for x in xs if pred(x)), len(xs))


def exposure_block(rows, label):
    cams = [int(r["base_cameras"]) for r in rows]
    km = sum(float(r["base_km"]) for r in rows)
    cams_sorted = sorted(cams)
    p = lambda q: cams_sorted[min(len(cams_sorted) - 1, int(q * len(cams_sorted)))]
    out = [f"  [{label}]"]
    out.append(describe(cams, "cameras passed (baseline)"))
    out.append(f"    >=1 camera {share(cams, lambda c: c >= 1):5.1f}%   >=5 {share(cams, lambda c: c >= 5):5.1f}%   "
               f">=10 {share(cams, lambda c: c >= 10):5.1f}%   p90 {p(0.90)}   p99 {p(0.99)}   "
               f"rate {sum(cams) / km:.4f} cameras/km")
    return "\n".join(out)


def avoidance_block(rows, label):
    zero = share(rows, lambda r: int(r["avoid_cameras"]) == 0)
    extra = [float(r["extra_min"]) for r in rows]
    over = [100.0 * float(r["extra_min"]) / float(r["base_min"]) for r in rows if float(r["base_min"]) > 0]
    per_cam = [float(r["extra_min"]) / int(r["cameras_evaded"]) for r in rows if int(r["cameras_evaded"]) > 0]
    resid = sum(int(r["avoid_cameras"]) for r in rows)
    base = sum(int(r["base_cameras"]) for r in rows)
    out = [f"  [{label}]  reduced to ZERO cameras: {zero:5.1f}%   residual exposure {pct(resid, base):.1f}% of baseline"]
    out.append(describe(extra, "extra time", " min"))
    out.append(describe(over, "avoidance overhead", " %"))
    out.append(describe(per_cam, "minutes per camera evaded", " min"))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="results.csv of the earlier run")
    ap.add_argument("--b", required=True, help="results.csv of the later run")
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    A, B = load(args.a), load(args.b)
    keys = sorted(set(A) & set(B))
    L = []
    w = L.append
    w("=" * 78)
    w(f"SNAPSHOT COMPARISON: {args.label_a} -> {args.label_b}, paired on commute")
    w("=" * 78)
    w(f"rows: {args.label_a} {len(A)}, {args.label_b} {len(B)}, matched {len(keys)} "
      f"(only in {args.label_a}: {len(set(A) - set(B))}, only in {args.label_b}: {len(set(B) - set(A))})")
    if not keys:
        sys.exit("no commutes in common")
    ra = [A[k] for k in keys]
    rb = [B[k] for k in keys]

    # --- 1. integrity: the unavoided route must not have moved ------------------------
    w("\n--- 1. BASELINE INTEGRITY (unavoided route: same extract, cameras invisible) ---")
    same = [k for k, a, b in zip(keys, ra, rb) if a["base_km"] == b["base_km"] and a["base_min"] == b["base_min"]]
    w(f"  identical baseline distance AND time: {len(same)}/{len(keys)} ({pct(len(same), len(keys)):.2f}%)")
    diff = [(k, a["base_km"], b["base_km"], a["base_min"], b["base_min"])
            for k, a, b in zip(keys, ra, rb) if k not in set(same)]
    for d in diff[:8]:
        w(f"    DIFFERS {d[0]}: km {d[1]} -> {d[2]}, min {d[3]} -> {d[4]}")
    if len(diff) > 8:
        w(f"    ... and {len(diff) - 8} more")

    # --- 2. exposure ------------------------------------------------------------------
    w("\n--- 2. EXPOSURE ON THE UNAVOIDED COMMUTE ---")
    w(exposure_block(ra, args.label_a))
    w(exposure_block(rb, args.label_b))
    d = [int(b["base_cameras"]) - int(a["base_cameras"]) for a, b in zip(ra, rb)]
    lo, hi = boot_ci(d, st.mean)
    w(f"  paired change in cameras passed: mean {st.mean(d):+.3f} [95% CI {lo:+.3f}..{hi:+.3f}]  "
      f"median {st.median(d):+.0f}")
    w(f"    commutes with MORE exposure {share(d, lambda x: x > 0):5.1f}%   same {share(d, lambda x: x == 0):5.1f}%   "
      f"fewer {share(d, lambda x: x < 0):5.1f}%")
    newly = sum(1 for a, b in zip(ra, rb) if int(a["base_cameras"]) == 0 and int(b["base_cameras"]) >= 1)
    cleared = sum(1 for a, b in zip(ra, rb) if int(a["base_cameras"]) >= 1 and int(b["base_cameras"]) == 0)
    w(f"    newly exposed (0 -> >=1): {newly} ({pct(newly, len(keys)):.1f}% of commutes)   "
      f"newly clean (>=1 -> 0): {cleared}")
    ta, tb = sum(int(r["base_cameras"]) for r in ra), sum(int(r["base_cameras"]) for r in rb)
    w(f"    total camera encounters: {ta} -> {tb} ({pct(tb - ta, ta):+.1f}%)")

    # --- 3. avoidance -----------------------------------------------------------------
    w("\n--- 3. COST OF AVOIDANCE ---")
    w(avoidance_block(ra, args.label_a))
    w(avoidance_block(rb, args.label_b))
    dm = [float(b["extra_min"]) - float(a["extra_min"]) for a, b in zip(ra, rb)]
    lo, hi = boot_ci(dm, st.mean)
    w(f"  paired change in extra time: mean {st.mean(dm):+.3f} min [95% CI {lo:+.3f}..{hi:+.3f}]  "
      f"median {st.median(dm):+.2f}")
    za = {k for k, a in zip(keys, ra) if int(a["avoid_cameras"]) == 0}
    zb = {k for k, b in zip(keys, rb) if int(b["avoid_cameras"]) == 0}
    w(f"    zero-exposure set: kept {len(za & zb)}, lost {len(za - zb)}, gained {len(zb - za)}   "
      f"(net {len(zb) - len(za):+d} commutes)")

    # --- 4. by state ------------------------------------------------------------------
    w("\n--- 4. BY STATE ---")
    w(f"  {'state':5s} {'n':>6s}  {'mean cams':>9s} {'->':>2s} {'mean cams':>9s} {'chg':>7s}   "
      f"{'>=1':>5s} {'->':>2s} {'>=1':>5s}   {'zero':>5s} {'->':>2s} {'zero':>5s}   {'med +min':>8s} {'->':>2s} {'med +min':>8s}")
    by = defaultdict(list)
    for k, a, b in zip(keys, ra, rb):
        by[a["state"]].append((a, b))
    for s_, pairs in sorted(by.items(), key=lambda kv: -st.mean(int(b["base_cameras"]) for a, b in kv[1])):
        aa = [p[0] for p in pairs]; bb = [p[1] for p in pairs]
        ma = st.mean(int(r["base_cameras"]) for r in aa); mb = st.mean(int(r["base_cameras"]) for r in bb)
        w(f"  {s_.upper():5s} {len(pairs):6d}  {ma:9.2f} {'->':>2s} {mb:9.2f} {pct(mb - ma, ma) if ma else float('nan'):+6.1f}%   "
          f"{share(aa, lambda r: int(r['base_cameras']) >= 1):5.1f} {'->':>2s} {share(bb, lambda r: int(r['base_cameras']) >= 1):5.1f}   "
          f"{share(aa, lambda r: int(r['avoid_cameras']) == 0):5.1f} {'->':>2s} {share(bb, lambda r: int(r['avoid_cameras']) == 0):5.1f}   "
          f"{st.median(float(r['extra_min']) for r in aa):8.2f} {'->':>2s} {st.median(float(r['extra_min']) for r in bb):8.2f}")

    txt = "\n".join(L)
    print(txt)
    if args.out:
        open(args.out, "w").write(txt + "\n")


if __name__ == "__main__":
    main()
