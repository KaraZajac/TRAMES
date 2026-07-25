#!/usr/bin/env python3
"""
Build GraphHopper custom-area geometry from OSM ALPR (license-plate reader) data.

Each camera becomes a *cone* pointing the way it actually looks, so routing only
detours when your path would cross a camera's field of view — not merely pass near
it. Naive radius avoidance over-detours badly: a camera watching northbound traffic
has no bearing on the southbound carriageway, and treating it as a circle makes the
router dodge roads no one is being read on.

All cones are unioned into ONE MultiPolygon with id "alpr". That matters: GraphHopper
resolves custom areas into a spatial index at import, so a per-request custom model can
then say

    {"priority": [{"if": "in_alpr", "multiply_by": 0.05}]}

with no geometry in the request at all. One area, one rule, and the avoidance strength
stays a continuous per-request knob. Emitting one feature per camera instead would
require an `in_<id>` clause per camera — unusable at any real density.

Tag handling is driven by what OSM actually contains, not what the wiki recommends —
see the notes on each constant below.

Usage:
    python3 build_cones.py --bbox 38.40,-75.80,39.85,-75.03 -o ../graphhopper/custom_areas/alpr.geojson
"""
import argparse
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request

from shapely.geometry import Polygon, mapping
from shapely.ops import unary_union

# The OSM wiki calls `camera:direction` the standard. Measured against live data
# (Atlanta metro, 3,899 cameras) it is used 21 times against 3,864 for plain
# `direction`. Read `direction` first or you discard ~99% of the directional data.
DIRECTION_KEYS = ("direction", "camera:direction")

# Where a camera gives a single bearing rather than an arc, assume this cone width.
# 45 deg is what contributors actually record: of 111 arc-range values measured,
# 94 spanned exactly 45 deg (then 70, 50, 44). It is also close to the Axon Fleet 3
# spec of a 60 deg field of view.
DEFAULT_SPAN_DEG = 45.0

# Cone length. Real plate-read range is shorter (~23 m for a fixed unit), but the cone
# has to actually reach across the carriageway to overlap the road geometry it watches
# — cameras sit set back from the pavement. Too short and the cone lands in the verge
# and flags nothing; too long and it starts catching parallel side streets.
DEFAULT_RADIUS_M = 60.0

CARDINALS = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5,
    "SE": 135, "SSE": 157.5, "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5,
    "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

ARC_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*$")

# Vendor lives under any of four keys and is spelled inconsistently. Measured over
# 120,838 North American nodes: exact `manufacturer == "Flock Safety"` matches 95,069,
# while a case-insensitive substring search across all four keys matches 99,650 — the
# 4,581 difference is mostly 4,812 nodes carrying the vendor in `brand`, plus variants
# ("Flock Group Inc.", "Flock Safety Inc", "FlockSafety", "flock"). Splitting the corpus
# by vendor on the exact key alone would misfile 4.6% of Flock cameras as non-Flock,
# which is the one error a vendor comparison cannot tolerate.
VENDOR_KEYS = ("manufacturer", "surveillance:manufacturer", "brand", "surveillance:brand")


def vendor_of(tags):
    """First non-empty vendor value across the known keys, or None."""
    for k in VENDOR_KEYS:
        v = tags.get(k)
        if v and v.strip():
            return v.strip()
    return None


def _parse_single(v, default_span):
    """Parse one direction token -> (center_bearing, span_deg) or None."""
    m = ARC_RE.match(v)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        span = (b - a) % 360.0
        if span == 0:
            span = default_span
        center = (a + span / 2.0) % 360.0
        return center, span

    try:
        return float(v) % 360.0, default_span
    except ValueError:
        pass

    key = v.upper().replace(" ", "")
    if key in CARDINALS:
        return CARDINALS[key], default_span
    return None


def parse_directions(value, default_span=DEFAULT_SPAN_DEG):
    """
    Return a LIST of (center_bearing, span_deg) — one per camera head. Empty if
    nothing parsed.

    A single OSM node often represents a multi-head unit, and OSM's multi-value
    separator is ';'. Real values measured in Delaware:

      "137"                single bearing        -> 1 cone
      "144-189"            explicit arc          -> 1 cone, span 45
      "338-23"             arc wrapping past 0   -> 1 cone (span via modulo)
      "320;190"            back-to-back pair     -> 2 cones
      "0;72;144;216;288"   5-head 360 unit       -> 5 cones
      "199-269;325-35"     two arcs              -> 2 cones
      "NE"                 cardinal              -> 1 cone

    Returning a list rather than one value matters for correctness, not tidiness: the
    multi-head units are precisely the ones covering both carriageways, so a router
    that silently drops them will happily send you past the cameras hardest to evade.
    """
    if not value:
        return []
    out = []
    for token in str(value).split(";"):
        token = token.strip()
        if not token:
            continue
        parsed = _parse_single(token, default_span)
        if parsed:
            out.append(parsed)
    return out


def cone_polygon(lat, lon, bearing, span, radius_m, segments=8):
    """
    Wedge with its apex at the camera, opening `span` degrees about `bearing`.

    Equirectangular offset — at a 60 m radius the error against a proper geodesic is
    millimetres, and it avoids a pyproj dependency.
    """
    coords = [(lon, lat)]
    start = bearing - span / 2.0
    step = span / segments
    lat_rad = math.radians(lat)
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(lat_rad)
    if abs(m_per_deg_lon) < 1.0:      # guard near the poles
        m_per_deg_lon = 1.0

    for i in range(segments + 1):
        th = math.radians(start + i * step)
        dlat = radius_m * math.cos(th) / m_per_deg_lat
        dlon = radius_m * math.sin(th) / m_per_deg_lon
        coords.append((lon + dlon, lat + dlat))
    coords.append((lon, lat))
    return Polygon(coords)


def fetch_overpass(bbox, timeout_s=180):
    south, west, north, east = bbox
    query = (
        f"[out:json][timeout:{timeout_s}];"
        f'(node["man_made"="surveillance"]["surveillance:type"="ALPR"]'
        f"({south},{west},{north},{east}););out body;"
    )
    payload = urllib.parse.urlencode({"data": query}).encode()
    last_err = None
    # Retry the SAME bbox with exponential backoff before the caller gives up and
    # subdivides. Measured during the continental run: 5 of the first 109 tiles
    # "failed" and were subdivided, and every one was an EMPTY OCEAN tile hit by a
    # transient upstream 504 — overpass-api.de reported full quota throughout, and the
    # identical query succeeded in 4 s moments later. Subdividing cannot fix a
    # transient error; it just issues four queries where one would have done.
    # Subdivision should be reserved for genuine size/complexity limits.
    for attempt in range(4):
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                req = urllib.request.Request(
                    endpoint, data=payload,
                    headers={"User-Agent": "TRAMES/0.1 (ALPR cone builder)"},
                )
                with urllib.request.urlopen(req, timeout=timeout_s + 30) as resp:
                    body = resp.read().decode()
                # Overpass answers 200 with a {"remark": "...timed out..."} body when it
                # gives up. Treat that as failure rather than "no cameras here".
                if '"remark"' in body and (
                    "timed out" in body.lower() or "runtime error" in body.lower()
                ):
                    last_err = f"{endpoint}: overpass runtime/timeout remark"
                    continue
                return json.loads(body)
            except Exception as e:                       # noqa: BLE001
                last_err = f"{endpoint}: {e}"
        time.sleep(min(60, 5 * (2 ** attempt)))          # 5s, 10s, 20s, 40s
    raise RuntimeError(f"all Overpass endpoints failed after 4 rounds: {last_err}")


def _tile_key(bbox):
    return "tile_%.3f_%.3f_%.3f_%.3f.json" % bbox


def fetch_tiled(bbox, tile_deg, cache_dir, polite_s=2.0, min_tile_deg=0.625, depth=0):
    """
    Fetch a large region as tiles, subdividing any tile Overpass can't answer.

    A single continental query exceeds Overpass's runtime limit, and a fixed tile grid
    fails differently: sparse rural tiles waste round-trips while dense metros still
    time out. Adaptive subdivision spends requests where the cameras actually are.

    Every tile response is cached to `cache_dir` keyed by its bbox, so a run that dies
    partway (rate limit, network) resumes without refetching what it already has.
    Nodes are deduplicated by OSM id — tiles share edges, so boundary nodes repeat.
    """
    import os
    os.makedirs(cache_dir, exist_ok=True)
    south, west, north, east = bbox

    tiles = []
    lat = south
    while lat < north:
        lon = west
        while lon < east:
            tiles.append((lat, lon, min(lat + tile_deg, north), min(lon + tile_deg, east)))
            lon += tile_deg
        lat += tile_deg

    nodes = {}
    failed = []
    for i, t in enumerate(tiles, 1):
        path = os.path.join(cache_dir, _tile_key(t))
        data = None
        if os.path.exists(path):
            try:
                data = json.load(open(path))
            except Exception:                            # noqa: BLE001
                data = None
        if data is None:
            try:
                data = fetch_overpass(t)
                json.dump(data, open(path, "w"))
                time.sleep(polite_s)                     # be kind to public Overpass
            except Exception as e:                       # noqa: BLE001
                span = t[2] - t[0]
                if span / 2.0 >= min_tile_deg:
                    print(f"  [{i}/{len(tiles)}] {t} failed ({e}); subdividing")
                    sub = fetch_tiled(t, span / 2.0, cache_dir, polite_s,
                                      min_tile_deg, depth + 1)
                    nodes.update(sub)
                else:
                    print(f"  [{i}/{len(tiles)}] {t} FAILED at min tile size: {e}")
                    failed.append(t)
                continue

        found = 0
        for e in data.get("elements", []):
            if e.get("type") == "node":
                nodes[e["id"]] = e
                found += 1
        if depth == 0:
            print(f"  [{i}/{len(tiles)}] {t[0]:.1f},{t[1]:.1f} -> {found:6d} nodes "
                  f"(running total {len(nodes)})")

    if failed and depth == 0:
        print(f"\n  WARNING: {len(failed)} tiles could not be fetched even at minimum "
              f"size — coverage is INCOMPLETE in those areas:")
        for t in failed[:10]:
            print(f"    {t}")
    return nodes


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", required=True, action="append",
                    help="south,west,north,east — repeatable, one per region. "
                         "Overpass answers the whole continental US in a single ~2 min "
                         "query, so prefer a handful of big regional boxes over tiling: "
                         "a 4-degree grid over North America spends ~86%% of its requests "
                         "on empty ocean and takes hours to return the same data.")
    ap.add_argument("-o", "--out", required=True, help="output GeoJSON path")
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M,
                    help=f"cone length in metres (default {DEFAULT_RADIUS_M})")
    ap.add_argument("--span", type=float, default=DEFAULT_SPAN_DEG,
                    help=f"cone width when only a bearing is given (default {DEFAULT_SPAN_DEG})")
    ap.add_argument("--manufacturer", default=None,
                    help="keep only this exact `manufacturer` tag, e.g. 'Flock Safety'")
    ap.add_argument("--vendor", default=None, metavar="SUBSTR",
                    help="keep only cameras whose vendor contains SUBSTR "
                         "(case-insensitive, across manufacturer/brand keys)")
    ap.add_argument("--vendor-exclude", default=None, metavar="SUBSTR",
                    help="drop cameras whose vendor contains SUBSTR")
    ap.add_argument("--untagged-only", action="store_true",
                    help="keep ONLY cameras with no vendor tag (the residual set that "
                         "belongs to neither vendor split)")
    ap.add_argument("--require-vendor", action="store_true",
                    help="drop cameras with no vendor tag at all (use with "
                         "--vendor-exclude so untagged cameras are not assumed to be "
                         "the complement of the excluded vendor)")
    ap.add_argument("--area-id", default="alpr",
                    help="feature id, referenced as in_<id> by custom models")
    ap.add_argument("--traffic-only", action="store_true",
                    help="keep only surveillance:zone=traffic (drops parking/entrance cams)")
    ap.add_argument("--omnidirectional", action="store_true",
                    help="also include cameras with no direction, as full circles")
    ap.add_argument("--cache", default=None, help="reuse/save raw Overpass JSON here")
    ap.add_argument("--tile-deg", type=float, default=None,
                    help="fetch in tiles of this many degrees (needed for large regions; "
                         "dense tiles are subdivided automatically)")
    ap.add_argument("--tile-cache", default="tile_cache",
                    help="directory for per-tile Overpass responses (resumable)")
    args = ap.parse_args()

    import os
    bboxes = []
    for raw in args.bbox:
        parts = tuple(float(x) for x in raw.split(","))
        if len(parts) != 4:
            sys.exit(f"--bbox needs exactly south,west,north,east (got {raw!r})")
        bboxes.append(parts)

    # Merge across regions by OSM id — regional boxes deliberately overlap at borders
    # (e.g. the US/Canada line) so nothing falls through a seam, which means duplicates
    # are expected and must be collapsed rather than double-counted.
    merged = {}
    for bbox in bboxes:
        if args.tile_deg:
            print(f"tiled fetch over {bbox} at {args.tile_deg} deg ...")
            got = fetch_tiled(bbox, args.tile_deg, args.tile_cache)
        else:
            cache_path = None
            if args.cache:
                cache_path = (args.cache if len(bboxes) == 1
                              else os.path.join(args.cache, _tile_key(bbox)))
                if len(bboxes) > 1:
                    os.makedirs(args.cache, exist_ok=True)
            data = None
            if cache_path and os.path.exists(cache_path):
                try:
                    data = json.load(open(cache_path))
                    print(f"cached: {bbox}")
                except Exception:                        # noqa: BLE001
                    data = None
            if data is None:
                print(f"querying Overpass for {bbox} ...", flush=True)
                t0 = time.time()
                data = fetch_overpass(bbox, timeout_s=600)
                print(f"  took {time.time()-t0:.0f}s", flush=True)
                if cache_path:
                    json.dump(data, open(cache_path, "w"))
            got = {e["id"]: e for e in data.get("elements", []) if e.get("type") == "node"}
        new = len(set(got) - set(merged))
        print(f"  {bbox} -> {len(got)} nodes ({new} new; total {len(merged)+new})")
        merged.update(got)

    nodes = list(merged.values())
    print(f"  {len(nodes)} ALPR nodes total across {len(bboxes)} region(s)")

    stats = {"kept": 0, "cones": 0, "multihead": 0, "no_direction": 0, "unparsed": 0,
             "filtered_manufacturer": 0, "filtered_zone": 0, "omni": 0,
             "filtered_vendor": 0, "filtered_untagged": 0}
    polys = []

    for e in nodes:
        tags = e.get("tags") or {}

        if args.manufacturer and tags.get("manufacturer") != args.manufacturer:
            stats["filtered_manufacturer"] += 1
            continue

        vend = vendor_of(tags)
        vlow = (vend or "").lower()
        if args.vendor and args.vendor.lower() not in vlow:
            stats["filtered_vendor"] += 1
            continue
        if args.vendor_exclude and args.vendor_exclude.lower() in vlow:
            stats["filtered_vendor"] += 1
            continue
        if args.untagged_only and vend:
            stats["filtered_vendor"] += 1
            continue
        if args.require_vendor and not vend:
            stats["filtered_untagged"] += 1
            continue
        if args.traffic_only and tags.get("surveillance:zone") != "traffic":
            stats["filtered_zone"] += 1
            continue

        lat, lon = e.get("lat"), e.get("lon")
        if lat is None or lon is None:
            continue

        raw = next((tags[k] for k in DIRECTION_KEYS if tags.get(k)), None)
        heads = parse_directions(raw, args.span)

        if not heads:
            if raw:
                stats["unparsed"] += 1
            else:
                stats["no_direction"] += 1
            if args.omnidirectional:
                polys.append(cone_polygon(lat, lon, 0.0, 359.9, args.radius, segments=24))
                stats["omni"] += 1
            continue

        for bearing, span in heads:
            polys.append(cone_polygon(lat, lon, bearing, span, args.radius))
        stats["kept"] += 1
        stats["cones"] += len(heads)
        if len(heads) > 1:
            stats["multihead"] += 1

    if not polys:
        sys.exit("no cones built — nothing to write")

    print(f"  cameras kept: {stats['kept']} -> {stats['cones']} cones "
          f"({stats['multihead']} multi-head units)"
          + (f", +{stats['omni']} omnidirectional" if stats["omni"] else ""))
    print(f"  skipped: {stats['no_direction']} no-direction, {stats['unparsed']} unparseable, "
          f"{stats['filtered_manufacturer']} wrong-manufacturer, {stats['filtered_zone']} wrong-zone, "
          f"{stats['filtered_vendor']} wrong-vendor, {stats['filtered_untagged']} untagged-vendor")

    # Union rather than concatenating: overlapping rings inside a single MultiPolygon
    # are invalid geometry, and JTS (which GraphHopper uses) can then answer contains()
    # inconsistently along the overlaps.
    merged = unary_union(polys)
    if merged.geom_type == "Polygon":
        merged = merged.buffer(0)
    print(f"  unioned into {merged.geom_type} "
          f"({len(getattr(merged, 'geoms', [merged]))} part(s)), valid={merged.is_valid}")

    fc = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "id": args.area_id,
            "properties": {"id": args.area_id,
                           "cameras": stats["kept"] + stats["omni"],
                           "radius_m": args.radius},
            "geometry": mapping(merged),
        }],
    }
    with open(args.out, "w") as fh:
        json.dump(fc, fh)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
