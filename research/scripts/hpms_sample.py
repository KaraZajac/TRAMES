#!/usr/bin/env python3
"""
Traffic for the siting analysis. siting.py compares cameras with kilometres of road; a road that
carries more cars offers more to watch, so the comparison that answers "do cameras simply follow
the traffic?" is per vehicle-kilometre. FHWA's Highway Performance Monitoring System gives the
annual average daily traffic (AADT) of every section of the federal-aid road system; this script
turns it into (1) a length-weighted sample of points along those roads, each with its AADT and
functional class, and (2) for every camera in roads.npz, the distance to the nearest section of
each class, with that section's AADT.

    python hpms_sample.py --gdb data/hpms/HPMS2024.gdb --roads out/siting/roads.npz \\
        -o out/siting/hpms.npz

Source: HPMS 2024, the U.S. DOT / BTS National Transportation Atlas Database edition (a file
geodatabase with one layer per state; fetch.sh step 5). Reading it needs pyogrio, whose wheels
bundle GDAL with the OpenFileGDB driver (pip install pyogrio); nothing downstream does.

Which sections: functional systems 1-5 (Interstate, other freeways and expressways, other
principal arterials, minor arterials, major collectors), the classes every state must count in
full. Minor collectors and local roads are counted in a handful of states only and are left out,
so the base is the same everywhere. Facility types 1 (one-way roadway), 2 (two-way), 4 (ramp) and
5 (non-mainline, e.g. frontage roads) with an AADT are kept; type 6, the non-inventory direction of
a divided road, is the second carriageway, mapped but not counted separately (the inventory
direction's AADT covers both), and is dropped, so a divided road is counted once, at its two-way
volume, while each road of a one-way pair carries its own. Check: AADT x length over the kept sections sums to 2,849 billion vehicle-miles a year for the
50 states and DC, 87% of the 3,279 billion FHWA estimates for all roads and streets in 2024
(Traffic Volume Trends, December 2024); the rest is on minor collectors and local roads.

Points: systematic, every --spacing metres from a random start on each section, so each point
stands for --spacing metres of road and AADT x spacing vehicle-kilometres a day. Distances are
measured in the equal-area projections of geo.py (within about a percent of true over each region).
"""
import argparse
import os
import sys
import time

import numpy as np
import pyogrio
import shapely
from pyogrio.raw import read

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import geo  # noqa: E402

KEEP_FT = (1, 2, 4, 5)
NF = 5          # functional systems 1..5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gdb", required=True)
    ap.add_argument("--roads", required=True, help="road_sample.py output, for the cameras")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--spacing", type=float, default=200.0, help="metres of road per sample point")
    ap.add_argument("--maxd", type=float, default=100.0, help="search radius for a camera's nearest section")
    ap.add_argument("--seed", type=int, default=20260725)
    args = ap.parse_args()
    t0 = time.time()

    R = np.load(args.roads)
    cam_lon, cam_lat = R["cam_lon"], R["cam_lat"]
    ncam = len(cam_lon)
    cam_d = np.full((ncam, NF), np.inf, np.float32)
    cam_aadt = np.full((ncam, NF), np.nan, np.float32)
    cam_ft = np.full((ncam, NF), -1, np.int8)

    pts = {k: [] for k in ("lon", "lat", "f", "ft", "aadt", "urban", "state")}
    summary = []
    layers = sorted(n for n, _ in pyogrio.list_layers(args.gdb) if n.startswith("HPMS_FULL_"))
    for lyr in layers:
        abbr = lyr.split("_")[2].lower()
        if abbr not in geo.FIPS:          # Puerto Rico: outside the study
            continue
        fips = geo.FIPS[abbr]
        reg = geo.region(fips)
        meta, _, wkb, fields = read(args.gdb, layer=lyr, read_geometry=True,
                                    columns=["f_system", "facility_type", "aadt", "urban_id"])
        f = dict(zip(meta["fields"], fields))
        fs = np.nan_to_num(f["f_system"].astype(float), nan=0).astype(np.int8)
        ft = np.nan_to_num(f["facility_type"].astype(float), nan=0).astype(np.int8)
        aadt = f["aadt"].astype(float)
        urb = np.nan_to_num(f["urban_id"].astype(float), nan=99999)
        keep = (fs >= 1) & (fs <= NF) & np.isin(ft, KEEP_FT) & np.isfinite(aadt) & (aadt > 0)
        keep &= np.array([w is not None for w in wkb])
        g = shapely.from_wkb(wkb[keep])
        fs, ft, aadt, urb = fs[keep], ft[keep], aadt[keep], urb[keep]
        ok = ~shapely.is_empty(g)
        g, fs, ft, aadt, urb = g[ok], fs[ok], ft[ok], aadt[ok], urb[ok]
        xy = shapely.get_coordinates(g)
        x, y = geo.albers(xy[:, 0], xy[:, 1], reg)
        G = shapely.set_coordinates(g.copy(), np.c_[x, y])
        L = shapely.length(G)

        # systematic sample, random start per section
        rng = np.random.default_rng(args.seed + int(fips))
        off = rng.uniform(0, args.spacing, len(G))
        k = np.where(L > off, np.floor((L - off) / args.spacing).astype(np.int64) + 1, 0)
        sec = np.repeat(np.arange(len(G)), k)
        start = np.cumsum(k) - k
        d = off[sec] + (np.arange(len(sec)) - start[sec]) * args.spacing
        p = shapely.line_interpolate_point(G[sec], d)
        lon, lat = geo.albers_inv(shapely.get_x(p), shapely.get_y(p), reg)
        pts["lon"].append(lon); pts["lat"].append(lat)
        pts["f"].append(fs[sec]); pts["ft"].append(ft[sec]); pts["aadt"].append(aadt[sec].astype(np.float32))
        # 0 rural, 1 small urban (5,000-49,999), 2 urbanized area
        pts["urban"].append(np.where(urb[sec] == 99999, 0, np.where(urb[sec] == 99998, 1, 2)).astype(np.int8))
        pts["state"].append(np.full(len(sec), int(fips), np.uint8))

        # cameras: nearest section of each functional system within maxd
        b = shapely.total_bounds(g)
        sel = np.flatnonzero((cam_lon > b[0] - 0.01) & (cam_lon < b[2] + 0.01)
                             & (cam_lat > b[1] - 0.01) & (cam_lat < b[3] + 0.01))
        if len(sel):
            cx, cy = geo.albers(cam_lon[sel], cam_lat[sel], reg)
            cp = shapely.points(cx, cy)
            ci, gi = shapely.STRtree(G).query(cp, predicate="dwithin", distance=args.maxd)
            if len(ci):
                dd = shapely.distance(cp[ci], G[gi])
                fi = fs[gi].astype(np.int64) - 1
                o = np.lexsort((dd, fi, ci))
                ci, gi, dd, fi = ci[o], gi[o], dd[o], fi[o]
                first = np.r_[True, (ci[1:] != ci[:-1]) | (fi[1:] != fi[:-1])]
                cam, fi, dd, gi = sel[ci[first]], fi[first], dd[first], gi[first]
                better = dd < cam_d[cam, fi]
                cam, fi, dd, gi = cam[better], fi[better], dd[better], gi[better]
                cam_d[cam, fi] = dd
                cam_aadt[cam, fi] = aadt[gi]
                cam_ft[cam, fi] = ft[gi]
        km = L.sum() / 1000
        summary.append((int(fips), len(G), km, float((aadt * L).sum() / 1000), len(sec)))
        print(f"{abbr.upper()}  {len(G):>8,} sections  {km:>9,.0f} km  {summary[-1][3] / 1e6:7.1f} M veh-km/day  "
              f"{len(sec):>9,} points  [{time.time() - t0:.0f}s]", flush=True)

    out = {k: np.concatenate(v) for k, v in pts.items()}
    S = np.array(summary)
    matched = np.isfinite(cam_d).any(axis=1)
    print(f"\n{len(out['lon']):,} points over {S[:, 2].sum():,.0f} km; "
          f"{S[:, 3].sum() * 365 / 1.609344 / 1e9:,.0f} billion vehicle-miles a year")
    print(f"{matched.sum():,} of {ncam:,} cameras within {args.maxd:.0f} m of a counted section; "
          f"within 30 m: {(cam_d.min(axis=1) <= 30).sum():,}")
    np.savez(args.out, **{f"pt_{k}": v for k, v in out.items()},
             cam_id=R["cam_id"], cam_d=cam_d, cam_aadt=cam_aadt, cam_ft=cam_ft,
             st_fips=S[:, 0].astype(int), st_sections=S[:, 1].astype(int), st_km=S[:, 2],
             st_vkt=S[:, 3], st_points=S[:, 4].astype(int),
             spacing=args.spacing, maxd=args.maxd, seed=args.seed, source="HPMS 2024 (USDOT/BTS NTAD)")
    print(f"wrote {args.out}  [{time.time() - t0:.0f}s]")


if __name__ == "__main__":
    main()
