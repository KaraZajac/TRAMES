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


def by_state(osm_only=False):
    """The merged map (the paper's basis), or with osm_only the map alone; the merged camera counts
    are the ones make_figures.py writes, so it runs first."""
    M = OUT if osm_only else os.path.join(OUT, "merged")
    S = {r["state"]: r for r in csv.DictReader(open(os.path.join(M, "by_state.csv")))}
    cams = list(csv.DictReader(open(os.path.join(M, "cameras_by_state.csv"))))
    latest = max(r["date"] for r in cams)
    C = {r["state"]: r for r in cams if r["date"] == latest}
    rows = list(csv.DictReader(open(os.path.join(M, "results.csv"))))
    D = Design(rows, os.path.join(OUT, "sampling_frame.json"), os.path.join(OUT, "sample_draws.csv"))
    f = lambda k: np.array([float(r[k]) for r in rows])
    bc, ac, em, bmin = f("base_cameras"), f("avoid_cameras"), f("extra_min"), f("base_min")
    ovh = np.where(bmin > 0, 100 * em / np.maximum(bmin, 1e-9), 0)
    L = [r"\begin{tabular}{lrrrrrrrrr}", r"\toprule",
         (r"& Commuters & Commutes & Mapped & per & Mean & $\geq$1 & Zero when & Extra & Over- \\" if osm_only else
          r"& Commuters & Commutes & & per & Mean & $\geq$1 & Zero when & Extra & Over- \\"),
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
    """The routed dates only: every monthly map is in trend.csv and fig_trend, but a table of
    thirty-five rows says less than the figure does."""
    T = list(csv.DictReader(open(os.path.join(OUT, "trend.csv"))))
    has = lambda r, k: r.get(k) not in ("", None)
    L = [r"\begin{tabular}{lrrrrrrr}", r"\toprule",
         r"Map date & Mapped & \multicolumn{2}{c}{Passing $\geq$1 (\%)} & Mean & Exposed who & Extra min, & Min per \\",
         r"& ALPR & & [95\% CI] & passed & reach zero (\%) & exposed & camera \\",
         r"\midrule"]
    for r in T:
        if not has(r, "pct_zero_after"):
            continue
        av = (f"{num(float(r['pct_zero_after_given_exposed']), 1)} & {num(float(r['median_extra_min_given_exposed']), 2)} & "
              f"{num(float(r['median_min_per_camera']), 2)}")
        L.append(f"{r['date']} & {num(float(r['cameras_us']))} & {num(float(r['pct_ge1']), 1)} & "
                 f"[{float(r['pct_ge1_lo']):.1f}--{float(r['pct_ge1_hi']):.1f}] & {num(float(r['mean_cameras']), 2)} & {av} \\\\")
    # the merged present: the latest map plus the registry's readers (compare_merged.py; the
    # installation count is the one make_figures.py writes, so it runs first)
    mpath, cpath = os.path.join(OUT, "merged", "compare.csv"), os.path.join(OUT, "merged", "cameras_by_state.csv")
    if os.path.exists(mpath) and os.path.exists(cpath):
        M = {r["statistic"]: r for r in csv.DictReader(open(mpath))}
        g = lambda k, f="merged": float(M[k][f])
        n = sum(int(r["cameras"]) for r in csv.DictReader(open(cpath)))
        L += [r"\midrule",
              f"\\quad + registry & {num(n)} & {num(g('passing >=1 camera (%)'), 1)} & "
              f"[{g('passing >=1 camera (%)', 'merged_lo'):.1f}--{g('passing >=1 camera (%)', 'merged_hi'):.1f}] & "
              f"{num(g('mean cameras passed'), 2)} & {num(g('reduced to zero, of exposed (%)'), 1)} & "
              f"{num(g('median extra min, exposed'), 2)} & {num(g('median min per camera evaded'), 2)} \\\\"]
    L += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(L) + "\n"


SITING_ROWS = [      # (csv, analysis, label); None rules off a group
    ("siting", "tract:all:road", "Cameras per km of road (any class)"),
    ("siting", "tract:arterial:road", "Arterial cameras per km of arterial"),
    ("siting", "tract:residential-street:road", "Residential cameras per km of residential street"),
    ("siting", "tract:all:people", "Cameras per resident"),
    ("siting", "tract:op=police:road", "Police-operated cameras per km of road"),
    None,
    ("siting_traffic", "tract:hpms-all-byclass:road", "Counted roads: cameras per km, by class"),
    ("siting_traffic", "tract:hpms-all-byclass:traffic", "Counted roads: cameras per vehicle-km, by class"),
    ("siting_traffic", "tract:hpms-surface:op=police:traffic", "Police-operated per vehicle-km of surface road"),
    None,
    ("siting", "tract:edge:all roads", "Edge concentration (edge/interior), all roads"),
    ("siting", "tract:edge:residential", "Edge concentration, residential streets"),
    None,
    ("siting", "bg:arterial:road", "Block groups: arterial cameras per km"),
    ("siting_traffic", "bg:hpms-all-byclass:traffic", "Block groups: cameras per vehicle-km, by class"),
    ("siting", "bg:edge:all roads", "Block groups: edge concentration, all roads"),
]


def siting():
    """Q4/Q1 siting contrasts from siting.py and siting_traffic.py: within county x density
    tercile (with intervals) and within county alone (point estimates)."""
    R = {}
    for name in ("siting", "siting_traffic"):
        path = os.path.join(OUT, "siting", f"{name}.csv")
        R[name] = list(csv.DictReader(open(path))) if os.path.exists(path) else []
    get = lambda src, an, a, sch: next((r for r in R[src] if r["analysis"] == an and r["attribute"] == a
                                        and r["scheme"] == sch), None)
    L = [r"\begin{tabular}{lrrrrrr}", r"\toprule",
         r"& \multicolumn{3}{c}{Within county and density tercile} & \multicolumn{3}{c}{Within county} \\",
         r"\cmidrule(lr){2-4} \cmidrule(lr){5-7}",
         r"Highest over lowest quartile & \% Black & \% Hispanic & Income & \% Black & \% Hispanic & Income \\",
         r"\midrule"]
    for row in SITING_ROWS:
        if row is None:
            L.append(r"\midrule")
            continue
        src, an, label = row
        if not any(r["analysis"] == an for r in R[src]):
            continue
        cells = []
        for a in ("black", "hispanic", "income"):
            r = get(src, an, a, "county x density")
            cells.append(f"{float(r['q4_over_q1']):.2f} [{float(r['lo']):.2f}--{float(r['hi']):.2f}]" if r else "---")
        for a in ("black", "hispanic", "income"):
            r = get(src, an, a, "within county")
            cells.append(f"{float(r['q4_over_q1']):.2f}" if r else "---")
        L.append(f"{label} & " + " & ".join(cells) + r" \\")
    L += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(L) + "\n"


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm-only", action="store_true", help="tabulate the map alone, as by_state_osm.tex")
    args = ap.parse_args()
    os.makedirs(TAB, exist_ok=True)
    t, latest = by_state(args.osm_only)
    name = "by_state_osm.tex" if args.osm_only else "by_state.tex"
    open(os.path.join(TAB, name), "w").write(f"% generated by make_tables.py (camera counts as of {latest})\n" + t)
    open(os.path.join(TAB, "trend.tex"), "w").write("% generated by make_tables.py\n" + trend())
    written = [name, "trend.tex"]
    if os.path.exists(os.path.join(OUT, "siting", "siting.csv")):
        open(os.path.join(TAB, "siting.tex"), "w").write("% generated by make_tables.py\n" + siting())
        written.append("siting.tex")
    print(f"wrote {', '.join(os.path.join(TAB, w) for w in written)}")


if __name__ == "__main__":
    main()
