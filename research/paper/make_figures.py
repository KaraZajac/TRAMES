#!/usr/bin/env python3
"""
Generate every figure in the TRAMES ALPR paper.

Figures are written as PDF (vector) for LaTeX inclusion. Nothing here recomputes a
result: each figure reads the same out/*.csv the analysis reads, so a number in a plot
and the same number in the text cannot disagree.

    ../server/.venv/bin/python paper/make_figures.py
"""
import csv
import glob
import json
import math
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch
from matplotlib.lines import Line2D

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIG = os.path.join(HERE, "figures")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

os.makedirs(FIG, exist_ok=True)

# ---------------------------------------------------------------- style
plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["DejaVu Serif"],
    "font.size": 9,
    "axes.titlesize": 10,
    "axes.labelsize": 9,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.25,
    "grid.linewidth": 0.5,
    "legend.frameon": False,
    "figure.dpi": 150,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.02,
})

# Colourblind-safe (Okabe-Ito)
C_FLOCK = "#D55E00"
C_OTHER = "#0072B2"
C_UNTAG = "#999999"
C_BASE = "#CC79A7"
C_AVOID = "#009E73"
C_NEUT = "#333333"
QCOLS = ["#0072B2", "#56B4E9", "#E69F00", "#D55E00"]


def save(fig, name):
    p = os.path.join(FIG, name)
    fig.savefig(p + ".pdf")
    fig.savefig(p + ".png", dpi=200)
    plt.close(fig)
    print(f"  wrote {name}.pdf")


# ---------------------------------------------------------------- projection
def albers(lon, lat):
    """USA Contiguous Albers Equal Area. Straight lat/lon looks wrong for a US map."""
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    p1, p2 = math.radians(29.5), math.radians(45.5)
    lat0, lon0 = math.radians(23.0), math.radians(-96.0)
    n = (math.sin(p1) + math.sin(p2)) / 2.0
    C = math.cos(p1) ** 2 + 2 * n * math.sin(p1)
    rho0 = math.sqrt(C - 2 * n * math.sin(lat0)) / n
    la, lo = np.radians(lat), np.radians(lon)
    rho = np.sqrt(np.maximum(C - 2 * n * np.sin(la), 1e-12)) / n
    th = n * (lo - lon0)
    return rho * np.sin(th), rho0 - rho * np.cos(th)


def state_paths():
    """Yield projected (x, y) rings for the lower-48 state outlines."""
    path = os.path.join(FIG, "us-states.json")
    if not os.path.exists(path):
        return
    gj = json.load(open(path))
    for feat in gj["features"]:
        if feat["properties"]["name"] in ("Alaska", "Hawaii", "Puerto Rico"):
            continue
        geom = feat["geometry"]
        polys = (geom["coordinates"] if geom["type"] == "MultiPolygon"
                 else [geom["coordinates"]])
        for poly in polys:
            ring = np.asarray(poly[0], dtype=float)
            yield albers(ring[:, 0], ring[:, 1])


def draw_basemap(ax, lw=0.4):
    for x, y in state_paths():
        ax.plot(x, y, color="#666666", lw=lw, zorder=1, solid_joinstyle="round")
    ax.set_aspect("equal")
    ax.axis("off")


# ---------------------------------------------------------------- data
def load_cameras():
    """(lon, lat, vendor_class) for every mapped ALPR node."""
    keys = ("manufacturer", "surveillance:manufacturer", "brand", "surveillance:brand")
    seen = {}
    for p in glob.glob(os.path.join(ROOT, "..", "server", "alpr",
                                    "region_cache", "*.json")):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        for el in d.get("elements", []):
            if el.get("type") == "node" and el.get("lat") is not None:
                seen[el["id"]] = el
    lon, lat, cls = [], [], []
    for el in seen.values():
        t = el.get("tags") or {}
        v = next((t[k] for k in keys if t.get(k)), None)
        lon.append(el["lon"])
        lat.append(el["lat"])
        cls.append("flock" if v and "flock" in v.lower()
                   else ("other" if v else "untagged"))
    return np.array(lon), np.array(lat), np.array(cls)


def load_results(path):
    rows = list(csv.DictReader(open(path)))
    for r in rows:
        for k in ("base_km", "base_min", "avoid_km", "avoid_min", "extra_km", "extra_min"):
            r[k] = float(r[k])
        for k in list(r):
            if k.startswith(("base_", "avoid_")) and k not in (
                    "base_km", "base_min", "avoid_km", "avoid_min"):
                r[k] = int(r[k])
            if k == "cameras_evaded":
                r[k] = int(r[k])
    return rows


NP_RNG = np.random.default_rng(20260725)


def boot_mean_ci(a, n=2000, alpha=0.05):
    a = np.asarray(a, dtype=float)
    if a.size < 2:
        return (float(a.mean()) if a.size else np.nan,) * 3
    out = []
    step = max(1, 4_000_000 // a.size)
    done = 0
    while done < n:
        b = min(step, n - done)
        out.append(a[NP_RNG.integers(0, a.size, size=(b, a.size))].mean(axis=1))
        done += b
    s = np.sort(np.concatenate(out))
    return a.mean(), s[int(alpha / 2 * n)], s[int((1 - alpha / 2) * n) - 1]


def boot_rate_ci(cams, kms, n=2000, alpha=0.05):
    cams = np.asarray(cams, float)
    kms = np.asarray(kms, float)
    pt = cams.sum() / max(kms.sum(), 1e-9)
    out = []
    step = max(1, 4_000_000 // max(cams.size, 1))
    done = 0
    while done < n:
        b = min(step, n - done)
        idx = NP_RNG.integers(0, cams.size, size=(b, cams.size))
        out.append(cams[idx].sum(axis=1) / np.maximum(kms[idx].sum(axis=1), 1e-9))
        done += b
    s = np.sort(np.concatenate(out))
    return pt, s[int(alpha / 2 * n)], s[int((1 - alpha / 2) * n) - 1]


def quartiles(rows, key):
    vals = np.array([float(r[key]) for r in rows if r.get(key) not in ("", None)])
    cuts = np.quantile(vals, [0.25, 0.5, 0.75])
    bins = [[] for _ in range(4)]
    for r in rows:
        if r.get(key) in ("", None):
            continue
        v = float(r[key])
        bins[0 if v <= cuts[0] else 1 if v <= cuts[1] else 2 if v <= cuts[2] else 3].append(r)
    return cuts, bins


def add_pct(rows):
    out = []
    for r in rows:
        if r.get("pop_total") in ("", None) or float(r["pop_total"] or 0) <= 0:
            continue
        tot = float(r["pop_total"])
        r["pct_black"] = 100 * float(r["nh_black"] or 0) / tot
        r["pct_hisp"] = 100 * float(r["hispanic"] or 0) / tot
        out.append(r)
    return out


# ================================================================ figures
def fig_camera_map(lon, lat, cls):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2),
                             gridspec_kw={"width_ratios": [1, 1], "wspace": 0.02})
    ax = axes[0]
    draw_basemap(ax)
    order = [("untagged", C_UNTAG, 0.35), ("other", C_OTHER, 0.75), ("flock", C_FLOCK, 0.5)]
    for name, col, al in order:
        m = cls == name
        x, y = albers(lon[m], lat[m])
        ax.scatter(x, y, s=0.45, c=col, alpha=al, linewidths=0, zorder=3,
                   rasterized=True)
    ax.set_xlim(-0.42, 0.52)
    ax.set_ylim(-0.02, 0.62)
    ax.set_title(f"(a) {len(lon):,} mapped ALPR installations, by vendor")
    ax.legend(handles=[
        Line2D([], [], marker="o", ls="", ms=4, color=C_FLOCK,
               label=f"Flock Safety ({100*(cls=='flock').mean():.1f}%)"),
        Line2D([], [], marker="o", ls="", ms=4, color=C_OTHER,
               label=f"other vendor ({100*(cls=='other').mean():.1f}%)"),
        Line2D([], [], marker="o", ls="", ms=4, color=C_UNTAG,
               label=f"unlabelled ({100*(cls=='untagged').mean():.1f}%)"),
    ], loc="lower left", fontsize=8)

    ax = axes[1]
    x, y = albers(lon, lat)
    keep = (x > -0.42) & (x < 0.52) & (y > -0.02) & (y < 0.62)
    hb = ax.hexbin(x[keep], y[keep], gridsize=110, bins="log", mincnt=1,
                   cmap="magma_r", linewidths=0, zorder=2)
    draw_basemap(ax, lw=0.3)
    ax.set_xlim(-0.42, 0.52)
    ax.set_ylim(-0.02, 0.62)
    ax.set_title("(b) Installation density (log scale)")
    cb = fig.colorbar(hb, ax=ax, fraction=0.03, pad=0.01)
    cb.set_label("cameras per cell", fontsize=8)
    cb.ax.tick_params(labelsize=7)
    save(fig, "fig_camera_map")


def fig_exposure(rows):
    base = np.array([r["base_cameras"] for r in rows])
    avoid = np.array([r["avoid_cameras"] for r in rows])
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.0))

    ax = axes[0]
    mx = 25
    bins = np.arange(0, mx + 2) - 0.5
    ax.hist(np.clip(base, 0, mx), bins=bins, color=C_BASE, edgecolor="white", lw=0.3,
            label="unavoided")
    ax.hist(np.clip(avoid, 0, mx), bins=bins, color=C_AVOID, alpha=0.85,
            edgecolor="white", lw=0.3, label="ALPR-avoiding")
    ax.set_xlabel(f"cameras passed (clipped at {mx})")
    ax.set_ylabel("commutes")
    ax.set_title("(a) Exposure distribution")
    ax.legend(fontsize=8)

    ax = axes[1]
    for arr, col, lab in ((base, C_BASE, "unavoided"), (avoid, C_AVOID, "ALPR-avoiding")):
        s = np.sort(arr)
        ax.plot(s, np.arange(1, s.size + 1) / s.size, color=col, lw=1.6, label=lab)
    ax.set_xscale("symlog", linthresh=1)
    ax.set_xlabel("cameras passed")
    ax.set_ylabel("cumulative fraction of commutes")
    ax.set_title("(b) Cumulative exposure")
    ax.axhline(0.846, color=C_NEUT, ls=":", lw=0.8)
    ax.annotate("84.6% reach zero\nwhen avoiding", xy=(0.06, 0.846), xycoords=("axes fraction", "data"),
                fontsize=7.5, va="bottom", color=C_NEUT)
    ax.legend(fontsize=8, loc="lower right")

    ax = axes[2]
    thr = [1, 3, 5, 10, 20]
    xb = [100 * (base >= t).mean() for t in thr]
    xa = [100 * (avoid >= t).mean() for t in thr]
    yy = np.arange(len(thr))
    ax.barh(yy - 0.2, xb, height=0.4, color=C_BASE, label="unavoided")
    ax.barh(yy + 0.2, xa, height=0.4, color=C_AVOID, label="ALPR-avoiding")
    for i, v in enumerate(xb):
        ax.text(v + 1, i - 0.2, f"{v:.1f}%", va="center", fontsize=7)
    for i, v in enumerate(xa):
        ax.text(v + 1, i + 0.2, f"{v:.1f}%", va="center", fontsize=7)
    ax.set_yticks(yy, [f"$\\geq${t}" for t in thr])
    ax.set_xlabel("% of commutes")
    ax.set_title("(c) Commutes exceeding an exposure threshold")
    ax.set_xlim(0, 100)
    ax.legend(fontsize=8, loc="lower right")
    save(fig, "fig_exposure")


def fig_cost(rows):
    ex_min = np.array([r["extra_min"] for r in rows])
    overhead = np.array([100 * r["extra_min"] / r["base_min"]
                         for r in rows if r["base_min"] > 0])
    evaded = np.array([r["cameras_evaded"] for r in rows])
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.0))

    ax = axes[0]
    ax.hist(np.clip(ex_min, 0, 30), bins=60, color=C_AVOID, edgecolor="white", lw=0.2)
    ax.axvline(np.median(ex_min), color=C_NEUT, ls="--", lw=1)
    ax.annotate(f"median {np.median(ex_min):.2f} min", xy=(np.median(ex_min), 0.92),
                xycoords=("data", "axes fraction"), fontsize=8, ha="left",
                xytext=(4, 0), textcoords="offset points")
    ax.set_xlabel("extra travel time (min, clipped at 30)")
    ax.set_ylabel("commutes")
    ax.set_title("(a) Cost of complete avoidance")

    ax = axes[1]
    s = np.sort(overhead)
    ax.plot(s, 100 * np.arange(1, s.size + 1) / s.size, color=C_AVOID, lw=1.6)
    for q, lab in ((50, "median"), (75, "75th"), (90, "90th")):
        v = np.percentile(overhead, q)
        ax.plot([v], [q], "o", ms=4, color=C_NEUT)
        ax.annotate(f"{lab}: {v:.1f}%", xy=(v, q), fontsize=7.5,
                    xytext=(6, -3), textcoords="offset points")
    ax.set_xscale("symlog", linthresh=1)
    ax.set_xlabel("avoidance overhead (% of baseline time)")
    ax.set_ylabel("percentile of commutes")
    ax.set_title("(b) Relative cost")

    ax = axes[2]
    m = evaded > 0
    per = ex_min[m] / evaded[m]
    ax.hist(np.clip(per, 0, 6), bins=60, color=C_OTHER, edgecolor="white", lw=0.2)
    ax.axvline(np.median(per), color=C_NEUT, ls="--", lw=1)
    ax.annotate(f"median {np.median(per):.2f} min", xy=(np.median(per), 0.92),
                xycoords=("data", "axes fraction"), fontsize=8,
                xytext=(4, 0), textcoords="offset points")
    ax.set_xlabel("minutes spent per camera evaded")
    ax.set_ylabel("commutes")
    ax.set_title("(c) Marginal price of evasion")
    save(fig, "fig_cost")


def fig_states(rows, cams_per_100k):
    by = {}
    for r in rows:
        by.setdefault(r["state"], []).append(r)
    st = sorted(by, key=lambda s: -np.mean([x["base_cameras"] for x in by[s]]))
    means = [np.mean([x["base_cameras"] for x in by[s]]) for s in st]
    ovh = [np.median([100 * x["extra_min"] / x["base_min"]
                      for x in by[s] if x["base_min"] > 0]) for s in st]

    fig, axes = plt.subplots(1, 2, figsize=(11, 3.4))
    ax = axes[0]
    yy = np.arange(len(st))
    ax.barh(yy, means, color=C_BASE, height=0.62)
    ax.set_yticks(yy, [s.upper() for s in st])
    ax.invert_yaxis()
    ax.set_xlabel("mean cameras passed per commute")
    ax.set_title("(a) Exposure by state")
    ax2 = ax.twiny()
    ax2.plot(ovh, yy, "o-", color=C_AVOID, ms=4, lw=1)
    ax2.set_xlabel("median avoidance overhead (%)", color=C_AVOID)
    ax2.tick_params(axis="x", colors=C_AVOID)
    ax2.grid(False)

    ax = axes[1]
    x = [cams_per_100k.get(s, np.nan) for s in st]
    ax.scatter(x, means, s=34, color=C_OTHER, zorder=3)
    for s, xi, yi in zip(st, x, means):
        ax.annotate(s.upper(), (xi, yi), fontsize=7.5,
                    xytext=(4, 3), textcoords="offset points")
    good = ~np.isnan(np.array(x, dtype=float))
    xa = np.array(x, dtype=float)[good]
    ya = np.array(means)[good]
    if xa.size > 2:
        k, b = np.polyfit(xa, ya, 1)
        xs = np.linspace(xa.min(), xa.max(), 20)
        ax.plot(xs, k * xs + b, color=C_NEUT, ls="--", lw=1)
        r = np.corrcoef(xa, ya)[0, 1]
        ax.annotate(f"$r$ = {r:.2f}", xy=(0.04, 0.9), xycoords="axes fraction", fontsize=9)
    ax.set_xlabel("mapped cameras per 100k residents")
    ax.set_ylabel("mean cameras passed per commute")
    ax.set_title("(b) Measured exposure tracks mapping intensity")
    save(fig, "fig_states")


def _contrast_panel(ax, bins, metric, labels, title, ylabel, rate=False):
    pts, los, his = [], [], []
    for g in bins:
        if rate:
            p, lo, hi = boot_rate_ci([x[metric] for x in g], [x["base_km"] for x in g])
        else:
            p, lo, hi = boot_mean_ci([x[metric] for x in g])
        pts.append(p)
        los.append(p - lo)
        his.append(hi - p)
    xx = np.arange(4)
    ax.bar(xx, pts, color=QCOLS, width=0.66,
           yerr=[los, his], capsize=3, error_kw={"lw": 1, "ecolor": "#222222"})
    ax.set_xticks(xx, labels)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    return pts, los, his


def fig_demographics(rows):
    rr = add_pct(rows)
    fig, axes = plt.subplots(2, 3, figsize=(11, 5.6))
    specs = [
        (rows, "median_income", ["Q1\nlowest", "Q2", "Q3", "Q4\nhighest"],
         "median household income"),
        (rr, "pct_black", ["Q1\nleast", "Q2", "Q3", "Q4\nmost"],
         "% non-Hispanic Black"),
        (rr, "pct_hisp", ["Q1\nleast", "Q2", "Q3", "Q4\nmost"], "% Hispanic"),
    ]
    for j, (src, key, labs, name) in enumerate(specs):
        _, bins = quartiles(src, key)
        _contrast_panel(axes[0][j], bins, "base_cameras", labs,
                        f"({'abc'[j]}) by {name}", "mean cameras passed")
        _contrast_panel(axes[1][j], bins, "base_cameras", labs, "", "cameras per km",
                        rate=True)
    axes[0][0].set_ylabel("mean cameras passed")
    axes[1][0].set_ylabel("cameras per km driven")
    fig.text(0.5, 0.985, "Origin-tract quartile contrasts (95% bootstrap CI)",
             ha="center", fontsize=10)
    fig.tight_layout(rect=[0, 0, 1, 0.965])
    save(fig, "fig_demographics")


def fig_within_county(rows, min_n=40):
    rr = add_pct(rows)
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.4))
    for j, (src, key, name) in enumerate([(rows, "median_income", "median household income"),
                                          (rr, "pct_black", "% non-Hispanic Black")]):
        _, nat = quartiles(src, key)
        # within-county quartiles, pooled
        by = {}
        for r in src:
            if r.get(key) not in ("", None):
                by.setdefault(r["h_tract"][:5], []).append(r)
        pooled = [[] for _ in range(4)]
        used = 0
        for g in by.values():
            if len(g) < min_n:
                continue
            used += 1
            _, b = quartiles(g, key)
            for i in range(4):
                pooled[i].extend(b[i])
        ax = axes[j]
        ratios = {}
        for offs, bins, col, lab in ((-0.19, nat, "#0072B2", "national quartiles"),
                                     (0.19, pooled, "#D55E00", f"within-county ({used} counties)")):
            pts, los, his = [], [], []
            for g in bins:
                p, lo, hi = boot_mean_ci([x["base_cameras"] for x in g])
                pts.append(p); los.append(p - lo); his.append(hi - p)
            ax.bar(np.arange(4) + offs, pts, width=0.36, color=col, label=lab,
                   yerr=[los, his], capsize=2.5, error_kw={"lw": 0.9, "ecolor": "#222222"})
            ratios[lab.split()[0]] = pts[3] / max(pts[0], 1e-9)
        # The collapse is the finding, and it is invisible in bar heights alone.
        ax.set_ylim(0, ax.get_ylim()[1] * 1.34)
        ax.annotate(f"Q4/Q1  national {ratios['national']:.2f}$\\times$"
                    f"   $\\rightarrow$   within-county {ratios['within-county']:.2f}$\\times$",
                    xy=(0.5, 0.965), xycoords="axes fraction", ha="center", va="top",
                    fontsize=8.5,
                    bbox=dict(boxstyle="round,pad=0.3", fc="#F5F5F5", ec="#BBBBBB", lw=0.6))
        ax.set_xticks(np.arange(4), ["Q1", "Q2", "Q3", "Q4"])
        ax.set_ylabel("mean cameras passed")
        ax.set_title(f"({'ab'[j]}) {name}")
        ax.legend(fontsize=7.5, loc="upper left", bbox_to_anchor=(0.0, 0.87))
    fig.tight_layout()
    save(fig, "fig_within_county")


def fig_vendor(vrows, counts):
    if not vrows:
        return
    vendors = ["flock", "other", "untagged"]
    exp = {v: sum(r["base_" + v] for r in vrows) for v in vendors}
    avo = {v: sum(r["avoid_" + v] for r in vrows) for v in vendors}
    km = sum(r["base_km"] for r in vrows)
    tot_exp = sum(exp.values())
    tot_cam = sum(counts.values())

    fig, axes = plt.subplots(1, 3, figsize=(11, 3.2))
    ax = axes[0]
    xx = np.arange(3)
    inst = [100 * counts[v] / tot_cam for v in vendors]
    expo = [100 * exp[v] / tot_exp for v in vendors]
    ax.bar(xx - 0.2, inst, width=0.4, color="#999999", label="share of installations")
    ax.bar(xx + 0.2, expo, width=0.4, color=C_OTHER, label="share of exposure")
    for i, (a, b) in enumerate(zip(inst, expo)):
        ax.text(i - 0.2, a + 1.2, f"{a:.1f}", ha="center", fontsize=7.5)
        ax.text(i + 0.2, b + 1.2, f"{b:.1f}", ha="center", fontsize=7.5)
    ax.set_xticks(xx, ["Flock", "other\nvendor", "unlabelled"])
    ax.set_ylabel("percent")
    ax.set_title("(a) Deployment share vs. exposure share")
    ax.legend(fontsize=7.5)

    ax = axes[1]
    rate = [1000 * exp[v] / counts[v] for v in vendors]
    ax.bar(xx, rate, color=[C_FLOCK, C_OTHER, C_UNTAG], width=0.6)
    for i, v in enumerate(rate):
        ax.text(i, v + max(rate) * 0.02, f"{v:.1f}", ha="center", fontsize=8)
    ax.set_xticks(xx, ["Flock", "other\nvendor", "unlabelled"])
    ax.set_ylabel("exposures per 1,000 cameras")
    ax.set_title("(b) Reach per unit deployed")

    ax = axes[2]
    ev = [100 * (exp[v] - avo[v]) / max(exp[v], 1) for v in vendors]
    ax.bar(xx, ev, color=[C_FLOCK, C_OTHER, C_UNTAG], width=0.6)
    for i, v in enumerate(ev):
        ax.text(i, v + 0.6, f"{v:.1f}%", ha="center", fontsize=8)
    ax.set_xticks(xx, ["Flock", "other\nvendor", "unlabelled"])
    ax.set_ylim(0, 105)
    ax.set_ylabel("percent of exposures evaded")
    ax.set_title("(c) Evasion under all-vendor avoidance")
    fig.tight_layout()
    save(fig, "fig_vendor")


def fig_route_example(lon, lat, rows):
    """
    One commute, routed both ways, with the camera cones the fast route crosses.

    Two panels because one cannot show both facts at once: at metro scale the detour is
    legible but a 60 m cone is smaller than a line width, and at street scale the cones
    are legible but the route is off-screen. The inset marks where it is drawn from.
    """
    try:
        from routes_io import read_routes
    except ImportError:
        return
    from shapely.geometry import LineString, shape
    from shapely.strtree import STRtree

    cones = list(shape(json.load(open(os.path.join(
        ROOT, "..", "server", "graphhopper", "custom_areas",
        "alpr.geojson")))["features"][0]["geometry"]).geoms)
    tree = STRtree(cones)
    index = {(r["state"], r["h_tract"], r["w_tract"]): r for r in rows}

    best = None
    for rec in read_routes(os.path.join(ROOT, "out", "routes.jsonl.gz")):
        res = index.get((rec["state"], rec["h_tract"], rec["w_tract"]))
        if res is None or res["avoid_cameras"] != 0 or res["base_cameras"] < 10:
            continue
        b = np.asarray(rec["base"])
        span = max(np.ptp(b[:, 0]), np.ptp(b[:, 1]))
        if not (0.06 < span < 0.30):
            continue
        if best is None or res["base_cameras"] > best[1]["base_cameras"]:
            best = (rec, res)
        if best[1]["base_cameras"] >= 30:
            break
    if not best:
        print("  (no suitable example route found)")
        return
    rec, res = best
    b = np.asarray(rec["base"])
    a = np.asarray(rec["avoid"])
    bl = LineString(rec["base"])
    hit = [cones[i] for i in tree.query(bl) if cones[i].intersects(bl)]

    fig = plt.figure(figsize=(11, 4.6))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.35, 1], wspace=0.18)
    ax = fig.add_subplot(gs[0, 0])

    x0, x1 = min(b[:, 0].min(), a[:, 0].min()), max(b[:, 0].max(), a[:, 0].max())
    y0, y1 = min(b[:, 1].min(), a[:, 1].min()), max(b[:, 1].max(), a[:, 1].max())
    mx, my = (x1 - x0) * .08 + .004, (y1 - y0) * .08 + .004
    sel = (lon > x0 - mx) & (lon < x1 + mx) & (lat > y0 - my) & (lat < y1 + my)
    ax.scatter(lon[sel], lat[sel], s=8, c=C_UNTAG, alpha=.65, linewidths=0, zorder=2,
               label=f"ALPR installations in view ({int(sel.sum()):,})")
    ax.plot(b[:, 0], b[:, 1], color=C_BASE, lw=2.4, zorder=4,
            label=f"fastest route — {res['base_cameras']} cameras, "
                  f"{res['base_min']:.0f} min")
    ax.plot(a[:, 0], a[:, 1], color=C_AVOID, lw=2.0, zorder=5,
            label=f"ALPR-avoiding — 0 cameras, {res['avoid_min']:.0f} min")
    for k, c in enumerate(hit):
        xs, ys = c.exterior.xy
        ax.fill(xs, ys, color="#8B0000", zorder=6,
                label="camera field of view crossed" if k == 0 else None)
    ax.plot(*b[0], "o", ms=8, color="#111111", zorder=7)
    ax.plot(*b[-1], "s", ms=8, color="#111111", zorder=7)
    ax.annotate("home", b[0], fontsize=8, xytext=(7, -9), textcoords="offset points")
    ax.annotate("work", b[-1], fontsize=8, xytext=(7, 4), textcoords="offset points")

    # zoom window centred on the densest cluster of crossed cones
    if hit:
        cx = np.array([c.centroid.x for c in hit])
        cy = np.array([c.centroid.y for c in hit])
        j = np.argmax([((cx - u) ** 2 + (cy - v) ** 2 < 0.004 ** 2).sum()
                       for u, v in zip(cx, cy)])
        zx, zy, zr = cx[j], cy[j], 0.0075
        ax.add_patch(plt.Rectangle((zx - zr, zy - zr), 2 * zr, 2 * zr, fill=False,
                                   ec="#111111", lw=1.1, zorder=8))
    ax.set_xlim(x0 - mx, x1 + mx)
    ax.set_ylim(y0 - my, y1 + my)
    ax.set_aspect(1 / math.cos(math.radians(float(b[:, 1].mean()))))
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    pct = 100 * res["extra_min"] / res["base_min"] if res["base_min"] else 0
    ax.set_title(f"(a) {rec['state'].upper()} commute: {res['base_cameras']} cameras "
                 f"evaded for +{res['extra_min']:.1f} min ({pct:.1f}%)", y=1.0, pad=8)
    ax.legend(fontsize=7.5, loc="lower right")

    ax = fig.add_subplot(gs[0, 1])
    zsel = (lon > zx - zr) & (lon < zx + zr) & (lat > zy - zr) & (lat < zy + zr)
    for i in tree.query(LineString([(zx - zr, zy - zr), (zx + zr, zy + zr)]).envelope):
        c = cones[i]
        if c.centroid.x < zx - zr or c.centroid.x > zx + zr:
            continue
        if c.centroid.y < zy - zr or c.centroid.y > zy + zr:
            continue
        xs, ys = c.exterior.xy
        ax.fill(xs, ys, color="#BBBBBB", ec="#888888", lw=.4, zorder=2)
    for c in hit:
        if abs(c.centroid.x - zx) > zr or abs(c.centroid.y - zy) > zr:
            continue
        xs, ys = c.exterior.xy
        ax.fill(xs, ys, color="#8B0000", ec="#5A0000", lw=.4, zorder=4)
    ax.plot(b[:, 0], b[:, 1], color=C_BASE, lw=3.0, zorder=5, solid_capstyle="round")
    ax.plot(a[:, 0], a[:, 1], color=C_AVOID, lw=2.6, zorder=6, solid_capstyle="round")
    ax.scatter(lon[zsel], lat[zsel], s=26, c="#111111", zorder=7, marker="^",
               linewidths=0)
    ax.set_xlim(zx - zr, zx + zr)
    ax.set_ylim(zy - zr, zy + zr)
    ax.set_aspect(1 / math.cos(math.radians(float(zy))))
    ax.set_xlabel("longitude")
    # Four ticks at this span; the default gives six 7-significant-digit labels that
    # collide into an unreadable smear.
    ax.xaxis.set_major_locator(plt.MaxNLocator(4))
    ax.yaxis.set_major_locator(plt.MaxNLocator(5))
    ax.tick_params(axis="x", labelsize=7.5)
    ax.set_title("(b) Street scale: directional fields of view", y=1.0, pad=8)
    handles = [
        Line2D([], [], marker="^", ls="", color="#111111", ms=6, label="camera"),
        Patch(fc="#8B0000", label="cone crossed by fast route"),
        Patch(fc="#BBBBBB", ec="#888888", label="cone not crossed"),
        Line2D([], [], color=C_BASE, lw=3, label="fastest"),
    ]
    in_win = ((np.abs(a[:, 0] - zx) < zr) & (np.abs(a[:, 1] - zy) < zr)).any()
    if in_win:
        handles.append(Line2D([], [], color=C_AVOID, lw=2.6, label="avoiding"))
    ax.legend(handles=handles, fontsize=7.5, loc="upper left")
    save(fig, "fig_route_example")



def fig_radius(path):
    """Sensitivity of exposure and of the avoidance result to the assumed cone radius."""
    import csv as _csv
    if not os.path.exists(path):
        print("  (no radius sweep yet)")
        return
    rows = list(_csv.DictReader(open(path)))
    radii = sorted(int(k[len("base_r"):]) for k in rows[0] if k.startswith("base_r"))
    for r in rows:
        for k in list(r):
            if k.startswith(("base_r", "avoid_r")):
                r[k] = int(r[k])

    fig, axes = plt.subplots(1, 3, figsize=(11, 3.2))

    ax = axes[0]
    means = [np.mean([x[f"base_r{r}"] for x in rows]) for r in radii]
    los, his = [], []
    for r in radii:
        m, lo, hi = boot_mean_ci([x[f"base_r{r}"] for x in rows])
        los.append(m - lo); his.append(hi - m)
    ax.errorbar(radii, means, yerr=[los, his], marker="o", ms=5, lw=1.6,
                color=C_BASE, capsize=3)
    ax.axvline(60, color=C_NEUT, ls=":", lw=1)
    ax.annotate("assumed", xy=(60, ax.get_ylim()[0]), fontsize=7.5,
                xytext=(3, 4), textcoords="offset points", color=C_NEUT)
    ax.set_xlabel("assumed cone radius (m)")
    ax.set_ylabel("mean cameras passed")
    ax.set_title("(a) Exposure scales with the assumption")

    ax = axes[1]
    clean = [100 * np.mean([1 if x[f"avoid_r{r}"] == 0 else 0 for x in rows]) for r in radii]
    ax.plot(radii, clean, marker="o", ms=5, lw=1.8, color=C_AVOID)
    for r, c in zip(radii, clean):
        ax.annotate(f"{c:.1f}%", (r, c), fontsize=7.5, xytext=(0, 7),
                    textcoords="offset points", ha="center")
    ax.axvline(60, color=C_NEUT, ls=":", lw=1)
    ax.set_ylim(0, 105)
    ax.set_xlabel("radius at which the route is scored (m)")
    ax.set_ylabel("% of commutes with zero exposure")
    ax.set_title("(b) Avoidance planned at 60 m, re-scored")

    ax = axes[2]
    w = 0.38
    xx = np.arange(len(radii))
    bmean = [np.mean([x[f"base_r{r}"] for x in rows]) for r in radii]
    amean = [np.mean([x[f"avoid_r{r}"] for x in rows]) for r in radii]
    ax.bar(xx - w/2, bmean, width=w, color=C_BASE, label="unavoided")
    ax.bar(xx + w/2, amean, width=w, color=C_AVOID, label="ALPR-avoiding")
    for i, (b, a) in enumerate(zip(bmean, amean)):
        ax.text(i + w/2, a + max(bmean)*0.02, f"{100*a/max(b,1e-9):.0f}%",
                ha="center", fontsize=7.5)
    ax.set_xticks(xx, [f"{r} m" for r in radii])
    ax.set_ylabel("mean cameras passed")
    ax.set_title("(c) Residual exposure as % of unavoided")
    ax.legend(fontsize=8)
    fig.tight_layout()
    save(fig, "fig_radius")


def main():
    print("loading cameras...")
    lon, lat, cls = load_cameras()
    counts = {v: int((cls == v).sum()) for v in ("flock", "other", "untagged")}
    print(f"  {len(lon):,} cameras {counts}")

    rows = load_results(os.path.join(ROOT, "out", "results.csv"))
    print(f"  {len(rows):,} commutes")

    # cameras per 100k, for the coverage-intensity panel
    POP = {"ga": 10.9, "tx": 30.0, "ca": 39.0, "fl": 22.6, "il": 12.6, "ny": 19.6,
           "pa": 13.0, "oh": 11.8, "nc": 10.7, "az": 7.4, "wa": 7.8, "co": 5.8,
           "tn": 7.1, "mo": 6.2, "va": 8.7}
    BOX = {"ga": (30.4, -85.6, 35.0, -80.8), "tx": (25.8, -106.6, 36.5, -93.5),
           "ca": (32.5, -124.4, 42.0, -114.1), "fl": (24.5, -87.6, 31.0, -80.0),
           "il": (36.9, -91.5, 42.5, -87.0), "ny": (40.5, -79.8, 45.0, -71.8),
           "pa": (39.7, -80.5, 42.3, -74.7), "oh": (38.4, -84.8, 42.0, -80.5),
           "nc": (33.8, -84.3, 36.6, -75.4), "az": (31.3, -114.8, 37.0, -109.0),
           "wa": (45.5, -124.8, 49.0, -116.9), "co": (37.0, -109.1, 41.0, -102.0),
           "tn": (35.0, -90.3, 36.7, -81.6), "mo": (36.0, -95.8, 40.6, -89.1),
           "va": (36.5, -83.7, 39.5, -75.2)}
    per100k = {}
    for s, (s0, w0, n0, e0) in BOX.items():
        m = (lat >= s0) & (lat <= n0) & (lon >= w0) & (lon <= e0)
        per100k[s] = int(m.sum()) / (POP[s] * 10)

    print("figures:")
    fig_camera_map(lon, lat, cls)
    fig_exposure(rows)
    fig_cost(rows)
    fig_states(rows, per100k)
    fig_demographics(rows)
    fig_within_county(rows)
    fig_route_example(lon, lat, rows)

    fig_radius(os.path.join(ROOT, "out", "radius_sweep.csv"))

    vpath = os.path.join(ROOT, "out", "vendor.csv")
    if os.path.exists(vpath):
        vrows = load_results(vpath)
        if vrows and "base_flock" in vrows[0]:
            fig_vendor(vrows, counts)


if __name__ == "__main__":
    main()
