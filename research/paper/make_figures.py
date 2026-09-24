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
FIG = os.environ.get("TRAMES_FIG_DIR", os.path.join(HERE, "figures"))
STATES_JSON = os.path.join(HERE, "figures", "us-states.json")
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
    path = STATES_JSON
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
from wstats import Design, ci  # noqa: E402
from trend import state_locator  # noqa: E402

# Overridable so the figures can be exercised on a scratch copy of the outputs.
OUT = os.environ.get("TRAMES_OUT_DIR", os.path.join(ROOT, "out"))
DESIGN = (os.path.join(OUT, "sampling_frame.json"), os.path.join(OUT, "sample_draws.csv"))


def load_cameras():
    """(lon, lat, vendor_class) for every mapped ALPR node in the 50 states + DC. The Overpass
    regions overlap Canada and Mexico; the paper is about American commutes, so only
    installations inside a state are drawn and counted."""
    keys = ("manufacturer", "surveillance:manufacturer", "brand", "surveillance:brand")
    seen = {}
    for p in glob.glob(os.path.join(ROOT, "..", "server", "alpr", "region_cache", "*.json")):
        try:
            d = json.load(open(p))
        except Exception:
            continue
        for el in d.get("elements", []):
            if el.get("type") == "node" and el.get("lat") is not None:
                seen[el["id"]] = el
    locate = state_locator()
    lon, lat, cls = [], [], []
    for el in seen.values():
        if locate(el["lat"], el["lon"]) is None:
            continue
        t = el.get("tags") or {}
        v = next((t[k] for k in keys if t.get(k)), None)
        lon.append(el["lon"]); lat.append(el["lat"])
        cls.append("flock" if v and "flock" in v.lower() else ("other" if v else "untagged"))
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


def col(rows, k):
    return np.array([float(r[k]) for r in rows])


def numcol(rows, k):
    return np.array([float(r[k]) if r.get(k) not in ("", None) else np.nan for r in rows])


def pct_col(rows, k):
    pop = numcol(rows, "pop_total")
    return np.where(pop > 0, 100 * numcol(rows, k) / np.where(pop > 0, pop, 1), np.nan)


def wquartile_masks(D, x):
    """Commuter-weighted quartiles of x, as four row masks (the analysis's own binning)."""
    valid = ~np.isnan(x)
    qs = [D.quantile(x, q, valid) for q in (0.25, 0.5, 0.75)]
    xb = np.where(valid, x, -np.inf)
    b = 1 + (xb > qs[0]).astype(int) + (xb > qs[1]) + (xb > qs[2])
    return [valid & (b == q) for q in (1, 2, 3, 4)]


def wcounty_masks(D, x, county, min_n=40):
    """Quartiles ranked within each county (n >= min_n), pooled — as analyze.wwithin()."""
    valid = ~np.isnan(x)
    groups = {}
    for i in np.flatnonzero(valid):
        groups.setdefault(county[i], []).append(i)
    b = np.zeros(D.n, int); used = 0
    for ix in groups.values():
        if len(ix) < min_n:
            continue
        m = np.zeros(D.n, bool); m[ix] = True
        qs = [D.quantile(x, q, m) for q in (0.25, 0.5, 0.75)]
        xi = x[ix]
        b[ix] = 1 + (xi > qs[0]).astype(int) + (xi > qs[1]) + (xi > qs[2])
        used += 1
    return [b == q for q in (1, 2, 3, 4)], used


def whist(ax, x, w, bins, **kw):
    """Histogram whose bar heights are percent of commuters, not counts of sampled rows."""
    return ax.hist(x, bins=bins, weights=100 * w / w.sum(), **kw)


def wcdf(x, w):
    o = np.argsort(x, kind="stable")
    return x[o], np.cumsum(w[o]) / w.sum()


# ================================================================ figures
def fig_camera_map(lon, lat, cls):
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2),
                             gridspec_kw={"width_ratios": [1, 1], "wspace": 0.02})
    ax = axes[0]
    draw_basemap(ax)
    order = [("untagged", C_UNTAG, 0.35), ("other", C_OTHER, 0.75), ("flock", C_FLOCK, 0.5)]
    for name, colr, al in order:
        m = cls == name
        x, y = albers(lon[m], lat[m])
        ax.scatter(x, y, s=0.45, c=colr, alpha=al, linewidths=0, zorder=3, rasterized=True)
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


def fig_exposure(rows, D):
    base, avoid, w = col(rows, "base_cameras"), col(rows, "avoid_cameras"), D.w
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.0))

    ax = axes[0]
    mx = 25
    bins = np.arange(0, mx + 2) - 0.5
    whist(ax, np.clip(base, 0, mx), w, bins, color=C_BASE, edgecolor="white", lw=0.3, label="unavoided")
    whist(ax, np.clip(avoid, 0, mx), w, bins, color=C_AVOID, alpha=0.85, edgecolor="white", lw=0.3,
          label="ALPR-avoiding")
    ax.set_xlabel(f"cameras passed (clipped at {mx})")
    ax.set_ylabel("% of commuters")
    ax.set_title("(a) Exposure distribution")
    ax.legend(fontsize=8)

    ax = axes[1]
    for arr, colr, lab in ((base, C_BASE, "unavoided"), (avoid, C_AVOID, "ALPR-avoiding")):
        s, c = wcdf(arr, w)
        ax.plot(s, c, color=colr, lw=1.6, label=lab, drawstyle="steps-post")
    zero = D.share(avoid == 0)
    ax.set_xscale("symlog", linthresh=1)
    ax.set_xlabel("cameras passed")
    ax.set_ylabel("cumulative share of commuters")
    ax.set_title("(b) Cumulative exposure")
    ax.axhline(zero, color=C_NEUT, ls=":", lw=0.8)
    ax.annotate(f"{100*zero:.1f}% reach zero\nwhen avoiding", xy=(0.06, zero),
                xycoords=("axes fraction", "data"), fontsize=7.5, va="bottom", color=C_NEUT)
    ax.legend(fontsize=8, loc="lower right")

    ax = axes[2]
    thr = [1, 3, 5, 10, 20]
    xb = [100 * D.share(base >= t) for t in thr]
    xa = [100 * D.share(avoid >= t) for t in thr]
    yy = np.arange(len(thr))
    ax.barh(yy - 0.2, xb, height=0.4, color=C_BASE, label="unavoided")
    ax.barh(yy + 0.2, xa, height=0.4, color=C_AVOID, label="ALPR-avoiding")
    for i, v in enumerate(xb):
        ax.text(v + 1, i - 0.2, f"{v:.1f}%", va="center", fontsize=7)
    for i, v in enumerate(xa):
        ax.text(v + 1, i + 0.2, f"{v:.1f}%", va="center", fontsize=7)
    ax.set_yticks(yy, [f"$\\geq${t}" for t in thr])
    ax.set_xlabel("% of commuters")
    ax.set_title("(c) Commuters exceeding an exposure threshold")
    ax.set_xlim(0, 100)
    ax.legend(fontsize=8, loc="upper right")
    save(fig, "fig_exposure")


def fig_cost(rows, D):
    ex_min, bmin, evaded, w = col(rows, "extra_min"), col(rows, "base_min"), col(rows, "cameras_evaded"), D.w
    overhead = np.where(bmin > 0, 100 * ex_min / np.maximum(bmin, 1e-9), 0.0)
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.0))

    ax = axes[0]
    whist(ax, np.clip(ex_min, 0, 30), w, 60, color=C_AVOID, edgecolor="white", lw=0.2)
    med = D.quantile(ex_min, .5)
    ax.axvline(med, color=C_NEUT, ls="--", lw=1)
    ax.annotate(f"median {med:.2f} min", xy=(med, 0.92), xycoords=("data", "axes fraction"),
                fontsize=8, ha="left", xytext=(4, 0), textcoords="offset points")
    ax.set_xlabel("extra travel time (min, clipped at 30)")
    ax.set_ylabel("% of commuters")
    ax.set_title("(a) Cost of complete avoidance")

    ax = axes[1]
    s, c = wcdf(overhead, w)
    ax.plot(s, 100 * c, color=C_AVOID, lw=1.6)
    for q, lab in ((50, "median"), (75, "75th"), (90, "90th")):
        v = D.quantile(overhead, q / 100)
        ax.plot([v], [q], "o", ms=4, color=C_NEUT)
        ax.annotate(f"{lab}: {v:.1f}%", xy=(v, q), fontsize=7.5, xytext=(6, -3), textcoords="offset points")
    ax.set_xscale("symlog", linthresh=1)
    ax.set_xlabel("avoidance overhead (% of baseline time)")
    ax.set_ylabel("percentile of commuters")
    ax.set_title("(b) Relative cost")

    ax = axes[2]
    m = evaded > 0
    per = np.where(m, ex_min / np.maximum(evaded, 1), 0.0)
    whist(ax, np.clip(per[m], 0, 6), w[m], 60, color=C_OTHER, edgecolor="white", lw=0.2)
    med = D.quantile(per, .5, m)
    ax.axvline(med, color=C_NEUT, ls="--", lw=1)
    ax.annotate(f"median {med:.2f} min", xy=(med, 0.92), xycoords=("data", "axes fraction"),
                fontsize=8, xytext=(4, 0), textcoords="offset points")
    ax.set_xlabel("minutes spent per camera evaded")
    ax.set_ylabel("% of commuters who evade any")
    ax.set_title("(c) Marginal price of evasion")
    save(fig, "fig_cost")


# ---- 51-state choropleth. Alaska and Hawaii are drawn as insets in their own simple
# projections, scaled and placed below the lower 48 — the Albers projection that suits the
# lower 48 would render Alaska huge and skewed.
def _state_rings():
    gj = json.load(open(STATES_JSON))
    from trend import CODES
    for feat in gj["features"]:
        code = CODES.get(feat["properties"]["name"])
        if not code:
            continue
        g = feat["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        rings = []
        for poly in polys:
            ring = np.asarray(poly[0], dtype=float)
            lon, lat = ring[:, 0], ring[:, 1]
            if code == "ak":
                lon = np.where(lon > 0, lon - 360, lon)
                x = (lon + 152) * math.cos(math.radians(62)) * 0.0072 - 0.33
                y = (lat - 63) * 0.0072 + 0.055
            elif code == "hi":
                x = (lon + 157.5) * math.cos(math.radians(20.5)) * 0.017 - 0.14
                y = (lat - 20.5) * 0.017 + 0.03
            else:
                x, y = albers(lon, lat)
            rings.append((x, y))
        yield code, rings


def fig_states(D, by_state, per100k):
    import matplotlib.colors as mcolors
    from matplotlib.cm import ScalarMappable
    val = {r["state"]: float(r["pct_ge1"]) for r in by_state}
    mean = {r["state"]: float(r["mean_cameras"]) for r in by_state}
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.9), gridspec_kw={"width_ratios": [1.35, 1], "wspace": 0.12})

    ax = axes[0]
    cmap = plt.get_cmap("magma_r")
    norm = mcolors.Normalize(vmin=0, vmax=100)
    for code, rings in _state_rings():
        v = val.get(code)
        for x, y in rings:
            ax.fill(x, y, color=cmap(norm(v)) if v is not None else "#EEEEEE",
                    ec="white", lw=0.35, zorder=2)
    ax.set_aspect("equal"); ax.axis("off")
    ax.set_xlim(-0.44, 0.52); ax.set_ylim(-0.08, 0.62)
    ax.set_title("(a) Share of commuters passing at least one ALPR")
    cax = ax.inset_axes([0.60, 0.035, 0.36, 0.035])
    cb = fig.colorbar(ScalarMappable(norm=norm, cmap=cmap), cax=cax, orientation="horizontal")
    cb.set_label("% of commuters", fontsize=7.5); cb.ax.tick_params(labelsize=7)

    ax = axes[1]
    st = sorted(mean)
    x = np.array([per100k.get(s, np.nan) for s in st]); y = np.array([mean[s] for s in st])
    size = np.array([D.W[s] for s in st]); size = 12 + 90 * size / size.max()
    ax.scatter(x, y, s=size, color=C_OTHER, alpha=0.75, linewidths=0.4, edgecolors="white", zorder=3)
    good = ~np.isnan(x)
    label = set()
    if good.sum() > 2:
        k, b = np.polyfit(x[good], y[good], 1)
        xs = np.linspace(x[good].min(), x[good].max(), 20)
        ax.plot(xs, k * xs + b, color=C_NEUT, ls="--", lw=1)
        r = np.corrcoef(x[good], y[good])[0, 1]
        ax.annotate(f"$r$ = {r:.2f} across {int(good.sum())} states", xy=(0.04, 0.9),
                    xycoords="axes fraction", fontsize=9)
        # Fifty-one labels are unreadable; name the twelve most populous and the six
        # furthest from the line, which are the ones the text discusses.
        resid = np.abs(y - (k * x + b))
        label |= {st[i] for i in np.argsort([-D.W[s] for s in st])[:12]}
        label |= {st[i] for i in np.argsort(-np.nan_to_num(resid))[:6]}
    fig.canvas.draw()
    placed = []
    for s, xi, yi in zip(st, x, y):
        if not (s in label or good.sum() <= 20):
            continue
        # Try a few offsets and keep the first whose box clears every label already drawn.
        for dx, dy in ((3, 2), (3, -9), (-14, 2), (-14, -9), (3, 9)):
            t = ax.annotate(s.upper(), (xi, yi), fontsize=6.5, xytext=(dx, dy), textcoords="offset points")
            bb = t.get_window_extent(renderer=fig.canvas.get_renderer())
            if not any(bb.overlaps(o) for o in placed):
                placed.append(bb)
                break
            t.remove()
    ax.set_xlabel("mapped cameras per 100k residents")
    ax.set_ylabel("mean cameras passed per commute")
    ax.set_title("(b) Measured exposure tracks mapping intensity")
    save(fig, "fig_states")


def _contrast_bars(ax, D, metric, den, masks, labels, title, ylabel):
    reps = D.boot_ratio(metric, den, masks)
    pts = [D.ratio(metric, den, m) for m in masks]
    lo, hi = ci(reps)
    xx = np.arange(4)
    ax.bar(xx, pts, color=QCOLS, width=0.66, yerr=[np.array(pts) - lo, hi - np.array(pts)],
           capsize=3, error_kw={"lw": 1, "ecolor": "#222222"})
    ax.set_xticks(xx, labels)
    ax.set_ylabel(ylabel)
    ax.set_title(title)


def fig_demographics(rows, D):
    bc, km, ones = col(rows, "base_cameras"), col(rows, "base_km"), np.ones(D.n)
    fig, axes = plt.subplots(2, 3, figsize=(11, 5.6))
    specs = [(numcol(rows, "median_income"), ["Q1\nlowest", "Q2", "Q3", "Q4\nhighest"], "median household income"),
             (pct_col(rows, "nh_black"), ["Q1\nleast", "Q2", "Q3", "Q4\nmost"], "% non-Hispanic Black"),
             (pct_col(rows, "hispanic"), ["Q1\nleast", "Q2", "Q3", "Q4\nmost"], "% Hispanic")]
    for j, (x, labs, name) in enumerate(specs):
        masks = wquartile_masks(D, x)
        _contrast_bars(axes[0][j], D, bc, ones, masks, labs, f"({'abc'[j]}) by {name}", "mean cameras passed")
        _contrast_bars(axes[1][j], D, bc, km, masks, labs, "", "cameras per km")
    axes[0][0].set_ylabel("mean cameras passed")
    axes[1][0].set_ylabel("cameras per km driven")
    fig.text(0.5, 0.985, "Origin-tract quartile contrasts, commuter-weighted (95% stratified-bootstrap CI)",
             ha="center", fontsize=10)
    fig.tight_layout(rect=[0, 0, 1, 0.965])
    save(fig, "fig_demographics")


def fig_within_county(rows, D, min_n=40):
    bc, ones = col(rows, "base_cameras"), np.ones(D.n)
    county = np.array([r["h_tract"][:5] for r in rows])
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.4))
    for j, (x, name) in enumerate([(numcol(rows, "median_income"), "median household income"),
                                   (pct_col(rows, "nh_black"), "% non-Hispanic Black"),
                                   (pct_col(rows, "hispanic"), "% Hispanic")]):
        nat = wquartile_masks(D, x)
        loc, used = wcounty_masks(D, x, county, min_n)
        ax = axes[j]
        ratios = {}
        for offs, masks, colr, lab in ((-0.19, nat, "#0072B2", "national quartiles"),
                                       (0.19, loc, "#D55E00", f"within-county ({used} counties)")):
            pts = np.array([D.mean(bc, m) for m in masks])
            lo, hi = ci(D.boot_ratio(bc, ones, masks))
            ax.bar(np.arange(4) + offs, pts, width=0.36, color=colr, label=lab,
                   yerr=[pts - lo, hi - pts], capsize=2.5, error_kw={"lw": 0.9, "ecolor": "#222222"})
            ratios[lab.split()[0]] = pts[3] / max(pts[0], 1e-9)
        ax.set_ylim(0, ax.get_ylim()[1] * 1.34)
        ax.annotate(f"Q4/Q1  national {ratios['national']:.2f}$\\times$"
                    f" $\\rightarrow$ within-county {ratios['within-county']:.2f}$\\times$",
                    xy=(0.5, 0.965), xycoords="axes fraction", ha="center", va="top", fontsize=7.8,
                    bbox=dict(boxstyle="round,pad=0.3", fc="#F5F5F5", ec="#BBBBBB", lw=0.6))
        ax.set_xticks(np.arange(4), ["Q1", "Q2", "Q3", "Q4"])
        ax.set_ylabel("mean cameras passed")
        ax.set_title(f"({'abc'[j]}) {name}")
        ax.legend(fontsize=7, loc="upper left", bbox_to_anchor=(0.0, 0.87))
    fig.tight_layout()
    save(fig, "fig_within_county")


def fig_vendor(vrows, D, counts):
    vendors = ["flock", "other", "untagged"]
    exp = {v: D.mean(col(vrows, "base_" + v)) for v in vendors}
    avo = {v: D.mean(col(vrows, "avoid_" + v)) for v in vendors}
    tot_exp, tot_cam = sum(exp.values()), sum(counts.values())
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.2))
    xx = np.arange(3)
    names = ["Flock", "other\nvendor", "unlabelled"]

    ax = axes[0]
    inst = [100 * counts[v] / tot_cam for v in vendors]
    expo = [100 * exp[v] / tot_exp for v in vendors]
    ax.bar(xx - 0.2, inst, width=0.4, color="#999999", label="share of installations")
    ax.bar(xx + 0.2, expo, width=0.4, color=C_OTHER, label="share of commuter exposure")
    for i, (a, b) in enumerate(zip(inst, expo)):
        ax.text(i - 0.2, a + 1.2, f"{a:.1f}", ha="center", fontsize=7.5)
        ax.text(i + 0.2, b + 1.2, f"{b:.1f}", ha="center", fontsize=7.5)
    ax.set_xticks(xx, names); ax.set_ylabel("percent")
    ax.set_title("(a) Deployment share vs. exposure share"); ax.legend(fontsize=7.5)

    ax = axes[1]
    reach = [(exp[v] / tot_exp) / (counts[v] / tot_cam) for v in vendors]
    ax.bar(xx, reach, color=[C_FLOCK, C_OTHER, C_UNTAG], width=0.6)
    ax.axhline(1, color=C_NEUT, ls=":", lw=0.8)
    for i, v in enumerate(reach):
        ax.text(i, v + max(reach) * 0.02, f"{v:.2f}", ha="center", fontsize=8)
    ax.set_xticks(xx, names); ax.set_ylabel("exposure share ÷ installation share")
    ax.set_title("(b) Reach per unit deployed")

    ax = axes[2]
    ev = [100 * (exp[v] - avo[v]) / max(exp[v], 1e-12) for v in vendors]
    ax.bar(xx, ev, color=[C_FLOCK, C_OTHER, C_UNTAG], width=0.6)
    for i, v in enumerate(ev):
        ax.text(i, v + 0.6, f"{v:.1f}%", ha="center", fontsize=8)
    ax.set_xticks(xx, names); ax.set_ylim(0, 105)
    ax.set_ylabel("percent of exposure evaded")
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
    if not os.path.exists(path):
        print("  (no radius sweep yet)")
        return
    rows = list(csv.DictReader(open(path)))
    radii = sorted(int(k[len("base_r"):]) for k in rows[0] if k.startswith("base_r"))
    D = Design(rows, *DESIGN)
    ones = np.ones(D.n)
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.2))

    ax = axes[0]
    means = np.array([D.mean(col(rows, f"base_r{r}")) for r in radii])
    lo, hi = ci(np.column_stack([D.boot_ratio(col(rows, f"base_r{r}"), ones)[:, 0] for r in radii]))
    ax.errorbar(radii, means, yerr=[means - lo, hi - means], marker="o", ms=5, lw=1.6, color=C_BASE, capsize=3)
    ax.axvline(60, color=C_NEUT, ls=":", lw=1)
    ax.annotate("assumed", xy=(60, ax.get_ylim()[0]), fontsize=7.5, xytext=(3, 4),
                textcoords="offset points", color=C_NEUT)
    ax.set_xlabel("assumed cone radius (m)"); ax.set_ylabel("mean cameras passed")
    ax.set_title("(a) Exposure scales with the assumption")

    ax = axes[1]
    clean = [100 * D.share(col(rows, f"avoid_r{r}") == 0) for r in radii]
    ax.plot(radii, clean, marker="o", ms=5, lw=1.8, color=C_AVOID)
    for r, c in zip(radii, clean):
        ax.annotate(f"{c:.1f}%", (r, c), fontsize=7.5, xytext=(0, 7), textcoords="offset points", ha="center")
    ax.axvline(60, color=C_NEUT, ls=":", lw=1)
    ax.set_ylim(0, 105)
    ax.set_xlabel("radius at which the route is scored (m)")
    ax.set_ylabel("% of commuters with zero exposure")
    ax.set_title("(b) Avoidance planned at 60 m, re-scored")

    ax = axes[2]
    w = 0.38
    xx = np.arange(len(radii))
    bmean = [D.mean(col(rows, f"base_r{r}")) for r in radii]
    amean = [D.mean(col(rows, f"avoid_r{r}")) for r in radii]
    ax.bar(xx - w/2, bmean, width=w, color=C_BASE, label="unavoided")
    ax.bar(xx + w/2, amean, width=w, color=C_AVOID, label="ALPR-avoiding")
    for i, (b, a) in enumerate(zip(bmean, amean)):
        ax.text(i + w/2, a + max(bmean)*0.02, f"{100*a/max(b,1e-9):.0f}%", ha="center", fontsize=7.5)
    ax.set_xticks(xx, [f"{r} m" for r in radii])
    ax.set_ylabel("mean cameras passed")
    ax.set_title("(c) Residual exposure as % of unavoided")
    ax.legend(fontsize=8)
    fig.tight_layout()
    save(fig, "fig_radius")


def fig_trend():
    """The same commutes on the same roads against the camera map of each date."""
    tpath, mpath = os.path.join(OUT, "trend.csv"), os.path.join(OUT, "mapped_alpr_monthly_by_state.csv")
    if not os.path.exists(tpath):
        print("  (no trend yet)")
        return
    import datetime as dt
    T = list(csv.DictReader(open(tpath)))
    day = lambda s: dt.date.fromisoformat(s[:10])
    dates = [day(r["date"]) for r in T]
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.3))

    ax = axes[0]
    if os.path.exists(mpath):
        by = {}
        for r in csv.DictReader(open(mpath)):
            by[r["month"]] = by.get(r["month"], 0) + int(r["mapped_alpr"])
        ms = sorted(by)
        ax.plot([day(m + "-01") for m in ms], [by[m] for m in ms], color=C_NEUT, lw=1.3,
                label="mapped, monthly (OSM history)")
    ax.plot(dates, [float(r["cameras_us"]) for r in T], "o", ms=5, color=C_FLOCK, zorder=3,
            label="study snapshots")
    ax.set_yscale("log")
    ax.axvline(dt.date(2024, 10, 15), color=C_OTHER, ls=":", lw=1)
    ax.annotate("DeFlock\nfounded\nOct 2024", xy=(dt.date(2024, 10, 15), 0.62),
                xycoords=("data", "axes fraction"), fontsize=7.5, color=C_OTHER,
                xytext=(-4, 0), textcoords="offset points", ha="right")
    ax.set_xlim(dt.date(2023, 1, 1), dt.date(2026, 11, 1))
    ax.set_ylabel("mapped ALPR, 50 states + DC")
    ax.set_title("(a) The map filled in")
    ax.legend(fontsize=7.5, loc="lower right")
    ax.tick_params(axis="x", labelrotation=30, labelsize=7.5)

    ax = axes[1]
    p = np.array([float(r["pct_ge1"]) for r in T])
    lo = np.array([float(r["pct_ge1_lo"]) for r in T]); hi = np.array([float(r["pct_ge1_hi"]) for r in T])
    ax.errorbar(dates, p, yerr=[p - lo, hi - p], marker="o", ms=5, lw=1.6, color=C_BASE, capsize=3,
                label="pass $\\geq$1 ALPR")
    has = [r.get("pct_zero_after") not in ("", None) for r in T]
    if any(has):
        dz = [d for d, h in zip(dates, has) if h]
        z = np.array([100 - float(r["pct_zero_after"]) for r, h in zip(T, has) if h])
        ax.plot(dz, z, marker="s", ms=5, lw=1.6, color=C_AVOID, label="still pass one when avoiding")
    ax.set_ylim(0, 100)
    ax.set_xlim(dt.date(2023, 10, 1), dt.date(2026, 11, 1))
    ax.set_ylabel("% of commuters")
    ax.set_title("(b) Exposure, and what avoidance leaves")
    h, l = ax.get_legend_handles_labels()  # errorbar entries list last; exposure reads first
    order = sorted(range(len(l)), key=lambda i: not l[i].startswith("pass"))
    ax.legend([h[i] for i in order], [l[i] for i in order], fontsize=7.5, loc="upper left")
    ax.tick_params(axis="x", labelrotation=30, labelsize=7.5)

    ax = axes[2]
    if any(has):
        top = 0
        for k, mk, colr, lab in (("median_extra_min_given_exposed", "o", C_AVOID, "median extra min (exposed commutes)"),
                                 ("median_min_per_camera", "^", C_OTHER, "median min per camera evaded")):
            v = np.array([float(r[k]) for r, h in zip(T, has) if h])
            if k + "_lo" in T[0]:
                lo = np.array([float(r[k + "_lo"]) for r, h in zip(T, has) if h])
                hi = np.array([float(r[k + "_hi"]) for r, h in zip(T, has) if h])
                ax.errorbar(dz, v, yerr=[v - lo, hi - v], marker=mk, ms=5, lw=1.6, capsize=3, color=colr, label=lab)
                top = max(top, hi.max())
            else:
                ax.plot(dz, v, marker=mk, ms=5, lw=1.6, color=colr, label=lab)
                top = max(top, v.max())
        ax.set_ylim(0, top * 1.3)
    ax.set_xlim(dt.date(2023, 10, 1), dt.date(2026, 11, 1))
    ax.set_ylabel("minutes")
    ax.set_title("(c) The price of refusal")
    ax.legend(fontsize=7.5, loc="upper left")
    ax.tick_params(axis="x", labelrotation=30, labelsize=7.5)
    fig.tight_layout()
    save(fig, "fig_trend")


def fig_trend_gradients():
    """The Q4/Q1 contrasts of fig_within_county, re-asked on every date's map."""
    path = os.path.join(OUT, "trend.csv")
    if not os.path.exists(path):
        return
    import datetime as dt
    T = list(csv.DictReader(open(path)))
    if "black_national" not in T[0]:
        return
    dates = [dt.date.fromisoformat(r["date"][:10]) for r in T]
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.3), sharey=True)
    for j, (a, name) in enumerate([("black", "% non-Hispanic Black"), ("hispanic", "% Hispanic"),
                                   ("income", "median household income")]):
        ax = axes[j]
        for offs, scope, colr, mk, lab in ((-12, "national", "#0072B2", "o", "national quartiles"),
                                           (12, "within", "#D55E00", "s", "within-county quartiles")):
            k = f"{a}_{scope}"
            v = np.array([float(r[k]) for r in T])
            lo = np.array([float(r[k + "_lo"]) for r in T]); hi = np.array([float(r[k + "_hi"]) for r in T])
            ax.errorbar([d + dt.timedelta(days=offs) for d in dates], v, yerr=[v - lo, hi - v], marker=mk,
                        ms=4.5, lw=1.3, capsize=2.5, color=colr, label=lab)
        ax.axhline(1, color=C_NEUT, ls="--", lw=0.8)
        ax.set_yscale("log")
        ax.set_ylim(0.4, 3.2)
        ax.set_yticks([0.5, 0.75, 1, 1.5, 2, 3], ["0.5", "0.75", "1", "1.5", "2", "3"])
        ax.minorticks_off()
        ax.set_xlim(dt.date(2023, 10, 1), dt.date(2026, 11, 1))
        ax.tick_params(axis="x", labelrotation=30, labelsize=7.5)
        ax.set_title(f"({'abc'[j]}) {name}")
        if j == 0:
            ax.set_ylabel("Q4/Q1 mean cameras passed")
            ax.legend(fontsize=7.5, loc="upper right")
    fig.tight_layout()
    save(fig, "fig_trend_gradients")


def fig_trend_states():
    """Share of each state's commuters passing a mapped reader, date by date."""
    path = os.path.join(OUT, "trend_by_state.csv")
    if not os.path.exists(path):
        return
    R = list(csv.DictReader(open(path)))
    dates = sorted({r["date"] for r in R})
    states = sorted({r["state"] for r in R})
    V = {(r["state"], r["date"]): float(r["pct_ge1"]) for r in R}
    states.sort(key=lambda s: -V[(s, dates[-1])])
    M = np.array([[V[(s, d)] for s in states] for d in dates])
    fig, ax = plt.subplots(figsize=(11, 0.55 * len(dates) + 1.3))
    im = ax.imshow(M, aspect="auto", cmap="magma_r", vmin=0, vmax=100)
    ax.set_xticks(np.arange(len(states)), [s.upper() for s in states], rotation=90, fontsize=6.5)
    ax.set_yticks(np.arange(len(dates)), [d[:7] for d in dates], fontsize=7.5)
    ax.grid(False)
    for i in range(len(dates)):
        for j in range(len(states)):
            ax.text(j, i, f"{M[i, j]:.0f}", ha="center", va="center", fontsize=4.6,
                    color="white" if M[i, j] > 55 else "#222222")
    cb = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01)
    cb.set_label("% of commuters\npassing $\\geq$1 ALPR", fontsize=7.5)
    ax.set_title("Share of each state's commuters passing a mapped ALPR, by map date "
                 "(states ordered by the latest date)", fontsize=9.5)
    save(fig, "fig_trend_states")


def main():
    print("loading cameras (50 states + DC)...")
    lon, lat, cls = load_cameras()
    counts = {v: int((cls == v).sum()) for v in ("flock", "other", "untagged")}
    print(f"  {len(lon):,} cameras {counts}")

    rows = load_results(os.path.join(OUT, "results.csv"))
    D = Design(rows, *DESIGN)
    print(f"  {len(rows):,} commutes in {len(D.states)} states (commuter-weighted)")

    by_state = list(csv.DictReader(open(os.path.join(OUT, "by_state.csv"))))
    cams = [r for r in csv.DictReader(open(os.path.join(OUT, "cameras_by_state.csv")))]
    latest = max(r["date"] for r in cams)
    per100k = {r["state"]: float(r["per_100k"]) for r in cams if r["date"] == latest}

    print("figures:")
    fig_camera_map(lon, lat, cls)
    fig_exposure(rows, D)
    fig_cost(rows, D)
    fig_states(D, by_state, per100k)
    fig_demographics(rows, D)
    fig_within_county(rows, D)
    fig_route_example(lon, lat, rows)
    fig_radius(os.path.join(OUT, "radius_sweep.csv"))
    vpath = os.path.join(OUT, "vendor.csv")
    if os.path.exists(vpath):
        vrows = load_results(vpath)
        if vrows and "base_flock" in vrows[0]:
            fig_vendor(vrows, Design(vrows, *DESIGN), counts)
    fig_trend()
    fig_trend_gradients()
    fig_trend_states()


if __name__ == "__main__":
    main()
