#!/usr/bin/env python3
"""
One pass over an OSM extract for the siting analysis (siting.py): a length-weighted sample of
points along every drivable road, with its class, and the nearest road to every camera.

    python3 road_sample.py --pbf ../server/graphhopper/data/north-america.osm.pbf \\
        --cameras ../server/alpr/region_cache -o out/siting/roads.npz

Why a sample of road points: cameras stand beside roads, and census-tract boundaries are often
drawn along the same arterials, so "cameras cluster at tract boundaries" would be true of any
point on those roads. The sample is the comparison set. A camera is compared with road points of
the same class, and the ratio of cameras to road points in any group estimates siting intensity
per road-kilometre. Systematic sampling every --spacing metres from a random start on each way
makes the sample proportional to length.

Runs under the system python3, which has pyosmium; numpy otherwise. Ways are processed in
batches so that the per-way Python work is only collecting coordinates.

Memory: node locations go to a file-backed index (--index, ~16 bytes per node: ~26 GB for North
America), NOT pyosmium's default in-memory one. The default ("flex_mem") picks a dense array for
an extract this size and grew past 56 GB of RAM before the kernel killed it, taking the terminal
with it. Keep the index on a real disk — /tmp is tmpfs (RAM) on Fedora — and run the pass in a
memory-capped scope so that an overrun can only kill the pass itself:

    systemd-run --user --scope -p MemoryMax=16G -p MemorySwapMax=0 python3 road_sample.py ...
"""
import argparse
import glob
import json
import math
import os
import sys
import time

import numpy as np
import osmium
from osmium.filter import EntityFilter, KeyFilter

CLASSES = ["motorway", "trunk", "primary", "secondary", "tertiary",
           "unclassified", "residential", "living_street", "service"]
CODE = {c: i for i, c in enumerate(CLASSES)}
for _c in CLASSES[:5]:
    CODE[_c + "_link"] = CODE[_c]
R = 6371008.8
G = 0.003          # camera grid cell, degrees: 3x3 cells cover >150 m even at 55 degrees N
NEAR = 100.0       # a camera further than this from every road has no nearest road
DENSE = 100.0      # segments are split to at most this length for the proximity test


def seg_lengths(a, b):
    """Haversine length in metres of segments a->b, (n,2) lon/lat arrays."""
    la1, la2 = np.radians(a[:, 1]), np.radians(b[:, 1])
    dla, dlo = la2 - la1, np.radians(b[:, 0] - a[:, 0])
    h = np.sin(dla / 2) ** 2 + np.cos(la1) * np.cos(la2) * np.sin(dlo / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(np.minimum(h, 1.0)))


def cell_keys(lon, lat, dx=0, dy=0):
    return (np.floor((lon + 180) / G).astype(np.int64) + dx) * 1_000_000 + \
           (np.floor((lat + 90) / G).astype(np.int64) + dy)


class Cameras:
    def __init__(self, tile_dir):
        nodes = {}
        for p in glob.glob(os.path.join(tile_dir, "tile_*.json")):
            for e in json.load(open(p)).get("elements", ()):
                if e.get("type") == "node":
                    nodes[e["id"]] = (e["lon"], e["lat"])
        self.id = np.array(sorted(nodes), np.int64)
        xy = np.array([nodes[i] for i in self.id])
        self.lon, self.lat = xy[:, 0], xy[:, 1]
        keys = cell_keys(self.lon, self.lat)
        o = np.argsort(keys, kind="stable")
        self.keys, self.order = keys[o], o
        nb = np.concatenate([cell_keys(self.lon, self.lat, dx, dy)
                             for dx in (-1, 0, 1) for dy in (-1, 0, 1)])
        self.near_keys = np.unique(nb)
        self.best_d = np.full(len(self.id), np.inf)
        self.best_cls = np.full(len(self.id), -1, np.int8)
        self.best_way = np.zeros(len(self.id), np.int64)
        # nearest distance to each road class, so siting.py can name the road a camera
        # watches (the highest class close by) rather than whatever driveway is nearest
        self.by_cls = np.full((len(self.id), len(CLASSES)), np.inf)

    def update(self, a, b, cls, way):
        """Offer short segments a->b (lon/lat) of class cls / way id to every nearby camera."""
        mid = (a + b) / 2
        mk = cell_keys(mid[:, 0], mid[:, 1])
        cand = np.flatnonzero(np.isin(mk, self.near_keys))
        if not len(cand):
            return
        a, b, cls, way, mid = a[cand], b[cand], cls[cand], way[cand], mid[cand]
        seg_parts, cam_parts = [], []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                k = cell_keys(mid[:, 0], mid[:, 1], dx, dy)
                lo, hi = np.searchsorted(self.keys, k, "left"), np.searchsorted(self.keys, k, "right")
                n = hi - lo
                if not n.any():
                    continue
                s = np.repeat(np.arange(len(k)), n)
                start = np.repeat(lo, n) + (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n))
                seg_parts.append(s); cam_parts.append(self.order[start])
        if not seg_parts:
            return
        s, c = np.concatenate(seg_parts), np.concatenate(cam_parts)
        # point-to-segment distance in a local plane at the camera's latitude
        kx = np.cos(np.radians(self.lat[c])) * (math.pi * R / 180)
        ky = math.pi * R / 180
        ax, ay = (a[s, 0] - self.lon[c]) * kx, (a[s, 1] - self.lat[c]) * ky
        bx, by = (b[s, 0] - self.lon[c]) * kx, (b[s, 1] - self.lat[c]) * ky
        vx, vy = bx - ax, by - ay
        t = np.clip(-(ax * vx + ay * vy) / np.maximum(vx * vx + vy * vy, 1e-9), 0, 1)
        d = np.hypot(ax + t * vx, ay + t * vy)
        near = d < NEAR
        np.minimum.at(self.by_cls, (c[near], cls[s[near]]), d[near])
        keep = d < np.minimum(NEAR, self.best_d[c])
        if not keep.any():
            return
        s, c, d = s[keep], c[keep], d[keep]
        o = np.lexsort((d, c))
        s, c, d = s[o], c[o], d[o]
        first = np.r_[True, c[1:] != c[:-1]]
        s, c, d = s[first], c[first], d[first]
        better = d < self.best_d[c]
        c, s, d = c[better], s[better], d[better]
        self.best_d[c], self.best_cls[c], self.best_way[c] = d, cls[s], way[s]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--cameras", required=True, help="directory of Overpass tiles (tile_*.json)")
    ap.add_argument("--boxes", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "..", "..", "server", "alpr", "us_state_boxes.json"))
    ap.add_argument("--spacing", type=float, default=1000.0, help="metres between sampled points")
    ap.add_argument("--seed", type=int, default=20260725)
    ap.add_argument("--batch", type=int, default=200_000)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--index", default=None,
                    help="file-backed node-location index (default: beside --out; deleted afterwards)")
    args = ap.parse_args()
    index = args.index or os.path.join(os.path.dirname(os.path.abspath(args.out)), "node_locations.idx")
    if os.path.exists(index):
        os.remove(index)

    rng = np.random.default_rng(args.seed)
    boxes = np.array(list(json.load(open(args.boxes))["boxes"].values()), float)  # s, w, n, e
    cams = Cameras(args.cameras)
    print(f"{len(cams.id):,} cameras; sampling roads every {args.spacing:.0f} m", flush=True)

    pts, total_len = [], np.zeros(len(CLASSES))
    stats = {"ways": 0, "batches": 0}

    def in_us(lon, lat):
        m = np.zeros(len(lon), bool)
        for s, w, n, e in boxes:
            m |= (lat >= s) & (lat <= n) & (lon >= w) & (lon <= e)
        return m

    def flush(batch):
        coords = [c for c, _, _ in batch]
        lens = np.fromiter((len(c) for c in coords), np.int64, len(coords))
        V = np.concatenate(coords)
        cls_w = np.fromiter((k for _, k, _ in batch), np.int8, len(batch))
        way_w = np.fromiter((w for _, _, w in batch), np.int64, len(batch))
        ends = np.cumsum(lens)
        is_start = np.ones(len(V), bool); is_start[ends - 1] = False
        i0 = np.flatnonzero(is_start)
        a, b = V[i0], V[i0 + 1]
        segway = np.repeat(np.arange(len(batch)), lens - 1)
        sl = seg_lengths(a, b)
        # length by class, counted where the segment midpoint falls in a US box
        mid = (a + b) / 2
        us = in_us(mid[:, 0], mid[:, 1])
        total_len[:] += np.bincount(cls_w[segway][us], sl[us], minlength=len(CLASSES))
        # systematic length-weighted sample, random start per way
        L = np.bincount(segway, sl, minlength=len(batch))
        segend = np.cumsum(sl)
        waystart = np.r_[0.0, np.cumsum(L)[:-1]]
        off = rng.uniform(0, args.spacing, len(batch))
        n_w = np.where(off < L, np.floor((L - off) / args.spacing).astype(np.int64) + 1, 0)
        if n_w.sum():
            wi = np.repeat(np.arange(len(batch)), n_w)
            k = np.arange(n_w.sum()) - np.repeat(np.cumsum(n_w) - n_w, n_w)
            pos = waystart[wi] + off[wi] + k * args.spacing
            si = np.minimum(np.searchsorted(segend, pos, "right"), len(sl) - 1)
            t = np.clip((pos - (segend[si] - sl[si])) / np.maximum(sl[si], 1e-9), 0, 1)
            p = a[si] + t[:, None] * (b[si] - a[si])
            keep = in_us(p[:, 0], p[:, 1])
            pts.append(np.column_stack([p[keep], cls_w[wi[keep]]]))
        # split segments to <= DENSE metres and offer them to nearby cameras
        m = np.maximum(np.ceil(sl / DENSE).astype(np.int64), 1)
        rep = np.repeat(np.arange(len(sl)), m)
        j = np.arange(m.sum()) - np.repeat(np.cumsum(m) - m, m)
        f0, f1 = (j / m[rep])[:, None], ((j + 1) / m[rep])[:, None]
        sa = a[rep] + f0 * (b[rep] - a[rep]); sb = a[rep] + f1 * (b[rep] - a[rep])
        cams.update(sa, sb, cls_w[segway[rep]], way_w[segway[rep]])
        stats["batches"] += 1

    t0 = time.time()
    batch = []
    fp = (osmium.FileProcessor(args.pbf, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations(f"sparse_file_array,{index}")
          .with_filter(EntityFilter(osmium.osm.WAY))
          .with_filter(KeyFilter("highway")))
    for w in fp:
        c = CODE.get(w.tags.get("highway"))
        if c is None or len(w.nodes) < 2:
            continue
        try:
            xy = np.array([(n.lon, n.lat) for n in w.nodes])
        except osmium.InvalidLocationError:
            continue
        batch.append((xy, c, w.id))
        stats["ways"] += 1
        if len(batch) >= args.batch:
            flush(batch); batch = []
            if stats["batches"] % 25 == 0:
                print(f"  {stats['ways']:,} ways, {sum(len(p) for p in pts):,} points, "
                      f"{int((cams.best_cls >= 0).sum()):,} cameras matched  ({time.time() - t0:.0f}s)", flush=True)
    if batch:
        flush(batch)

    del fp
    if os.path.exists(index):
        os.remove(index)
    P = np.concatenate(pts) if pts else np.zeros((0, 3))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    np.savez_compressed(args.out, null_lon=P[:, 0], null_lat=P[:, 1], null_cls=P[:, 2].astype(np.int8),
                        cam_id=cams.id, cam_lon=cams.lon, cam_lat=cams.lat,
                        cam_near_cls=cams.best_cls, cam_near_d=np.where(np.isfinite(cams.best_d), cams.best_d, -1).astype(np.float32),
                        cam_near_way=cams.best_way, cam_d_by_cls=np.where(np.isfinite(cams.by_cls), cams.by_cls, -1).astype(np.float32),
                        len_by_class=total_len, classes=np.array(CLASSES),
                        spacing=args.spacing, seed=args.seed)
    print(f"{stats['ways']:,} drivable ways; {len(P):,} sampled points; "
          f"{int((cams.best_cls >= 0).sum()):,} of {len(cams.id):,} cameras within {NEAR:.0f} m of a road "
          f"({time.time() - t0:.0f}s)")
    for name, km in zip(CLASSES, total_len / 1000):
        print(f"  {name:14s} {km:12,.0f} km")


if __name__ == "__main__":
    main()
