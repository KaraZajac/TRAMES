#!/usr/bin/env python3
"""
Where the cameras stand. The commute analysis asks how many cameras a resident of each tract
drives past; this asks where cameras are sited relative to neighbourhoods, their edges, and the
people who live in them. Two hypotheses motivate it, and each makes predictions the commute
measure cannot test.

  1. Perimeter. Cameras watch who goes in and out of marginalized neighbourhoods rather than
     standing inside them. Predictions: (a) high-minority tracts carry more cameras at their
     edges, relative to their interiors, than other tracts do; (b) cameras are over-represented
     on boundaries between demographically different tracts ("seams"), relative to roads of the
     same class in the same county; (c) at a seam, cameras stand on, or face, one side.
  2. Protection. More cameras where residents are wealthier, bought by police for them or by
     them. Predictions: more cameras per resident and per road-kilometre in richer tracts,
     especially police-operated cameras and cameras on residential streets; and cameras
     ringing the limits of municipalities richer than their county, facing in.

Method: case-control on the road network. Cameras are cases; a length-weighted sample of road
points (road_sample.py) are controls; each camera is classed by the road it watches (the
highest-class road within 30 m). The ratio of cameras to road points in a group is proportional
to cameras per road-kilometre there, so every comparison is per kilometre of the same kind of
road. Every contrast is stratified by county, as the commute analysis is, because mapping effort
varies from place to place and a between-county contrast measures mappers as much as cameras.
Intervals resample whole counties within states: neighbouring tracts are not independent.

    python siting.py --roads out/siting/roads.npz --tiles ../server/alpr/region_cache \\
        --report out/analysis_siting.txt --tables out/siting
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
import time

import numpy as np
import shapely

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "server", "alpr"))
import geo  # noqa: E402
from build_cones import DIRECTION_KEYS, parse_directions, vendor_of  # noqa: E402

SEED, B = 20260725, 2000
EDGE = 50.0        # a point this close to a unit boundary is "at the edge"
NBR = 100.0        # neighbour search radius
WATCH = 30.0       # a camera watches the highest-class road within this distance
LOOK = 30.0        # a camera head "looks at" the point this far along its bearing
GROUPS = ("major", "minor", "service")
GROUP_OF_CLASS = np.array([0, 0, 0, 0, 0, 1, 1, 1, 2])   # motorway..tertiary | unclassified..living | service

OPERATORS = [
    ("police", r"police|sheriff|\bpd\b|\bp\.d\.|highway patrol|state patrol|trooper|constable|marshal"
               r"|public safety|\bnypd\b|\blapd\b|\bchp\b|\bdps\b"),
    ("transport", r"transportation|\bdot\b|\btoll|turnpike|thruway|expressway|transit|port authority"),
    ("HOA", r"\bhoa\b|homeowner|home owner|owners'? association|property owners|community association"
            r"|maintenance association|civic association|neighbou?rhood association|\bpoa\b|condominium|subdivision"),
    ("business", r"lowe'?s|home depot|walmart|wal-mart|simon property|\bmall\b|target|costco|kroger|publix"
                 r"|shopping|retail|\binc\b|\bllc\b|\bcorp|company|properties|realty|hotel|casino|\bbank\b"),
    ("school", r"universit|college|school|campus|academy"),
    ("municipal", r"city of|town of|village of|county|borough|township|municipal|government|parish"),
    ("Flock (customer unknown)", r"^flock"),
]


def operator_class(tags):
    name = " ".join(v for v in (tags.get("operator"), tags.get("owner")) if v).strip().lower()
    if not name:
        return "untagged"
    for label, rx in OPERATORS:
        if re.search(rx, name):
            return label
    return "other named"


# --- points ---------------------------------------------------------------------------------

def load_points(roads_path, tile_dir):
    R = np.load(roads_path)
    tags = {}
    for p in glob.glob(os.path.join(tile_dir, "tile_*.json")):
        for e in json.load(open(p)).get("elements", ()):
            if e.get("type") == "node":
                tags[e["id"]] = e.get("tags") or {}
    D = R["cam_d_by_cls"]
    # the road a camera watches: the highest class within WATCH m, else its nearest road
    cls = np.full(len(D), -1, np.int64)
    close = (D >= 0) & (D <= WATCH)
    has = close.any(axis=1)
    cls[has] = np.argmax(close[has], axis=1)
    near = ~has & (R["cam_near_cls"] >= 0)
    cls[near] = R["cam_near_cls"][near]
    ids = R["cam_id"]
    T = [tags.get(int(i), {}) for i in ids]
    vend = []
    for t in T:
        v = (vendor_of(t) or "").lower()
        vend.append("flock" if "flock" in v else ("other" if v else "untagged"))
    heads = []
    for t in T:
        raw = next((t[k] for k in DIRECTION_KEYS if t.get(k)), None)
        heads.append([b for b, _ in parse_directions(raw)] if raw else [])
    cams = {"id": ids, "lon": R["cam_lon"], "lat": R["cam_lat"], "cls": cls,
            "group": np.where(cls >= 0, GROUP_OF_CLASS[np.maximum(cls, 0)], -1),
            "vendor": np.array(vend), "op": np.array([operator_class(t) for t in T]),
            "zone": np.array([(t.get("surveillance:zone") or "").lower() for t in T]),
            "heads": heads}
    nulls = {"lon": R["null_lon"], "lat": R["null_lat"], "cls": R["null_cls"].astype(np.int64)}
    nulls["group"] = GROUP_OF_CLASS[nulls["cls"]]
    return cams, nulls, float(R["spacing"]) / 1000


def regions_of(lon, lat):
    r = np.zeros(len(lon), np.int8)                   # 0 conus, 1 ak, 2 hi
    r[(lat > 51) & ((lon < -129) | (lon > 170))] = 1
    r[(lat > 18) & (lat < 23) & (lon > -161) & (lon < -154)] = 2
    return r


REG = ("conus", "ak", "hi")


def assign(U, lon, lat, neighbours=True, chunk=400_000):
    """Unit containing each point (-1 if none), distance to that unit's boundary, and the nearest
    other unit within NBR with its distance (-1 / inf if none)."""
    n = len(lon)
    unit = np.full(n, -1, np.int64); bd = np.full(n, np.inf)
    nbr = np.full(n, -1, np.int64); nd = np.full(n, np.inf)
    reg = regions_of(lon, lat)
    for ri, rname in enumerate(REG):
        ui = np.flatnonzero(U["region"] == rname)
        pi = np.flatnonzero(reg == ri)
        if not len(ui) or not len(pi):
            continue
        geoms = U["geom"][ui]
        tree = shapely.STRtree(geoms)
        bnd = shapely.boundary(geoms)
        x, y = geo.albers(lon[pi], lat[pi], rname)
        for c0 in range(0, len(pi), chunk):
            gi_all = pi[c0:c0 + chunk]
            pts = shapely.points(x[c0:c0 + chunk], y[c0:c0 + chunk])
            p, u = tree.query(pts, predicate="intersects")
            if not len(p):
                continue
            o = np.argsort(p, kind="stable"); p, u = p[o], u[o]
            f = np.r_[True, p[1:] != p[:-1]]
            p, u = p[f], u[f]
            gi = gi_all[p]
            unit[gi] = ui[u]
            d = shapely.distance(pts[p], bnd[u])
            bd[gi] = d
            if not neighbours:
                continue
            near = np.flatnonzero(d <= NBR)
            if not len(near):
                continue
            pn = pts[p[near]]
            q, v = tree.query(pn, predicate="dwithin", distance=NBR)
            keep = v != u[near][q]
            q, v = q[keep], v[keep]
            if not len(q):
                continue
            dv = shapely.distance(pn[q], geoms[v])
            o = np.lexsort((dv, q)); q, v, dv = q[o], v[o], dv[o]
            f = np.r_[True, q[1:] != q[:-1]]
            tgt = gi[near][q[f]]
            nbr[tgt], nd[tgt] = ui[v[f]], dv[f]
    return unit, bd, nbr, nd


def nearest_within(U, lon, lat, dist):
    """For points outside every unit: the nearest unit within dist (-1 if none) and the distance."""
    n = len(lon)
    near, nd = np.full(n, -1, np.int64), np.full(n, np.inf)
    reg = regions_of(lon, lat)
    for ri, rname in enumerate(REG):
        ui = np.flatnonzero(U["region"] == rname)
        pi = np.flatnonzero(reg == ri)
        if not len(ui) or not len(pi):
            continue
        tree = shapely.STRtree(U["geom"][ui])
        x, y = geo.albers(lon[pi], lat[pi], rname)
        pts = shapely.points(x, y)
        (p, u), d = tree.query_nearest(pts, max_distance=dist, return_distance=True, all_matches=False)
        near[pi[p]], nd[pi[p]] = ui[u], d
    return near, nd


CACHE = {"dir": None, "src": 0.0}


def cached(name, fn, *a, **k):
    """Point-to-unit assignment is most of the run time and depends only on the points and the
    boundaries: keep it beside the tables, invalidated when roads.npz changes."""
    path = os.path.join(CACHE["dir"], f"assign_{name}.npz") if CACHE["dir"] else None
    if path and os.path.exists(path):
        z = np.load(path)
        if float(z["src"]) == CACHE["src"]:
            return tuple(z[f"a{i}"] for i in range(int(z["k"])))
    out = fn(*a, **k)
    if path:
        np.savez(path, src=CACHE["src"], k=len(out), **{f"a{i}": x for i, x in enumerate(out)})
    return out


# --- statistics -----------------------------------------------------------------------------

def wquartile_cuts(x, w):
    o = np.argsort(x, kind="stable")
    c = np.cumsum(w[o]); c = c / c[-1]
    return [x[o][min(np.searchsorted(c, q), len(o) - 1)] for q in (0.25, 0.5, 0.75)]


def quartiles(x, w, groups=None, min_units=8):
    """Population-weighted quartile (1-4, 0 = unranked) nationally, or ranked inside each group."""
    q = np.zeros(len(x), np.int8)
    ok = ~np.isnan(x) & (w > 0)
    idx = np.flatnonzero(ok)
    if groups is None:
        blocks = [idx]
    else:
        idx = idx[np.argsort(groups[idx], kind="stable")]
        g = groups[idx]
        cut = np.flatnonzero(np.r_[True, g[1:] != g[:-1], True])
        blocks = [idx[a:b] for a, b in zip(cut[:-1], cut[1:]) if b - a >= min_units]
    for ii in blocks:
        c = wquartile_cuts(x[ii], w[ii])
        q[ii] = 1 + (x[ii] > c[0]).astype(np.int8) + (x[ii] > c[1]) + (x[ii] > c[2])
    return q


class Boot:
    """Cluster bootstrap: whole counties resampled within states. Statistics are ratios of sums
    over counties, so every replicate is a weighted sum: W (B x counties) @ per-county totals."""

    def __init__(self, counties):
        self.cty = np.unique(counties)
        st = np.array([c[:2] for c in self.cty])
        self.W = np.zeros((B, len(self.cty)), np.float32)
        rng = np.random.default_rng(SEED)
        for s in np.unique(st):
            ix = np.flatnonzero(st == s)
            draws = rng.integers(0, len(ix), (B, len(ix)))
            cnt = np.zeros((B, len(ix)), np.float32)
            np.add.at(cnt, (np.repeat(np.arange(B), len(ix)), draws.ravel()), 1)
            self.W[:, ix] = cnt

    def index(self, counties):
        return np.searchsorted(self.cty, counties)

    def table(self, cidx, cols, values=None, ncols=None):
        """Per-county totals: counties x columns (values default to 1 per row)."""
        M = np.zeros((len(self.cty), ncols if ncols else int(cols.max()) + 1))
        np.add.at(M, (cidx, cols), 1.0 if values is None else values)
        return M

    def reps(self, M):
        return self.W @ M


def div(a, b):
    """a / b, undefined (NaN) where b is 0 — a replicate with nothing in a group has no rate."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(np.asarray(b) != 0, np.asarray(a, float) / np.where(np.asarray(b) != 0, b, 1), np.nan)


def ci(x):
    x = x[np.isfinite(x)]
    return (np.percentile(x, 2.5), np.percentile(x, 97.5)) if len(x) else (np.nan, np.nan)


def fmt_ratio(r, lo, hi):
    tag = "" if lo <= 1 <= hi else "  *"
    return f"{r:5.2f}x [{lo:.2f}-{hi:.2f}]{tag}"


# --- analyses -------------------------------------------------------------------------------

def attributes(U):
    pop = U["pop_total"]
    ok = pop > 0
    safe = np.where(ok, pop, 1)
    return {"income": np.where(ok, U["median_income"], np.nan),
            "black": np.where(ok, 100 * U["nh_black"] / safe, np.nan),
            "hispanic": np.where(ok, 100 * U["hispanic"] / safe, np.nan)}, np.where(ok, pop, 0)


LABEL = {"income": "median household income", "black": "% non-Hispanic Black", "hispanic": "% Hispanic"}


def mh(C, N, s2c, bt):
    """Mantel-Haenszel rate ratios of quartiles 2..4 over quartile 1 across strata: C and N are
    K x 5 tables of cases (cameras) and denominators (road points or residents) by stratum and
    quartile, s2c the county of each stratum. Returns the three point ratios and B x 3 replicates
    that resample counties."""
    pts, reps = [], []
    for qh in (2, 3, 4):
        tot = N[:, 1] + N[:, qh]
        with np.errstate(divide="ignore", invalid="ignore"):
            a = np.where(tot > 0, C[:, qh] * N[:, 1] / tot, 0.0)
            b = np.where(tot > 0, C[:, 1] * N[:, qh] / tot, 0.0)
        ac = np.bincount(s2c, weights=a, minlength=len(bt.cty))
        bc = np.bincount(s2c, weights=b, minlength=len(bt.cty))
        pts.append(float(div(ac.sum(), bc.sum())))
        reps.append(div(bt.W @ ac, bt.W @ bc))
    return np.array(pts), np.stack(reps, axis=1)


def rates_by_quartile(P, bt, ucix, strata, Q, pop, cam_u, cam_mask, null_u, null_mask, per, title, csv_rows, key):
    """Nationally: crude rates by quartile (cameras per 100 km of road, or per 10,000 residents)
    and their ratio. Within county, and within county x density tercile: Mantel-Haenszel rate
    ratios of each quartile over the lowest, stratified by county (or county x tercile). A crude
    pooled rate is not a within-county comparison: within-county quartiles balance residents, not
    kilometres, so rural counties' long and camera-sparse networks would pile into whichever
    quartile their tracts fall in and stand in for other counties' roads."""
    P(f"\n  {title}")
    P(f"  {'':24s} {'scheme':16s} {'Q1':>8s} {'Q2':>8s} {'Q3':>8s} {'Q4':>8s}   Q4/Q1 [95% CI]")
    cu = cam_u[cam_mask]
    nu = null_u[null_mask] if per == "road" else None
    for a in ("income", "black", "hispanic"):
        for scheme in ("national", "within county", "county x density"):
            q = Q[(a, scheme)].astype(int)
            if scheme == "national":
                num = bt.table(ucix[cu], q[cu], ncols=5)
                den = (bt.table(ucix[nu], q[nu], ncols=5) if per == "road" else
                       bt.table(ucix[q > 0], q[q > 0], values=pop[q > 0], ncols=5))
                vals = div(num.sum(0)[1:], den.sum(0)[1:]) * (100.0 / per_km if per == "road" else 1e4)
                rn, rd = bt.reps(num), bt.reps(den)
                ratio = float(div(vals[3], vals[0]))
                lo, hi = ci(div(div(rn[:, 4], rd[:, 4]), div(rn[:, 1], rd[:, 1])))
                note = "   (rates)"
            else:
                u2s, s2c = strata[scheme]
                C = np.zeros((len(s2c), 5)); N = np.zeros((len(s2c), 5))
                np.add.at(C, (u2s[cu], q[cu]), 1)
                if per == "road":
                    np.add.at(N, (u2s[nu], q[nu]), 1)
                else:
                    r_ = q > 0
                    np.add.at(N, (u2s[r_], q[r_]), pop[r_])
                pts, reps = mh(C, N, s2c, bt)
                vals = np.r_[1.0, pts]
                ratio = float(pts[2])
                lo, hi = ci(reps[:, 2])
                note = "   (vs Q1, MH)"
            P(f"  {LABEL[a]:24s} {scheme:16s} " + " ".join(f"{v:8.2f}" for v in vals)
              + f"   {fmt_ratio(ratio, lo, hi)}{note}")
            csv_rows.append({"analysis": key, "attribute": a, "scheme": scheme, "per": per,
                             **{f"q{i + 1}": round(float(v), 4) for i, v in enumerate(vals)},
                             "q4_over_q1": round(float(ratio), 4), "lo": round(float(lo), 4), "hi": round(float(hi), 4)})


per_km = 1.0   # set in main: kilometres of road per control point


def standardized_rr(bt, key_c, cix_c, case_in, key_n, ctrl_in):
    """Indirectly standardized rate ratio: cameras in a category over the number expected if they
    fell into it as often as road points of the same stratum do. key_* are integer strata
    (county x road group); cix_c is each camera's county, the bootstrap cluster."""
    K = int(max(key_c.max(initial=0), key_n.max(initial=0))) + 1
    n_all = np.bincount(key_n, minlength=K).astype(float)
    n_in = np.bincount(key_n, weights=ctrl_in.astype(float), minlength=K)
    use = n_all[key_c] > 0
    share = n_in[key_c[use]] / n_all[key_c[use]]
    O = np.bincount(cix_c[use], weights=case_in[use].astype(float), minlength=len(bt.cty))
    E = np.bincount(cix_c[use], weights=share, minlength=len(bt.cty))
    lo, hi = ci(div(bt.W @ O, bt.W @ E))
    return float(div(O.sum(), E.sum())), lo, hi, O.sum(), E.sum(), int(use.sum())


def main():
    global per_km, EDGE, WATCH
    ap = argparse.ArgumentParser()
    ap.add_argument("--roads", required=True)
    ap.add_argument("--tiles", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--tables", required=True)
    ap.add_argument("--levels", nargs="+", default=["tract", "bg"])
    ap.add_argument("--edge", type=float, default=EDGE, help="metres from a boundary that count as its edge (<= 100)")
    ap.add_argument("--watch", type=float, default=WATCH, help="a camera watches the highest-class road this close")
    ap.add_argument("--tag", default="", help="suffix for siting<tag>.csv (sensitivity runs)")
    ap.add_argument("--skip-places", action="store_true", help="omit the municipal-limits section")
    args = ap.parse_args()
    EDGE, WATCH = args.edge, args.watch
    assert EDGE <= NBR, "neighbours are searched only within NBR"
    t0 = time.time()
    L = []

    def P(s=""):
        L.append(s); print(s, flush=True)

    cams, nulls, per_km = load_points(args.roads, args.tiles)
    P("=" * 78); P("WHERE THE CAMERAS STAND: SITING BY NEIGHBOURHOOD, EDGE AND SEAM"); P("=" * 78)
    P(f"{len(cams['id']):,} mapped cameras; {len(nulls['lon']):,} road points, one per {per_km:.1f} km of drivable road")
    P("Case-control on the road network: intensities are cameras per road point of the same class,")
    P("i.e. per road-kilometre; contrasts are stratified by county; 95% CIs resample whole counties")
    P(f"within states ({B:,} replicates). '*' marks a ratio whose interval excludes 1.")
    csv_rows = []

    # tracts carry the county of every point, for strata and bootstrap clusters
    UT = geo.load_units("tract")
    CACHE["dir"], CACHE["src"] = args.tables, os.path.getmtime(args.roads)
    os.makedirs(args.tables, exist_ok=True)
    cam_t, cam_bd, cam_nb, cam_nd = cached("cam_tract", assign, UT, cams["lon"], cams["lat"])
    nul_t, nul_bd, nul_nb, nul_nd = cached("null_tract", assign, UT, nulls["lon"], nulls["lat"])
    in_c, in_n = cam_t >= 0, nul_t >= 0
    cty_c = np.where(in_c, UT["county"][np.maximum(cam_t, 0)], "")
    cty_n = np.where(in_n, UT["county"][np.maximum(nul_t, 0)], "")
    bt = Boot(np.unique(UT["county"]))      # every county, so population-only counties map correctly
    cix_c, cix_n = bt.index(cty_c), bt.index(cty_n)
    skey_c = cix_c * 3 + np.maximum(cams["group"], 0)       # strata: county x road group
    skey_n = cix_n * 3 + nulls["group"]
    P(f"\nIn the 50 states + DC: {in_c.sum():,} cameras ({(in_c & (cams['group'] >= 0)).sum():,} matched to a "
      f"road within 100 m), {in_n.sum():,} road points, {len(bt.cty):,} counties  [{time.time() - t0:.0f}s]")
    for g, name in enumerate(GROUPS):
        P(f"  {name:8s} roads: {100 * (cams['group'][in_c] == g).mean():5.1f}% of cameras, "
          f"{100 * (nulls['group'][in_n] == g).mean():5.1f}% of road length")
    ops, cnt = np.unique(cams["op"][in_c], return_counts=True)
    P("  operator: " + ", ".join(f"{o} {c:,}" for o, c in sorted(zip(ops, cnt), key=lambda z: -z[1])))

    for level in args.levels:
        U = UT if level == "tract" else geo.load_units(level)
        if level == "tract":
            cu, cbd, cnb, cnd, nu, nbd, nnb, nnd = cam_t, cam_bd, cam_nb, cam_nd, nul_t, nul_bd, nul_nb, nul_nd
        else:
            cu, cbd, cnb, cnd = cached(f"cam_{level}", assign, U, cams["lon"], cams["lat"])
            nu, nbd, nnb, nnd = cached(f"null_{level}", assign, U, nulls["lon"], nulls["lat"])
        attrs, pop = attributes(U)
        ucix = bt.index(U["county"])
        # urban form: rank also inside county x population-density tercile, so that a dense core
        # is compared with other dense tracts of its county, not with its suburbs
        dens = np.where(U["aland"] > 0, pop / np.maximum(U["aland"] / 1e6, 1e-3), np.nan)
        ok = np.isfinite(dens) & (pop > 0)
        o = np.argsort(dens[ok]); cw = np.cumsum(pop[ok][o]); cw = cw / cw[-1]
        t1, t2 = dens[ok][o][np.searchsorted(cw, 1 / 3)], dens[ok][o][np.searchsorted(cw, 2 / 3)]
        band = np.where(dens <= t1, "lo", np.where(dens <= t2, "mid", "hi"))
        cband = np.char.add(np.char.add(U["county"].astype(str), ":"), band)
        grouping = {"national": None, "within county": U["county"], "county x density": cband}
        Q = {(a, sch): quartiles(attrs[a], pop, g) for a in LABEL for sch, g in grouping.items()}
        bidx = np.where(dens <= t1, 0, np.where(dens <= t2, 1, 2))
        ncty = len(bt.cty)
        strata = {"within county": (ucix, np.arange(ncty)),
                  "county x density": (ucix * 3 + bidx, np.repeat(np.arange(ncty), 3))}
        mc, mn = (cu >= 0) & in_c, (nu >= 0) & in_n
        unitname = {"tract": "TRACTS", "bg": "BLOCK GROUPS"}[level]
        P("\n" + "=" * 78)
        P(f"{unitname}  ({len(U['geoid']):,} units)  [{time.time() - t0:.0f}s]")
        P("=" * 78)

        # A. where cameras are sited, by who lives there
        P("\nA. SITING INTENSITY BY NEIGHBOURHOOD (population-weighted quartiles; Q4 = highest)")
        grp_c, grp_n = cams["group"], nulls["group"]
        rates_by_quartile(P, bt, ucix, strata, Q, pop, cu, mc & (grp_c >= 0), nu, mn, "road",
                          "cameras per 100 km of road (all drivable roads)", csv_rows, f"{level}:all:road")
        rates_by_quartile(P, bt, ucix, strata, Q, pop, cu, mc, nu, mn, "people",
                          "cameras per 10,000 residents", csv_rows, f"{level}:all:people")
        for g, name in ((0, "arterial"), (1, "residential-street")):
            rates_by_quartile(P, bt, ucix, strata, Q, pop, cu, mc & (grp_c == g), nu, mn & (grp_n == g), "road",
                              f"{name} cameras per 100 km of {name} road", csv_rows, f"{level}:{name}:road")
        if level == "tract":
            for op in ("police", "business", "HOA", "transport", "municipal"):
                if (cams["op"][mc] == op).sum() >= 200:
                    rates_by_quartile(P, bt, ucix, strata, Q, pop, cu, mc & (grp_c >= 0) & (cams["op"] == op), nu, mn, "road",
                                      f"{op}-operated cameras per 100 km of road  (n={(cams['op'][mc] == op).sum():,})",
                                      csv_rows, f"{level}:op={op}:road")
            ent = mc & (grp_c >= 0) & ((cams["op"] == "HOA") | np.isin(cams["zone"], ["entrance", "gate", "parking_entrance", "residential"]))
            rates_by_quartile(P, bt, ucix, strata, Q, pop, cu, ent, nu, mn, "road",
                              f"entrance, gate or HOA cameras (zone / operator tags) per 100 km of road  (n={ent.sum():,})",
                              csv_rows, f"{level}:entrance-or-HOA:road")
            for v in ("flock", "other", "untagged"):
                rates_by_quartile(P, bt, ucix, strata, Q, pop, cu, mc & (grp_c >= 0) & (cams["vendor"] == v), nu, mn, "road",
                                  f"{v}-vendor cameras per 100 km of road", csv_rows, f"{level}:vendor={v}:road")

        # B. edges versus interiors
        P("\nB. EDGE VERSUS INTERIOR: camera intensity within the edge distance of the unit's own boundary over")
        P("   that inside it (Mantel-Haenszel over the strata), by quartile. A cordon predicts a higher ratio in Q4.")
        edge_c, edge_n = cbd <= EDGE, nbd <= EDGE
        for a in ("black", "hispanic", "income"):
            for sch in ("within county", "county x density"):
                q = Q[(a, sch)]
                for g, name in ((0, "arterial"), (1, "residential"), (None, "all roads")):
                    sc = mc & ((grp_c == g) if g is not None else (grp_c >= 0))
                    sn = mn & ((grp_n == g) if g is not None else np.ones(len(grp_n), bool))
                    # edge vs interior within each quartile, Mantel-Haenszel over the strata, so a
                    # quartile's ratio compares edge and interior roads of the same county
                    u2s, s2c = strata[sch]
                    qi = q.astype(int)
                    col_c = qi[cu[sc]] * 2 + edge_c[sc]
                    col_n = qi[nu[sn]] * 2 + edge_n[sn]
                    C = np.zeros((len(s2c), 10)); N = np.zeros((len(s2c), 10))
                    np.add.at(C, (u2s[cu[sc]], col_c), 1)
                    np.add.at(N, (u2s[nu[sn]], col_n), 1)
                    ratio_q, pr = [], []
                    for k in (1, 2, 3, 4):
                        ce, ci_, ne, ni = C[:, 2 * k + 1], C[:, 2 * k], N[:, 2 * k + 1], N[:, 2 * k]
                        tot = ne + ni
                        with np.errstate(divide="ignore", invalid="ignore"):
                            aa = np.where(tot > 0, ce * ni / tot, 0.0)
                            bb = np.where(tot > 0, ci_ * ne / tot, 0.0)
                        ac = np.bincount(s2c, weights=aa, minlength=len(bt.cty))
                        bc_ = np.bincount(s2c, weights=bb, minlength=len(bt.cty))
                        ratio_q.append(float(div(ac.sum(), bc_.sum())))
                        pr.append(div(bt.W @ ac, bt.W @ bc_))
                    lo, hi = ci(div(pr[3], pr[0]))
                    P(f"  {LABEL[a]:24s} {name:12s} {sch:16s} edge/interior Q1..Q4: " + " ".join(f"{v:5.2f}" for v in ratio_q)
                      + f"   Q4 vs Q1 {fmt_ratio(float(div(ratio_q[3], ratio_q[0])), lo, hi)}")
                    csv_rows.append({"analysis": f"{level}:edge:{name}", "attribute": a, "scheme": sch, "per": "edge/interior",
                                     **{f"q{i + 1}": round(float(v), 4) for i, v in enumerate(ratio_q)},
                                     "q4_over_q1": round(float(div(ratio_q[3], ratio_q[0])), 4),
                                     "lo": round(float(lo), 4), "hi": round(float(hi), 4)})

        # C. boundaries, and seams between different neighbourhoods
        P("\nC. BOUNDARIES AND SEAMS (standardized by county x road class against road points)")
        m = mc & (grp_c >= 0)
        rr = standardized_rr(bt, skey_c[m], cix_c[m], edge_c[m], skey_n[mn], edge_n[mn])
        P(f"  cameras within {EDGE:.0f} m of a unit boundary: {rr[3]:,.0f} observed vs {rr[4]:,.0f} expected from roads "
          f"-> {fmt_ratio(*rr[:3])}")
        seam_c = mc & (grp_c >= 0) & edge_c & (cnb >= 0) & (cnd <= EDGE)
        seam_n = mn & edge_n & (nnb >= 0) & (nnd <= EDGE)
        own_c, nb_c = cu, np.maximum(cnb, 0)
        own_n, nb_n = nu, np.maximum(nnb, 0)
        for a, thr, unit_s in (("black", 30, "points"), ("hispanic", 30, "points"), ("income", 1.0, "doubling")):
            x = attrs[a]
            if a == "income":
                dc = np.abs(np.log2(x[own_c] / x[nb_c])); dn = np.abs(np.log2(x[own_n] / x[nb_n]))
                bins = [0, 0.25, 0.5, 1.0, 1.5, 99]
            else:
                dc = np.abs(x[own_c] - x[nb_c]); dn = np.abs(x[own_n] - x[nb_n])
                bins = [0, 10, 20, 30, 50, 101]
            okc, okn = seam_c & np.isfinite(dc), seam_n & np.isfinite(dn)
            P(f"\n  {LABEL[a]}: contrast across the boundary ({'|log2 income ratio|' if a == 'income' else '|difference|, points'})")
            for lo_b, hi_b in zip(bins[:-1], bins[1:]):
                r = standardized_rr(bt, skey_c[okc], cix_c[okc], (dc[okc] >= lo_b) & (dc[okc] < hi_b),
                                    skey_n[okn], (dn[okn] >= lo_b) & (dn[okn] < hi_b))
                P(f"    contrast {lo_b:>5}-{hi_b:<5} cameras {r[3]:7,.0f}  expected {r[4]:8,.1f}  {fmt_ratio(*r[:3])}")
                csv_rows.append({"analysis": f"{level}:seam", "attribute": a, "scheme": f"{lo_b}-{hi_b}", "per": "rr",
                                 "q4_over_q1": round(float(r[0]), 4), "lo": round(float(r[1]), 4), "hi": round(float(r[2]), 4)})
            # D. at high-contrast seams: which side do cameras stand on, and face?
            hc, hn = okc & (dc >= thr), okn & (dn >= thr)
            hi_side_c = x[own_c] > x[nb_c]
            hi_side_n = x[own_n] > x[nb_n]
            r = standardized_rr(bt, skey_c[hc], cix_c[hc], hi_side_c[hc], skey_n[hn], hi_side_n[hn])
            side = "richer" if a == "income" else "higher-" + ("Black" if a == "black" else "Hispanic")
            P(f"    at seams with contrast >= {thr} ({hc.sum():,} cameras): standing on the {side} side "
              f"{r[3]:,.0f} vs {r[4]:,.1f} expected {fmt_ratio(*r[:3])}")
            idx = np.flatnonzero(hc)
            hl, hb, hs = [], [], []
            for i in idx:
                for bearing in cams["heads"][i]:
                    hl.append(cams["lon"][i]); hb.append(bearing); hs.append(i)
            if hl:
                lo_, la_ = geo.destination(np.array(hl), cams["lat"][np.array(hs)], np.array(hb), LOOK)
                lu, _, _, _ = assign(U, lo_, la_, neighbours=False)
                hs = np.array(hs)
                own, oth = own_c[hs], nb_c[hs]
                into_hi = np.where(lu == own, hi_side_c[hs], np.where(lu == oth, ~hi_side_c[hs], False))
                into_known = (lu == own) | (lu == oth)
                across = (lu == oth)
                n_known = into_known.sum()
                share = into_hi[into_known].mean() if n_known else np.nan
                # cameras standing on the low side looking across into the high side, and vice versa
                stand_lo = ~hi_side_c[hs]
                a1 = (across & stand_lo).sum(); a2 = (across & ~stand_lo).sum()
                P(f"    heads looking {LOOK:.0f} m ahead ({len(hs):,}): {100 * share:.1f}% into the {side} side; "
                  f"looking across the seam: {a1:,} from the other side into it, {a2:,} out of it")
                csv_rows.append({"analysis": f"{level}:facing", "attribute": a, "scheme": f">={thr}", "per": "share into " + side,
                                 "q4_over_q1": round(float(share), 4), "lo": int(a1), "hi": int(a2)})

    # F. municipalities: cameras at city limits
    if not args.skip_places:
        P("\n" + "=" * 78)
        P("F. CITY LIMITS: cameras within 50 m of the boundary of an incorporated municipality")
        P("=" * 78)
        UP = geo.load_units("place")
        # Only incorporated places (TIGER class C*) have limits, a government and usually a police
        # department; census-designated places are statistical outlines with none of those.
        keep = np.char.startswith(UP["classfp"].astype(str), "C")
        UP = {k: v[keep] for k, v in UP.items()}
        P(f"  {keep.sum():,} incorporated places (census-designated places excluded)")
        pc, pcbd, _, _ = cached("cam_iplace", assign, UP, cams["lon"], cams["lat"], neighbours=False)
        pn, pnbd, _, _ = cached("null_iplace", assign, UP, nulls["lon"], nulls["lat"], neighbours=False)
        oc, ocd = cached(f"cam_iplace_near{EDGE:.0f}", nearest_within, UP, cams["lon"], cams["lat"], EDGE)
        on, ond = cached(f"null_iplace_near{EDGE:.0f}", nearest_within, UP, nulls["lon"], nulls["lat"], EDGE)
        lim_place_c = np.where(pc >= 0, np.where(pcbd <= EDGE, pc, -1), oc)
        lim_place_n = np.where(pn >= 0, np.where(pnbd <= EDGE, pn, -1), on)
        lim_c, lim_n = lim_place_c >= 0, lim_place_n >= 0
        sc, sn = in_c & (cams["group"] >= 0), in_n
        rr = standardized_rr(bt, skey_c[sc], cix_c[sc], lim_c[sc], skey_n[sn], lim_n[sn])
        P(f"  at a city limit: {rr[3]:,.0f} cameras vs {rr[4]:,.0f} expected from roads -> {fmt_ratio(*rr[:3])}")
        county_inc = {g: v.get("median_income", np.nan) for g, v in geo.load_acs("county").items()}
        pinc = UP["median_income"]
        rel_c = np.full(len(lim_c), np.nan); rel_n = np.full(len(lim_n), np.nan)
        ci_c = np.array([county_inc.get(c, np.nan) for c in cty_c]); ci_n = np.array([county_inc.get(c, np.nan) for c in cty_n])
        rel_c[lim_c] = np.log2(pinc[lim_place_c[lim_c]] / ci_c[lim_c])
        rel_n[lim_n] = np.log2(pinc[lim_place_n[lim_n]] / ci_n[lim_n])
        P("  by the municipality's median income relative to its county's:")
        for name, lo_b, hi_b in (("poorer by >25%", -99, -np.log2(1.25)), ("within 25%", -np.log2(1.25), np.log2(1.25)),
                                 ("richer by 25-50%", np.log2(1.25), np.log2(1.5)), ("richer by >50%", np.log2(1.5), 99)):
            cat_c = lim_c & (rel_c >= lo_b) & (rel_c < hi_b)
            cat_n = lim_n & (rel_n >= lo_b) & (rel_n < hi_b)
            r = standardized_rr(bt, skey_c[sc], cix_c[sc], cat_c[sc], skey_n[sn], cat_n[sn])
            # facing. A camera just inside the limit "looks" inside even if aimed at random, so the
            # telling heads are those whose look point is across the limit from the camera: standing
            # outside looking in, versus inside looking out. Intervals resample counties.
            idx = np.flatnonzero(cat_c & sc)
            hl, hla, hb, hp, hc = [], [], [], [], []
            for i in idx:
                for bearing in cams["heads"][i]:
                    hl.append(cams["lon"][i]); hla.append(cams["lat"][i]); hb.append(bearing)
                    hp.append(lim_place_c[i]); hc.append(i)
            cross_in = np.nan; lo_ci = hi_ci = np.nan; n_cross = 0; stand_in = np.nan
            if hl:
                lo_, la_ = geo.destination(np.array(hl), np.array(hla), np.array(hb), LOOK)
                lu, _, _, _ = assign(UP, lo_, la_, neighbours=False)
                hp, hc = np.array(hp), np.array(hc)
                look_in, is_in = lu == hp, pc[hc] == hp
                cross = look_in != is_in
                n_cross = int(cross.sum())
                stand_in = is_in.mean()
                if n_cross:
                    ccix = cix_c[hc[cross]]
                    num = np.bincount(ccix, weights=look_in[cross].astype(float), minlength=len(bt.cty))
                    den = np.bincount(ccix, minlength=len(bt.cty)).astype(float)
                    cross_in = num.sum() / den.sum()
                    lo_ci, hi_ci = ci(div(bt.W @ num, bt.W @ den))
            P(f"    {name:18s} cameras {r[3]:7,.0f}  expected {r[4]:8,.1f}  {fmt_ratio(*r[:3])}")
            P(f"    {'':18s} {100 * stand_in:.0f}% stand inside; of {n_cross:,} heads looking across the limit, "
              f"{100 * cross_in:.1f}% [{100 * lo_ci:.1f}-{100 * hi_ci:.1f}] look in from outside")
            csv_rows.append({"analysis": "place:limit", "attribute": "income vs county", "scheme": name, "per": "rr",
                             "q4_over_q1": round(float(r[0]), 4), "lo": round(float(r[1]), 4), "hi": round(float(r[2]), 4),
                             "q1": round(float(stand_in), 4), "q2": round(float(cross_in), 4),
                             "q3": round(float(lo_ci), 4), "q4": round(float(hi_ci), 4)})

    os.makedirs(args.tables, exist_ok=True)
    fields = []
    for r in csv_rows:
        fields += [k for k in r if k not in fields]
    with open(os.path.join(args.tables, f"siting{args.tag}.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(csv_rows)
    P(f"\n[{time.time() - t0:.0f}s]")
    open(args.report, "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
