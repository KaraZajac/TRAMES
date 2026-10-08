#!/usr/bin/env python3
"""
The OpenStreetMap-only run against the merged-map run, commute by commute, commuter-weighted.

    python3 compare_merged.py --a out/results.csv --b out/merged/results.csv \\
        --frame out/sampling_frame.json --draws out/sample_draws.csv \\
        [--by-state-a out/by_state.csv --by-state-b out/merged/by_state.csv] \\
        -o out/merged/compare.csv --report out/analysis_compare_merged.txt

Both runs route the same commutes on the same roads and differ only in the camera set the
graph avoids: the map's wedges (a), or those plus a 60 m disc for each in-service registry
plate reader the map lacks (b). The unavoided route never sees the cameras, so it must be
identical in both, and is checked to the metre and the second.

Statistics are weighted as in analyze.py and intervals come from the stratified bootstrap of
wstats.py. Its replicates are common random numbers, so the difference between the two maps
is bootstrapped replicate by replicate: a paired interval, narrower than the two marginal
intervals would suggest. Fixed cohorts - the commuters each map exposes, the ones only the
registry exposes - separate what the registry adds to a given commute from who it adds.
"""
import argparse
import csv
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wstats import Design, ci  # noqa: E402

KEY = ("state", "h_tract", "w_tract")


def arrays(rows):
    f = lambda k: np.array([float(r[k]) for r in rows])
    R = {"base": f("base_cameras"), "avoid": f("avoid_cameras"), "extra": f("extra_min"),
         "extra_km": f("extra_km"), "base_min": f("base_min"), "km": f("base_km"), "evaded": f("cameras_evaded")}
    R["ovh"] = np.where(R["base_min"] > 0, 100 * R["extra"] / np.maximum(R["base_min"], 1e-9), 0)
    R["per"] = np.where(R["evaded"] > 0, R["extra"] / np.maximum(R["evaded"], 1), 0)
    return R


def statistics(D, R, cohort=None):
    """(label, point, replicates) for each statistic; cohort restricts the 'exposed' ones to a
    fixed set instead of the commuters this map exposes."""
    ones = np.ones(D.n)
    b, a, em = R["base"], R["avoid"], R["extra"]
    exp = (b >= 1) if cohort is None else cohort
    return [
        ("passing >=1 camera (%)", 100 * D.share(b >= 1), lambda: 100 * D.boot_ratio(b >= 1, ones)[:, 0]),
        ("passing >=5 cameras (%)", 100 * D.share(b >= 5), lambda: 100 * D.boot_ratio(b >= 5, ones)[:, 0]),
        ("mean cameras passed", D.mean(b), lambda: D.boot_ratio(b, ones)[:, 0]),
        ("median cameras passed", D.quantile(b, .5), lambda: D.boot_quantile(b, .5)),
        ("cameras per km", D.ratio(b, R["km"]), lambda: D.boot_ratio(b, R["km"])[:, 0]),
        ("reduced to zero, all (%)", 100 * D.share(a == 0), lambda: 100 * D.boot_ratio(a == 0, ones)[:, 0]),
        ("reduced to zero, of exposed (%)", 100 * D.share(a == 0, exp),
         lambda: 100 * D.boot_ratio((a == 0) & exp, exp)[:, 0]),
        ("residual exposure (% of baseline)", 100 * D.ratio(a, b), lambda: 100 * D.boot_ratio(a, b)[:, 0]),
        ("median extra min, all", D.quantile(em, .5), lambda: D.boot_quantile(em, .5)),
        ("median extra min, exposed", D.quantile(em, .5, exp), lambda: D.boot_quantile(em, .5, exp)),
        ("mean extra min, all", D.mean(em), lambda: D.boot_ratio(em, ones)[:, 0]),
        ("median extra km, all", D.quantile(R["extra_km"], .5), lambda: D.boot_quantile(R["extra_km"], .5)),
        ("median overhead (%)", D.quantile(R["ovh"], .5), lambda: D.boot_quantile(R["ovh"], .5)),
        ("mean overhead (%)", D.mean(R["ovh"]), lambda: D.boot_ratio(R["ovh"], ones)[:, 0]),
        ("median min per camera evaded", D.quantile(R["per"], .5, R["evaded"] > 0),
         lambda: D.boot_quantile(R["per"], .5, R["evaded"] > 0)),
    ]


def state_price(D, R, min_evaders=100, B=2000):
    """Per state with at least min_evaders commutes that evade a camera: the median minutes per
    camera evaded among them, and cameras per km driven over all the state's commutes (both
    draw-weighted within the state). r is taken against the logarithm of the density, with a
    Fisher interval and a percentile interval from resampling the states themselves."""
    out = {}
    for s in D.states:
        m = D.state == s
        ev = m & (R["evaded"] > 0)
        if ev.sum() >= min_evaders:
            out[s] = (D.quantile(R["per"], .5, ev), D.ratio(R["base"], R["km"], m), int(ev.sum()))
    S = sorted(out)
    price = np.array([out[s][0] for s in S]); dens = np.log(np.array([out[s][1] for s in S]))
    r = float(np.corrcoef(price, dens)[0, 1])
    z, se = np.arctanh(r), 1 / np.sqrt(len(S) - 3)
    fisher = (float(np.tanh(z - 1.96 * se)), float(np.tanh(z + 1.96 * se)))
    rng = np.random.default_rng(20260725)
    reps = []
    for _ in range(B):
        ix = rng.integers(0, len(S), len(S))
        if np.ptp(price[ix]) > 0 and np.ptp(dens[ix]) > 0:
            reps.append(np.corrcoef(price[ix], dens[ix])[0, 1])
    return out, r, fisher, tuple(float(v) for v in np.percentile(reps, [2.5, 97.5]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="results.csv of the OpenStreetMap-only run")
    ap.add_argument("--b", required=True, help="results.csv of the merged-map run")
    ap.add_argument("--frame", required=True)
    ap.add_argument("--draws", required=True)
    ap.add_argument("--by-state-a", default=None)
    ap.add_argument("--by-state-b", default=None)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    A = list(csv.DictReader(open(args.a, newline="")))
    Bk = {tuple(r[k] for k in KEY): r for r in csv.DictReader(open(args.b, newline=""))}
    keys = [tuple(r[k] for k in KEY) for r in A]
    missing = [k for k in keys if k not in Bk]
    if missing or len(Bk) != len(A):
        raise SystemExit(f"the runs do not hold the same commutes ({len(A)} against {len(Bk)}, {len(missing)} missing)")
    B = [Bk[k] for k in keys]
    D = Design(A, args.frame, args.draws)
    Ra, Rb = arrays(A), arrays(B)

    L = []; P = L.append
    P("=" * 78); P("THE MAP ALONE AGAINST THE MERGED MAP: the same commutes, paired, commuter-weighted"); P("=" * 78)
    dkm = np.abs(Ra["km"] - Rb["km"]).max(); dmin = np.abs(Ra["base_min"] - Rb["base_min"]).max()
    P(f"{D.n:,} commutes in {len(D.states)} states. Unavoided routes identical in both runs: "
      f"largest difference {dkm:.4f} km, {dmin:.4f} min{'' if dkm < 1e-3 and dmin < 1e-3 else '  <-- PIPELINE FAULT'}")
    P("Intervals: stratified bootstrap, 2,000 replicates; the difference is paired, replicate by replicate.\n")

    out = []
    P(f"{'':36s} {'map alone':>20s} {'merged map':>20s} {'difference':>24s}")
    for (lab, pa, fa), (_, pb, fb) in zip(statistics(D, Ra), statistics(D, Rb)):
        ra, rb = fa(), fb()
        (alo, ahi), (blo, bhi), (dlo, dhi) = ci(ra), ci(rb), ci(rb - ra)
        P(f"{lab:36s} {pa:8.2f} [{alo:.2f}-{ahi:.2f}] {pb:8.2f} [{blo:.2f}-{bhi:.2f}] {pb-pa:+8.2f} [{dlo:+.2f} to {dhi:+.2f}]")
        out.append({"statistic": lab, "osm": pa, "osm_lo": alo, "osm_hi": ahi, "merged": pb, "merged_lo": blo,
                    "merged_hi": bhi, "diff": pb - pa, "diff_lo": dlo, "diff_hi": dhi})

    ones = np.ones(D.n)
    P("\nFIXED COHORTS")
    for name, cohort in (("exposed on the map alone", Ra["base"] >= 1),
                         ("exposed only once the registry is added", (Ra["base"] == 0) & (Rb["base"] >= 1))):
        s = D.share(cohort); lo, hi = ci(D.boot_ratio(cohort, ones)[:, 0])
        P(f"  [{name}] {100*s:.1f}% of commuters [{100*lo:.1f}-{100*hi:.1f}]")
        for lab, key, q in (("median extra min", "extra", .5), ("median cameras passed", "base", .5)):
            xa, xb = D.quantile(Ra[key], q, cohort), D.quantile(Rb[key], q, cohort)
            ra, rb = D.boot_quantile(Ra[key], q, cohort), D.boot_quantile(Rb[key], q, cohort)
            dlo, dhi = ci(rb - ra)
            P(f"    {lab:28s} map alone {xa:6.2f}   merged {xb:6.2f}   difference {xb-xa:+.2f} [{dlo:+.2f} to {dhi:+.2f}]")
        za, zb = D.share(Ra["avoid"] == 0, cohort), D.share(Rb["avoid"] == 0, cohort)
        ra = D.boot_ratio((Ra["avoid"] == 0) & cohort, cohort)[:, 0]; rb = D.boot_ratio((Rb["avoid"] == 0) & cohort, cohort)[:, 0]
        dlo, dhi = ci(rb - ra)
        P(f"    {'reach zero (%)':28s} map alone {100*za:6.1f}   merged {100*zb:6.1f}   difference {100*(zb-za):+.1f} "
          f"[{100*dlo:+.1f} to {100*dhi:+.1f}]")
    lost = (Ra["avoid"] == 0) & (Rb["avoid"] >= 1)
    lo, hi = ci(D.boot_ratio(lost, ones)[:, 0])
    P(f"\n  could reach zero against the map alone, cannot against the merged map: {100*D.share(lost):.1f}% of commuters "
      f"[{100*lo:.1f}-{100*hi:.1f}]")
    up = Rb["extra"] - Ra["extra"]
    P(f"  extra time, merged minus map alone, per commuter: rose for {100*D.share(up > 0.05):.1f}%, "
      f"fell for {100*D.share(up < -0.05):.1f}%, within 3 s for {100*D.share(np.abs(up) <= 0.05):.1f}%")
    fell = up < -0.05
    P(f"  where it fell, the merged avoiding route keeps a camera for {100*D.share(Rb['avoid'] >= 1, fell):.0f}% "
      f"(their map-alone avoiding routes: {100*D.share(Ra['avoid'] >= 1, fell):.0f}%; every exposed commuter's on the "
      f"merged map: {100*D.share(Rb['avoid'] >= 1, Rb['base'] >= 1):.0f}%)")
    for name, R in (("map alone", Ra), ("merged map", Rb)):
        q = R["extra"] < -0.001     # quicker by over 0.06 s: an unchanged route differs by float noise
        P(f"  [{name}] the avoiding route is the quicker for {100*D.share(q):.1f}% of commuters, by a median "
          f"{D.quantile(-R['extra'], .5, q):.2f} min, and the longer in every such case: "
          f"{bool((R['extra_km'][q] > 0).all())}; mean extra time {D.mean(R['extra']):.2f} min, "
          f"{D.mean(np.maximum(R['extra'], 0)):.2f} with negative values clipped to zero")

    if args.by_state_a and args.by_state_b:
        Sa = {r["state"]: r for r in csv.DictReader(open(args.by_state_a))}
        Sb = {r["state"]: r for r in csv.DictReader(open(args.by_state_b))}
        P(f"\nBY STATE (within-state estimates): map alone -> merged map, ordered by the rise in exposure")
        P(f"  {'state':6s} {'%>=1':>15s} {'mean cameras':>15s} {'%zero':>15s} {'med +min':>15s}")
        g = lambda S, s, k: float(S[s][k])
        for s in sorted(Sa, key=lambda s: g(Sa, s, "pct_ge1") - g(Sb, s, "pct_ge1")):
            P(f"  {s.upper():6s} {g(Sa,s,'pct_ge1'):6.1f} -> {g(Sb,s,'pct_ge1'):5.1f} {g(Sa,s,'mean_cameras'):6.2f} -> "
              f"{g(Sb,s,'mean_cameras'):5.2f} {g(Sa,s,'pct_zero_after'):6.1f} -> {g(Sb,s,'pct_zero_after'):5.1f} "
              f"{g(Sa,s,'median_extra_min'):6.2f} -> {g(Sb,s,'median_extra_min'):5.2f}")

    P("\nPRICE PER CAMERA AGAINST DENSITY, ACROSS STATES (states with >= 100 commutes that evade a camera)")
    for name, R in (("map alone", Ra), ("merged map", Rb)):
        st, r, (flo, fhi), (blo, bhi) = state_price(D, R)
        order = sorted(st, key=lambda s: -st[s][0])
        P(f"  [{name}] {len(st)} states; r = {r:+.2f} against log cameras per km "
          f"(Fisher [{flo:+.2f}, {fhi:+.2f}]; resampling states [{blo:+.2f}, {bhi:+.2f}])")
        P("    dearest per camera: " + ", ".join(f"{s.upper()} {st[s][0]:.2f} min ({st[s][1]:.3f}/km)" for s in order[:4]))
        P("    cheapest per camera: " + ", ".join(f"{s.upper()} {st[s][0]:.2f} min ({st[s][1]:.3f}/km)" for s in order[-4:]))
        if "ga" in st:
            P(f"    Georgia: {st['ga'][0]:.2f} min per camera at {st['ga'][1]:.3f} cameras/km")

    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)
    txt = "\n".join(L); print(txt); open(args.report, "w").write(txt + "\n")


if __name__ == "__main__":
    main()
