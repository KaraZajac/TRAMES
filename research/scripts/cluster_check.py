#!/usr/bin/env python3
"""
Are the commute-exposure intervals too narrow? wstats.py's bootstrap resamples commutes within
each state, which treats every commute as independent. Commutes from the same county share
corridors, and so cameras, and would be expected to vary together; the usual answer is to
resample the clusters instead. This recomputes the headline shares, medians and quartile
contrasts with a bootstrap that resamples whole origin counties within each state (all of a
drawn county's commutes, each county as often as it is drawn, weights re-derived as in
wstats.py), and sets its intervals beside the commute-level ones.

    python cluster_check.py --results out/results.csv --vendor out/vendor.csv --history out/history \\
        --frame out/sampling_frame.json --draws out/sample_draws.csv -o out/analysis_cluster.txt

Quartiles, within-county ranking and the n >= 40 rule are analyze.py's. The per-kilometre
contrast (cameras per km driven, Q4 over Q1) is reported with an interval on the ratio itself,
which analyze.py gives only for each quartile separately.
"""
import argparse
import csv
import os
import sys
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from wstats import B, SEED, Design, ci  # noqa: E402


class ClusterBoot:
    """Origin counties resampled with replacement within each state, B times."""

    def __init__(self, D, county):
        self.D = D
        self.cols = []            # per state: (row indices, cluster index per row, counts B x K)
        for s_i, s in enumerate(D.states):
            ix = D.idx[s]
            cl, inv = np.unique(county[ix], return_inverse=True)
            rng = np.random.default_rng([SEED, 7, s_i])
            draws = rng.integers(0, len(cl), size=(B, len(cl)))
            cnt = np.zeros((B, len(cl)))
            np.add.at(cnt, (np.repeat(np.arange(B), len(cl)), draws.ravel()), 1)
            self.cols.append((s, ix, inv, cnt))

    def ratio(self, num, den, masks):
        """(B, G): per replicate, sum(w*num)/sum(w*den) in each mask, as wstats.boot_ratio."""
        D = self.D
        A = np.zeros((B, len(masks))); C = np.zeros((B, len(masks)))
        for s, ix, inv, cnt in self.cols:
            K = cnt.shape[1]
            d = D.d[ix]
            dsum = np.bincount(inv, weights=d, minlength=K)
            scale = D.W[s] / (cnt @ dsum)
            for g, m in enumerate(masks):
                mm = m[ix]
                A[:, g] += scale * (cnt @ np.bincount(inv, weights=d * mm * num[ix], minlength=K))
                C[:, g] += scale * (cnt @ np.bincount(inv, weights=d * mm * den[ix], minlength=K))
        return A / np.maximum(C, 1e-12)

    def quantile(self, x, q, mask=None, batch=200):
        D = self.D
        m = np.ones(D.n, bool) if mask is None else mask
        rows = np.flatnonzero(m)
        o = rows[np.argsort(x[rows], kind="stable")]
        xs = x[o]
        pos = np.full(D.n, -1); pos[o] = np.arange(len(o))
        out = np.empty(B)
        for b0 in range(0, B, batch):
            b1 = min(B, b0 + batch)
            Wt = np.zeros((b1 - b0, len(o)))
            for s, ix, inv, cnt in self.cols:
                d = D.d[ix]
                K = cnt.shape[1]
                c = cnt[b0:b1]                                  # (b, K)
                dsum = c @ np.bincount(inv, weights=d, minlength=K)
                w = D.W[s] * d[None, :] * c[:, inv] / dsum[:, None]
                keep = pos[ix] >= 0
                Wt[:, pos[ix][keep]] = w[:, keep]
            cw = np.cumsum(Wt, axis=1)
            for i in range(b1 - b0):
                out[b0 + i] = xs[min(np.searchsorted(cw[i], q * cw[i, -1]), len(xs) - 1)]
        return out


def masks_national(D, x):
    valid = ~np.isnan(x)
    qs = [D.quantile(x, q, valid) for q in (0.25, 0.5, 0.75)]
    xb = np.where(valid, x, -np.inf)
    b = 1 + (xb > qs[0]).astype(int) + (xb > qs[1]) + (xb > qs[2])
    return [valid & (b == q) for q in (1, 2, 3, 4)]


def masks_within(D, x, county, min_n=40):
    valid = ~np.isnan(x)
    groups = defaultdict(list)
    for i in np.flatnonzero(valid):
        groups[county[i]].append(i)
    b = np.zeros(D.n, int)
    for c, ix in groups.items():
        if len(ix) < min_n:
            continue
        m = np.zeros(D.n, bool); m[ix] = True
        qs = [D.quantile(x, q, m) for q in (0.25, 0.5, 0.75)]
        xi = x[ix]
        b[ix] = 1 + (xi > qs[0]).astype(int) + (xi > qs[1]) + (xi > qs[2])
    return [b == q for q in (1, 2, 3, 4)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--vendor", default=None)
    ap.add_argument("--history", default=None,
                    help="out/history: the earlier maps' results.csv (run_history.py), for the trend's contrasts")
    ap.add_argument("--frame", required=True)
    ap.add_argument("--draws", required=True)
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.results)))
    D = Design(rows, args.frame, args.draws)
    f = lambda R, k: np.array([float(r[k]) for r in R])
    num = lambda k: np.array([float(r[k]) if r.get(k) not in ("", None) else np.nan for r in rows])
    bc, ac, ev = f(rows, "base_cameras"), f(rows, "avoid_cameras"), f(rows, "cameras_evaded")
    km, bmin, em = f(rows, "base_km"), f(rows, "base_min"), f(rows, "extra_min")
    county = np.array([r["h_tract"][:5] for r in rows])
    ones = np.ones(D.n)
    CB = ClusterBoot(D, county)
    ncty = len(np.unique(county))

    L = []
    P = L.append
    P("=" * 78)
    P("COUNTY-CLUSTER BOOTSTRAP: are the commute-exposure intervals too narrow?")
    P("=" * 78)
    P(f"{D.n:,} commutes from {ncty:,} origin counties. 'commute' = wstats.py's bootstrap (commutes")
    P("resampled within state); 'county' = origin counties resampled within state. 2,000 replicates each.")
    P(f"\n  {'statistic':44s} {'point':>8s}  {'commute 95% CI':>17s}  {'county 95% CI':>17s}  width x")

    def row(label, pt, rc, rk, fmt="{:.3f}"):
        (a, b), (c, d) = ci(rc), ci(rk)
        wx = (d - c) / max(b - a, 1e-12)
        P(f"  {label:44s} {fmt.format(pt):>8s}  [{fmt.format(a)}, {fmt.format(b)}]".ljust(74)
          + f"  [{fmt.format(c)}, {fmt.format(d)}]".ljust(20) + f"  {wx:4.2f}")
        return (a, b), (c, d)

    P("\n--- headline figures ---")
    row("share passing >= 1 camera", D.share(bc >= 1), D.boot_ratio((bc >= 1).astype(float), ones)[:, 0],
        CB.ratio((bc >= 1).astype(float), ones, [np.ones(D.n, bool)])[:, 0])
    row("mean cameras passed", D.mean(bc), D.boot_ratio(bc, ones)[:, 0], CB.ratio(bc, ones, [np.ones(D.n, bool)])[:, 0])
    row("share reduced to zero", D.share(ac == 0), D.boot_ratio((ac == 0).astype(float), ones)[:, 0],
        CB.ratio((ac == 0).astype(float), ones, [np.ones(D.n, bool)])[:, 0])
    row("median extra minutes", D.quantile(em, .5), D.boot_quantile(em, .5), CB.quantile(em, .5), "{:.2f}")
    evm = ev > 0
    per = np.where(evm, em / np.maximum(ev, 1), 0.0)
    row("median minutes per camera evaded", D.quantile(per, .5, evm), D.boot_quantile(per, .5, evm),
        CB.quantile(per, .5, evm), "{:.2f}")
    ovh = np.where(bmin > 0, 100 * em / np.maximum(bmin, 1e-9), 0.0)
    row("median avoidance overhead (%)", D.quantile(ovh, .5), D.boot_quantile(ovh, .5), CB.quantile(ovh, .5), "{:.2f}")

    pop = num("pop_total")
    pct = lambda k: np.where(pop > 0, 100 * num(k) / np.where(pop > 0, pop, 1), np.nan)
    attrs = {"income": num("median_income"), "black": pct("nh_black"), "hispanic": pct("hispanic")}

    def contrast(label, x_metric, masks):
        """Q4/Q1 ratio of mean cameras and of cameras per km, both bootstraps."""
        mc = D.boot_ratio(x_metric, ones, masks); mk = CB.ratio(x_metric, ones, masks)
        rc = D.boot_ratio(x_metric, km, masks); rk = CB.ratio(x_metric, km, masks)
        m1, m4 = D.mean(x_metric, masks[0]), D.mean(x_metric, masks[3])
        r1, r4 = D.ratio(x_metric, km, masks[0]), D.ratio(x_metric, km, masks[3])
        a = row(f"{label}: mean cameras Q4/Q1", m4 / m1, mc[:, 3] / mc[:, 0], mk[:, 3] / mk[:, 0], "{:.2f}")
        b = row(f"{label}: cameras per km Q4/Q1", r4 / r1, rc[:, 3] / rc[:, 0], rk[:, 3] / rk[:, 0], "{:.2f}")
        return a, b

    P("\n--- quartile contrasts, ranked nationally (income: richest over poorest) ---")
    for k, lab in (("income", "income"), ("black", "% Black"), ("hispanic", "% Hispanic")):
        contrast(lab, bc, masks_national(D, attrs[k]))
    P("\n--- quartile contrasts, ranked within county (n >= 40) ---")
    for k, lab in (("income", "income"), ("black", "% Black"), ("hispanic", "% Hispanic")):
        contrast(lab, bc, masks_within(D, attrs[k], county))

    P("\n--- cost of refusal by quartile: median extra minutes of the avoiding route, and median")
    P("    minutes per camera evaded (commuters who evade any), lowest vs highest quartile ---")
    for sch, maker in (("national", lambda x: masks_national(D, x)), ("within county", lambda x: masks_within(D, x, county))):
        for k, lab in (("income", "income"), ("black", "% Black"), ("hispanic", "% Hispanic")):
            masks = maker(attrs[k])
            for metric, mlab, base in ((em, "extra min", None), (per, "min/camera", evm)):
                m1 = masks[0] if base is None else masks[0] & base
                m4 = masks[3] if base is None else masks[3] & base
                q1, q4 = D.quantile(metric, .5, m1), D.quantile(metric, .5, m4)
                c1, c4 = D.boot_quantile(metric, .5, m1), D.boot_quantile(metric, .5, m4)
                k1, k4 = CB.quantile(metric, .5, m1), CB.quantile(metric, .5, m4)
                row(f"{lab} {sch} {mlab}: Q4 - Q1 (Q1 {q1:.2f}, Q4 {q4:.2f})", q4 - q1, c4 - c1, k4 - k1, "{:+.2f}")

    if args.vendor:
        vrows = list(csv.DictReader(open(args.vendor)))
        key = lambda r: (r["state"], r["h_tract"], r["w_tract"])
        vi = {key(r): r for r in vrows}
        assert all(key(r) in vi for r in rows), "vendor.csv does not cover results.csv"
        P("\n--- % Black contrast by vendor (mean cameras of that vendor passed, Q4/Q1) ---")
        for v in ("flock", "other", "untagged"):
            xv = np.array([float(vi[key(r)]["base_" + v]) for r in rows])
            for sch, masks in (("national", masks_national(D, attrs["black"])),
                               ("within county", masks_within(D, attrs["black"], county))):
                mc = D.boot_ratio(xv, ones, masks); mk = CB.ratio(xv, ones, masks)
                row(f"{v} {sch}", D.mean(xv, masks[3]) / D.mean(xv, masks[0]),
                    mc[:, 3] / mc[:, 0], mk[:, 3] / mk[:, 0], "{:.2f}")

    if args.history:
        P("\n--- the trend's contrasts on the earlier maps (mean cameras passed, Q4/Q1; income: richest over poorest) ---")
        key = lambda r: (r["state"], r["h_tract"], r["w_tract"])
        for date in sorted(os.listdir(args.history)):
            path = os.path.join(args.history, date, "results.csv")
            if not os.path.exists(path):
                continue
            got = {key(r): float(r["base_cameras"]) for r in csv.DictReader(open(path))}
            x = np.array([got[key(r)] for r in rows])
            for k, lab in (("income", "income"), ("black", "% Black"), ("hispanic", "% Hispanic")):
                for sch, masks in (("national", masks_national(D, attrs[k])),
                                   ("within county", masks_within(D, attrs[k], county))):
                    mc = D.boot_ratio(x, ones, masks); mk = CB.ratio(x, ones, masks)
                    row(f"{date} {lab} {sch}", D.mean(x, masks[3]) / D.mean(x, masks[0]),
                        mc[:, 3] / mc[:, 0], mk[:, 3] / mk[:, 0], "{:.2f}")

    open(args.out, "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
