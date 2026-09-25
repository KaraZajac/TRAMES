"""
Geography for the siting analysis: TIGER/Line boundaries, ACS attributes, and an equal-area
projection, with no GDAL, pyproj or pyshp — the research environment has shapely and numpy, and
the shapefile polygon format is simple enough to read directly.

    units = load_units("tract")      # GEOID, state, county, projected geometry, ACS attributes
    x, y = project(lon, lat, state_fips)

Distances matter here in tens of metres, so every geometry is projected before use: spherical
Albers equal-area with the standard parallels of EPSG:5070 for the conterminous states, EPSG:3338's
for Alaska and a Hawaii-centred cone for Hawaii. The spherical form is within about a percent of
the ellipsoidal one over each region, well inside what a 25-100 m threshold needs.
"""
import glob
import io
import os
import pickle
import struct
import zipfile

import numpy as np
import shapely

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
TIGER = os.path.join(DATA, "tiger")
R = 6371008.8

FIPS = {"al": "01", "ak": "02", "az": "04", "ar": "05", "ca": "06", "co": "08", "ct": "09", "de": "10",
        "dc": "11", "fl": "12", "ga": "13", "hi": "15", "id": "16", "il": "17", "in": "18", "ia": "19",
        "ks": "20", "ky": "21", "la": "22", "me": "23", "md": "24", "ma": "25", "mi": "26", "mn": "27",
        "ms": "28", "mo": "29", "mt": "30", "ne": "31", "nv": "32", "nh": "33", "nj": "34", "nm": "35",
        "ny": "36", "nc": "37", "nd": "38", "oh": "39", "ok": "40", "or": "41", "pa": "42", "ri": "44",
        "sc": "45", "sd": "46", "tn": "47", "tx": "48", "ut": "49", "vt": "50", "va": "51", "wa": "53",
        "wv": "54", "wi": "55", "wy": "56"}
ABBR = {v: k for k, v in FIPS.items()}
# (lat1, lat2, lat0, lon0) of each region's Albers cone
CONES = {"conus": (29.5, 45.5, 23.0, -96.0), "ak": (55.0, 65.0, 50.0, -154.0), "hi": (8.0, 18.0, 13.0, -157.0)}


def region(state_fips):
    return {"02": "ak", "15": "hi"}.get(state_fips, "conus")


def albers(lon, lat, reg):
    p1, p2, p0, l0 = (np.radians(v) for v in CONES[reg])
    lon, lat = np.radians(np.asarray(lon, float)), np.radians(np.asarray(lat, float))
    n = (np.sin(p1) + np.sin(p2)) / 2
    C = np.cos(p1) ** 2 + 2 * n * np.sin(p1)
    rho0 = R * np.sqrt(C - 2 * n * np.sin(p0)) / n
    rho = R * np.sqrt(C - 2 * n * np.sin(lat)) / n
    th = n * (lon - l0)
    return rho * np.sin(th), rho0 - rho * np.cos(th)


def destination(lon, lat, bearing_deg, dist_m):
    """Point dist_m along a great circle from (lon, lat) at bearing (degrees from north)."""
    la, lo, b = np.radians(lat), np.radians(lon), np.radians(bearing_deg)
    d = np.asarray(dist_m, float) / R
    la2 = np.arcsin(np.sin(la) * np.cos(d) + np.cos(la) * np.sin(d) * np.cos(b))
    lo2 = lo + np.arctan2(np.sin(b) * np.sin(d) * np.cos(la), np.cos(d) - np.sin(la) * np.sin(la2))
    return np.degrees(lo2), np.degrees(la2)


# --- shapefile + dbf, polygons only ------------------------------------------------------

def _dbf(raw):
    n, hlen, rlen = struct.unpack("<IHH", raw[4:12])
    fields, off = [], 32
    while raw[off] != 0x0D:
        name = raw[off:off + 11].split(b"\0")[0].decode()
        fields.append((name, raw[off + 16]))
        off += 32
    rows = []
    for i in range(n):
        rec, pos, row = raw[hlen + i * rlen: hlen + (i + 1) * rlen], 1, {}
        for name, width in fields:
            row[name] = rec[pos:pos + width].decode("latin-1").strip()
            pos += width
        rows.append(row)
    return rows


def _ring_area(xy):
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * (np.dot(x[:-1], y[1:]) - np.dot(x[1:], y[:-1]))


def _polygons(raw, reg):
    """Every polygon record of a .shp, projected; shapefile outer rings run clockwise."""
    out, off = [], 100
    while off < len(raw):
        clen = struct.unpack(">i", raw[off + 4:off + 8])[0] * 2
        rec = raw[off + 8: off + 8 + clen]
        off += 8 + clen
        if struct.unpack("<i", rec[:4])[0] == 0:
            out.append(None); continue
        nparts, npts = struct.unpack("<ii", rec[36:44])
        parts = np.frombuffer(rec, "<i4", nparts, 44)
        pts = np.frombuffer(rec, "<f8", 2 * npts, 44 + 4 * nparts).reshape(-1, 2)
        x, y = albers(pts[:, 0], pts[:, 1], reg)
        P = np.column_stack([x, y])
        bounds = list(parts) + [npts]
        shells, holes = [], []
        for a, b in zip(bounds[:-1], bounds[1:]):
            ring = P[a:b]
            if len(ring) < 4:
                continue
            (shells if _ring_area(ring) < 0 else holes).append(ring)
        if not shells:                        # orientation reversed in the source: take all as shells
            shells, holes = holes, []
        if len(shells) == 1:
            g = shapely.Polygon(shells[0], holes)
        else:
            polys = [shapely.Polygon(s) for s in shells]
            hs = [[] for _ in shells]
            for h in holes:
                pt = shapely.Point(h[0])
                j = next((k for k, p in enumerate(polys) if p.contains(pt)), 0)
                hs[j].append(h)
            g = shapely.MultiPolygon([shapely.Polygon(s, h) for s, h in zip(shells, hs)])
        if not g.is_valid:
            g = shapely.make_valid(g)
        out.append(g)
    return out


def read_layer(state, layer):
    """(attribute rows, projected geometries) of one TIGER/Line 2022 layer for one state."""
    fips = FIPS[state]
    z = zipfile.ZipFile(os.path.join(TIGER, f"tl_2022_{fips}_{layer}.zip"))
    stem = f"tl_2022_{fips}_{layer}"
    return _dbf(z.read(stem + ".dbf")), _polygons(z.read(stem + ".shp"), region(fips))


# --- ACS ------------------------------------------------------------------------------

ACS_COLS = {"pop_total": ("b03002", "B03002_E001"), "nh_white": ("b03002", "B03002_E003"),
            "nh_black": ("b03002", "B03002_E004"), "hispanic": ("b03002", "B03002_E012"),
            "median_income": ("b19013", "B19013_E001")}
SUMLEV = {"tract": "1400000US", "bg": "1500000US", "place": "1600000US", "county": "0500000US"}


def load_acs(level):
    out = {}
    for table in ("b03002", "b19013"):
        want = {k: c for k, (t, c) in ACS_COLS.items() if t == table}
        with open(os.path.join(DATA, f"acs_{table}.dat"), encoding="latin-1") as fh:
            head = fh.readline().rstrip("\n").split("|")
            idx = {k: head.index(c) for k, c in want.items()}
            pre = SUMLEV[level]
            for line in fh:
                if not line.startswith(pre):
                    continue
                p = line.rstrip("\n").split("|")
                d = out.setdefault(p[0][len(pre):], {})
                for k, i in idx.items():
                    try:
                        v = float(p[i])
                    except ValueError:
                        v = np.nan
                    d[k] = v if v >= 0 else np.nan     # ACS jam values (-666666666 etc.) are missing
    return out


def load_units(level, cache=True):
    """All 51 states' units of one level: dict of arrays + a list of projected geometries."""
    path = os.path.join(DATA, f"units_{level}.pkl")
    if cache and os.path.exists(path):
        return pickle.load(open(path, "rb"))
    acs = load_acs(level)
    rec = {"geoid": [], "state": [], "county": [], "aland": [], "region": [], "classfp": [], "name": []}
    geoms = []
    for st in FIPS:
        rows, gs = read_layer(st, level)
        for row, g in zip(rows, gs):
            if g is None or g.is_empty:
                continue
            gid = row["GEOID"]
            rec["geoid"].append(gid); rec["state"].append(FIPS[st])
            rec["county"].append(gid[:5] if level != "place" else "")
            rec["aland"].append(float(row.get("ALAND") or 0)); rec["region"].append(region(FIPS[st]))
            rec["classfp"].append(row.get("CLASSFP", ""))     # places: C* incorporated, U* census-designated
            rec["name"].append(row.get("NAME", ""))
            geoms.append(g)
    U = {k: np.array(v) for k, v in rec.items()}
    for k in ACS_COLS:
        U[k] = np.array([acs.get(g, {}).get(k, np.nan) for g in U["geoid"]])
    U["geom"] = np.array(geoms, dtype=object)
    if cache:
        pickle.dump(U, open(path, "wb"), protocol=pickle.HIGHEST_PROTOCOL)
    return U
