#!/usr/bin/env python3
"""
Does routing every worker by car bias the exposure contrasts? LODES counts jobs, not drivers, and
the study routes every sampled home-work pair as a car trip. Workers who take transit, walk or
work from home are not on the road, and they are concentrated in dense and often minority tracts
— so a car-only reading could overstate those tracts' exposure. This re-weights each commute by
the share of its home tract's workers who get to work by car, truck, van or motorcycle (ACS
B08301), and repeats the headline and demographic figures for drivers.

    python commute_mode.py --results out/results.csv --frame out/sampling_frame.json \\
        --draws out/sample_draws.csv --acs data/acs_b08301.dat -o out/analysis_commute_mode.txt
"""
import argparse
import csv
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from trend import quartile_masks  # noqa: E402
from wstats import Design, ci  # noqa: E402


def drive_share(path):
    out = {}
    with open(path, encoding="latin-1") as fh:
        head = fh.readline().rstrip("\n").split("|")
        i_all, i_car, i_moto = head.index("B08301_E001"), head.index("B08301_E002"), head.index("B08301_E017")
        for line in fh:
            if not line.startswith("1400000US"):
                continue
            p = line.rstrip("\n").split("|")
            try:
                tot, car, moto = float(p[i_all]), float(p[i_car]), float(p[i_moto])
            except ValueError:
                continue
            if tot > 0:
                out[p[0][9:]] = (car + max(moto, 0)) / tot
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument("--frame", required=True)
    ap.add_argument("--draws", required=True)
    ap.add_argument("--acs", required=True)
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.results, newline="")))
    D = Design(rows, args.frame, args.draws)
    share = drive_share(args.acs)
    s = np.array([share.get(r["h_tract"], np.nan) for r in rows])
    missing = int(np.isnan(s).sum())
    s = np.where(np.isnan(s), np.nanmedian(s), s)
    f = lambda k: np.array([float(r[k]) for r in rows])
    num = lambda k: np.array([float(r[k]) if r.get(k) not in ("", None) else np.nan for r in rows])
    bc, ac, em = f("base_cameras"), f("avoid_cameras"), f("extra_min")
    pop = num("pop_total")
    pct = lambda k: np.where(pop > 0, 100 * num(k) / np.where(pop > 0, pop, 1), np.nan)
    attrs = {"black": pct("nh_black"), "hispanic": pct("hispanic"), "income": num("median_income")}
    county = np.array([r["h_tract"][:5] for r in rows])
    ones = np.ones(D.n)

    L = []; P = L.append
    P("=" * 78)
    P("COMMUTE MODE: the headline figures re-weighted to workers who drive")
    P("=" * 78)
    P(f"{D.n:,} commutes; home tract's share of workers commuting by car/truck/van/motorcycle (ACS B08301)")
    P(f"({missing} commutes with no B08301 tract take the median). Mean drive share, commuter-weighted: "
      f"{D.mean(s):.3f}")
    for a, lab in (("black", "% non-Hispanic Black"), ("hispanic", "% Hispanic"), ("income", "median household income")):
        masks = quartile_masks(D, attrs[a], None)
        P(f"  drive share by national quartile of {lab}: " + " ".join(f"{D.ratio(s, ones, m):.3f}" for m in masks))

    def both(label, x, cond=False):
        a = D.mean(x) if not cond else D.share(x)
        b = D.ratio(x * s, s)
        lo, hi = ci(D.boot_ratio(x * s, s)[:, 0])
        k = 100 if cond else 1
        P(f"  {label:34s} all workers {k * a:7.2f}   drivers {k * b:7.2f} [{k * lo:.2f}-{k * hi:.2f}]")

    P("")
    both("commutes passing >=1 camera (%)", (bc >= 1).astype(float), True)
    both("mean cameras passed", bc)
    both("reduced to zero when avoiding (%)", (ac == 0).astype(float), True)
    both("mean extra minutes", em)

    for scope, cty in (("within county", county), ("national", None)):
        P(f"\n--- highest-quartile over lowest-quartile mean cameras passed, quartiles {scope} ---")
        P(f"  {'':26s} {'all workers':>22s} {'drivers':>22s}")
        for a, lab in (("black", "% non-Hispanic Black"), ("hispanic", "% Hispanic"), ("income", "median household income")):
            masks = quartile_masks(D, attrs[a], cty)
            r0 = D.boot_ratio(bc, ones, masks); r1 = D.boot_ratio(bc * s, s, masks)
            p0 = D.mean(bc, masks[3]) / D.mean(bc, masks[0])
            p1 = D.ratio(bc * s, s, masks[3]) / D.ratio(bc * s, s, masks[0])
            l0, h0 = ci(r0[:, 3] / r0[:, 0]); l1, h1 = ci(r1[:, 3] / r1[:, 0])
            P(f"  {lab:26s} {p0:5.2f}x [{l0:.2f}-{h0:.2f}]      {p1:5.2f}x [{l1:.2f}-{h1:.2f}]")
    txt = "\n".join(L)
    print(txt)
    open(args.out, "w").write(txt + "\n")


if __name__ == "__main__":
    main()
