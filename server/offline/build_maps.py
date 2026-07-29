#!/usr/bin/env python3
"""Batch-build ALPR-tagged OsmAnd .obf maps for US states (or any Geofabrik regions).

    # one-time: download OsmAndMapCreator, unzip somewhere, then:
    python3 build_maps.py --mapcreator /path/to/OsmAndMapCreator \
        --cameras ../cameras/cameras.json --out ./maps --states all

For each region it downloads the Geofabrik extract, stamps `alpr=yes` onto every road a
camera cone watches (tag_ways.py), and builds a `.obf` whose routing section carries the
tag — so OsmAnd's offline router avoids cameras with the car_alpr berth profile. The cone
set is built once from the local camera snapshot (cones_from_cameras.py) and shared across
every state.

Resumable: a region whose output `.obf` already exists is skipped. Idempotent MapCreator
patch: the two rendering_types lines (see rendering_types.delta.md) are added to the
MapCreator jars only if absent.

Getting OsmAndMapCreator: https://download.osmand.net/latest-night-build/OsmAndMapCreator-main.zip
"""

import argparse
import glob
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
GEOFABRIK = "https://download.geofabrik.de/north-america/us/{slug}-latest.osm.pbf"

# 50 states + DC. Geofabrik region slugs.
US_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "district-of-columbia", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "new-hampshire",
    "new-jersey", "new-mexico", "new-york", "north-carolina", "north-dakota",
    "ohio", "oklahoma", "oregon", "pennsylvania", "rhode-island", "south-carolina",
    "south-dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west-virginia", "wisconsin", "wyoming",
]

# The two lines that make `alpr` a routing tag (rendering_types.delta.md).
TYPE_LINE = '\t<type tag="alpr" value="yes" minzoom="9" additional="true" poi="false"/>'
ROUTING_LINE = '\t\t<routing_type tag="alpr" mode="amend" base="true"/>'


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, check=True, **kw)


def ensure_cones(cameras, cones_path):
    if os.path.exists(cones_path):
        print(f"cones: {cones_path} exists, reusing")
        return
    print("cones: building us-alpr.geojson from the camera snapshot ...")
    sh(f'python3 "{HERE}/cones_from_cameras.py" --cameras "{cameras}" -o "{cones_path}"')


def patch_mapcreator(mc):
    """Add the alpr <type> + <routing_type> to rendering_types.xml in the MC jars, once."""
    jars = glob.glob(os.path.join(mc, "lib", "*.jar"))
    for jar in jars:
        listing = subprocess.run(["jar", "tf", jar], capture_output=True, text=True).stdout
        if "net/osmand/osm/rendering_types.xml" not in listing:
            continue
        # extract, check, patch
        tmp = os.path.join(HERE, ".mc_patch")
        os.makedirs(tmp, exist_ok=True)
        sh(f'cd "{tmp}" && jar xf "{jar}" net/osmand/osm/rendering_types.xml')
        rt = os.path.join(tmp, "net/osmand/osm/rendering_types.xml")
        s = open(rt).read()
        if 'tag="alpr"' in s:
            print(f"patch: {os.path.basename(jar)} already carries alpr")
            continue
        s = s.replace('<type tag="toll" value="snowmobile" minzoom="9" additional="true"/>',
                      '<type tag="toll" value="snowmobile" minzoom="9" additional="true"/>\n' + TYPE_LINE, 1)
        s = s.replace('<routing_type tag="toll" mode="amend" base="true"/>',
                      '<routing_type tag="toll" mode="amend" base="true"/>\n' + ROUTING_LINE, 1)
        open(rt, "w").write(s)
        sh(f'cd "{tmp}" && jar uf "{jar}" net/osmand/osm/rendering_types.xml')
        print(f"patch: added alpr to {os.path.basename(jar)}")


def build_state(slug, mc, cones, work, out, roads_only):
    obf_out = os.path.join(out, f"{slug}-alpr.obf")
    if os.path.exists(obf_out):
        print(f"[{slug}] output exists, skip")
        return "skip"
    t0 = time.time()
    pbf = os.path.join(work, f"{slug}.osm.pbf")
    if not os.path.exists(pbf):
        print(f"[{slug}] downloading extract ...")
        urllib.request.urlretrieve(GEOFABRIK.format(slug=slug), pbf)
    tagged = os.path.join(work, f"{slug}-alpr.osm.pbf")
    print(f"[{slug}] tagging ...")
    sh(f'python3 "{HERE}/tag_ways.py" --cones "{cones}" --in "{pbf}" --out "{tagged}"')
    print(f"[{slug}] building obf ...")
    cmd = "generate-roads" if roads_only else "generate-obf"
    env = dict(os.environ, JAVA_OPTS="-Xms1G -Xmx8G")
    build_start = time.time()
    sh(f'bash "{mc}/utilities.sh" {cmd} "{tagged}"', cwd=work, env=env)
    # the obf MapCreator just wrote (named after the input, casing varies) — pick by mtime
    produced = [f for f in glob.glob(os.path.join(work, "*.obf"))
                if os.path.getmtime(f) >= build_start - 1]
    if not produced:
        sys.exit(f"[{slug}] no obf produced")
    os.replace(max(produced, key=os.path.getmtime), obf_out)
    os.remove(tagged)
    print(f"[{slug}] done in {time.time()-t0:.0f}s -> {obf_out}")
    return "built"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mapcreator", required=True, help="unzipped OsmAndMapCreator dir")
    ap.add_argument("--cameras", default=os.path.join(HERE, "..", "cameras", "cameras.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "maps"))
    ap.add_argument("--work", default=os.path.join(HERE, ".work"))
    ap.add_argument("--states", default="all", help="'all' or comma-separated slugs")
    ap.add_argument("--roads-only", action="store_true", help="road-only obf (faster, no map/POI)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.work, exist_ok=True)
    cones = os.path.join(args.work, "us-alpr.geojson")

    ensure_cones(args.cameras, cones)
    patch_mapcreator(args.mapcreator)

    states = US_STATES if args.states == "all" else args.states.split(",")
    print(f"building {len(states)} regions -> {args.out}\n")
    tally = {"built": 0, "skip": 0}
    for slug in states:
        tally[build_state(slug, args.mapcreator, cones, args.work, args.out, args.roads_only)] += 1
    print(f"\ndone: {tally['built']} built, {tally['skip']} skipped, in {args.out}")


if __name__ == "__main__":
    main()
