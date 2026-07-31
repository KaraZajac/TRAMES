# TRAMES

*Latin: byways, side roads.*

Navigation that routes around automated licence-plate readers.

**Website · [trames.karazajac.io](https://trames.karazajac.io)** — what it is, how it works, and the APK.
**Download · [latest release](https://github.com/KaraZajac/TRAMES/releases/latest)** — debug-signed APK, arm64-v8a, Android 7.0+. Sideload; there is no Play Store build.

TRAMES answers an ordinary navigation request — an address, a route — with one
additional constraint: prefer paths that no ALPR camera can actually see. Cameras are
modelled as **directional wedges**, not circles, because a reader watching northbound
traffic says nothing about the southbound carriageway, and treating it as a circle makes
the router detour around roads nobody is being read on.

Since v1.2.0 it does this **offline by default**. Routes are computed on the device
against ALPR-tagged maps, so a start, a destination and a departure time never leave the
phone. Online routing still exists and still avoids cameras, but it is opt-in: sending a
server your itinerary in order to dodge cameras trades one movement record for another.

---

## What is here

| Path | What it is |
|---|---|
| `client/` | Android app — a fork of [OsmAnd](https://github.com/osmandapp/OsmAnd) (GPLv3): offline ALPR avoidance for car/bike/foot, an ALPR-avoiding online engine, a camera map layer, and an in-app map downloader |
| `server/alpr`, `server/graphhopper` | Online routing backend — stock GraphHopper 11 plus a preprocessor that turns OSM ALPR data into camera-cone geometry |
| `server/offline/` | The offline pipeline — tags camera-watched ways, builds `.obf` maps that carry the tag, and packs camera positions for the map layer |
| `research/` | The measurement study: pipeline, results, and the paper |

## The finding

We routed **17,580 real home–work commutes** — drawn from Census LEHD LODES with
probability proportional to the number of workers actually making each trip, across 15
states — twice each: once normally, once avoiding the fields of view of **120,838 mapped
ALPR installations**.

- **74.8%** of American commutes pass at least one licence-plate reader.
- **84.6%** can be routed to *zero* exposure, for a median of **1.50 minutes** — an
  overhead of 5.96%.
- Avoidance is **cheapest where cameras are densest**. Metropolitan grids offer a parallel
  street a block over; rural routes often have one road. The burden of refusal falls
  hardest on drivers with the fewest cameras to refuse.
- Demographic gradients that look robust nationally **do not survive ranking tracts within
  their own county**. Mapped camera density varies 3.9× per capita across the states
  studied, and we cannot separate "more cameras" from "more mappers".

Paper: [`research/paper/trames.pdf`](research/paper/trames.pdf) — build with
`tectonic -X compile research/paper/trames.tex`.

## How it works — offline

The offline path has to solve a different problem from the online one: OsmAnd's router
reads only what is inside the `.obf` map, so the cameras have to be *in the map*.

1. **Tag the roads.** `server/offline/tag_ways.py` intersects the same 60 m / 45° cones
   against an OSM extract and writes `alpr=yes` onto every way a camera watches.
2. **Keep the tag through the build.** A `.obf` drops unknown tags unless they are
   declared, so `rendering_types.xml` gains a `<routing_type tag="alpr">` — the one
   genuinely uncertain step, proven on a spike before the continent was built.
3. **Penalise it while routing.** `routing.xml` gets five mutually exclusive levels
   (`alpr_off` … `alpr_max`) in the `car`, `bicycle` and `pedestrian` profiles, sharing
   an `alpr_avoidance` group so OsmAnd renders them as one picker with no UI code.

The multipliers are the same numbers the online engine uses — `0.3 / 0.1 / 0.05 / 0.01`,
default *Strong* — so a level means the same thing whichever engine routes, by
construction rather than by coincidence.

Two things worth knowing:

- **Rule order is load-bearing.** `GeneralRouter` returns the *first* matching priority
  rule and stops; priorities do not accumulate. The ALPR rules are therefore first in
  each block. Anywhere later, a watched road that also carried `smoothness=bad` or
  `access=destination` would match that instead and silently escape avoidance.
- **`routing.xml` is not ours and is overwritten on every build** — it is synced from
  upstream OsmAnd-resources and gitignored at both ends. `trames-patch-resources.sh`
  keeps the delta in a tracked, idempotent script that the build applies and that fails
  loudly if upstream moves. Without it the app builds clean and quietly avoids nothing,
  which is the worst failure this project has: routing still works, it just stops doing
  the one thing the app is for.

Prebuilt maps for all 50 states + DC are hosted at
**[maps.blackflagintel.com](https://maps.blackflagintel.com)** and download in-app.

## How it works — online

GraphHopper resolves `custom_areas.directory` into a spatial index **at graph import
time**. Register 114,172 camera cones as one merged area named `alpr`, and a per-request
custom model can then reference it with no geometry in the request at all:

```json
{ "priority": [ { "if": "in_alpr", "multiply_by": 0.01 } ] }
```

That is the whole trick, and it is why this needs no forked routing engine. Avoidance
strength stays a continuous per-request knob, so the client can expose it as a slider.
The cost is that regenerating cones requires a full re-import.

Two consequences worth knowing before changing the config:

- **Contraction Hierarchies must stay disabled.** CH bakes edge weights into the prepared
  graph, which is incompatible with per-request avoidance strength.
- **`car_alpr` borrows `car`'s landmark preparation.** That halves preparation time and is
  valid *only* because avoidance multiplies priority strictly downward, so the borrowing
  profile's weights never exceed the preparation profile's on any edge.

## Getting started

Building the app needs only steps 0 and 1 — the graph is for running your own online
endpoint, which the app no longer requires.

```sh
# 0. third-party OsmAnd assets the client build reads (~70 MB)
./setup-resources.sh

# 1. build the app  (applies trames-patch-resources.sh first — see below)
cd client && ./trames-build.sh
```

Optional, to host the online endpoint yourself:

```sh
# camera geometry (Overpass -> cones)
cd server/alpr && ./fetch-na-cones.sh

# import the graph, then serve  (~82 min for North America)
cd ../graphhopper && ./rebuild-with-cones.sh
```

Optional, to build your own ALPR-tagged offline maps instead of using the hosted ones:

```sh
cd server/offline
python3 cones_from_cameras.py --cameras ../cameras/cameras.json -o .work/us-alpr.geojson
python3 build_maps.py --mapcreator /path/to/OsmAndMapCreator --states delaware
python3 build_camera_pack.py -o cameras-us.json.gz     # positions for the map layer
```

> A full-map state build is memory-hungry: California and Texas each need roughly a 48 GB
> JVM heap and a couple of hours. `--roads-only` is far cheaper if you only need routing
> and not map display. `build_maps.py` is resumable and skips states already built.

### About `resources/`

The client build copies map styles, routing profiles, POI types, icons, fonts and
voice prompts from `../../resources`, a checkout of
[OsmAnd-resources](https://github.com/osmandapp/OsmAnd-resources). It is **not** tracked
here: it is 576 MB of third-party assets we neither maintain nor usefully diff, and
vendoring it would quadruple the size of this repository.

`setup-resources.sh` fetches it instead. The build reads roughly a tenth of that tree, so
the script uses a blobless partial clone plus a sparse checkout and pulls only the paths
the Gradle files actually reference — **~70 MB** rather than 576 MB.

```sh
./setup-resources.sh              # what the app build needs   (~70 MB)
./setup-resources.sh --with-tests # plus upstream test fixtures
./setup-resources.sh --full       # the entire upstream repo   (~576 MB)
./setup-resources.sh --update     # refresh an existing checkout
```

> **Do not build without it.** Several of the consuming Gradle tasks are `Sync` tasks,
> which make the destination match the source. With the source missing they do not
> fail — they empty the destination, and the build then succeeds and produces an app with
> no rendering styles, no routing profiles and no fonts. `setup-resources.sh` verifies its
> own output for exactly this reason.

If a build ever fails on a missing asset, the path list in the script came from:
>
> ```sh
> grep -rn '\.\./\.\./resources/' --include='*.gradle' client/
> ```

The research pipeline is independent of the app:

```sh
cd research
./data/fetch.sh ga tx ca fl il ny pa oh nc az wa co tn mo va
../server/.venv/bin/python scripts/build_sample.py --states ga tx ... -o out/commutes.csv
./scripts/run-full.sh          # ~7 h, detached, resumable
../server/.venv/bin/python scripts/analyze.py --results out/results.csv
```

## What leaves the device

Under the default configuration, nothing about where you are. Routes are computed
on-device and the map draws cameras from a downloaded pack.

A camera query carries a bounding box around the current view — which is to say, the
user's location — so those requests **default to denied** and are permitted only while
the active profile uses an online routing engine. The gate lives inside the single
function that builds the request rather than at its call sites, so a later change cannot
reintroduce the leak by forgetting a check. Until v1.2.3 this was wrong: a fresh install
queried cameras before the user had opted into anything.

The remaining network calls carry no location and are all user-initiated: a static map
manifest, a map file by name, and the camera pack. Choosing online routing sends start
and destination coordinates to whichever endpoint is configured — that is what routing
is — which is why self-hosting is supported.

## Security note

**The routing server has no authentication and no rate limiting.** It binds to localhost
and must not be exposed directly to the internet. Any public deployment needs a reverse
proxy supplying both.

**Released APKs are signed with the public Android debug key.** Anyone can build an APK
signed with the identical key and Android will accept it as an update, so the signature
proves nothing about who built it. Verify the SHA-256 published on the release, or build
from source.

## Licensing and provenance

`client/` derives from OsmAnd and is **GPLv3**. Upstream history is not carried here: the
project does not track OsmAnd continuously, and the fork cost 1.17 GB to hold five commits
of TRAMES work. `client/` is the upstream tree at `99f04deacd` with those five commits
replayed; `LICENSE`, `AUTHORS.md` and all per-file copyright headers are preserved
verbatim. See [`client/TRAMES-NOTICE.md`](client/TRAMES-NOTICE.md).

To diff against or re-sync with upstream:

```sh
git remote add osmand https://github.com/osmandapp/OsmAnd.git
```

> **OsmAnd's artwork is CC-BY-NC-ND 4.0**, not GPLv3. It may not be modified in a
> derivative work — it has to be replaced. This is a live obligation for anyone
> rebranding the client, not a footnote.

Camera data comes from OpenStreetMap (ODbL) via the Overpass API, the same corpus
surfaced by [DeFlock](https://deflock.me). It is contributed by volunteers and is
**incomplete in ways that are not random** — the central caveat of the study.
