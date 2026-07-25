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


if __name__ == "__main__":
    main()
