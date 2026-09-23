# TRAMES offline routing — ALPR-baked maps

Offline navigation that still avoids licence-plate readers, with no server in the loop.
**Shipped in v1.2.0 and the app's default since**: the online engine bakes camera cones
into a GraphHopper graph; this pipeline bakes the same knowledge into OsmAnd's offline
router by stamping `alpr=yes` onto the road ways a camera watches, building `.obf` maps
that carry the tag, and penalising it in `routing.xml`.

The design keeps the soft, continuous berth — OFF / LIGHT / MODERATE / STRONG / MAXIMUM —
rather than the hard road-blocking of OsmAnd's impassable-roads feature, and it reuses
the exact same geometry and multipliers as the online engine, so a level means the same
thing whichever engine routes.

## Why this works — the one thing that had to be true

OsmAnd's offline `routing.xml` can apply a soft priority penalty to a custom way tag,
gated by a user parameter. It already does this for toll roads:

```xml
<if param="avoid_toll">
    <select value="0.1" t="toll" v="yes"/>   <!-- priority ×0.1: discouraged, not blocked -->
</if>
```

`priority ×0.1` halves-and-then-some the road's effective speed — a discouragement, not a
wall. That is precisely the offline twin of the server's per-request
`{"if": "in_alpr", "multiply_by": "0.1"}`. So the berth maps one-to-one:

| Berth | Online (`in_alpr`) | Offline (`alpr=yes` priority) |
|---|---|---|
| OFF | (no custom model) | (no rule) |
| LIGHT | 0.3 | 0.3 |
| MODERATE | 0.1 | 0.1 |
| STRONG | 0.05 | 0.05 |
| MAXIMUM | 0.01 | 0.01 |

Same numbers as `TramesEngine.BERTH_MULTIPLIERS`, same 60 m / 45° cone geometry
(`TramesGeometry` / `build_cones.py`). Online and offline agree by construction rather
than by coincidence.

## The pipeline

```
cameras.json (the served snapshot, ../cameras/)
        │
        ▼  cones_from_cameras.py  ── same parse/geometry as the online build_cones.py
us-alpr.geojson (114,172 cone parts, ~34 s)
        +
region .osm.pbf (Geofabrik)
        │
        ▼  tag_ways.py   ── snap cones to ways, write alpr=yes onto watched ways
region-alpr.osm.pbf
        │
        ▼  OsmAndMapCreator (jars patched with the two rendering_types lines)
region-alpr.obf            ── ships to the phone
        +
alpr_avoidance levels in routing.xml (trames-patch-resources.sh)
        │
        ▼
offline route that avoids cameras, no server
```

`build_maps.py` drives all of it per state: download the Geofabrik extract, tag, build.
It patches the MapCreator jars idempotently (`patch_mapcreator()`), skips states whose
`.obf` already exists (resumable), and one state failing does not abort the run — a later
higher-heap pass picks up the stragglers.

```sh
# one-time: https://download.osmand.net/latest-night-build/OsmAndMapCreator-main.zip,
# unzipped into .work/OsmAndMapCreator (gitignored with the rest of the workspace)
./build-all-detached.sh                                   # all 51 states, detached, resumable
python3 build_camera_pack.py -o maps/cameras-us.json.gz   # positions for the map layer
./deploy.sh                                               # rsync to the host; catalogue rebuilds itself
```

`build-all-detached.sh` wraps `build_maps.py` with a 48 GB heap and JDK 21, and refuses
to start while the GraphHopper server or the commute experiment is up — the two do not
fit in a 62 GB box together, and a JVM that collides with them swaps rather than fails.

There is no manifest step: on the host, `trames-manifest.timer` runs
`/srv/maps/gen-manifest.sh` every five minutes and rewrites `manifest.json` from the
`*-alpr.obf` files present, taking each map's `date` from its mtime. So maps rsynced in
overnight appear in the app's catalogue by themselves. `deploy.sh` uses rsync for that
reason and two more: `-t` keeps the mtimes a plain copy would reset to the upload day,
and rsync's rename-on-complete means a half-uploaded map is never listed for download.

Needs `pyosmium` and `shapely` importable by plain `python3` (they are invoked outside
the server venv, which carries only `shapely`).

## The client half (all shipped)

- **The routing rules.** Five mutually exclusive levels (`alpr_off` … `alpr_max`) in the
  `car`, `bicycle` and `pedestrian` profiles, sharing an `alpr_avoidance` group so OsmAnd
  renders them as one picker with no fork UI code. Default **Strong** (0.05). Applied to
  `routing.xml` at build time by `trames-patch-resources.sh` — that file is upstream's
  and overwritten on every build; the script is the tracked home of the delta and fails
  loudly if upstream moves. Rule *placement* is load-bearing (first in each priority
  block); the script documents why.
- **The maps.** An in-app catalogue (`TramesMapsDialog` / `TramesMapDownloader`) reads
  `manifest.json` from the map host and installs a `.obf` straight into the maps
  directory with an immediate re-index — no restart, no file picker.
- **The camera pack.** `build_camera_pack.py` slims the served snapshot to position +
  direction (~1.1 MB gzipped for all 120k US cameras); it rides along with a map download
  so the offline map draws exactly the cameras the offline router is avoiding.
- **The default.** `TramesDefaults` points the car profile at OsmAnd's offline engine;
  the online TRAMES engine is seeded but not selected. Offline is the default because
  sending a server your itinerary to dodge cameras trades one movement record for another.

## Hosted maps

All 50 states + DC are published at **https://maps.blackflagintel.com** — 51 full maps
(rendering + POI + routing), ~22 GB, built 2026-09-22 → 23 from the 142,991-camera
snapshot on fresh Geofabrik extracts (the first set, 2026-07-29 → 31, was built from the
120,838-camera snapshot). `manifest.json` is `{version, maps: [{name, file, size, date},
…]}`; the `date` is the freshness stamp the in-app catalogue shows, and re-downloading a
map is how a user picks up newly mapped cameras.

What a full rebuild costs on a 62 GB / 16-core machine, measured on the September run:
Texas 3.5 h, California 3.6 h (with an hour lost to swapping — see below), New York
1.9 h, Florida 1.9 h, the other giants (NC, VA, PA, WA, OH, MI) 65–85 min each, mid-sized
states 25–50 min, small ones 4–20 min — about 26 hours of build time, done in 20 wall-clock
hours by running a second builder on the non-giant states. That second builder is safe
only under a memory watchdog: California's JVM reaches its full 48 GB, and a 10 GB
sibling beside it filled swap and stalled both for an hour before it was killed.

## Things that will bite you

- **Full-map state builds are memory-hungry.** California and Texas each need roughly a
  48 GB JVM heap (`TRAMES_JAVA_OPTS="-Xms1G -Xmx48G"`; the default is 8 GB) and three and
  a half hours. `--roads-only` is far cheaper if you only need routing and not map display.
  Never run a second builder while one of them is building: the JVM does not fail when
  memory runs out, it swaps, and everything on the box slows to a crawl.
- **Refreshing means rebuilding.** `build_maps.py` skips any state whose output `.obf`
  exists, so a camera-data refresh needs the old outputs moved aside first, then the new
  set published with `deploy.sh` (the host's manifest timer does the rest). There is no
  automated build cadence yet; maps age as DeFlock contributors add cameras.
- **Avoidance only works on these maps.** Stock OsmAnd maps have no `alpr` tag, so on
  them the levels select fine and change nothing. The app steers users to this catalogue,
  but a region covered only by a stock map silently gets no offline avoidance there.
- **The two `rendering_types.xml` lines are non-negotiable.** Without the
  `<routing_type>` declaration the tag survives into the map section but not the routing
  section, and the router ignores it — see `rendering_types.delta.md` for the proof.

## How it was proven

The plan ran as phases (this file used to be the phase tracker; "Path A" in old commit
messages is this design). What each spike established, kept for the record:

- **Tag preservation was the make-or-break unknown, closed by a two-road spike**: with
  the `<type>` line alone the router ignored `alpr=yes` (took the short 1,112 m road);
  adding the `<routing_type>` line rerouted it onto the 1,905 m detour, matching a
  `toll=yes` control. Config-only — no OsmAnd-tools code change. Recipe and proof table:
  `rendering_types.delta.md`.
- **End-to-end on real Delaware**: 529 cameras → 505 cone parts; `tag_ways.py` tagged
  1,183 of 138,180 road ways in 15.6 s; on the built `.obf`, **7 of 12 routes across
  camera-dense Wilmington rerouted** versus the untagged control, detours up to +758 m —
  realistic, cheap avoidance, matching the online engine's behaviour.
- **Batch tooling validated small, then run in full**: DC 63 s, Rhode Island 158 s,
  Delaware 99 s (road-only) proved `build_maps.py`; the full 51-region full-map run
  produced the hosted set above.
