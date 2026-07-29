#!/usr/bin/env python3
"""Stamp `alpr=yes` onto every road way an ALPR camera cone watches.

    python3 tag_ways.py --cones ../graphhopper/custom_areas/alpr.geojson \
                        --in region.osm.pbf --out region-alpr.osm.pbf

This is Phase 1 of the offline pipeline (server/offline/README.md). The camera cones are
the same 60 m / 45° sectors build_cones.py produces for the online GraphHopper graph;
here they are intersected against the road network so the offline OsmAnd map can carry
the avoidance in a way its router understands. A way is tagged if its geometry crosses
any cone — the offline analogue of "this road segment is observed".

Two passes over the extract: the first builds way geometries (needs node locations) and
records which way ids a cone touches; the second copies the whole file through, adding
`alpr=yes` to those ways. Downstream, rendering_types.delta.md registers the tag so it
survives the .obf build, and car_alpr.routing.xml.md penalises it by berth.
"""

import argparse
import json
import sys

import osmium
from shapely import STRtree
from shapely.geometry import LineString, shape


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


class Identify(osmium.SimpleHandler):
    """Pass 1: collect ids of highway ways whose geometry intersects a cone."""

    def __init__(self, polys):
        super().__init__()
        self.tree = STRtree(polys)
        self.polys = polys
        self.watched = set()
        self.roads = 0

    def way(self, w):
        if "highway" not in w.tags:
            return
        self.roads += 1
        coords = [(n.location.lon, n.location.lat) for n in w.nodes if n.location.valid()]
        if len(coords) < 2:
            return
        line = LineString(coords)
        # STRtree returns candidate indices by bounding box; confirm with a real intersect.
        for idx in self.tree.query(line):
            if self.polys[idx].intersects(line):
                self.watched.add(w.id)
                return


class Tagger(osmium.SimpleHandler):
    """Pass 2: copy everything, adding alpr=yes to the watched ways."""

    def __init__(self, watched, writer):
        super().__init__()
        self.watched = watched
        self.writer = writer
        self.tagged = 0

    def node(self, n):
        self.writer.add_node(n)

    def way(self, w):
        if w.id in self.watched and "alpr" not in w.tags:
            tags = dict(w.tags)
            tags["alpr"] = "yes"
            self.writer.add_way(w.replace(tags=tags))
            self.tagged += 1
        else:
            self.writer.add_way(w)

    def relation(self, r):
        self.writer.add_relation(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cones", required=True, help="alpr.geojson from build_cones.py")
    ap.add_argument("--in", dest="inp", required=True, help="input .osm / .osm.pbf")
    ap.add_argument("--out", required=True, help="output .osm / .osm.pbf")
    args = ap.parse_args()

    polys = load_cones(args.cones)
    print(f"loaded {len(polys)} cone polygons")

    ident = Identify(polys)
    ident.apply_file(args.inp, locations=True)
    print(f"scanned {ident.roads} road ways; {len(ident.watched)} are watched by a cone")

    writer = osmium.SimpleWriter(args.out, overwrite=True)
    tagger = Tagger(ident.watched, writer)
    tagger.apply_file(args.inp)          # no locations needed for a straight copy
    writer.close()
    print(f"wrote {args.out} with alpr=yes on {tagger.tagged} ways")


if __name__ == "__main__":
    main()
