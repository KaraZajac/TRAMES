#!/usr/bin/env python3
"""
Tagging statistics for a camera snapshot — every number in the paper's data tables.

    python3 snapshot_stats.py --cache ../../server/alpr/region_cache \
        [--cones ../../server/graphhopper/custom_areas/alpr.geojson] \
        [--set flock=path.geojson --set other=... --set untagged=...]

Reads the raw Overpass region tiles the cone builder cached (deduplicated by node id, the
same way build_cones.py merges them) and reports what the paper's \\Cref{tab:tags} and
\\Cref{tab:vendorsets} state: direction-key usage, multi-head units, arc-range tokens,
operator tagging, vendor shares, the exact-vs-substring Flock match, and per-vendor-set
camera/cone/union-part counts with their direction coverage.

It exists so a data refresh is a rerun, not a re-derivation. The July 2026 tables were
assembled from ad-hoc one-liners while the paper was being written; when the snapshot is
replaced, every one of those numbers has to change together, and a script that reproduces
the July table to the digit is the only evidence that the September table is computed the
same way. Parsing is imported from build_cones.py rather than copied, so the statistics
count exactly what the router avoids.
"""
import argparse
import collections
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "server", "alpr"))
from build_cones import ARC_RE, DIRECTION_KEYS, parse_directions, vendor_of  # noqa: E402

# Named vendors the paper tabulates, matched as case-insensitive substrings of the
# vendor value under any of build_cones.VENDOR_KEYS — the same rule the vendor-split
# cone sets use, so the table and the sets agree by construction.
VENDORS = (("Flock Safety", "flock"), ("Motorola Solutions", "motorola"),
           ("Genetec", "genetec"), ("Leonardo", "leonardo"), ("Axis Communications", "axis"))


def load_nodes(cache):
    nodes, stamps = {}, []
    for p in sorted(glob.glob(os.path.join(cache, "tile_*.json"))):
        d = json.load(open(p))
        stamps.append((d.get("osm3s") or {}).get("timestamp_osm_base"))
        for e in d.get("elements", ()):
            if e.get("type") == "node":
                nodes[e["id"]] = e
    return nodes, [s for s in stamps if s]


def heads_of(tags):
    raw = next((tags[k] for k in DIRECTION_KEYS if tags.get(k)), None)
    return raw, parse_directions(raw)


def union_parts(path):
    g = json.load(open(path))["features"][0]["geometry"]
    return len(g["coordinates"]) if g["type"] == "MultiPolygon" else 1


def pct(n, d):
    return f"{100.0 * n / d:5.1f}%" if d else "   --"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True, help="directory of tile_*.json Overpass responses")
    ap.add_argument("--cones", default=None, help="the unioned alpr.geojson, for its part count")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=GEOJSON",
                    help="vendor-split cone set to report union parts for (flock/other/untagged)")
    args = ap.parse_args()

    nodes, stamps = load_nodes(args.cache)
    N = len(nodes)
    print(f"snapshot: {N} unique camera nodes, OSM base {min(stamps)} .. {max(stamps)}")

    # --- direction tagging ---------------------------------------------------------
    via_dir = sum(1 for e in nodes.values() if (e.get("tags") or {}).get("direction"))
    via_cam = sum(1 for e in nodes.values() if (e.get("tags") or {}).get("camera:direction"))
    both = sum(1 for e in nodes.values()
               if (e.get("tags") or {}).get("direction") and (e.get("tags") or {}).get("camera:direction"))
    any_dir = via_dir + via_cam - both
    multihead = semi = arc_tokens = arc45 = unparsed = kept = cones = 0
    for e in nodes.values():
        tags = e.get("tags") or {}
        raw, heads = heads_of(tags)
        if raw and ";" in str(raw):
            semi += 1
        if raw and not heads:
            unparsed += 1
        if heads:
            kept += 1
            cones += len(heads)
            if len(heads) > 1:
                multihead += 1
        for tok in str(raw or "").split(";"):
            m = ARC_RE.match(tok)
            if m:
                arc_tokens += 1
                if (float(m.group(2)) - float(m.group(1))) % 360.0 == 45.0:
                    arc45 += 1
    operator = sum(1 for e in nodes.values() if (e.get("tags") or {}).get("operator"))
    stype = collections.Counter((e.get("tags") or {}).get("surveillance:type") for e in nodes.values())

    print("\n-- direction / tagging (paper tab:tags) --")
    print(f"  any direction tag            {any_dir:7d}  {pct(any_dir, N)}")
    print(f"    via direction              {via_dir:7d}  {pct(via_dir, N)}")
    print(f"    via camera:direction       {via_cam:7d}  {pct(via_cam, N)}   (both keys: {both})")
    # Two definitions, both reported: the July paper table counted values containing
    # ";" (7,014); the router's own notion is "more than one head parsed" (7,010) — the
    # difference is values like "N;" whose second token is empty.
    print(f"  multi-head (>1 parsed head)  {multihead:7d}  {pct(multihead, N)}   (raw ';' values: {semi})")
    print(f"  arc-range tokens             {arc_tokens:7d}  tokens ({arc45} span exactly 45 deg = {pct(arc45, arc_tokens).strip()})")
    print(f"  unparseable direction value  {unparsed:7d}")
    print(f"  operator tagged              {operator:7d}  {pct(operator, N)}")
    print(f"  cameras with >=1 cone        {kept:7d}  -> {cones} cones before union")
    print(f"  surveillance:type spellings  {dict(stype.most_common(6))}")

    # --- vendors ---------------------------------------------------------------------
    print("\n-- vendors (substring across manufacturer/brand keys) --")
    vend = {vid: 0 for _, vid in VENDORS}
    untagged = 0
    for e in nodes.values():
        v = vendor_of(e.get("tags") or {})
        if not v:
            untagged += 1
            continue
        vl = v.lower()
        for _, vid in VENDORS:
            if vid in vl:
                vend[vid] += 1
                break
    exact_m = collections.Counter((e.get("tags") or {}).get("manufacturer") for e in nodes.values())
    for name, vid in VENDORS:
        print(f"  {name:24s} {vend[vid]:7d}  {pct(vend[vid], N)}   (exact manufacturer={name!r}: {exact_m.get(name, 0)})")
    print(f"  {'No vendor tag':24s} {untagged:7d}  {pct(untagged, N)}")
    exact = sum(1 for e in nodes.values() if (e.get("tags") or {}).get("manufacturer") == "Flock Safety")
    brand_flock = sum(1 for e in nodes.values()
                      if "flock" in ((e.get("tags") or {}).get("brand") or "").lower())
    print(f"  exact manufacturer=\"Flock Safety\" {exact} vs substring {vend['flock']} "
          f"(diff {vend['flock'] - exact}; nodes with flock under brand: {brand_flock})")

    # --- vendor-split sets (paper tab:vendorsets) --------------------------------------
    print("\n-- vendor-split sets: cameras with cones / cones / direction coverage --")
    sets = {"flock": [], "other": [], "untagged": []}
    for e in nodes.values():
        tags = e.get("tags") or {}
        v = vendor_of(tags)
        key = "untagged" if not v else ("flock" if "flock" in v.lower() else "other")
        sets[key].append(tags)
    parts = {}
    for spec in args.set:
        name, path = spec.split("=", 1)
        parts[name] = union_parts(path)
    for key, members in sets.items():
        with_cones = [len(parse_directions(next((t[k] for k in DIRECTION_KEYS if t.get(k)), None)))
                      for t in members]
        cams = sum(1 for n in with_cones if n)
        cn = sum(with_cones)
        has_dir = sum(1 for t in members if any(t.get(k) for k in DIRECTION_KEYS))
        print(f"  {key:9s} nodes {len(members):7d}  cameras {cams:7d}  cones {cn:7d}  "
              f"cones/camera {cn / cams if cams else 0:.2f}  direction {pct(has_dir, len(members))}"
              + (f"  union parts {parts[key]}" if key in parts else ""))

    if args.cones:
        print(f"\n-- unioned alpr.geojson: {union_parts(args.cones)} parts --")


if __name__ == "__main__":
    main()
