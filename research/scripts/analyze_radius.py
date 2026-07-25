#!/usr/bin/env python3
"""
Report the cone-radius sensitivity sweep (paper section 7.3).

Distinguishes two things that a single "exposure vs radius" table would conflate:

  * how much measured EXPOSURE depends on the assumed radius, and
  * whether the AVOIDANCE result survives the assumption being wrong.

The second matters more. Routes were planned against 60 m cones; scoring those same
routes at 90 m asks what happens if real fields of view are half again as long as we
assumed. If the headline "84.6% reach zero exposure" collapsed there, it would be a
property of the parameter rather than of the road network.
"""
import argparse
import csv
import statistics as st

import numpy as np

NP_RNG = np.random.default_rng(20260725)


def boot_mean_ci(a, n=2000, alpha=0.05):
    a = np.asarray(a, dtype=float)
    if a.size < 2:
        return float(a.mean()) if a.size else float("nan"), float("nan"), float("nan")
    idx = NP_RNG.integers(0, a.size, size=(n, a.size))
    s = np.sort(a[idx].mean(axis=1))
    return float(a.mean()), float(s[int(alpha / 2 * n)]), float(s[int((1 - alpha / 2) * n) - 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--results", help="results.csv, for extra-time context")
    ap.add_argument("-o", "--out")
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.sweep)))
    radii = sorted(int(k[len("base_r"):]) for k in rows[0] if k.startswith("base_r"))
    for r in rows:
        for k in list(r):
            if k.startswith(("base_r", "avoid_r")):
                r[k] = int(r[k])

    L = []
    P = L.append
    P("=" * 78)
    P("CONE RADIUS SENSITIVITY  (paper section 7.3)")
    P("=" * 78)
    P(f"\n{len(rows)} routes re-scored at radii {radii} m.")
    P("Routes were PLANNED against 60 m cones; only exposure is recomputed.")

    P("\n--- 1. EXPOSURE on the unavoided route ---")
    P(f"  {'radius':>7s} {'mean':>8s} {'median':>7s} {'>=1 cam':>9s} "
      f"{'>=5 cam':>9s} {'vs 60 m':>9s}")
    # Computed up front: the 30 and 45 m rows are printed before the loop would
    # otherwise reach 60, and referencing it there gave a None.
    base60 = st.mean([x["base_r60"] for x in rows])
    for r in radii:
        c = [x[f"base_r{r}"] for x in rows]
        m = st.mean(c)
        P(f"  {r:5d} m {m:8.2f} {st.median(c):7.1f} "
          f"{100*sum(1 for v in c if v)/len(c):8.1f}% "
          f"{100*sum(1 for v in c if v>=5)/len(c):8.1f}% "
          f"{'--' if r==60 else f'{m/base60:8.2f}x'}")

    P("\n--- 2. AVOIDANCE ROBUSTNESS ---")
    P("  Share of the SAME routes (planned at 60 m) that still cross no cone")
    P("  when scored at each radius:")
    P(f"  {'radius':>7s} {'clean':>9s} {'mean exp':>9s} {'residual vs baseline':>22s}")
    for r in radii:
        a = [x[f"avoid_r{r}"] for x in rows]
        b = [x[f"base_r{r}"] for x in rows]
        clean = 100 * sum(1 for v in a if v == 0) / len(a)
        P(f"  {r:5d} m {clean:8.1f}% {st.mean(a):9.2f} "
          f"{100*st.mean(a)/max(st.mean(b),1e-9):21.1f}%")

    P("\n--- 3. DOES THE HEADLINE SURVIVE? ---")
    a60 = [x["avoid_r60"] for x in rows]
    c60 = 100 * sum(1 for v in a60 if v == 0) / len(a60)
    worst = max(radii)
    aw = [x[f"avoid_r{worst}"] for x in rows]
    cw = 100 * sum(1 for v in aw if v == 0) / len(aw)
    P(f"  at the assumed 60 m:      {c60:.1f}% of commutes reach zero exposure")
    P(f"  at {worst} m (50% wider):    {cw:.1f}%")
    P(f"  absolute change:          {cw-c60:+.1f} points")
    # Bootstrap the difference so the change is not read off two point estimates.
    d = np.array([(1 if x[f"avoid_r{worst}"] == 0 else 0) - (1 if x["avoid_r60"] == 0 else 0)
                  for x in rows], dtype=float)
    m, lo, hi = boot_mean_ci(d)
    P(f"  95% CI on the change:     [{100*lo:+.1f}, {100*hi:+.1f}] points")

    smallest = min(radii)
    asm = [x[f"avoid_r{smallest}"] for x in rows]
    P(f"  at {smallest} m (half):        "
      f"{100*sum(1 for v in asm if v==0)/len(asm):.1f}%")

    P("\n  Interpretation: routes planned against 60 m cones are re-scored here without")
    P("  re-planning. A router given wider cones would find different clean routes, so")
    P("  the figures above are a LOWER bound on what avoidance could achieve at those")
    P("  radii, not an estimate of it.")

    txt = "\n".join(L)
    print(txt)
    if args.out:
        open(args.out, "w").write(txt + "\n")


if __name__ == "__main__":
    main()
