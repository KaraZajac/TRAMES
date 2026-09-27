#!/usr/bin/env python3
"""
Every surveillance device this project has data for, deduplicated, in one file.

    python build_supermap.py -o out/supermap/alpr_supermap.json \\
        --report out/analysis_supermap.txt

Four sources, none of them complete on its own:

  osm         OpenStreetMap's ALPR nodes as Overpass answered them on 2026-09-22
              (server/alpr/region_cache). Every vendor, and the only source that
              records which way a camera looks — the field TRAMES routes on.
  osm-history The same query against OSM's attic data at five earlier dates
              (server/alpr/history), which dates each node's entry into the map.
  flock       Flock Safety's own device registry, published by flocksurveillance.org
              (Michael 2026; records as of December 2025). 335,701 devices with type,
              status and coordinates: the planned and the decommissioned as well as the
              running, and the companion products — audio detectors, PTZ cameras,
              speakers — that an ALPR query never sees. No bearings.
  portals     Agency transparency portals as aggregated by eyesonflock.com (2026-09-24):
              1,528 agencies, the camera count each publishes, and its retention.

DEDUPLICATION. The two per-device sources overlap heavily but disagree about where a
camera is: Flock's coordinates come from its own provisioning, OSM's from a volunteer with
aerial imagery, and the same camera can sit 30 m apart in the two. Records are therefore
paired spatially, not by any shared key (there is none — the registry's externalId columns
are empty in the published extract).

  * An OSM node may pair with a Flock record within MATCH_M metres, if the node's vendor
    is Flock or unrecorded, and the Flock record reads plates. A Genetec node 10 m from a
    Flock falcon is a different device, and so is a Flock audio detector.
  * Pairing is one-to-one and greedy on cost = distance + a 30 m penalty for a record that
    is not in service, because a mapper maps what is visible today. A node carrying several
    bearings ("0;72;144") is a multi-head pole and may claim one Flock record per head.
  * Every Flock record becomes one device. An OSM node that paired adds its tags, bearings
    and provenance to that device; an OSM node that did not becomes a device of its own —
    it is a camera from another vendor, or a Flock camera installed after the registry was
    published, or one the registry omits.
  * Co-located devices are then grouped into sites by single-linkage at SITE_M metres, so a
    consumer can collapse the heads of one pole without having to redo the geometry.

A WARNING ABOUT rotationAngle. The registry's rotationAngle looks like the bearing this
project needs and is not one. Across the one-to-one pairs whose OSM node carries a single
surveyed bearing (some 73,000; the report gives the count), the median absolute
disagreement is 90.0° — what independent angles would give. The best-fitting axial model
(rotationAngle + 49°, modulo 180°) still leaves half the pairs more than 30° out. It is
carried through verbatim as flock.rotation_angle and is not used to derive a bearing; the
only usable direction evidence remains OSM's, plus the travel direction many registry
names name in words.

POSITION. A paired device is published at the OSM node's position when the node stands for
that device alone and the two sources agree to within POSITION_M metres: the volunteer
placed the node against imagery on the pole they saw, and it is the apex the bearing
belongs to. Otherwise — a node shared by several heads, or sources further apart than
that — the registry coordinate stands and the gap is flagged, so that a wrong pairing can
mislabel a device but cannot move it.

WHAT A REGISTRY TYPE IS. Flock publishes no type-to-product table. Classes are read off the
registry itself — the features column where it is populated, and naming conventions where
it is not — and a type whose function that evidence does not settle (picard, avicore, owl)
is classed unknown rather than guessed. The fields.device_types table in the output says
what the evidence was for each.

AGENCIES. Portals come in two kinds: municipal ones carrying a city, and county ones —
sheriffs and county police, typed "SD" by the portal — carrying a county and no city. An
operator naming a county or a sheriff is matched against the county portals only: stripping
"County" from "Fairfax County Police Department" would otherwise hand its cameras to
Fairfax City PD, a different agency.

Geography, and so the US scope, is TIGER/Line 2022 via geo.py: a device is in the United
States if it falls inside a tract of the 50 states or DC. Devices in Puerto Rico and the US
Virgin Islands are kept on a bounding box, since the project holds no TIGER geography for
the territories and Overpass was never asked for them.
"""
import argparse
import csv
import glob
import gzip
import json
import os
import re
import sys
import time
import zlib
from collections import Counter, defaultdict

import numpy as np
import shapely

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "server", "alpr"))
import geo  # noqa: E402
import siting as S  # noqa: E402
from build_cones import ARC_RE, DEFAULT_SPAN_DEG, parse_directions, vendor_of  # noqa: E402

SCHEMA = "trames.supermap/1"
MATCH_M = 50.0          # OSM-to-registry pairing radius; the paper's recall threshold
OFF_SERVICE_PENALTY = 30.0
POSITION_M = 25.0       # a paired device moves to its OSM position only if the sources agree this closely
SITE_M = 25.0           # single-linkage distance for grouping co-located devices into a site
DUP_M = 3.0             # two OSM nodes this close, same bearing and vendor: one camera mapped twice
ROAD_M = 100.0          # a device is "on" the nearest counted road within this (hpms_sample.py's maxd)

# HPMS codings, from hpms_sample.py: the functional systems every state must count in full, the
# facility types it keeps, and its urban flag.
HPMS_SYSTEM = {1: "interstate", 2: "freeway or expressway", 3: "other principal arterial",
               4: "minor arterial", 5: "major collector"}
HPMS_FACILITY = {1: "one-way", 2: "two-way", 4: "ramp", 5: "non-mainline"}
HPMS_URBAN = {0: "rural", 1: "small urban", 2: "urbanized area"}

# Territories with devices in the registry but no TIGER geography here, and no Overpass
# coverage either: the continental box stops at 24 N, so nothing was ever asked for them.
TERRITORY_BOX = {"PR": (17.85, -67.30, 18.55, -65.20), "VI": (17.65, -65.10, 18.45, -64.55),
                 "GU": (13.20, 144.60, 13.70, 145.00), "MP": (14.00, 145.10, 20.60, 146.10),
                 "AS": (-14.60, -171.10, -14.05, -168.10)}

# --- Flock device types -------------------------------------------------------------------
# Flock publishes no type-to-product table, so this is read off the registry: the `features`
# column where it is populated (only in-service records carry it), and otherwise the naming
# conventions in `name`. `plates` is None where the type reads plates only sometimes, in
# which case the record's own features decide.
KIND = {
    "falcon":              ("alpr", True, "fixed plate reader: every record with features reads plates"),
    "falconFlex":          ("alpr", True, "relocatable plate reader"),
    "falconHighway":       ("alpr", True, "highway-speed plate reader"),
    "sparrow":             ("alpr", True, "plate reader (the earlier model)"),
    "lprTrailer":          ("alpr", True, "plate reader on a trailer"),
    "automotus":           ("alpr", True, "Automotus curb-enforcement camera"),
    "wingUbicquia":        ("alpr", True, "plate reader on Ubicquia street-light hardware: 84% of names carry a "
                                          "direction of travel, as Falcons' do, and most name LPR"),
    "condor":              ("video", None, "pan-tilt-zoom video camera; features say livestream"),
    "picardPtz":           ("video", None, "pan-tilt-zoom video camera"),
    "picardTrailer":       ("video", None, "camera on a trailer"),
    "wing":                ("video", None, "video camera, indoor and outdoor; features say livestream, and 7% read plates"),
    "multiEvidenceDevice": ("video", None, "multi-camera unit (names say 'Quad Cam Multiview')"),
    "picard":              ("unknown", None, "not established: no record carries features; 33,078 of 48,680 share a "
                                             "Falcon's coordinate and 13,828 a Condor's, 57% of names carry a direction "
                                             "of travel, and half are still planned"),
    "avicore":             ("unknown", None, "not established: a companion at Condor and trailer sites whose names "
                                             "repeat the Condor's with '- Avicore'; no features"),
    "owl":                 ("unknown", None, "not established: names say hub, or a concession stand; no features"),
    "trailer":             ("mobile_platform", None, "towable platform carrying other devices"),
    "raven":               ("audio_detection", False, "audio/gunshot detector"),
    "talkDown":            ("speaker", False, "remote speaker"),
    "drone":               ("drone", None, "drone"),
    "droneDockingStation": ("drone_infrastructure", False, "drone dock"),
    "droneControllerBox":  ("drone_infrastructure", False, "drone control box"),
    "droneRadar":          ("drone_infrastructure", False, "radar supporting drone operations"),
    "backhaulBox":         ("infrastructure", False, "network backhaul"),
    "wingGateway":         ("infrastructure", False, "camera network gateway"),
    "wingApi":             ("infrastructure", False, "software integration, not a device"),
    "factoryFixture":      ("infrastructure", False, "Flock's own factory test fixture"),
    "external":            ("external_integration", None, "third-party or in-vehicle camera fed to Flock"),
}
STATUS = {"inService": "in_service", "inPlanning": "planned",
          "decommissioned": "decommissioned", "unknown": "unknown"}

# Registry names carry the traffic a device watches ("#23 SB Motel at Hadley"). That is the
# direction of travel being read, not the way the camera faces, and it is recorded as such.
TRAVEL = {"nb": "northbound", "sb": "southbound", "eb": "eastbound", "wb": "westbound",
          "northbound": "northbound", "southbound": "southbound", "eastbound": "eastbound",
          "westbound": "westbound", "nbfs": "northbound", "sbfs": "southbound",
          "ebfs": "eastbound", "wbfs": "westbound", "inbound": "inbound", "outbound": "outbound"}
TRAVEL_RE = re.compile(r"(?<![a-z0-9])(" + "|".join(sorted(TRAVEL, key=len, reverse=True)) + r")(?![a-z0-9])", re.I)

# Registry names also carry status in words the status column has not caught up with — 69
# in-service devices are named DNU, "do not use" — and mark trials as temp, demo or pilot. The
# "#29" most names open with is the number the agency itself uses for the camera, which is how
# a public-records response or a council agenda refers to it.
RETIRED_RE = re.compile(r"\bDNU\b|\bdo not use\b|\bnot in use\b|\bunused\b|\bremoved?\b|decommission|"
                        r"\boffline\b|\binactive\b", re.I)
TEMPORARY_RE = re.compile(r"\btemp(orary)?\b|\bdemo\b|\bpilot\b|\btrial\b", re.I)
FLEET_RE = re.compile(r"#\s*0*(\d{1,5})\b")

# Vendor strings are spelled 210 ways across the OSM corpus. Where a node carries
# manufacturer:wikidata the QID decides the family; otherwise these alternates do, matched
# against the value lowercased with punctuation and spaces removed. Anything unmatched is
# passed through verbatim rather than dropped.
VENDOR_ALT = {
    "Flock Safety": ("flocksafety", "flockgroupinc", "flock", "flocksafetyinc", "flocksafteyinc"),
    "Motorola Solutions": ("motorolasolutions", "motorola", "mortorolasolutions", "vigilantsolutions",
                           "motorolasolutionsvigilant", "motorolavigilant", "motorolasolutionsl6q",
                           "vigilant", "motorolasolutionsinc"),
    "Avigilon": ("avigilon",),
    "Genetec": ("genetec", "genetech", "genetecinc"),
    "Axis Communications": ("axiscommunications", "axis", "axiscommunication", "axiscommunicationsab"),
    "Leonardo": ("leonardo", "elsag", "leonardouscyberandsecuritysolutionsinc", "selexes"),
    "Rekor": ("rekor", "rekorsystems", "rekorsystemsinc", "otherwrekorscout", "rekorscout"),
    "PlateSmart": ("platesmart", "platesmartcyclopstchnlgs", "cyclopstechnologies", "cyclopstchnlgs"),
    "Axon Enterprise": ("axonenterprise", "axon", "axonenterpriseinc"),
    "Neology": ("neologyinc", "neology", "neology3m", "pipstechnology"),
    "Ubicquia": ("ubicquiainc", "ubicquia"),
    "Ekin": ("ekinboxspotter", "ekin", "ekinxspotter", "ekinspotter"),
    "Verkada": ("verkada", "verkadainc"),
    "NDI Recognition Systems": ("ndirecognitionsystems", "ndirs"),
    "Kapsch": ("kapsch", "kapschvrx350x", "kapschtrafficcom"),
    "Hanwha Vision": ("hanwhavision", "hanwha"),
    "Bosch": ("boschsecuritysystems", "bosch"),
    "Dahua Technology": ("dahuatechnology", "dahua"),
    "Hikvision": ("hikvision",),
    "Uniview": ("univiewtechnologies", "uniview", "unv"),
    "Mobotix": ("mobotix", "mobitix"),
    "TransCore": ("transcore",),
    "LiveView Technologies": ("liveviewtechnologies", "lvt", "liveviewtechnology"),
    "Packetalk": ("packetalk", "pacetalk"),
    "Insight LPR": ("insightlpr", "insight"),
    "Redspeed": ("redspeedredcurb", "redspeed"),
    "Redflex": ("redflexholdings", "redflex"),
    "Verra Mobility": ("verramobility",),
    "Iteris": ("iteris",),
    "CoStar": ("costar",),
    "Turing AI": ("turingai",),
    "Eagle Eye Networks": ("eagleeye", "eagleeyenetworks"),
    "Pelco": ("pelco",),
    "Teledyne FLIR": ("flir", "teledyneflir"),
    "Adaptive Recognition": ("adaptiverecognition",),
    "SkyCop": ("skycop", "skycopinc"),
    "Municipal Parking Services": ("municipalparkingservices",),
    "EPIC IO Technologies": ("epiciotechnologies", "epicio"),
    "Mesa Technologies": ("mesatechnologies",),
    "Secure Technical Solutions": ("securetechnicalsolutions", "securetechnicalsystems"),
    "Reconyx": ("reconyx",),
    "Viewtron": ("viewtron",),
    "Linovision": ("linovision",),
    "RTX Corporation": ("rtxcorporation", "rtx", "raytheon"),
    "Novoa Global": ("novoaglobal",),
    "Mobile Pro Systems": ("mobileprosystems",),
    "Gridless": ("gridless",),
    "ARVOO": ("arvoodiamond", "arvoo"),
    "Intelisite": ("intelisite",),
    "Mav": ("maviq350xr", "mav"),
}
VENDOR_LOOKUP = {a: fam for fam, alts in VENDOR_ALT.items() for a in alts}
UNKNOWN_VENDOR = {"unknown", "unkwn", "other", "n/a", "none", "?", "", "q108485435"}


def squash(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def log(L, s=""):
    L.append(s)
    print(s, flush=True)


# --- loading ------------------------------------------------------------------------------

def load_osm(cache_dir):
    """Every ALPR node of the current Overpass snapshot, by node id; tiles overlap."""
    nodes, stamps = {}, []
    for p in sorted(glob.glob(os.path.join(cache_dir, "tile_*.json"))):
        d = json.load(open(p))
        st = (d.get("osm3s") or {}).get("timestamp_osm_base")
        if st:
            stamps.append(st)
        for e in d.get("elements", ()):
            if e.get("type") == "node" and e.get("lat") is not None:
                nodes[e["id"]] = e
    return nodes, (min(stamps) if stamps else None)


def load_history(hist_dir, extra=()):
    """node id -> earliest dated snapshot in which it was already on the map."""
    dirs = [(date, os.path.join(hist_dir, date)) for date in os.listdir(hist_dir)
            if os.path.isdir(os.path.join(hist_dir, date)) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)]
    dirs += list(extra)
    first, dates = {}, []
    for date, d in sorted(dirs):
        ids = set()
        for p in glob.glob(os.path.join(d, "tile_*.json")):
            for e in json.load(open(p)).get("elements", ()):
                if e.get("type") == "node":
                    ids.add(e["id"])
        if not ids:
            continue
        dates.append((date, len(ids)))
        for i in ids:
            first.setdefault(i, date)
    return first, dates


def load_inventory(path):
    return list(csv.DictReader(open(path, newline=""), delimiter="\t"))


def load_portals(full_path, slim_path, with_sharing=False):
    """Agencies, from the aggregate with the detail plus the slim file's freshness stamp."""
    full = json.load(open(full_path))
    slim = {p["slug"]: p for p in json.load(open(slim_path))["portals"]}
    out, summary = [], full.get("summary") or {}
    for p in full.get("portals", ()):
        url = p.get("portal_url") or ""
        slug = p.get("slug") or url.rstrip("/").rsplit("/", 1)[-1]
        s = slim.get(slug, {})
        out.append({
            "slug": slug, "portal_url": url or None,
            "city": p.get("city"), "county": p.get("county"), "state": p.get("state"),
            "agency_type": p.get("type"), "population": p.get("population"),
            "cameras_published": p.get("total_cameras"),
            "data_retention_days": p.get("data_retention"),
            "searches": p.get("total_searches"), "vehicles_captured": p.get("vehicles_captured"),
            "hotlist_hits": p.get("hotlist_hits"), "hotlist_hit_rate": p.get("hotlist_hit_rate"),
            "organizations_shared_with": p.get("organization_count"),
            "organizations_shared_with_named": len(p.get("organizations_shared_with") or ()) or None,
            "portal_updated": s.get("data_last_updated"),
        })
        if with_sharing:
            out[-1]["organizations_shared_with_list"] = p.get("organizations_shared_with") or []
    return out, summary


# --- classification -----------------------------------------------------------------------

def vendor_family(raw, qid, qid_names):
    """(canonical family, whether it was recognised). QID wins; else the alternates table."""
    if qid and qid in qid_names:
        raw = qid_names[qid]
    elif raw and re.fullmatch(r"Q\d+", raw.strip()) and raw.strip() in qid_names:
        raw = qid_names[raw.strip()]                     # a QID typed into the manufacturer field
    k = squash(raw)
    if not k or k in UNKNOWN_VENDOR:
        return None, False
    if k in VENDOR_LOOKUP:
        return VENDOR_LOOKUP[k], True
    for fam, alts in VENDOR_ALT.items():                 # a longer string containing a known one
        if any(a in k and len(a) >= 5 for a in alts):
            return fam, True
    return (raw or "").strip(), False


def classify(dev_type, features):
    """(class, reads_plates, note). features decides where it is populated; else the type."""
    kind, plates, note = KIND.get(dev_type, ("unknown", None, "type not in the registry's known set"))
    if features:
        reads = "readsLicensePlates" in features or "lpr" in features
        return kind, reads, note
    return kind, plates, note


def travel_hint(name):
    m = TRAVEL_RE.search(name or "")
    return TRAVEL[m.group(1).lower()] if m else None


def directions(value):
    """build_cones' parser, except that an arc closing on itself ("0-360", 33 nodes) is one head
    spanning the full circle, not the 45-degree default the parser gives an arc of zero width."""
    out = []
    for tok in str(value or "").split(";"):
        m = ARC_RE.match(tok)
        if m and m.group(1) != m.group(2) and (float(m.group(2)) - float(m.group(1))) % 360.0 == 0:
            out.append((0.0, 360.0))
        else:
            out.extend(parse_directions(tok))
    return out


# --- geometry -----------------------------------------------------------------------------

def pair_osm_to_flock(olon, olat, oheads, oeligible, flon, flat, feligible, fpenalty):
    """One-to-one greedy pairing within MATCH_M. Returns (osm index, flock index, distance)."""
    oi_all, fi_all, d_all, c_all = [], [], [], []
    reg_o, reg_f = S.regions_of(olon, olat), S.regions_of(flon, flat)
    for ri, rname in enumerate(S.REG):
        io = np.flatnonzero((reg_o == ri) & oeligible)
        jf = np.flatnonzero((reg_f == ri) & feligible)
        if not len(io) or not len(jf):
            continue
        ox, oy = geo.albers(olon[io], olat[io], rname)
        fx, fy = geo.albers(flon[jf], flat[jf], rname)
        tree = shapely.STRtree(shapely.points(fx, fy))
        q = tree.query(shapely.points(ox, oy), predicate="dwithin", distance=MATCH_M)
        if not q.shape[1]:
            continue
        a, b = q[0], q[1]
        d = np.hypot(ox[a] - fx[b], oy[a] - fy[b])
        oi_all.append(io[a]); fi_all.append(jf[b]); d_all.append(d)
        c_all.append(d + fpenalty[jf[b]])
    if not oi_all:
        return np.empty(0, int), np.empty(0, int), np.empty(0)
    oi, fi, d, c = (np.concatenate(x) for x in (oi_all, fi_all, d_all, c_all))
    order = np.argsort(c, kind="stable")
    cap = oheads.copy()
    taken = np.zeros(len(flon), bool)
    keep = []
    for k in order:
        if cap[oi[k]] <= 0 or taken[fi[k]]:
            continue
        cap[oi[k]] -= 1
        taken[fi[k]] = True
        keep.append(k)
    keep = np.array(keep, int)
    return oi[keep], fi[keep], d[keep]


def nearest_hpms(H, lon, lat):
    """Index of the nearest HPMS road point within ROAD_M of each device (-1 if none), and how far.

    hpms_sample.py laid a point every 200 m along every counted section, so the nearest point is
    the nearest counted road to within 100 m along it; a device with none within ROAD_M stands on
    a local street or minor collector, which HPMS does not count.
    """
    plon, plat = H["pt_lon"], H["pt_lat"]
    ix, dist = np.full(len(lon), -1, np.int64), np.full(len(lon), np.nan)
    rp, rd = S.regions_of(plon, plat), S.regions_of(lon, lat)
    for ri, rname in enumerate(S.REG):
        pi, di = np.flatnonzero(rp == ri), np.flatnonzero(rd == ri)
        if not len(pi) or not len(di):
            continue
        px, py = geo.albers(plon[pi], plat[pi], rname)
        dx, dy = geo.albers(lon[di], lat[di], rname)
        tree = shapely.STRtree(shapely.points(px, py))
        idx, d = tree.query_nearest(shapely.points(dx, dy), max_distance=ROAD_M,
                                    return_distance=True, all_matches=False)
        ix[di[idx[0]]], dist[di[idx[0]]] = pi[idx[1]], d
    return ix, dist


def near_duplicate_nodes(lon, lat, mask, sig):
    """For each node, the lowest-indexed other node within DUP_M carrying the same signature
    (direction string and vendor), or -1. Two heads on one pole point different ways; two nodes
    a metre apart pointing the same way are one camera mapped twice. Nodes with no direction
    are left alone: without one the two readings cannot be told apart."""
    partner = np.full(len(lon), -1, np.int64)
    reg = S.regions_of(lon, lat)
    for ri, rname in enumerate(S.REG):
        ix = np.flatnonzero((reg == ri) & mask)
        if len(ix) < 2:
            continue
        x, y = geo.albers(lon[ix], lat[ix], rname)
        pts = shapely.points(x, y)
        q = shapely.STRtree(pts).query(pts, predicate="dwithin", distance=DUP_M)
        for a, b in zip(ix[q[0]], ix[q[1]]):
            if a != b and sig[a][0] and sig[a] == sig[b]:
                partner[a] = b if partner[a] < 0 else min(partner[a], b)
    return partner


def site_ids(lon, lat, link_m):
    """Single-linkage clusters of co-located devices, as a stable index per device.

    Devices sharing a coordinate exactly are collapsed before the linkage runs. That is not an
    optimisation for its own sake: the registry gives 860 devices one building's coordinate, and
    a clique that size contributes 369,000 pairs to a union-find that gains nothing from them.
    """
    q = np.round(np.column_stack([lat, lon]), 7)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    ulon, ulat = lon[first], lat[first]
    m = len(first)
    parent = np.arange(m)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    reg = S.regions_of(ulon, ulat)
    for ri, rname in enumerate(S.REG):
        ix = np.flatnonzero(reg == ri)
        if len(ix) < 2:
            continue
        x, y = geo.albers(ulon[ix], ulat[ix], rname)
        pts = shapely.points(x, y)
        pq = shapely.STRtree(pts).query(pts, predicate="dwithin", distance=link_m)
        keep = pq[0] < pq[1]
        for a, b in zip(ix[pq[0][keep]], ix[pq[1][keep]]):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    roots = np.array([find(i) for i in range(m)])
    # renumber in a deterministic order: by each cluster's minimum (lat, lon)
    uniq, ui = np.unique(roots, return_inverse=True)
    ui = ui.ravel()
    mlat, mlon = np.full(len(uniq), np.inf), np.full(len(uniq), np.inf)
    np.minimum.at(mlat, ui, ulat)
    np.minimum.at(mlon, ui, ulon)
    order = np.lexsort((np.round(mlon, 6), np.round(mlat, 6)))
    rank = np.empty(len(uniq), int)
    rank[order] = np.arange(len(uniq))
    return rank[ui][inv], len(uniq)


# --- agency matching ----------------------------------------------------------------------

AGENCY_WORDS = re.compile(
    r"\b(police|department|dept|office|division|bureau|public safety|dps|pd|sheriff|sheriffs|so|"
    r"city of|town of|village of|borough of|of|the)\b", re.I)
COUNTY_RE = re.compile(r"\b(county|parish|sheriff|sheriff's)\b", re.I)
MUNICIPAL_RE = re.compile(r"police|\bpd\b|public safety|\bdps\b|university|college|campus", re.I)


def agency_key(s):
    """A name reduced to what identifies the place: possessives, agency words and the
    township/twp spelling difference removed, then squashed."""
    s = re.sub(r"'s\b", "", s or "")
    s = re.sub(r"\btwp\b", "township", s, flags=re.I)
    return squash(AGENCY_WORDS.sub(" ", s))


def agency_index(agencies):
    """(city portals by (place, state), county portals by (county, state))."""
    city, county = defaultdict(list), defaultdict(list)
    for a in agencies:
        st = (a["state"] or "").upper()
        if not st:
            continue
        if a["agency_type"] == "SD" and a["county"]:
            county[(agency_key(re.sub(r"\bcounty\b", "", a["county"], flags=re.I)), st)].append(a["slug"])
        elif a["city"]:
            city[(agency_key(a["city"]), st)].append(a["slug"])
    return city, county


def match_agency(operator, state, ix):
    """A portal slug for an OSM operator string: exact place+state match, and only one portal."""
    if not operator or not state:
        return None, None
    city, county = ix
    st = state.upper()
    if COUNTY_RE.search(operator):
        hit = county.get((agency_key(re.sub(r"\b(county|parish)\b", "", operator, flags=re.I)), st))
        return (hit[0], "operator_county") if hit and len(hit) == 1 else (None, None)
    if not MUNICIPAL_RE.search(operator):
        return None, None
    hit = city.get((agency_key(operator), st))
    return (hit[0], "operator_city") if hit and len(hit) == 1 else (None, None)


# --- main ---------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", default=os.path.join(ROOT, "server", "alpr", "region_cache"))
    ap.add_argument("--history", default=os.path.join(ROOT, "server", "alpr", "history"))
    ap.add_argument("--inventory", default=os.path.join(ROOT, "research", "data", "flocksurveillance", "cameras.tsv"))
    ap.add_argument("--portals", default=os.path.join(ROOT, "research", "data", "validation", "eyesonflock_portals.json"))
    ap.add_argument("--portals-slim", default=os.path.join(ROOT, "research", "data", "validation", "transparency_portals_2026-09-24.json"))
    ap.add_argument("--cache", default=None, help="directory for the point-to-unit assignment cache")
    ap.add_argument("--extra-snapshot", action="append", default=None, metavar="DATE=TILE_DIR",
                    help="further dated Overpass caches for first_seen (default: the July 2026 cache, if present)")
    ap.add_argument("--hpms", default=os.path.join(ROOT, "research", "out", "siting", "hpms.npz"))
    ap.add_argument("--roads", default=os.path.join(ROOT, "research", "out", "siting", "roads.npz"))
    ap.add_argument("--with-sharing", action="store_true",
                    help="embed each agency's list of the organisations it shares with (~15 MB)")
    ap.add_argument("--node-dates", default=os.path.join(ROOT, "server", "alpr", "osm-history", "node_dates.json"),
                    help="node_dates.py output: exact dates per OSM node (used if present)")
    ap.add_argument("--roads-watched", default=os.path.join(ROOT, "research", "out", "supermap", "roads_watched.json"),
                    help="roads_watched.py output: the ways each device's cone or circle touches (used if present)")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--gzip", action="store_true", help="also write <out>.gz")
    args = ap.parse_args()
    L = []
    t0 = time.time()

    log(L, "=" * 78)
    log(L, "ALPR SUPER-MAP: every device the project has data for, deduplicated")
    log(L, "=" * 78)

    # --- sources ---------------------------------------------------------------------
    nodes, osm_stamp = load_osm(args.osm)
    extra = [tuple(s.partition("=")[::2]) for s in (args.extra_snapshot or [])]
    july = os.path.join(ROOT, "server", "alpr", "region_cache.2026-07-24")
    if args.extra_snapshot is None and os.path.isdir(july):
        extra = [("2026-07-24", july)]
    first_seen, hist_dates = load_history(args.history, extra)
    rows = load_inventory(args.inventory)
    agencies, portal_summary = load_portals(args.portals, args.portals_slim, args.with_sharing)
    log(L, f"\nosm          {len(nodes):,} ALPR nodes, Overpass base {osm_stamp}")
    log(L, f"osm-history  {len(hist_dates)} dated snapshots: " +
        ", ".join(f"{d} ({n:,})" for d, n in hist_dates))
    log(L, f"flock        {len(rows):,} device records (flocksurveillance.org, December 2025)")
    log(L, f"portals      {len(agencies):,} agency portals, "
           f"{sum(a['cameras_published'] or 0 for a in agencies):,} cameras published between them")
    node_dates, nd_meta = {}, {}
    if os.path.exists(args.node_dates):
        nd_meta = json.load(open(args.node_dates))
        node_dates = nd_meta.pop("nodes")
        log(L, f"node-dates   exact dates for {len(node_dates):,} ever-ALPR nodes from the OSM history dump of "
               f"{nd_meta.get('dump_date')}")
    roads_watched, rw_meta = {}, {}
    if os.path.exists(args.roads_watched):
        rw_meta = json.load(open(args.roads_watched))
        roads_watched = rw_meta.pop("devices")
        log(L, f"roads        the ways each device's cone or circle touches, for {len(roads_watched):,} devices "
               f"({rw_meta.get('pbf')})")

    oid = np.array(sorted(nodes))
    onode = [nodes[i] for i in oid]
    olon = np.array([e["lon"] for e in onode])
    olat = np.array([e["lat"] for e in onode])
    otags = [e.get("tags") or {} for e in onode]

    flon = np.array([float(r["lon"]) for r in rows])
    flat = np.array([float(r["lat"]) for r in rows])

    # --- geography -------------------------------------------------------------------
    cache = args.cache or os.path.join(os.path.dirname(os.path.abspath(args.out)), "assign")
    os.makedirs(cache, exist_ok=True)
    lon_all, lat_all = np.concatenate([olon, flon]), np.concatenate([olat, flat])
    # The cache is keyed on the points themselves. siting.py keys its own on roads.npz's mtime;
    # a constant here would hand a new OSM snapshot the old snapshot's tracts, silently, because
    # every registry index shifts by the difference in node count.
    S.CACHE["dir"] = cache
    S.CACHE["src"] = float(zlib.crc32(lon_all.tobytes()) ^ (zlib.crc32(lat_all.tobytes()) << 1))
    UT = geo.load_units("tract")
    tract = S.cached("supermap_tract", S.assign, UT, lon_all, lat_all, neighbours=False)[0]
    UP = geo.load_units("place")
    place, _, _, _ = S.cached("supermap_place", S.assign, UP, lon_all, lat_all, neighbours=False)
    pnear, pnd = S.cached("supermap_place_near", S.nearest_within, UP, lon_all, lat_all, 2000.0)

    terr = np.full(len(lon_all), "", dtype=object)
    out_of_tract = tract < 0
    for code, (s, w, n, e) in TERRITORY_BOX.items():
        m = out_of_tract & (lat_all >= s) & (lat_all <= n) & (lon_all >= w) & (lon_all <= e)
        terr[m] = code
    in_us = (tract >= 0) | (terr != "")
    n_o = len(olon)
    log(L, f"\ngeography    {int(in_us.sum()):,} of {len(lon_all):,} records fall in the United States "
           f"({int((tract >= 0).sum()):,} in a tract of the 50 states + DC, "
           f"{int((terr != '').sum()):,} in a territory by bounding box)")
    drop = Counter()
    for i in np.flatnonzero(~in_us):
        drop["osm" if i < n_o else "flock"] += 1
    log(L, f"             excluded as outside the US: {drop['osm']:,} OSM nodes (Canada and Mexico, "
           f"which the Overpass boxes cover on purpose), {drop['flock']:,} registry records")

    us_o, us_f = in_us[:n_o], in_us[n_o:]

    # --- vendor canonicalisation -----------------------------------------------------
    qid_names = defaultdict(Counter)
    for t in otags:
        for k in ("manufacturer:wikidata", "brand:wikidata"):
            if t.get(k):
                v = vendor_of(t)
                if v:
                    qid_names[t[k]][v] += 1
    qid_names = {q: c.most_common(1)[0][0] for q, c in qid_names.items()}

    ovendor, ovrec = [], []
    for t in otags:
        fam, known = vendor_family(vendor_of(t), t.get("manufacturer:wikidata") or t.get("brand:wikidata"), qid_names)
        ovendor.append(fam); ovrec.append(known)
    is_flock_osm = np.array([v == "Flock Safety" for v in ovendor])
    no_vendor_osm = np.array([v is None for v in ovendor])

    # --- pairing ---------------------------------------------------------------------
    odirs = [directions(t.get("direction") or t.get("camera:direction")) for t in otags]
    oheads = np.array([max(1, len(d)) for d in odirs])
    fkind, fplates = [], []
    for r in rows:
        k, p, _ = classify(r["type"], r["features"])
        fkind.append(k); fplates.append(p)
    fplates_arr = np.array([p is True for p in fplates])
    fstatus = np.array([STATUS.get(r["status"], "unknown") for r in rows])

    o_elig = us_o & (is_flock_osm | no_vendor_osm)
    f_elig = us_f & fplates_arr
    penalty = np.where(fstatus == "in_service", 0.0, OFF_SERVICE_PENALTY)
    pi_o, pi_f, pd_m = pair_osm_to_flock(olon, olat, oheads, o_elig, flon, flat, f_elig, penalty)
    paired_o = np.zeros(n_o, bool); paired_o[pi_o] = True
    osm_of_flock = np.full(len(rows), -1, int); osm_of_flock[pi_f] = pi_o
    dist_of_flock = np.full(len(rows), np.nan); dist_of_flock[pi_f] = pd_m
    dist_of_flock = np.round(dist_of_flock, 1)        # the published precision, so the position rule
    node_uses = np.bincount(pi_o, minlength=n_o)      # and its flag agree with the file exactly
    # (node_uses: a multi-head node stands for several devices)

    log(L, "\n" + "-" * 78)
    log(L, "PAIRING (one-to-one, within %.0f m, Flock-or-unrecorded vendor to a plate reader)" % MATCH_M)
    log(L, "-" * 78)
    log(L, f"  eligible: {int(o_elig.sum()):,} OSM nodes ({int(oheads[o_elig].sum()):,} heads), "
           f"{int(f_elig.sum()):,} registry plate readers")
    log(L, f"  paired:   {len(pi_o):,} pairs covering {int(paired_o.sum()):,} OSM nodes "
           f"({100 * paired_o[o_elig].mean():.1f}% of eligible nodes) and "
           f"{100 * len(pi_f) / max(1, int(f_elig.sum())):.1f}% of eligible registry readers")
    if len(pd_m):
        log(L, f"  separation between the two sources' coordinates: median {np.median(pd_m):.1f} m, "
               f"mean {pd_m.mean():.1f} m, 90th percentile {np.percentile(pd_m, 90):.1f} m")
    got = np.zeros(len(rows), bool); got[pi_f] = True
    log(L, "  share of eligible registry plate readers that a mapped node stands on, by status —")
    log(L, "  a planned camera is one nobody can photograph yet, and a decommissioned one is a pole "
           "the map may not\n  have caught up with:")
    for stt in ("in_service", "planned", "decommissioned", "unknown"):
        m = f_elig & (fstatus == stt)
        if m.sum():
            log(L, f"    {stt:16s} {int(m.sum()):8,d} eligible   {100 * got[m].mean():5.1f}% paired")
    log(L, f"    {'':16s} {'':8s}           of which a node stands on more than one: "
           f"{int((node_uses > 1).sum()):,} nodes")
    log(L, "  The in-service figure runs a few points under validate_inventory.py's recall at the same"
           "\n  50 m, and should: that measure asks whether any node is near, and lets one node answer"
           "\n  for every camera on the pole, where this pairing makes each node answer for as many "
           "cameras\n  as it has bearings and no more.")
    log(L, f"  unpaired OSM nodes in the US: {int((us_o & ~paired_o).sum()):,} — another vendor, or a "
           f"camera the December 2025 registry does not carry")

    # --- rotationAngle against surveyed bearings -------------------------------------
    rot = np.array([float(r["rotationAngle"]) if r["rotationAngle"].strip() else np.nan for r in rows])
    single = np.array([len(odirs[i]) == 1 and odirs[i][0][1] < 360 for i in pi_o])
    have = single & ~np.isnan(rot[pi_f])
    if have.sum() > 100:
        bear = np.array([odirs[i][0][0] for i in pi_o[have]])
        rr = rot[pi_f[have]]
        d360 = (rr - bear) % 360
        d360 = np.where(d360 > 180, d360 - 360, d360)
        ax = (rr + 49 - bear) % 180
        ax = np.where(ax > 90, ax - 180, ax)
        log(L, f"\n  rotationAngle against OSM's surveyed bearing, on {int(have.sum()):,} pairs whose node "
               f"carries one bearing:\n    median |difference| {np.median(np.abs(d360)):.1f} deg "
               f"(independent angles would give 90.0); within 20 deg {100 * np.mean(np.abs(d360) < 20):.1f}%. "
               f"Best axial fit\n    (rotationAngle + 49 deg, modulo 180) leaves "
               f"{100 * np.mean(np.abs(ax) >= 30):.1f}% of pairs 30 deg or more out. Carried verbatim; "
               f"no bearing is derived from it.")

    # --- devices ---------------------------------------------------------------------
    # One device per registry record, plus one per OSM node that paired with none. A paired
    # device is published at its OSM position when that node stands for it alone and the sources
    # agree to within POSITION_M: the volunteer placed the node against aerial imagery on the pole
    # they saw, and it is the apex the one bearing we have belongs to. A node shared by several
    # heads, or a pair further apart than that, leaves the registry coordinate in place: a wrong
    # pairing may then mislabel a device but cannot move it.
    agix = agency_index(agencies)
    at_osm = (osm_of_flock >= 0) & (dist_of_flock <= POSITION_M) & (node_uses[np.maximum(osm_of_flock, 0)] == 1)
    dev_lon, dev_lat, dev_src = [], [], []          # source rows for the emitted devices
    for j in np.flatnonzero(us_f):
        oi = int(osm_of_flock[j])
        dev_lon.append(olon[oi] if at_osm[j] else flon[j])
        dev_lat.append(olat[oi] if at_osm[j] else flat[j])
        dev_src.append(("flock", j))
    for i in np.flatnonzero(us_o & ~paired_o):
        dev_lon.append(olon[i]); dev_lat.append(olat[i]); dev_src.append(("osm", i))
    dev_lon, dev_lat = np.array(dev_lon), np.array(dev_lat)
    sid, n_sites = site_ids(dev_lon, dev_lat, SITE_M)
    log(L, f"\n  {len(dev_src):,} devices in {n_sites:,} sites ({SITE_M:.0f} m single linkage); "
           f"largest site {int(np.bincount(sid).max()):,} devices")

    # coordinates shared by many registry records are a site address, not a device position
    stack = Counter((r["lat"], r["lon"]) for r in rows)

    # --- context per device ----------------------------------------------------------
    # An operator propagated within a site: the heads of one pole belong to one agency, and 8,000
    # registry devices stand within 25 m of a mapped node that names theirs.
    # Some mappers write the vendor into operator ("Flock Safety", 2,300 nodes): kept verbatim on
    # those devices as what OSM says, but not propagated, since it names no agency.
    site_ops = defaultdict(set)
    for n, (src, i) in enumerate(dev_src):
        oi = int(osm_of_flock[i]) if src == "flock" else i
        op = otags[oi].get("operator") if oi >= 0 else None
        if op and not vendor_family(op, None, {})[1]:
            site_ops[int(sid[n])].add(op)
    site_op = {s: next(iter(v)) for s, v in site_ops.items() if len(v) == 1}

    # The one police portal of the place a device stands in. Not its operator — sheriffs, state
    # police, businesses and HOAs run cameras inside city limits too — but where an operator is
    # known to check against, the place's portal is that operator 97% of the time.
    place_portal = defaultdict(list)
    for a in agencies:
        if a["agency_type"] == "PD" and a["city"] and a["state"]:
            place_portal[(squash(a["city"]), a["state"].upper())].append(a["slug"])
    place_portal = {k: v[0] for k, v in place_portal.items() if len(v) == 1}

    # The nearest counted road (HPMS, federal-aid system) and its traffic; and for a mapped node,
    # the OSM road road_sample.py already found it standing on.
    road_ix = road_d = pt = None
    if os.path.exists(args.hpms):
        H = np.load(args.hpms)
        road_ix, road_d = nearest_hpms(H, dev_lon, dev_lat)
        pt = {k: H[k] for k in ("pt_aadt", "pt_f", "pt_ft", "pt_urban")}
        log(L, f"  {int((road_ix >= 0).sum()):,} devices within {ROAD_M:.0f} m of a counted road")
    osm_road = {}
    if os.path.exists(args.roads):
        R = np.load(args.roads)
        cls = R["classes"]
        for cid, c, dd, w in zip(R["cam_id"], R["cam_near_cls"], R["cam_near_d"], R["cam_near_way"]):
            if c >= 0:
                osm_road[int(cid)] = (str(cls[c]), float(dd), int(w))

    # Probable duplicates on each side: registry records identical in coordinate, name and type,
    # and OSM nodes within DUP_M of each other pointing the same way with the same vendor. Flagged,
    # not merged: the later record points at the earlier, so a count minus the flags is deduplicated.
    def rkey(r):
        return (r["lat"], r["lon"], re.sub(r"\s+", " ", r["name"]).strip().lower(), r["type"])
    dup_flock = defaultdict(list)
    for j in np.flatnonzero(us_f):
        dup_flock[rkey(rows[j])].append(int(rows[j]["OBJECTID"]))
    dup_flock = {k: min(v) for k, v in dup_flock.items() if len(v) > 1}
    dev_of_node = {}
    for n, (src, i) in enumerate(dev_src):
        oi = int(osm_of_flock[i]) if src == "flock" else i
        if oi >= 0:
            dev_of_node.setdefault(oi, f"flock-{rows[i]['OBJECTID']}" if src == "flock" else f"osm-{int(oid[i])}")
    sig = [(squash(t.get("direction") or t.get("camera:direction") or ""), v) for t, v in zip(otags, ovendor)]
    dup_osm = near_duplicate_nodes(olon, olat, us_o, sig)

    def geo_of(gi):
        t = int(tract[gi])
        rec = {"state": None, "tract": None, "place": None, "place_geoid": None,
               "place_portal": None, "locality_hint": None, "basis": None}
        if t >= 0:
            rec.update(state=geo.ABBR[UT["state"][t]].upper(), tract=UT["geoid"][t])
            p = int(place[gi])
            if p >= 0:
                rec.update(place=UP["name"][p], place_geoid=UP["geoid"][p],
                           place_portal=place_portal.get((squash(UP["name"][p]), rec["state"])))
            elif int(pnear[gi]) >= 0:
                rec["locality_hint"] = f"{UP['name'][int(pnear[gi])]} ({pnd[gi]:.0f} m outside)"
        else:
            rec.update(state=str(terr[gi]), basis="territory_bounding_box")
        return rec

    def osm_payload(i):
        t = otags[i]
        d = odirs[i]
        nd = node_dates.get(str(int(oid[i]))) or {}
        return {
            "node_id": int(oid[i]),
            "lat": float(olat[i]), "lon": float(olon[i]),
            "heads": int(oheads[i]),
            "bearings": [round(b, 1) for b, _ in d] or None,
            "cone_spans_deg": [round(s, 1) for _, s in d] or None,
            "direction_raw": t.get("direction") or t.get("camera:direction"),
            "first_mapped": nd.get("first_alpr"),
            "created": nd.get("created"),
            "last_edit": nd.get("last_edit"),
            "versions": nd.get("versions"),
            "moved_m": nd.get("moved_m"),
            "first_seen_snapshot": first_seen.get(int(oid[i])),
            "tags": t,
        }

    BULK = "2024-03-26"      # the registry's bulk-import day: 175,000 records were "created" then

    def placed(rec):
        """The earliest date any source has the device, and which source. Not an installation
        date: OSM's first mapping follows installation, the registry's record creation can precede
        it. A planned registry device has not been placed at all."""
        cands = []
        o = rec.get("osm") or {}
        if o.get("first_mapped"):
            cands.append((o["first_mapped"][:10], "osm: first mapped as an ALPR"))
        t = o.get("tags") or {}
        for k, basis in (("start_date", "osm: start_date"), ("survey:date", "osm: surveyed"),
                         ("check_date", "osm: checked")):
            v = (t.get(k) or "").strip()
            if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", v):
                cands.append((v, basis))
        f = rec.get("flock") or {}
        if f.get("created") and rec.get("status") != "planned":
            day = f["created"][:10]
            cands.append((day, "registry: in the bulk import" if day == BULK else "registry: record created"))
        if not cands:
            return None, None
        norm = lambda v: v + {4: "-01-01", 7: "-01"}.get(len(v), "")
        return min(cands, key=lambda c: norm(c[0]))

    def prune(x):
        """Drop keys a source said nothing about, so an absent key reads as 'not known'."""
        if isinstance(x, dict):
            return {k: prune(v) for k, v in x.items() if v is not None and v != [] and v != {}}
        return x

    def lifted(t):
        """The OSM tags worth reading without descending into the raw tag dict.

        surveillance:zone is on 160,477 nodes and says what the camera is pointed at; the photo
        links are how a reader checks a device exists; and the free-text description and note are
        where mappers record retention and sharing policy they have had to FOIL for.
        """
        photos = [t[k] for k in ("image", "source:url") if t.get(k)]
        if t.get("mapillary"):
            photos.append("https://www.mapillary.com/map/im/" + t["mapillary"])
        if t.get("wikimedia_commons"):
            photos.append("https://commons.wikimedia.org/wiki/" + t["wikimedia_commons"].replace(" ", "_"))
        notes = [t[k] for k in ("description", "note") if t.get(k)]
        if t.get("highway") == "toll_gantry" or t.get("toll") or t.get("barrier") == "toll_booth" or t.get("fee"):
            role = "toll"
        elif t.get("enforcement"):
            role = "traffic_enforcement"
        elif t.get("unsigned_ref:US:eff"):
            role = "border"                          # EFF's atlas ids: CBP checkpoints, ports of entry
        else:
            role = None
        return {
            "zone": t.get("surveillance:zone"),
            "role": role,
            "enforcement": t.get("enforcement"),
            "model": t.get("model"),
            "operator_type": t.get("operator:type"),
            "operator_wikidata": t.get("operator:wikidata"),
            "owner": t.get("owner"),
            "power": t.get("electricity"),
            "installed": t.get("start_date"),
            "checked": t.get("check_date") or t.get("survey:date"),
            "ref": t.get("ref") or t.get("unsigned_ref:US:eff"),
            "website": t.get("website"),
            "photos": photos or None,
            "notes": " | ".join(notes) or None,
            "mapper_source": t.get("source"),
        }

    counts = Counter()
    by_state = defaultdict(Counter)
    vendors = Counter()
    kinds = Counter()
    flags_seen = Counter()
    bearing_n = 0
    unmatched_vendor = Counter()
    n_agency = n_op_inferred = n_road = n_place_portal = 0
    placed_basis, n_rw = Counter(), Counter()

    head = {
        "sources": {
            "osm": {"name": "OpenStreetMap ALPR nodes via Overpass",
                    "query": 'node[man_made=surveillance][surveillance:type~"^alpr$",i]',
                    "snapshot": osm_stamp, "licence": "ODbL 1.0",
                    "nodes_in_snapshot": len(nodes),
                    "url": "https://www.openstreetmap.org/",
                    "note": "the only source carrying the direction a camera looks"},
            "osm_history": {"name": "the same query against OSM attic data",
                            "snapshots": [{"date": d, "nodes": n} for d, n in hist_dates],
                            "note": "osm.first_seen_snapshot is the earliest of these in which the "
                                    "node already existed as an ALPR node; absent means it appeared "
                                    "after the last one. A camera re-mapped under a new node id dates "
                                    "from the new node"},
            "flock": {"name": "Flock Safety device registry",
                      "published_by": "flocksurveillance.org (Joshua Michael)",
                      "records_as_of": "2025-12", "records": len(rows),
                      "note": "every Flock device type, planned and decommissioned included; "
                              "no bearings, and rotationAngle is not one"},
            "portals": {"name": "Flock agency transparency portals",
                        "aggregated_by": "eyesonflock.com",
                        "retrieved": "2026-09-24", "agencies": len(agencies),
                        "note": "agency-level only: what each agency publishes it runs. " +
                                ("Each agency's sharing list is embedded." if args.with_sharing else
                                 "Sharing lists stay in the source file (--with-sharing embeds them)."),
                        "aggregate_cameras_estimate": portal_summary.get("estimated_total_cameras")},
            "geography": {"name": "TIGER/Line 2022 tracts and places",
                          "note": "state, county, tract and place for each device"},
            "hpms": {"name": "HPMS 2024 (USDOT/BTS NTAD), via hpms_sample.py",
                     "note": "road.aadt is the annual average daily traffic of the nearest counted "
                             f"road within {ROAD_M:.0f} m; HPMS counts the federal-aid system only, "
                             "so a device without one stands on a local street"},
            "osm_roads": {"name": "road_sample.py's nearest-way assignment for every mapped node",
                          "note": "road.osm_highway is the OSM class of the road the node stands on"},
            **({"osm_node_dates": {"name": "every version of every ever-ALPR node, from the OSM full-history dump",
                                   "dump_date": nd_meta.get("dump_date"),
                                   "note": "osm.first_mapped is the instant the node was first tagged as an "
                                           "ALPR; osm.created the node's own creation (a signal post a camera "
                                           "was later hung on keeps its older date); a node created after "
                                           "dump_date carries neither"}} if node_dates else {}),
            **({"roads_watched": {"name": "roads_watched.py over " + str(rw_meta.get("pbf")),
                                  "method": rw_meta.get("method"),
                                  "note": "roads_watched lists the drivable ways a device's cone (per surveyed "
                                          "head) or, with no bearing, its circle touches; no_road_within_reach "
                                          "flags a device that touches none"}} if roads_watched else {}),
        },
        "method": {
            "one_device_per": "one device per registry record, plus one per OSM node that paired "
                              "with no registry record",
            "pairing": f"one-to-one greedy within {MATCH_M:.0f} m on cost = distance + "
                       f"{OFF_SERVICE_PENALTY:.0f} m when the registry record is not in service; "
                       f"a Flock-or-unrecorded OSM vendor may pair only with a plate-reading "
                       f"registry record, and a multi-head node may claim one record per head",
            "position": f"a paired device is published at its OSM position when the node stands for "
                        f"it alone and the sources agree to within {POSITION_M:.0f} m - the node was "
                        f"placed against aerial imagery and is the apex its bearing belongs to. "
                        f"Otherwise, and for every unpaired registry device, the registry coordinate "
                        f"stands. position_source says which; the registry coordinate is always kept "
                        f"at flock.lat/lon and the gap at source_separation_m",
            "sites": f"single linkage at {SITE_M:.0f} m over the published positions",
            "ids": "flock-<OBJECTID> or osm-<node id>, stable across rebuilds",
            "rotation_angle": "carried verbatim, never used as a bearing: against surveyed OSM "
                              "bearings its median absolute disagreement is 90 degrees, which is "
                              "what independent angles would give",
            "absent_keys": "a key is omitted where no source recorded it; absence means unknown, "
                           "not false",
        },
        "fields": {
            "class": "alpr, video, audio_detection, speaker, drone, drone_infrastructure, "
                     "mobile_platform, infrastructure, external_integration, unknown",
            "reads_plates": "true, false, or absent where neither the features list nor the device "
                            "type settles it",
            "status": "in_service, planned, decommissioned or unknown from the registry; absent for "
                      "an OSM-only device, which OSM gives no status",
            "bearings": "degrees from north, one per camera head, from OSM only. cone_spans_deg "
                        f"pairs with it; a bearing given without an arc is taken to span "
                        f"{DEFAULT_SPAN_DEG:.0f} degrees, and an omnidirectional camera is bearing 0 "
                        f"with a span of 360",
            "watches_travel_direction": "the traffic a registry name says the device watches - not "
                                        "the way the camera faces",
            "device_type": "Flock's own product type; see device_types below",
            "camera_type": "OSM's camera:type - fixed, panning, dome, mobile",
            "zone": "what OSM says the camera is pointed at: traffic, street, parking, entrance",
            "notes": "free text a mapper wrote, and where one has FOILed a retention or sharing "
                     "policy this is where it is written down",
            "photos": "a photograph or record of the device: an image URL, a Mapillary frame, a "
                      "Wikimedia Commons file, or the source the mapper worked from",
            "checked": "when a mapper last verified the device on the ground",
            "installed": "OSM start_date, where a mapper recorded when the camera went up",
            "role": "toll (a toll gantry or booth), traffic_enforcement (red-light or speed camera, "
                    "with the enforcement tag beside it), border (a CBP checkpoint or port of entry "
                    "carrying EFF's atlas reference in ref)",
            "owner": "OSM owner, where it differs from the operator",
            "operator_wikidata": "the operator's Wikidata id, a stable key across spellings",
            "operator_inferred": "an operator no source recorded for this device, taken from the one "
                                 "operator named on other devices at the same site; "
                                 "operator_inferred_basis says so",
            "website": "a document a mapper linked: usually the contract, policy or agenda item",
            "mapper_source": "OSM source: what the mapper worked from, often a contract PDF",
            "fleet_number": "the '#29' in a registry name - the number the agency itself gives the "
                            "camera, as its records and agendas refer to it",
            "road": "aadt, system, facility and setting from the nearest HPMS-counted road within "
                    f"{ROAD_M:.0f} m (distance_m); osm_highway, osm_way and osm_distance_m from the "
                    "OSM way a mapped node stands on. Absent aadt means no counted road nearby - a "
                    "local street",
            "duplicate_of": "the earlier record this one is probably a duplicate of; see flags",
            "earliest_record": "the earliest date any source has the device. No source records "
                               "installation: a mapped node's first_mapped follows it (the camera was there "
                               "to be seen), while a registry record's creation can precede it (records are "
                               "created when a device is provisioned, and half the registry is still "
                               "planned). earliest_record_basis names the source; 'registry: in the bulk "
                               "import' says only that the device was in Flock's system by 2024-03-26. Absent "
                               "for a planned device. For a bracket, read osm.first_mapped and flock.created "
                               "together",
            "roads_watched": "every drivable OSM way the device's cone (roads_watched_basis = cone, one "
                             "60 m sector per surveyed head) or, lacking a bearing, its circle (omni) "
                             "touches, with the way's class, name, ref, oneway, lanes and maxspeed. "
                             "The circle's radius is the cone's length, 60 m: where no source records "
                             "which way a camera looks, it is taken to see as far as a camera with a "
                             "bearing does, in every direction",
            "flags": "shared_coordinate (ten or more registry devices share this coordinate, so it "
                     "is a site address rather than a device position), flock_internal (Flock's own "
                     "factory and validation hardware, named MFR, NPI, QC, HW Validation or "
                     "production build, or typed factoryFixture), labelled_test (the name says "
                     "test: an agency's pilot as often as Flock's own), "
                     "flock_tagged_but_not_in_registry (mapped as Flock, with no registry record "
                     "within the pairing distance - mostly installed after December 2025), "
                     "shared_osm_node (see osm below), sources_far_apart (the two sources put the "
                     f"device more than {POSITION_M:.0f} m apart, so it stays at the registry "
                     "coordinate), osm_position_uncertain (the mapper left a fixme saying so), "
                     "name_says_retired (the registry name says DNU, removed, offline or the like, "
                     "whatever the status column says), name_says_temporary (temp, demo, pilot, "
                     "trial), possible_duplicate (a registry record identical in coordinate, name "
                     "and type to an earlier one), possible_duplicate_mapping (an OSM node within "
                     f"{DUP_M:.0f} m of an earlier node with the same bearing and vendor)",
            "site": "devices sharing it are within linkage distance of each other, as the heads of "
                    "one pole are",
            "sources": "which of the sources above the device's record was built from",
            "geo": "state, TIGER/Line 2022 tract and place; the county is the tract's first five "
                   "digits. locality_hint names the nearest place within 2 km for a device outside "
                   "every place boundary. place_portal is the one police transparency portal of the "
                   "place the device stands in - the likely operator of a municipal camera, not a "
                   "recorded one; sheriffs, state police, businesses and HOAs run cameras inside "
                   "city limits too",
            "osm": "the node as Overpass returned it, tags and all, at "
                   "https://www.openstreetmap.org/node/<node_id>. A node carrying several bearings "
                   "is one pole of several heads and may be cited by several devices, flagged "
                   "shared_osm_node on each: neither source says which head is which device, so "
                   "each cites the whole node. Dedupe on osm.node_id before building geometry",
            "device_types": {k: {"class": c, "note": nt,
                                 "reads_plates": p if p is not None else "from the record's own features"}
                             for k, (c, p, nt) in KIND.items()},
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    fh = open(args.out, "w")
    fh.write('{"schema":' + json.dumps(SCHEMA))
    fh.write(',"generated":' + json.dumps(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    fh.write(',"scope":"United States: the 50 states and DC by TIGER/Line 2022 tract, plus Puerto '
             'Rico and the US Virgin Islands by bounding box"')
    for k, v in head.items():
        fh.write("," + json.dumps(k) + ":" + json.dumps(v, separators=(",", ":"), default=str))
    fh.write(',"agencies":' + json.dumps(agencies, separators=(",", ":")))
    fh.write(',"devices":[')

    first_out = True
    for n, (src, i) in enumerate(dev_src):
        flags = []
        if src == "flock":
            r = rows[i]
            oi = int(osm_of_flock[i])
            gi = oi if at_osm[i] else n_o + i
            feats = [f for f in (r["features"] or "").split(",") if f]
            if stack[(r["lat"], r["lon"])] >= 10:
                flags.append("shared_coordinate")           # a site address, not a device position
            if re.search(r"\b(mfr|npi|hw validation|flockqc|qc\d|production build)\b", r["name"], re.I) \
                    or r["type"] == "factoryFixture":
                flags.append("flock_internal")              # Flock's own factory and validation units
            if re.search(r"\btest\b", r["name"], re.I):
                flags.append("labelled_test")               # an agency's pilot, or Flock's; the name says test
            if RETIRED_RE.search(r["name"]):
                flags.append("name_says_retired")
            if TEMPORARY_RE.search(r["name"]):
                flags.append("name_says_temporary")
            fleet = FLEET_RE.search(r["name"])
            rec = {
                "id": f"flock-{r['OBJECTID']}",
                "lat": float(dev_lat[n]), "lon": float(dev_lon[n]),
                "position_source": "osm" if at_osm[i] else "flock",
                "site": int(sid[n]),
                "class": fkind[i], "reads_plates": fplates[i],
                "vendor": "Flock Safety", "device_type": r["type"],
                "status": fstatus[i], "active": r["active"] == "1",
                "label": r["name"] or None,
                "fleet_number": int(fleet.group(1)) if fleet else None,
                "watches_travel_direction": travel_hint(r["name"]),
                "flock": {"objectid": int(r["OBJECTID"]), "created": r["CreationDate"] or None,
                          "lat": float(flat[i]), "lon": float(flon[i]),
                          "features": feats or None,
                          "rotation_angle": None if not r["rotationAngle"].strip() else float(r["rotationAngle"])},
                "sources": ["flock"],
            }
            if oi >= 0:
                op = osm_payload(oi)
                rec["osm"] = op
                rec["sources"] = ["flock", "osm"]
                rec["bearings"] = op["bearings"]
                rec["cone_spans_deg"] = op["cone_spans_deg"]
                rec["source_separation_m"] = round(float(dist_of_flock[i]), 1)
                rec["operator"] = otags[oi].get("operator")
                rec["camera_type"] = otags[oi].get("camera:type")
                rec["mount"] = otags[oi].get("camera:mount") or otags[oi].get("surveillance:mount")
                rec["vendor_raw"] = vendor_of(otags[oi])
                rec.update(lifted(otags[oi]))
                if ovendor[oi] and ovendor[oi] != "Flock Safety":
                    rec["vendor_disagreement"] = ovendor[oi]
                if node_uses[oi] > 1:
                    flags.append("shared_osm_node")
                if dist_of_flock[i] > POSITION_M:
                    flags.append("sources_far_apart")
                if re.search(r"approximate|location|position", otags[oi].get("fixme") or "", re.I):
                    flags.append("osm_position_uncertain")
                counts["paired"] += 1
            else:
                counts["flock_only"] += 1
            k = rkey(r)
            if k in dup_flock and int(r["OBJECTID"]) != dup_flock[k]:
                flags.append("possible_duplicate")
                rec["duplicate_of"] = f"flock-{dup_flock[k]}"
        else:
            t, op, ven = otags[i], osm_payload(i), ovendor[i]
            gi = i
            if not ovrec[i] and ven:
                unmatched_vendor[ven] += 1
            if ven == "Flock Safety":
                flags.append("flock_tagged_but_not_in_registry")
            rec = {
                "id": f"osm-{int(oid[i])}",
                "lat": float(olat[i]), "lon": float(olon[i]),
                "position_source": "osm",
                "site": int(sid[n]),
                "class": "alpr", "reads_plates": True,
                "vendor": ven, "vendor_raw": vendor_of(t),
                "camera_type": t.get("camera:type"),
                "mount": t.get("camera:mount") or t.get("surveillance:mount"),
                "label": t.get("name") or None,
                "bearings": op["bearings"], "cone_spans_deg": op["cone_spans_deg"],
                "operator": t.get("operator"),
                "osm": op,
                "sources": ["osm"],
                **lifted(t),
            }
            if re.search(r"approximate|location|position", t.get("fixme") or "", re.I):
                flags.append("osm_position_uncertain")
            counts["osm_only"] += 1

        g = geo_of(gi)
        rec["geo"] = g
        oi_ = int(osm_of_flock[i]) if src == "flock" else i
        if oi_ >= 0 and 0 <= dup_osm[oi_] < oi_:
            flags.append("possible_duplicate_mapping")
            rec["duplicate_of"] = dev_of_node.get(int(dup_osm[oi_]))
        if not rec.get("operator") and int(sid[n]) in site_op:
            rec["operator_inferred"] = site_op[int(sid[n])]
            rec["operator_inferred_basis"] = "same_site"
            n_op_inferred += 1
        road = {}
        if road_ix is not None and road_ix[n] >= 0:
            k = int(road_ix[n])
            road = {"aadt": int(pt["pt_aadt"][k]), "system": HPMS_SYSTEM.get(int(pt["pt_f"][k])),
                    "facility": HPMS_FACILITY.get(int(pt["pt_ft"][k])),
                    "setting": HPMS_URBAN.get(int(pt["pt_urban"][k])), "distance_m": round(float(road_d[n]), 1)}
        if oi_ >= 0 and int(oid[oi_]) in osm_road:
            c, dd, w = osm_road[int(oid[oi_])]
            road.update(osm_highway=c, osm_way=w, osm_distance_m=round(dd, 1))
        rec["road"] = road or None
        n_road += 1 if "aadt" in road else 0
        n_place_portal += 1 if g.get("place_portal") else 0
        pb, basis = placed(rec)
        if pb:
            rec["earliest_record"] = pb
            rec["earliest_record_basis"] = basis
            placed_basis[basis] += 1
        if roads_watched:
            rw = roads_watched.get(rec["id"])
            if rw:
                rec["roads_watched"] = rw
                rec["roads_watched_basis"] = "cone" if rec.get("bearings") else "omni"
                n_rw[rec["roads_watched_basis"]] += 1
            else:
                flags.append("no_road_within_reach")
                n_rw["none:" + ("cone" if rec.get("bearings") else "omni")] += 1
        rec["flags"] = flags
        for f in flags:
            flags_seen[f] += 1
        if rec.get("operator"):
            slug, how = match_agency(rec["operator"], g["state"], agix)
            if slug:
                rec["agency"] = slug
                rec["agency_match"] = how
                n_agency += 1
        counts["devices"] += 1
        kinds[rec["class"]] += 1
        bearing_n += 1 if rec.get("bearings") else 0
        vendors[rec["vendor"] or "(unrecorded)"] += 1
        if g["state"]:
            by_state[g["state"]][rec["class"]] += 1
            by_state[g["state"]]["all"] += 1
            if rec["reads_plates"] and rec.get("status") in (None, "in_service"):
                by_state[g["state"]]["live_alpr"] += 1
        if not first_out:
            fh.write(",")
        fh.write(json.dumps(prune(rec), separators=(",", ":"), default=str))
        first_out = False

    fh.write("]")
    live = sum(c["live_alpr"] for c in by_state.values())
    tally = {
        "devices": counts["devices"],
        "corroborated_by_both_sources": counts["paired"],
        "registry_only": counts["flock_only"],
        "osm_only": counts["osm_only"],
        "sites": int(n_sites),
        "plate_readers_in_service_or_status_unrecorded": live,
        "with_a_surveyed_bearing": bearing_n,
        "with_an_operator_inferred": n_op_inferred,
        "earliest_record_by_basis": dict(placed_basis.most_common()),
        "roads_watched": dict(n_rw.most_common()),
        "with_a_place_portal": n_place_portal,
        "on_a_counted_road": n_road,
        "agency_linked": n_agency,
        "by_class": dict(kinds.most_common()),
        "by_status": dict(Counter(
            (fstatus[i] if s == "flock" else "unrecorded") for s, i in dev_src).most_common()),
        "by_state": {s: dict(c) for s, c in sorted(by_state.items())},
        "by_vendor": dict(vendors.most_common()),
        "flags": dict(flags_seen.most_common()),
    }
    fh.write(',"counts":' + json.dumps(tally, separators=(",", ":"), default=str))
    fh.write("}")
    fh.close()
    size = os.path.getsize(args.out)

    if args.gzip:
        with open(args.out, "rb") as a, gzip.open(args.out + ".gz", "wb", compresslevel=6) as b:
            while chunk := a.read(1 << 20):
                b.write(chunk)

    # --- report ----------------------------------------------------------------------
    log(L, "\n" + "-" * 78)
    log(L, "THE MERGED MAP")
    log(L, "-" * 78)
    log(L, f"  {counts['devices']:,} devices in the United States, of which")
    log(L, f"    {counts['paired']:,} carry both sources (a registry device with a mapped node on it)")
    log(L, f"    {counts['flock_only']:,} only the registry")
    log(L, f"    {counts['osm_only']:,} only OpenStreetMap")
    log(L, f"  {live:,} read plates and are either in service or carry no status, OSM recording none "
           f"— the\n  population TRAMES routes around")
    log(L, f"  {bearing_n:,} carry a surveyed bearing, the only direction evidence any source gives")
    log(L, "\n  by class")
    for k, c in kinds.most_common():
        log(L, f"    {k:22s} {c:8,d}")
    log(L, "\n  by status")
    for k, c in Counter((fstatus[i] if s == "flock" else "unrecorded (OSM only)")
                        for s, i in dev_src).most_common():
        log(L, f"    {k:22s} {c:8,d}")
    log(L, "\n  by vendor (top 25 of %d distinct)" % len(vendors))
    for k, c in vendors.most_common(25):
        log(L, f"    {k:34s} {c:8,d}")
    if unmatched_vendor:
        log(L, f"\n  vendor strings passed through unrecognised: {len(unmatched_vendor)} distinct, "
               f"{sum(unmatched_vendor.values()):,} devices — " +
            ", ".join(f"{k} ({c})" for k, c in unmatched_vendor.most_common(8)))
    log(L, "\n  data-quality flags")
    for k, c in flags_seen.most_common():
        log(L, f"    {k:34s} {c:8,d}")
    log(L, f"\n  operator recorded (OSM): {sum(1 for s, i in dev_src if (osm_of_flock[i] if s == 'flock' else i) >= 0 and otags[osm_of_flock[i] if s == 'flock' else i].get('operator')):,} devices; "
           f"inferred from a same-site device: {n_op_inferred:,}")
    log(L, f"  agency linked from an operator name: {n_agency:,} devices; "
           f"the place's police portal known for {n_place_portal:,}")
    log(L, f"  nearest counted road (HPMS) within {ROAD_M:.0f} m: {n_road:,} devices")
    if placed_basis:
        log(L, "\n  earliest record of the device in any source, by basis (see fields.earliest_record)")
        for k, c in placed_basis.most_common():
            log(L, f"    {k:36s} {c:8,d}")
    if n_rw:
        log(L, f"\n  roads watched: with a cone {n_rw['cone']:,} touch a road, {n_rw['none:cone']:,} none; "
               f"with a circle {n_rw['omni']:,} touch a road, {n_rw['none:omni']:,} none")
    log(L, f"\n  by state (all devices / plate readers live or undated)")
    line = [f"{s} {c['all']:,}/{c['live_alpr']:,}" for s, c in
            sorted(by_state.items(), key=lambda kv: -kv[1]["all"])]
    for i in range(0, len(line), 6):
        log(L, "    " + "   ".join(f"{x:<18s}" for x in line[i:i + 6]))
    log(L, f"\n  first appearance on the OSM map, for devices carrying an OSM node")
    fs = Counter()
    after = f"after {hist_dates[-1][0]}" if hist_dates else "undated"
    for s, i in dev_src:
        oi = osm_of_flock[i] if s == "flock" else i
        if oi >= 0:
            fs[first_seen.get(int(oid[oi]), after)] += 1
    for k in sorted(fs, key=lambda x: (x == after, x)):
        log(L, f"    {k:22s} {fs[k]:8,d}")
    log(L, f"\nwrote {args.out} ({size / 1e6:.1f} MB" +
        (f", {os.path.getsize(args.out + '.gz') / 1e6:.1f} MB gzipped" if args.gzip else "") +
        f") in {time.time() - t0:.0f} s")
    open(args.report, "w").write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
