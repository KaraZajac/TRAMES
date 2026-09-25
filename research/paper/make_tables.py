#!/usr/bin/env python3
"""
Generate the paper's data tables as LaTeX tabulars, from the same out/*.csv the text and
figures read — so a table cannot disagree with the analysis that produced it.

    ../server/.venv/bin/python paper/make_tables.py      # -> paper/tables/*.tex

Only the tabular bodies are generated; captions and surrounding prose stay in trames.tex.
"""
import csv
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.environ.get("TRAMES_OUT_DIR", os.path.join(ROOT, "out"))
TAB = os.environ.get("TRAMES_TAB_DIR", os.path.join(HERE, "tables"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from trend import CODES  # noqa: E402
from wstats import Design  # noqa: E402

NAME = {v: k for k, v in CODES.items()}
NAME["dc"] = "District of Columbia"


def num(x, d=0):
    return f"\\num{{{x:.{d}f}}}"


def by_state():
    S = {r["state"]: r for r in csv.DictReader(open(os.path.join(OUT, "by_state.csv")))}
    cams = list(csv.DictReader(open(os.path.join(OUT, "cameras_by_state.csv"))))
    latest = max(r["date"] for r in cams)
    C = {r["state"]: r for r in cams if r["date"] == latest}
    rows = list(csv.DictReader(open(os.path.join(OUT, "results.csv"))))
    D = Design(rows, os.path.join(OUT, "sampling_frame.json"), os.path.join(OUT, "sample_draws.csv"))
    f = lambda k: np.array([float(r[k]) for r in rows])
    bc, ac, em, bmin = f("base_cameras"), f("avoid_cameras"), f("extra_min"), f("base_min")
    ovh = np.where(bmin > 0, 100 * em / np.maximum(bmin, 1e-9), 0)
    L = [r"\begin{tabular}{lrrrrrrrrr}", r"\toprule",
         r"& Commuters & Commutes & Mapped & per & Mean & $\geq$1 & Zero when & Extra & Over- \\",
         r"State & (M) & routed & ALPR & 100k & passed & (\%) & avoiding (\%) & min & head (\%) \\",
         r"\midrule"]
    for code in sorted(S, key=lambda c: NAME[c]):
        r, c = S[code], C.get(code, {"cameras": 0, "per_100k": 0})
        L.append(f"{NAME[code]} & {num(float(r['commuters'])/1e6, 2)} & {num(float(r['n']))} & "
                 f"{num(float(c['cameras']))} & {num(float(c['per_100k']), 1)} & {num(float(r['mean_cameras']), 2)} & "
                 f"{num(float(r['pct_ge1']), 1)} & {num(float(r['pct_zero_after']), 1)} & "
                 f"{num(float(r['median_extra_min']), 2)} & {num(float(r['median_overhead_pct']), 1)} \\\\")
    tot_c = sum(float(c["cameras"]) for c in C.values())
    L += [r"\midrule",
          f"\\textbf{{United States}} & {num(sum(D.W.values())/1e6, 1)} & {num(D.n)} & {num(tot_c)} & & "
          f"{num(D.mean(bc), 2)} & {num(100*D.share(bc >= 1), 1)} & {num(100*D.share(ac == 0), 1)} & "
          f"{num(D.quantile(em, .5), 2)} & {num(D.quantile(ovh, .5), 1)} \\\\",
          r"\bottomrule", r"\end{tabular}"]
    return "\n".join(L) + "\n", latest


def trend():
    T = list(csv.DictReader(open(os.path.join(OUT, "trend.csv"))))
    has = lambda r, k: r.get(k) not in ("", None)
    L = [r"\begin{tabular}{lrrrrrrr}", r"\toprule",
         r"Map date & Mapped & \multicolumn{2}{c}{Passing $\geq$1 (\%)} & Mean & Exposed who & Extra min, & Min per \\",
         r"& ALPR & & [95\% CI] & passed & reach zero (\%) & exposed & camera \\",
         r"\midrule"]
    for r in T:
        av = (f"{num(float(r['pct_zero_after_given_exposed']), 1)} & {num(float(r['median_extra_min_given_exposed']), 2)} & "
              f"{num(float(r['median_min_per_camera']), 2)}") if has(r, "pct_zero_after") else "--- & --- & ---"
        L.append(f"{r['date']} & {num(float(r['cameras_us']))} & {num(float(r['pct_ge1']), 1)} & "
                 f"[{float(r['pct_ge1_lo']):.1f}--{float(r['pct_ge1_hi']):.1f}] & {num(float(r['mean_cameras']), 2)} & {av} \\\\")
    L += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(L) + "\n"


SITING_ROWS = [
    ("tract:all:road", "Cameras per km of road (any class)"),
    ("tract:arterial:road", "Arterial cameras per km of arterial"),
    ("tract:residential-street:road", "Residential cameras per km of residential street"),
    ("tract:all:people", "Cameras per resident"),
    ("tract:op=police:road", "Police-operated cameras per km of road"),
    ("tract:edge:all roads", "Edge concentration (edge/interior), all roads"),
    ("tract:edge:residential", "Edge concentration, residential streets"),
    ("bg:arterial:road", "Block groups: arterial cameras per km"),
    ("bg:edge:all roads", "Block groups: edge concentration, all roads"),
]


def siting():
    """Q4/Q1 siting contrasts from siting.py: within county x density tercile (with intervals)
    and within county alone (point estimates)."""
    path = os.path.join(OUT, "siting", "siting.csv")
    R = list(csv.DictReader(open(path)))
    get = lambda an, a, sch: next((r for r in R if r["analysis"] == an and r["attribute"] == a
                                   and r["scheme"] == sch), None)
    L = [r"\begin{tabular}{lrrrrrr}", r"\toprule",
         r"& \multicolumn{3}{c}{Within county and density tercile} & \multicolumn{3}{c}{Within county} \\",
         r"\cmidrule(lr){2-4} \cmidrule(lr){5-7}",
         r"Highest over lowest quartile & \% Black & \% Hispanic & Income & \% Black & \% Hispanic & Income \\",
         r"\midrule"]
    for an, label in SITING_ROWS:
        cells = []
        for a in ("black", "hispanic", "income"):
            r = get(an, a, "county x density")
            cells.append(f"{float(r['q4_over_q1']):.2f} [{float(r['lo']):.2f}--{float(r['hi']):.2f}]" if r else "---")
        for a in ("black", "hispanic", "income"):
            r = get(an, a, "within county")
            cells.append(f"{float(r['q4_over_q1']):.2f}" if r else "---")
        L.append(f"{label} & " + " & ".join(cells) + r" \\")
    L += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(L) + "\n"


def main():
    os.makedirs(TAB, exist_ok=True)
    t, latest = by_state()
    open(os.path.join(TAB, "by_state.tex"), "w").write(f"% generated by make_tables.py (camera counts as of {latest})\n" + t)
    open(os.path.join(TAB, "trend.tex"), "w").write("% generated by make_tables.py\n" + trend())
    written = ["by_state.tex", "trend.tex"]
    if os.path.exists(os.path.join(OUT, "siting", "siting.csv")):
        open(os.path.join(TAB, "siting.tex"), "w").write("% generated by make_tables.py\n" + siting())
        written.append("siting.tex")
    print(f"wrote {', '.join(os.path.join(TAB, w) for w in written)}")


if __name__ == "__main__":
    main()
