#!/usr/bin/env python3
"""Stamp `alpr=yes` onto the stretches of road an ALPR camera cone watches.

    python3 tag_ways.py --cones ../graphhopper/custom_areas/alpr.geojson \
                        --in region.osm.pbf --out region-alpr.osm.pbf

This is Phase 1 of the offline pipeline (server/offline/README.md). The camera cones are
the same 60 m / 45° sectors build_cones.py produces for the online GraphHopper graph;
here they are intersected against the road network so the offline OsmAnd map can carry
the avoidance in a way its router understands.

What is tagged is each stretch of road from one junction to the next that a cone touches:
the unit the study's GraphHopper penalised (an edge). OsmAnd's router weighs whole ways,
and one OSM way can run past many junctions, so a watched way is split where its watched
and unwatched stretches meet and only the watched pieces carry the tag. Maps before
v1.2.7 tagged the whole way instead (one cone tagged 2.9 km of the Dulles Toll Road);
`--whole-ways` still does that.

Junctions are found as GraphHopper's import finds them: a node another road also uses,
counting the roads it counted. It skipped footway, cycleway, path, steps and construction
(import.osm.ignored_highways in server/graphhopper/config/trames.yml), so a footpath
meeting a road is no junction here either.

Splitting keeps the map's routing intact:
  - the first piece keeps the way's id; the others get ids derived from the way and the
    piece's end nodes, so neighbouring states' maps, which both carry a way that crosses
    their border, give the same piece the same id. OsmAnd drops a road whose id another
    loaded map already supplied (RoutingContext.loadSubregionTile), so two different
    pieces must never share one.
  - a turn restriction names the piece that holds its via node, and no way is split at
    a via node it is restricted at. MapCreator stores a restriction as from-way -> to-way
    and applies it wherever the two meet (IndexRouteCreator.indexHighwayRestrictions).
    A restriction through a via way keeps every way it names whole.
  - every other relation lists the pieces, in order, where it listed the way.
  - no way is cut where it bends. OsmAnd charges a turn wherever one way ends and the
    next begins (GeneralRouter.calculateTurnTime: from 30° over 5 m), so a cut on a bend
    would add a turn the road does not have, even with avoidance off. The watched stretch
    runs on to the next junction instead: a little more road is tagged, never less.
  - closed ways, roundabouts and areas are tagged whole, as before.

Three passes over the extract: the first finds the ways a cone touches (needs node
locations); the second counts which of their nodes other roads use and reads the turn
restrictions; the third copies the file through, splitting and tagging. Downstream,
rendering_types.delta.md registers the tag so it survives the .obf build, and the
alpr_avoidance levels in routing.xml (applied by trames-patch-resources.sh; designed in
car_alpr.routing.xml.md) penalise it by berth.
"""

import argparse
import hashlib
import json
import math
import sys
from collections import Counter

import osmium
from shapely import STRtree
from shapely.geometry import LineString, shape

# The study's GraphHopper import skipped these highway values, so a node a road shares only
# with one of them was no junction there (import.osm.ignored_highways, config/trames.yml).
IGNORED_FOR_JUNCTIONS = frozenset({"footway", "construction", "cycleway", "path", "steps"})

# Ids for the extra pieces of a split way. Above every real OSM way id (about 1.5e9 in
# 2026) and below 2^34: OsmAnd stores a way's id shifted left by 6, and from bit 40 up
# those bits mean "split by MapCreator" or "from a relation" (ObfConstants).
PIECE_ID_BASE = 1 << 32
PIECE_ID_SPAN = (1 << 34) - PIECE_ID_BASE

# OsmAnd's lightest turn charge starts at 30° (GeneralRouter.calculateTurnTime); cut only
# where the way is straighter than this, with a margin for the two measuring it apart.
MAX_CUT_BEND_DEG = 25.0


def load_cones(path):
    """Return a flat list of shapely Polygons from an alpr.geojson (Feature/FC/geometry)."""
    data = json.load(open(path))
    if data.get("type") == "FeatureCollection":
        geoms = [shape(f["geometry"]) for f in data["features"]]
    elif data.get("type") == "Feature":
        geoms = [shape(data["geometry"])]
    else:  # a bare geometry
        geoms = [shape(data)]
    polys = []
    for g in geoms:
        if g.geom_type == "MultiPolygon":
            polys.extend(g.geoms)
        elif g.geom_type == "Polygon":
            polys.append(g)
    if not polys:
        sys.exit(f"no polygons in {path}")
    return polys


def bend_deg(coords, i):
    """How far a way turns at its node i, measured the way OsmAnd measures a turn: from
    the first point at least 5 m back along it to the first at least 5 m on
    (RouteDataObject.directionRoute)."""
    def reach(step):
        j, d = i, 0.0
        while 0 <= j + step < len(coords) and d < 5.0:
            d += length_m([coords[j], coords[j + step]])
            j += step
        return j
    j, k = reach(-1), reach(1)
    if j == i or k == i:
        return 0.0
    c = math.cos(math.radians(coords[i][1]))
    ax, ay = (coords[i][0] - coords[j][0]) * c, coords[i][1] - coords[j][1]
    bx, by = (coords[k][0] - coords[i][0]) * c, coords[k][1] - coords[i][1]
    if (ax == 0 and ay == 0) or (bx == 0 and by == 0):
        return 0.0
    return abs(math.degrees(math.atan2(ax * by - ay * bx, ax * bx + ay * by)))


def length_m(coords):
    """Haversine length of a lon/lat polyline, in metres."""
    total = 0.0
    for (lo1, la1), (lo2, la2) in zip(coords, coords[1:]):
        p1, p2 = math.radians(la1), math.radians(la2)
        a = (math.sin((p2 - p1) / 2) ** 2
             + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2)
        total += 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))
    return total


class Watched:
    """What the later passes need to know about a way a cone touches."""
    __slots__ = ("refs", "coords", "counted", "whole_only")

    def __init__(self, refs, coords, counted, whole_only):
        self.refs = refs            # node ids, in way order
        self.coords = coords        # (lon, lat) per node, or None if any node has no location
        self.counted = counted      # does GraphHopper count this way when finding junctions?
        self.whole_only = whole_only


class Identify(osmium.SimpleHandler):
    """Pass 1: find the highway ways whose geometry intersects a cone."""

    def __init__(self, polys):
        super().__init__()
        self.tree = STRtree(polys)
        self.polys = polys
        self.watched = {}
        self.roads = 0

    def way(self, w):
        hw = w.tags.get("highway")
        if hw is None:
            return
        self.roads += 1
        refs, coords, located = [], [], True
        for n in w.nodes:
            refs.append(n.ref)
            if n.location.valid():
                coords.append((n.location.lon, n.location.lat))
            else:
                located = False
        if len(coords) < 2:
            return
        line = LineString(coords)
        # STRtree returns candidate indices by bounding box; confirm with a real intersect.
        for idx in self.tree.query(line):
            if self.polys[idx].intersects(line):
                if "alpr" in w.tags:
                    return  # someone already tagged it; leave it as it is
                whole_only = (refs[0] == refs[-1] or w.tags.get("area") == "yes"
                              or w.tags.get("junction") in ("roundabout", "circular"))
                self.watched[w.id] = Watched(refs, coords if located else None,
                                             hw not in IGNORED_FOR_JUNCTIONS, whole_only)
                return


class Survey(osmium.SimpleHandler):
    """Pass 2: count the road ways using each node of a watched way, and find the turn
    restrictions that name a watched way."""

    def __init__(self, watched):
        super().__init__()
        self.watched = watched
        self.uses = dict.fromkeys((n for w in watched.values() for n in w.refs), 0)
        self.via_nodes = {}         # way id -> via nodes it must not be split at
        self.keep_whole = set()     # ways a via-way restriction needs whole

    def way(self, w):
        hw = w.tags.get("highway")
        if hw is None or hw in IGNORED_FOR_JUNCTIONS:
            return
        uses = self.uses
        for n in w.nodes:
            if n.ref in uses:
                uses[n.ref] += 1

    def relation(self, r):
        if not r.tags.get("type", "").startswith("restriction"):
            return
        members = [(m.type, m.ref, m.role) for m in r.members]
        if not any(t == "w" and ref in self.watched for t, ref, _ in members):
            return
        via_nodes = {ref for t, ref, role in members if role == "via" and t == "n"}
        via_way = any(role == "via" and t == "w" for t, _, role in members)
        for t, ref, role in members:
            if t != "w" or ref not in self.watched or role not in ("from", "to", "via"):
                continue
            if via_way or role == "via":
                self.keep_whole.add(ref)
            else:
                self.via_nodes.setdefault(ref, set()).update(via_nodes)


class Piece:
    __slots__ = ("id", "first", "last", "tagged")

    def __init__(self, first, last, tagged):
        self.id = None
        self.first = first          # index into the way's node list
        self.last = last
        self.tagged = tagged


def piece_id(way_id, first_node, last_node, used):
    """A stable id for a piece: the same piece gets the same id in every state's build."""
    h = hashlib.blake2b(f"{way_id}:{first_node}:{last_node}".encode(), digest_size=8).digest()
    off = int.from_bytes(h, "big") % PIECE_ID_SPAN
    while PIECE_ID_BASE + off in used:   # never two pieces with one id in a build
        off = (off + 1) % PIECE_ID_SPAN
    used.add(PIECE_ID_BASE + off)
    return PIECE_ID_BASE + off


def plan(watched, survey, polys):
    """Decide, per watched way, whether it is tagged whole or split, and into what."""
    tree = STRtree(polys)
    whole, split, used = set(), {}, set()
    stats = Counter()
    for wid in sorted(watched):
        w = watched[wid]
        old_m = length_m(w.coords) if w.coords else 0.0
        stats["whole_way_m"] += old_m
        if w.coords is None or w.whole_only or wid in survey.keep_whole:
            whole.add(wid)
            stats["tagged_m"] += old_m
            continue
        occurs = Counter(w.refs)
        protected = survey.via_nodes.get(wid, ())
        cuts = [0]
        for i in range(1, len(w.refs) - 1):
            n = w.refs[i]
            others = survey.uses[n] - (occurs[n] if w.counted else 0)
            if (others > 0 or occurs[n] > 1) and n not in protected:
                cuts.append(i)
        cuts.append(len(w.refs) - 1)
        stretches = []
        for a, b in zip(cuts, cuts[1:]):
            seg = LineString(w.coords[a:b + 1])
            stretches.append([a, b, any(polys[k].intersects(seg) for k in tree.query(seg))])
        # Where a watched stretch meets an unwatched one on a bend, tag the unwatched one
        # too, until every boundary left is a straight junction.
        moved = True
        while moved:
            moved = False
            for left, right in zip(stretches, stretches[1:]):
                if left[2] != right[2] and bend_deg(w.coords, left[1]) > MAX_CUT_BEND_DEG:
                    (right if left[2] else left)[2] = True
                    stats["bends_tagged_through"] += 1
                    moved = True
        pieces = []
        for a, b, hit in stretches:
            if pieces and pieces[-1].tagged == hit:
                pieces[-1].last = b      # same status as the stretch before: one piece
            else:
                pieces.append(Piece(a, b, hit))
        if len(pieces) == 1 or not any(p.tagged for p in pieces):
            whole.add(wid)               # every stretch is watched (or rounding hid the one)
            stats["tagged_m"] += old_m
            continue
        pieces[0].id = wid
        for p in pieces[1:]:
            p.id = piece_id(wid, w.refs[p.first], w.refs[p.last], used)
        split[wid] = pieces
        stats["split_ways"] += 1
        stats["new_ways"] += len(pieces) - 1
        stats["tagged_m"] += sum(length_m(w.coords[p.first:p.last + 1]) for p in pieces if p.tagged)
    return whole, split, stats


class Tagger(osmium.SimpleHandler):
    """Pass 3: copy everything, tagging whole ways and writing split ones as pieces."""

    def __init__(self, whole, split, writer):
        super().__init__()
        self.whole = whole
        self.split = split
        self.writer = writer
        self.refs = {}              # node ids of each split way, for the relations
        self.pending = []           # the new pieces: their ids sort after every real way
        self.flushed = False
        self.restrictions = 0
        self.relations = 0

    def node(self, n):
        self.writer.add_node(n)

    def way(self, w):
        if w.id in self.whole:
            tags = dict(w.tags)
            tags["alpr"] = "yes"
            self.writer.add_way(w.replace(tags=tags))
        elif w.id in self.split:
            refs = [n.ref for n in w.nodes]
            self.refs[w.id] = refs
            base = dict(w.tags)
            for p in self.split[w.id]:
                tags = dict(base)
                if p.tagged:
                    tags["alpr"] = "yes"
                nodes = refs[p.first:p.last + 1]
                if p.id == w.id:
                    self.writer.add_way(w.replace(nodes=nodes, tags=tags))
                else:
                    self.pending.append(osmium.osm.mutable.Way(
                        id=p.id, nodes=nodes, tags=tags, version=w.version,
                        changeset=w.changeset, timestamp=w.timestamp, uid=w.uid, user=w.user))
        else:
            self.writer.add_way(w)

    def flush(self):
        if not self.flushed:
            for way in sorted(self.pending, key=lambda x: x.id):
                self.writer.add_way(way)
            self.flushed = True

    def relation(self, r):
        self.flush()
        if not any(m.type == "w" and m.ref in self.split for m in r.members):
            self.writer.add_relation(r)
            return
        restriction = r.tags.get("type", "").startswith("restriction")
        via_nodes = {m.ref for m in r.members if m.type == "n" and m.role == "via"}
        members = []
        for m in r.members:
            if m.type != "w" or m.ref not in self.split:
                members.append((m.type, m.ref, m.role))
                continue
            pieces = self.split[m.ref]
            if restriction and m.role in ("from", "to") and via_nodes:
                refs = self.refs[m.ref]
                at_via = [p for p in pieces if via_nodes.intersection(refs[p.first:p.last + 1])]
                pieces = at_via[:1] or pieces
                self.restrictions += 1
            members.extend(("w", p.id, m.role) for p in pieces)
        self.relations += 1
        self.writer.add_relation(r.replace(members=members))


class WholeTagger(osmium.SimpleHandler):
    """--whole-ways: copy everything, adding alpr=yes to every watched way."""

    def __init__(self, watched, writer):
        super().__init__()
        self.watched = watched
        self.writer = writer

    def node(self, n):
        self.writer.add_node(n)

    def way(self, w):
        if w.id in self.watched:
            tags = dict(w.tags)
            tags["alpr"] = "yes"
            self.writer.add_way(w.replace(tags=tags))
        else:
            self.writer.add_way(w)

    def relation(self, r):
        self.writer.add_relation(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cones", required=True, help="alpr.geojson from build_cones.py")
    ap.add_argument("--in", dest="inp", required=True, help="input .osm / .osm.pbf")
    ap.add_argument("--out", required=True, help="output .osm / .osm.pbf")
    ap.add_argument("--whole-ways", action="store_true",
                    help="tag every watched way whole, as maps before v1.2.7 did")
    args = ap.parse_args()

    polys = load_cones(args.cones)
    print(f"loaded {len(polys)} cone polygons")

    ident = Identify(polys)
    ident.apply_file(args.inp, locations=True)
    print(f"scanned {ident.roads} road ways; {len(ident.watched)} are watched by a cone")

    writer = osmium.SimpleWriter(args.out, overwrite=True)
    if args.whole_ways:
        WholeTagger(ident.watched, writer).apply_file(args.inp)
        writer.close()
        print(f"wrote {args.out} with alpr=yes on {len(ident.watched)} whole ways")
        return

    survey = Survey(ident.watched)
    survey.apply_file(args.inp)
    whole, split, stats = plan(ident.watched, survey, polys)

    tagger = Tagger(whole, split, writer)
    tagger.apply_file(args.inp)          # no locations needed: nodes are copied as they are
    tagger.flush()
    writer.close()
    print(f"wrote {args.out}: {len(whole)} ways tagged whole, {stats['split_ways']} split into "
          f"{stats['split_ways'] + stats['new_ways']} pieces ({stats['new_ways']} new ways); "
          f"{tagger.relations} relations rewritten, {tagger.restrictions} restriction members moved; "
          f"{stats['bends_tagged_through']} stretches tagged through a bend")
    print(f"alpr=yes on {stats['tagged_m'] / 1000:.1f} km of road, against "
          f"{stats['whole_way_m'] / 1000:.1f} km if whole ways were tagged")


if __name__ == "__main__":
    main()
