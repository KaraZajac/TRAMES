#!/usr/bin/env python3
"""
Statistical analysis of ALPR commute exposure and avoidance cost.

Reporting choices worth stating up front, because they change the headline numbers:

* Medians and IQRs lead, not means. Exposure and avoidance cost are both heavily
  right-skewed — a minority of long commutes through dense corridors pull the mean far
  above what a typical commuter experiences. Reporting only the mean would overstate the
  typical burden; reporting only the median would hide the tail. Both are given.

* Confidence intervals are bootstrapped rather than assumed normal, because neither
  distribution is close to normal and camera counts are discrete and zero-inflated.

* Demographic comparisons are between tract quartiles, not continuous regressions. With
  tract-level aggregates, a regression coefficient invites causal reading that the
  design cannot support. Quartile contrasts state what is actually observed: commuters
  originating in these tracts encounter this much surveillance.
"""
import argparse
import csv
import math
import random
import statistics as st
import sys
from collections import defaultdict

import numpy as np

RNG = random.Random(20260725)
# Separate generator for the vectorised bootstrap. Seeded from the same constant, so the
# analysis stays reproducible run to run; the interval endpoints differ negligibly from
# the scalar implementation it replaced (both are 2,000-resample percentile bootstraps).
NP_RNG = np.random.default_rng(20260725)


def _resample_chunks(n, k, batch):
    """Yield (rows_in_batch) sizes so a bootstrap never materialises n*k at once."""
    done = 0
    while done < n:
        b = min(batch, n - done)
        yield b
        done += b


def boot_ci(xs, fn=st.median, n=2000, alpha=0.05):
    """
    Percentile bootstrap CI. No normality assumption.

    Vectorised over numpy: the pure-Python version cost ~7.9 s per call at n=2,400 and the
    full analysis makes roughly forty such calls over samples an order of magnitude
    larger, which does not finish. Resamples are drawn in batches so the index matrix
    never exceeds a few tens of MB — at the full sample size a single n*k draw would be
    ~290 MB.
    """
    if not xs:
        return (float("nan"), float("nan"))
    a = np.asarray(xs, dtype=float)
    k = a.size
    if k == 1:
        return (float(a[0]), float(a[0]))
    if fn is st.median:
        agg = lambda m: np.median(m, axis=1)
    elif fn is st.mean:
        agg = lambda m: m.mean(axis=1)
    else:                                    # rare path; correctness over speed
        agg = lambda m: np.array([fn(r.tolist()) for r in m])
    out = []
    for b in _resample_chunks(n, k, max(1, 4_000_000 // max(k, 1))):
        out.append(agg(a[NP_RNG.integers(0, k, size=(b, k))]))
    stats = np.sort(np.concatenate(out))
    return (float(stats[int((alpha / 2) * n)]),
            float(stats[int((1 - alpha / 2) * n) - 1]))


def describe(xs, label, unit=""):
    if not xs:
        return f"  {label:34s} n=0"
    med = st.median(xs)
    lo, hi = boot_ci(xs)
    q1 = st.quantiles(xs, n=4)[0] if len(xs) >= 4 else med
    q3 = st.quantiles(xs, n=4)[2] if len(xs) >= 4 else med
    return (f"  {label:34s} n={len(xs):6d}  median {med:8.2f}{unit} "
            f"[95% CI {lo:.2f}–{hi:.2f}]  IQR {q1:.2f}–{q3:.2f}  mean {st.mean(xs):8.2f}")


def quartile_bins(rows, key):
    vals = sorted(float(r[key]) for r in rows if r.get(key) not in ("", None))
    if len(vals) < 8:
        return None
    qs = st.quantiles(vals, n=4)
    return qs


def rate_ci(group, n=2000, alpha=0.05, metric="base_cameras"):
    """
    Cameras per km for a group, with a bootstrap CI.

    The statistic is a ratio of sums (total cameras / total km), not a mean of ratios, so
    resampling happens over commutes and the ratio is recomputed each time. A mean of
    per-commute rates would let very short commutes dominate.
    """
    if not group:
        return float("nan"), (float("nan"), float("nan"))
    cams = np.asarray([g[metric] for g in group], dtype=float)
    kms = np.asarray([g["base_km"] for g in group], dtype=float)
    point = float(cams.sum() / max(kms.sum(), 1e-9))
    m = cams.size
    if m == 1:
        return point, (point, point)
    out = []
    for b in _resample_chunks(n, m, max(1, 4_000_000 // max(m, 1))):
        idx = NP_RNG.integers(0, m, size=(b, m))
        out.append(cams[idx].sum(axis=1) / np.maximum(kms[idx].sum(axis=1), 1e-9))
    stats = np.sort(np.concatenate(out))
    return point, (float(stats[int((alpha / 2) * n)]),
                   float(stats[int((1 - alpha / 2) * n) - 1]))


def split_quartiles(rows, key, qs):
    """Bin rows into quartiles 1..4 by `key` against cut-points `qs`."""
    bins = defaultdict(list)
    for r in rows:
        v = float(r[key])
        bins[1 if v <= qs[0] else 2 if v <= qs[1] else 3 if v <= qs[2] else 4].append(r)
    return bins


def contrast(P, rows, key, title, cutfmt, qlabels, ratio="Q4/Q1", metric="base_cameras"):
    """
    Quartile contrast on `key`, always reported with the per-kilometre control.

    The control is emitted here rather than in a separate block so it cannot become
    detached from the contrast it qualifies. An earlier version computed it once at the
    end against whichever `bins` variable happened to survive, which mislabelled income
    quartiles as racial ones whenever the racial binning was skipped.
    """
    P(f"\n--- {title} ---")
    usable = [r for r in rows if r.get(key) not in ("", None)]
    qs = quartile_bins(usable, key)
    if not qs:
        P("  insufficient data")
        return None
    bins = split_quartiles(usable, key, qs)
    P("  quartile cut-points: " + " / ".join(cutfmt(q) for q in qs))
    P(f"  {'quartile':12s} {'n':>5s} {'med cams':>9s} {'mean cams':>10s} "
      f"{'med +min':>9s} {'cams/km':>9s} {'med km':>8s}")
    for q in (1, 2, 3, 4):
        g = bins[q]
        if not g:
            continue
        c = [x[metric] for x in g]
        rate = sum(c) / max(sum(x["base_km"] for x in g), 1e-9)
        P(f"  {qlabels[q]:12s} {len(g):5d} {st.median(c):9.1f} {st.mean(c):10.2f} "
          f"{st.median([x['extra_min'] for x in g]):9.2f} {rate:9.4f} "
          f"{st.median([x['base_km'] for x in g]):8.1f}")
    c1 = [x[metric] for x in bins[1]]
    c4 = [x[metric] for x in bins[4]]
    if not (c1 and c4):
        return None
    lo1, hi1 = boot_ci(c1, st.mean)
    lo4, hi4 = boot_ci(c4, st.mean)
    disjoint = lo1 > hi4 or lo4 > hi1
    P(f"  Q1 mean {st.mean(c1):.2f} [{lo1:.2f}–{hi1:.2f}] vs "
      f"Q4 mean {st.mean(c4):.2f} [{lo4:.2f}–{hi4:.2f}]")
    num, den = (c4, c1) if ratio == "Q4/Q1" else (c1, c4)
    P(f"  ratio {ratio} = {st.mean(num)/max(st.mean(den),1e-9):.2f}x"
      + ("  (CIs disjoint)" if disjoint else "  (CIs OVERLAP — not distinguishable)"))
    # Per-km control. If the raw gap is present but the rate gap is not, the difference
    # is commute length, not camera siting. The rate is a ratio of sums, so it gets its
    # own bootstrap rather than a bare point comparison — an earlier version declared a
    # "rate gap" from two point estimates, which would have reported differences well
    # inside the noise as though they were findings.
    r1, (rl1, rh1) = rate_ci(bins[1], metric=metric)
    r4, (rl4, rh4) = rate_ci(bins[4], metric=metric)
    rate_disjoint = rl1 > rh4 or rl4 > rh1
    P(f"  CONTROL per-km: Q1 {r1:.4f} [{rl1:.4f}–{rh1:.4f}] vs "
      f"Q4 {r4:.4f} [{rl4:.4f}–{rh4:.4f}] cameras/km")
    # Count and rate answer different questions and are reported as peers, not as a
    # finding and its caveat. Total count is a day's exposure; cameras/km is how densely
    # watched the roads are. They can diverge — a group with shorter but more heavily
    # surveilled commutes shows a rate gap and no count gap, which is a result in itself
    # and would be discarded by treating the rate purely as a control on the count.
    hi = "Q4" if r4 > r1 else "Q1"
    if disjoint and rate_disjoint:
        verdict = f"total exposure AND per-km density both differ ({hi} higher on rate)"
    elif disjoint and not rate_disjoint:
        verdict = "total differs, per-km density does not — a commute-length artefact"
    elif rate_disjoint:
        verdict = (f"total exposure indistinguishable, but per-km density differs "
                   f"({hi} higher): {hi} commutes are more densely watched per km, "
                   f"offset by length")
    else:
        verdict = "neither total exposure nor per-km density distinguishable"
    P(f"    -> {verdict}")
    return {"qs": qs, "bins": bins, "disjoint": disjoint,
            "m1": st.mean(c1), "m4": st.mean(c4), "r1": r1, "r4": r4,
            "rate_disjoint": rate_disjoint}


def within_area_contrast(P, rows, key, title, area_of, min_n=40, metric="base_cameras"):
    """
    Repeat the quartile contrast using quartiles computed *within* each local area, then
    pooled.

    This is the coverage-bias check (paper §7.2). The threat to the demographic findings
    is that OSM ALPR mapping effort varies between places, so a national contrast may
    measure where volunteers are active rather than where cameras are. Ranking tracts
    only against others in the same area holds that variation roughly constant: if the
    gradient survives here, it is much harder to explain as a mapping artefact; if it
    vanishes, the national result should not be trusted.
    """
    P(f"\n--- {title} ---")
    by_area = defaultdict(list)
    for r in rows:
        if r.get(key) not in ("", None):
            by_area[area_of(r)].append(r)
    pooled = defaultdict(list)
    used = 0
    for area, g in by_area.items():
        if len(g) < min_n:
            continue
        qs = quartile_bins(g, key)
        if not qs:
            continue
        used += 1
        for q, rs in split_quartiles(g, key, qs).items():
            pooled[q].extend(rs)
    n_pooled = sum(len(v) for v in pooled.values())
    P(f"  {used} areas with n>={min_n} contribute {n_pooled} commutes "
      f"({100*n_pooled/max(len(rows),1):.0f}% of sample)")
    if used < 2 or not (pooled[1] and pooled[4]):
        P("  insufficient within-area data")
        return
    P(f"  {'quartile':12s} {'n':>5s} {'mean cams':>10s} {'cams/km':>9s}")
    for q in (1, 2, 3, 4):
        g = pooled[q]
        if not g:
            continue
        c = [x[metric] for x in g]
        rate = sum(c) / max(sum(x["base_km"] for x in g), 1e-9)
        P(f"  Q{q:<11d} {len(g):5d} {st.mean(c):10.2f} {rate:9.4f}")
    c1 = [x[metric] for x in pooled[1]]
    c4 = [x[metric] for x in pooled[4]]
    lo1, hi1 = boot_ci(c1, st.mean)
    lo4, hi4 = boot_ci(c4, st.mean)
    P(f"  Q1 mean {st.mean(c1):.2f} [{lo1:.2f}–{hi1:.2f}] vs "
      f"Q4 mean {st.mean(c4):.2f} [{lo4:.2f}–{hi4:.2f}]")
    P(f"  ratio Q4/Q1 = {st.mean(c4)/max(st.mean(c1),1e-9):.2f}x"
      + ("  (CIs disjoint)" if lo1 > hi4 or lo4 > hi1
         else "  (CIs OVERLAP — not distinguishable)"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.results)))
    # Per-vendor columns are present only when the input came from vendor_exposure.py.
    vendors = sorted({k[len("base_"):] for k in rows[0]
                      if k.startswith("base_") and k not in
                      ("base_km", "base_min", "base_cameras")}) if rows else []
    for r in rows:
        for k in ("base_km", "base_min", "avoid_km", "avoid_min",
                  "extra_km", "extra_min"):
            r[k] = float(r[k])
        for k in ("base_cameras", "avoid_cameras", "cameras_evaded"):
            r[k] = int(r[k])
        for v in vendors:
            for pre in ("base_", "avoid_"):
                if pre + v in r:
                    r[pre + v] = int(r[pre + v])

    lines = []
    P = lines.append

    P("=" * 78)
    P("ALPR COMMUTE EXPOSURE AND AVOIDANCE COST")
    P("=" * 78)
    P(f"\nSample: {len(rows)} commute-weighted origin-destination pairs, "
      f"{len(set(r['state'] for r in rows))} states")
    P(f"Median commute: {st.median([r['base_km'] for r in rows]):.1f} km / "
      f"{st.median([r['base_min'] for r in rows]):.1f} min")

    P("\n--- RQ1: EXPOSURE ON THE UNAVOIDED COMMUTE ---")
    bc = [r["base_cameras"] for r in rows]
    P(describe(bc, "cameras passed (baseline)"))
    P(f"  commutes passing >=1 camera:       {100*sum(1 for x in bc if x)/len(bc):5.1f}%")
    P(f"  commutes passing >=5 cameras:      {100*sum(1 for x in bc if x>=5)/len(bc):5.1f}%")
    P(f"  commutes passing >=10 cameras:     {100*sum(1 for x in bc if x>=10)/len(bc):5.1f}%")
    P(f"  90th percentile exposure:          {sorted(bc)[int(.9*len(bc))]:5d} cameras")
    P(f"  99th percentile exposure:          {sorted(bc)[int(.99*len(bc))]:5d} cameras")
    km = sum(r["base_km"] for r in rows)
    P(f"  exposure rate:                     {sum(bc)/km:5.3f} cameras per km driven")

    P("\n--- RQ2: COST OF AVOIDANCE ---")
    P(describe([r["extra_min"] for r in rows], "extra time", " min"))
    P(describe([r["extra_km"] for r in rows], "extra distance", " km"))
    ac = [r["avoid_cameras"] for r in rows]
    P(describe(ac, "cameras passed (avoided)"))
    clean = sum(1 for r in rows if r["avoid_cameras"] == 0)
    P(f"  commutes reduced to ZERO cameras:  {100*clean/len(rows):5.1f}%")
    ev = [r for r in rows if r["cameras_evaded"] > 0]
    if ev:
        per = [r["extra_min"] / r["cameras_evaded"] for r in ev]
        P(describe(per, "minutes per camera evaded", " min"))
    # Relative cost matters more than absolute for policy framing
    rel = [100 * r["extra_min"] / r["base_min"] for r in rows if r["base_min"] > 0]
    P(describe(rel, "avoidance overhead", " %"))

    P("\n--- RQ3a: BY STATE ---")
    P(f"  {'state':6s} {'n':>5s} {'med cams':>9s} {'%>=1':>6s} {'med +min':>9s} {'med +%':>7s}")
    by_state = defaultdict(list)
    for r in rows:
        by_state[r["state"]].append(r)
    for stt in sorted(by_state, key=lambda s: -st.median([x["base_cameras"] for x in by_state[s]])):
        g = by_state[stt]
        c = [x["base_cameras"] for x in g]
        P(f"  {stt.upper():6s} {len(g):5d} {st.median(c):9.1f} "
          f"{100*sum(1 for x in c if x)/len(c):5.0f}% "
          f"{st.median([x['extra_min'] for x in g]):9.2f} "
          f"{st.median([100*x['extra_min']/x['base_min'] for x in g if x['base_min']>0]):6.1f}%")

    contrast(P, rows, "median_income",
             "RQ3b: BY ORIGIN-TRACT MEDIAN HOUSEHOLD INCOME",
             lambda q: f"${q:,.0f}",
             {1: "Q1 lowest", 2: "Q2", 3: "Q3", 4: "Q4 highest"},
             ratio="Q1/Q4")

    # Percentages are derived here so both the national and within-area contrasts see them.
    rr = [r for r in rows if r.get("pop_total") not in ("", None)
          and float(r["pop_total"] or 0) > 0]
    for r in rr:
        r["pct_black"] = 100 * float(r["nh_black"] or 0) / float(r["pop_total"])
        r["pct_white"] = 100 * float(r["nh_white"] or 0) / float(r["pop_total"])
        r["pct_hisp"] = 100 * float(r["hispanic"] or 0) / float(r["pop_total"])

    contrast(P, rr, "pct_black",
             "RQ3c: BY ORIGIN-TRACT RACIAL COMPOSITION (% non-Hispanic Black)",
             lambda q: f"{q:.1f}%",
             {1: "Q1 least", 2: "Q2", 3: "Q3", 4: "Q4 most"})

    contrast(P, rr, "pct_hisp",
             "RQ3d: BY ORIGIN-TRACT HISPANIC SHARE",
             lambda q: f"{q:.1f}%",
             {1: "Q1 least", 2: "Q2", 3: "Q3", 4: "Q4 most"})

    # --- RQ4: coverage-bias sensitivity (paper section 7.2) ---
    # Ranking within county holds local OSM mapping intensity roughly constant, so a
    # gradient that survives here is not simply a map of where volunteers are active.
    P("\n" + "=" * 78)
    P("RQ4: COVERAGE-BIAS SENSITIVITY — quartiles computed WITHIN local areas")
    P("=" * 78)
    county_of = lambda r: r["h_tract"][:5]
    within_area_contrast(P, [r for r in rows if r.get("median_income") not in ("", None)],
                         "median_income",
                         "RQ4a: INCOME, within-county quartiles", county_of)
    within_area_contrast(P, rr, "pct_black",
                         "RQ4b: % NON-HISPANIC BLACK, within-county quartiles", county_of)

    # --- RQ5: does exposure differ by camera vendor? ---
    # Present only when analysing vendor_exposure.py output. Flock sells subscriptions to
    # municipal police and HOAs, so its siting follows local purchasing; other vendors
    # skew to DOT and tolling infrastructure. If the demographic gradient lives in one
    # subset and not the other, that is a sharper claim than "ALPRs correlate with
    # demographics" — it identifies which deployment model carries the disparity.
    if vendors:
        P("\n" + "=" * 78)
        P("RQ5: BY CAMERA VENDOR")
        P("=" * 78)
        totals = {v: sum(r["base_" + v] for r in rows) for v in vendors}
        allv = sum(r["base_cameras"] for r in rows)
        km = sum(r["base_km"] for r in rows)
        P(f"\n  union of all vendors: {allv} exposures over {km:,.0f} km")
        P(f"  {'vendor':10s} {'exposures':>10s} {'share':>7s} {'/km':>8s} "
          f"{'%routes>=1':>11s} {'med':>5s}")
        for v in vendors:
            col = [r["base_" + v] for r in rows]
            P(f"  {v:10s} {totals[v]:10d} {100*totals[v]/max(sum(totals.values()),1):6.1f}% "
              f"{sum(col)/km:8.4f} {100*sum(1 for x in col if x)/len(col):10.1f}% "
              f"{st.median(col):5.1f}")
        P(f"  split sum {sum(totals.values())} vs union {allv}: the difference is cameras "
          f"in neither\n  named set plus cross-vendor cone overlaps merged by the union.")

        # Avoidance is driven by the all-vendor custom model; this shows whether one
        # vendor's cameras are systematically harder to route around than another's.
        P("\n  --- evasion under the all-vendor avoidance model ---")
        for v in vendors:
            b = totals[v]
            a = sum(r["avoid_" + v] for r in rows)
            P(f"  {v:10s} {b:6d} -> {a:5d}   {100*(b-a)/max(b,1):5.1f}% evaded")

        for v in vendors:
            contrast(P, rows, "median_income",
                     f"RQ5a[{v}]: INCOME vs {v.upper()} EXPOSURE",
                     lambda q: f"${q:,.0f}",
                     {1: "Q1 lowest", 2: "Q2", 3: "Q3", 4: "Q4 highest"},
                     ratio="Q1/Q4", metric="base_" + v)
            contrast(P, rr, "pct_black",
                     f"RQ5b[{v}]: % NON-HISPANIC BLACK vs {v.upper()} EXPOSURE",
                     lambda q: f"{q:.1f}%",
                     {1: "Q1 least", 2: "Q2", 3: "Q3", 4: "Q4 most"},
                     metric="base_" + v)

        # Any vendor-specific gradient must face the same within-county control that
        # dissolved the all-vendor one; otherwise RQ5 would be held to a weaker standard
        # than RQ3 and could revive a between-county artefact under a vendor label.
        P("\n" + "-" * 78)
        P("RQ5c: VENDOR GRADIENTS UNDER WITHIN-COUNTY RANKING")
        P("-" * 78)
        for v in vendors:
            within_area_contrast(P, rr, "pct_black",
                                 f"RQ5c[{v}]: % NON-HISPANIC BLACK, within-county",
                                 county_of, metric="base_" + v)

    txt = "\n".join(lines)
    print(txt)
    if args.out:
        open(args.out, "w").write(txt + "\n")



# =====================================================================================
# COMMUTER-WEIGHTED REPORT (the 51-state sample) — see wstats.py for why weights and why
# a stratified bootstrap. Structured like the unweighted report above so the two read
# side by side; every national figure here is a weighted estimate of American commuters.
# =====================================================================================

def _wfmt(D, x, label, unit="", mask=None, boot=True):
    import numpy as np
    from wstats import ci
    med = D.quantile(x, 0.5, mask); q1 = D.quantile(x, 0.25, mask); q3 = D.quantile(x, 0.75, mask)
    mu = D.mean(x, mask)
    s = f"  {label:34s} median {med:8.2f}{unit}"
    if boot:
        lo, hi = ci(D.boot_quantile(x, 0.5, mask))
        s += f" [95% CI {lo:.2f}–{hi:.2f}]"
    return s + f"  IQR {q1:.2f}–{q3:.2f}  mean {mu:8.2f}"


def _wshare(D, cond, label, mask=None):
    import numpy as np
    from wstats import ci
    lo, hi = ci(D.boot_ratio(np.asarray(cond, float), np.ones(D.n), None if mask is None else [mask])[:, 0])
    return f"  {label:34s} {100*D.share(cond, mask):5.1f}% [{100*lo:.1f}–{100*hi:.1f}]"


def wcontrast(P, D, x, metric, km, extra, title, cutfmt, qlabels, ratio="Q4/Q1"):
    """Weighted twin of contrast(): weighted quartile cut-points, weighted group means, the
    per-km control as a peer, and stratified-bootstrap intervals from shared replicates."""
    import numpy as np
    from wstats import ci
    P(f"\n--- {title} ---")
    valid = ~np.isnan(x)
    qs = [D.quantile(x, q, valid) for q in (0.25, 0.5, 0.75)]
    xb = np.where(valid, x, -np.inf)
    b = 1 + (xb > qs[0]).astype(int) + (xb > qs[1]) + (xb > qs[2])
    masks = [valid & (b == q) for q in (1, 2, 3, 4)]
    P("  quartile cut-points (commuter-weighted): " + " / ".join(cutfmt(q) for q in qs))
    P(f"  {'quartile':12s} {'n':>5s} {'commuters':>9s} {'med cams':>9s} {'mean cams':>10s} "
      f"{'med +min':>9s} {'cams/km':>9s} {'med km':>8s}")
    for q, m in zip((1, 2, 3, 4), masks):
        P(f"  {qlabels[q]:12s} {int(m.sum()):5d} {100*D.weight_share(m):8.1f}% "
          f"{D.quantile(metric, .5, m):9.1f} {D.mean(metric, m):10.2f} {D.quantile(extra, .5, m):9.2f} "
          f"{D.ratio(metric, km, m):9.4f} {D.quantile(km, .5, m):8.1f}")
    mean_reps = D.boot_ratio(metric, np.ones(D.n), masks)
    rate_reps = D.boot_ratio(metric, km, masks)
    m1, m4 = D.mean(metric, masks[0]), D.mean(metric, masks[3])
    (lo1, lo4), (hi1, hi4) = [v[[0, 3]] for v in ci(mean_reps)]
    disjoint = lo1 > hi4 or lo4 > hi1
    num, den = (3, 0) if ratio == "Q4/Q1" else (0, 3)
    rlo, rhi = ci(mean_reps[:, num] / np.maximum(mean_reps[:, den], 1e-12))
    P(f"  Q1 mean {m1:.2f} [{lo1:.2f}–{hi1:.2f}] vs Q4 mean {m4:.2f} [{lo4:.2f}–{hi4:.2f}]")
    rat = (m4 / max(m1, 1e-12)) if ratio == "Q4/Q1" else (m1 / max(m4, 1e-12))
    P(f"  ratio {ratio} = {rat:.2f}x [95% CI {rlo:.2f}–{rhi:.2f}]"
      + ("  (CIs disjoint)" if disjoint else "  (CIs OVERLAP — not distinguishable)"))
    r1, r4 = D.ratio(metric, km, masks[0]), D.ratio(metric, km, masks[3])
    (rl1, rl4), (rh1, rh4) = [v[[0, 3]] for v in ci(rate_reps)]
    rate_disjoint = rl1 > rh4 or rl4 > rh1
    P(f"  CONTROL per-km: Q1 {r1:.4f} [{rl1:.4f}–{rh1:.4f}] vs Q4 {r4:.4f} [{rl4:.4f}–{rh4:.4f}] cameras/km")
    hi_ = "Q4" if r4 > r1 else "Q1"
    if disjoint and rate_disjoint:
        verdict = f"total exposure AND per-km density both differ ({hi_} higher on rate)"
    elif disjoint:
        verdict = "total differs, per-km density does not — a commute-length artefact"
    elif rate_disjoint:
        verdict = (f"total exposure indistinguishable, but per-km density differs ({hi_} higher): "
                   f"{hi_} commutes are more densely watched per km, offset by length")
    else:
        verdict = "neither total exposure nor per-km density distinguishable"
    P(f"    -> {verdict}")


def wwithin(P, D, x, metric, km, county, title, min_n=40):
    """Weighted twin of within_area_contrast(): quartiles ranked inside each county, then
    pooled with commuter weights, so a county in California counts for its commuters."""
    import numpy as np
    from collections import defaultdict
    from wstats import ci
    P(f"\n--- {title} ---")
    valid = ~np.isnan(x)
    groups = defaultdict(list)
    for i in np.flatnonzero(valid):
        groups[county[i]].append(i)
    b = np.zeros(D.n, int); used = 0
    for c, ix in groups.items():
        if len(ix) < min_n:
            continue
        m = np.zeros(D.n, bool); m[ix] = True
        qs = [D.quantile(x, q, m) for q in (0.25, 0.5, 0.75)]
        xi = x[ix]
        b[ix] = 1 + (xi > qs[0]).astype(int) + (xi > qs[1]) + (xi > qs[2])
        used += 1
    masks = [b == q for q in (1, 2, 3, 4)]
    pooled = int(sum(m.sum() for m in masks))
    P(f"  {used} counties with n>={min_n} contribute {pooled} commutes "
      f"({100*pooled/D.n:.0f}% of sample, {100*D.weight_share(b > 0):.0f}% of commuters)")
    if used < 2:
        P("  insufficient within-county data"); return
    P(f"  {'quartile':12s} {'n':>5s} {'mean cams':>10s} {'cams/km':>9s}")
    for q, m in zip((1, 2, 3, 4), masks):
        P(f"  Q{q:<11d} {int(m.sum()):5d} {D.mean(metric, m):10.2f} {D.ratio(metric, km, m):9.4f}")
    reps = D.boot_ratio(metric, np.ones(D.n), masks)
    (lo1, lo4), (hi1, hi4) = [v[[0, 3]] for v in ci(reps)]
    m1, m4 = D.mean(metric, masks[0]), D.mean(metric, masks[3])
    rlo, rhi = ci(reps[:, 3] / np.maximum(reps[:, 0], 1e-12))
    P(f"  Q1 mean {m1:.2f} [{lo1:.2f}–{hi1:.2f}] vs Q4 mean {m4:.2f} [{lo4:.2f}–{hi4:.2f}]")
    P(f"  ratio Q4/Q1 = {m4/max(m1,1e-12):.2f}x [95% CI {rlo:.2f}–{rhi:.2f}]"
      + ("  (CIs disjoint)" if lo1 > hi4 or lo4 > hi1 else "  (CIs OVERLAP — not distinguishable)"))


def report_weighted(rows, D, vendors, P, by_state_csv=None):
    import numpy as np
    from wstats import ci
    f = lambda k: np.array([float(r[k]) for r in rows])
    num = lambda k: np.array([float(r[k]) if r.get(k) not in ("", None) else np.nan for r in rows])
    bc, ac, ev = f("base_cameras"), f("avoid_cameras"), f("cameras_evaded")
    km, bmin, em, ek = f("base_km"), f("base_min"), f("extra_min"), f("extra_km")
    ones = np.ones(D.n)

    P("=" * 78)
    P("ALPR COMMUTE EXPOSURE AND AVOIDANCE COST — COMMUTER-WEIGHTED")
    P("=" * 78)
    P(f"\nSample: {D.n} commutes in {len(D.states)} states, standing for "
      f"{sum(D.W.values())/1e6:.1f}M in-state commuters (LODES). National figures are weighted")
    P("by each state's commuters and each pair's draw count; 95% CIs from a stratified bootstrap")
    P(f"(each state resampled at its own size, 2,000 replicates).")
    P(f"Median commute: {D.quantile(km, .5):.1f} km / {D.quantile(bmin, .5):.1f} min")

    P("\n--- RQ1: EXPOSURE ON THE UNAVOIDED COMMUTE ---")
    P(_wfmt(D, bc, "cameras passed (baseline)"))
    lo, hi = ci(D.boot_ratio(bc, ones)[:, 0]); P(f"  {'mean cameras passed':34s} {D.mean(bc):5.2f} [{lo:.2f}–{hi:.2f}]")
    for k in (1, 5, 10):
        P(_wshare(D, bc >= k, f"commutes passing >={k} camera{'s' if k > 1 else ''}:"))
    P(f"  90th / 99th percentile exposure:   {D.quantile(bc, .90):.0f} / {D.quantile(bc, .99):.0f} cameras")
    lo, hi = ci(D.boot_ratio(bc, km)[:, 0])
    P(f"  exposure rate:                     {D.ratio(bc, km):.4f} cameras per km driven [{lo:.4f}–{hi:.4f}]")

    P("\n--- RQ2: COST OF AVOIDANCE ---")
    P(_wfmt(D, em, "extra time", " min"))
    P(_wfmt(D, ek, "extra distance", " km"))
    P(f"  {'cameras passed (avoided), mean':34s} {D.mean(ac):5.2f}")
    P(_wshare(D, ac == 0, "commutes reduced to ZERO cameras:"))
    lo, hi = ci(D.boot_ratio(ac, bc)[:, 0])
    P(f"  residual exposure:                 {100*D.ratio(ac, bc):.1f}% of baseline [{100*lo:.1f}–{100*hi:.1f}]")
    evm = ev > 0
    per = np.where(evm, em / np.maximum(ev, 1), np.nan)
    P(_wfmt(D, np.nan_to_num(per), "minutes per camera evaded", " min", mask=evm))
    ovh = np.where(bmin > 0, 100 * em / np.maximum(bmin, 1e-9), 0.0)
    P(_wfmt(D, ovh, "avoidance overhead", " %"))

    P("\n--- RQ3a: BY STATE (within-state estimates, draw-weighted) ---")
    P(f"  {'state':6s} {'n':>5s} {'mean cams':>9s} {'%>=1':>6s} {'%zero':>6s} {'med +min':>9s} {'med +%':>7s} {'cams/km':>8s}")
    st_rows = []
    order = sorted(D.states, key=lambda s: -D.mean(bc, D.state == s))
    ge1_reps = D.boot_ratio((bc >= 1).astype(float), ones, [D.state == s for s in order])
    lo_all, hi_all = ci(ge1_reps)
    for j, s in enumerate(order):
        m = D.state == s
        rec = {"state": s, "n": int(m.sum()), "commuters": int(D.W[s]),
               "mean_cameras": D.mean(bc, m), "pct_ge1": 100 * D.share(bc >= 1, m),
               "pct_ge1_lo": 100 * lo_all[j], "pct_ge1_hi": 100 * hi_all[j],
               "pct_zero_after": 100 * D.share(ac == 0, m), "median_extra_min": D.quantile(em, .5, m),
               "median_overhead_pct": D.quantile(ovh, .5, m), "cameras_per_km": D.ratio(bc, km, m),
               "median_km": D.quantile(km, .5, m)}
        st_rows.append(rec)
        P(f"  {s.upper():6s} {rec['n']:5d} {rec['mean_cameras']:9.2f} {rec['pct_ge1']:5.1f}% {rec['pct_zero_after']:5.1f}% "
          f"{rec['median_extra_min']:9.2f} {rec['median_overhead_pct']:6.1f}% {rec['cameras_per_km']:8.4f}")
    if by_state_csv:
        with open(by_state_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(st_rows[0].keys()))
            w.writeheader()
            for r in st_rows:
                w.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
        P(f"  (per-state table written to {by_state_csv})")

    inc = num("median_income")
    pop = num("pop_total")
    pct = lambda k: np.where(pop > 0, 100 * num(k) / np.where(pop > 0, pop, 1), np.nan)
    pb, ph = pct("nh_black"), pct("hispanic")
    county = np.array([r["h_tract"][:5] for r in rows])
    ql = {1: "Q1 lowest", 2: "Q2", 3: "Q3", 4: "Q4 highest"}
    qm = {1: "Q1 least", 2: "Q2", 3: "Q3", 4: "Q4 most"}
    wcontrast(P, D, inc, bc, km, em, "RQ3b: BY ORIGIN-TRACT MEDIAN HOUSEHOLD INCOME",
              lambda q: f"${q:,.0f}", ql, ratio="Q1/Q4")
    wcontrast(P, D, pb, bc, km, em, "RQ3c: BY ORIGIN-TRACT RACIAL COMPOSITION (% non-Hispanic Black)",
              lambda q: f"{q:.1f}%", qm)
    wcontrast(P, D, ph, bc, km, em, "RQ3d: BY ORIGIN-TRACT HISPANIC SHARE", lambda q: f"{q:.1f}%", qm)

    P("\n" + "=" * 78)
    P("RQ4: COVERAGE-BIAS SENSITIVITY — quartiles computed WITHIN counties, pooled by commuters")
    P("=" * 78)
    wwithin(P, D, inc, bc, km, county, "RQ4a: INCOME, within-county quartiles")
    wwithin(P, D, pb, bc, km, county, "RQ4b: % NON-HISPANIC BLACK, within-county quartiles")
    # The Hispanic gradient is the strongest national contrast in the 51-state sample, so it
    # faces the same control as the others; leaving it out would exempt the largest effect.
    wwithin(P, D, ph, bc, km, county, "RQ4c: HISPANIC SHARE, within-county quartiles")

    if vendors:
        P("\n" + "=" * 78)
        P("RQ5: BY CAMERA VENDOR (commuter-weighted)")
        P("=" * 78)
        cols = {v: f("base_" + v) for v in vendors}
        tot = {v: D.mean(c) for v, c in cols.items()}
        P(f"\n  {'vendor':10s} {'share of exposure':>18s} {'/km':>8s} {'%routes>=1':>11s}")
        for v in vendors:
            P(f"  {v:10s} {100*tot[v]/sum(tot.values()):17.1f}% {D.ratio(cols[v], km):8.4f} "
              f"{100*D.share(cols[v] >= 1):10.1f}%")
        P("\n  --- evasion under the all-vendor avoidance model ---")
        for v in vendors:
            a = D.mean(f("avoid_" + v))
            P(f"  {v:10s} {100*(tot[v]-a)/max(tot[v],1e-12):5.1f}% of weighted exposure evaded")
        for v in vendors:
            wcontrast(P, D, pb, cols[v], km, em, f"RQ5b[{v}]: % NON-HISPANIC BLACK vs {v.upper()} EXPOSURE",
                      lambda q: f"{q:.1f}%", qm)
        P("\n" + "-" * 78)
        P("RQ5c: VENDOR GRADIENTS UNDER WITHIN-COUNTY RANKING")
        P("-" * 78)
        for v in vendors:
            wwithin(P, D, pb, cols[v], km, county, f"RQ5c[{v}]: % NON-HISPANIC BLACK, within-county")


def main_weighted():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--frame", required=True, help="sampling_frame.json from build_sample.py")
    ap.add_argument("--draws", required=True, help="sample_draws.csv from build_sample.py")
    ap.add_argument("--by-state", default=None, help="write the per-state table as CSV here")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from wstats import Design
    rows = list(csv.DictReader(open(args.results)))
    vendors = sorted({k[len("base_"):] for k in rows[0] if k.startswith("base_")
                      and k not in ("base_km", "base_min", "base_cameras")}) if rows else []
    D = Design(rows, args.frame, args.draws)
    lines = []
    report_weighted(rows, D, vendors, lines.append, args.by_state)
    txt = "\n".join(lines)
    print(txt)
    if args.out:
        open(args.out, "w").write(txt + "\n")


if __name__ == "__main__":
    # --frame/--draws select the commuter-weighted report (the 51-state sample); without
    # them this is the original unweighted report, kept so earlier outputs stay reproducible.
    main_weighted() if "--frame" in sys.argv else main()
