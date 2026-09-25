#!/usr/bin/env python3
"""
Is the within-county siting gradient a property of the cameras or of how far mapping had got?
Re-computes siting.py's tract-level camera intensities (cameras per 100 km of road, quartiles
nationally, within county, and within county x density tercile) on the camera maps of earlier
dates. A gradient produced by where volunteers happened to map first should move as the map
fills in; one produced by siting should hold.

    python siting_trend.py --tables out/siting \\
        --snapshot 2025-07-01=../server/alpr/history/2025-07-01 ... -o out/analysis_siting_trend.txt

Uses the road points' tract assignment cached by siting.py (assign_null_tract.npz); every
camera of every date is counted, whatever road it watches, as in siting.py's all-roads rate.
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import geo  # noqa: E402
import siting as S  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", required=True, help="siting.py's --tables directory (for its cache)")
    ap.add_argument("--snapshot", action="append", required=True, metavar="DATE=TILE_DIR")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    U = geo.load_units("tract")
    attrs, pop = S.attributes(U)
    z = np.load(os.path.join(args.tables, "assign_null_tract.npz"))
    nul_u = z["a0"]
    bt = S.Boot(np.unique(U["county"]))
    ucix = bt.index(U["county"])
    dens = np.where(U["aland"] > 0, pop / np.maximum(U["aland"] / 1e6, 1e-3), np.nan)
    ok = np.isfinite(dens) & (pop > 0)
    o = np.argsort(dens[ok]); cw = np.cumsum(pop[ok][o]); cw = cw / cw[-1]
    t1, t2 = dens[ok][o][np.searchsorted(cw, 1 / 3)], dens[ok][o][np.searchsorted(cw, 2 / 3)]
    cband = np.char.add(np.char.add(U["county"].astype(str), ":"),
                        np.where(dens <= t1, "lo", np.where(dens <= t2, "mid", "hi")))
    grouping = {"national": None, "within county": U["county"], "county x density": cband}
    Q = {(a, sch): S.quartiles(attrs[a], pop, g) for a in S.LABEL for sch, g in grouping.items()}
    bidx = np.where(dens <= t1, 0, np.where(dens <= t2, 1, 2))
    ncty = len(bt.cty)
    strata = {"within county": (ucix, np.arange(ncty)),
              "county x density": (ucix * 3 + bidx, np.repeat(np.arange(ncty), 3))}
    S.per_km = 1.0

    L = []

    def P(s=""):
        L.append(s); print(s, flush=True)

    P("=" * 78)
    P("SITING GRADIENTS ON EARLIER CAMERA MAPS (tracts; cameras per 100 km of road, all roads)")
    P("=" * 78)
    rows = []
    for spec in sorted(args.snapshot):
        date, _, tiles = spec.partition("=")
        nodes = {}
        for p in glob.glob(os.path.join(tiles, "tile_*.json")):
            for e in json.load(open(p)).get("elements", ()):
                if e.get("type") == "node":
                    nodes[e["id"]] = (e["lon"], e["lat"])
        xy = np.array(list(nodes.values()))
        cu = S.assign(U, xy[:, 0], xy[:, 1], neighbours=False)[0]
        P(f"\n--- {date}: {int((cu >= 0).sum()):,} mapped cameras in tracts ---")
        S.rates_by_quartile(P, bt, ucix, strata, Q, pop, cu, cu >= 0, nul_u, nul_u >= 0, "road",
                            "cameras per 100 km of road", rows, f"trend:{date}")
    open(args.out, "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
