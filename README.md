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
phone: sending a server your itinerary in order to dodge cameras trades one movement record
for another. The hosted online server was **retired on 2026-08-29**. The app's online engine
remains for anyone who runs their own (`server/`); left pointed at the retired address, as it
is out of the box, it returns no route.

---

## What is here

| Path | What it is |
|---|---|
| `client/` | Android app — a fork of [OsmAnd](https://github.com/osmandapp/OsmAnd) (GPLv3): offline ALPR avoidance for car/bike/foot, an ALPR-avoiding online engine for a self-hosted server, a camera map layer, and an in-app map downloader |
| `server/alpr`, `server/graphhopper` | Online routing backend, to self-host (the public instance was retired) and the engine the study routes against — stock GraphHopper 11 plus a preprocessor that turns OSM ALPR data into camera-cone geometry |
| `server/offline/` | The offline pipeline — tags camera-watched ways, builds `.obf` maps that carry the tag, and packs camera positions for the map layer |
| `research/` | The measurement study: pipeline, results, and the paper |

## The finding

We routed **56,131 real home–work commutes** — drawn from Census LEHD LODES with
probability proportional to the number of workers actually making each trip, in all 50
states and the District of Columbia — twice each: once normally, once avoiding the fields
of view of every licence-plate reader we can place: the **142,257 mapped in OpenStreetMap**
in the 50 states and DC (snapshot of 2026-09-22), and **44,059 more in service that only
Flock Safety's own device registry records** (as of December 2025) — **186,316 in all**.
National figures weight each state by its commuters. Then we did it again on OpenStreetMap's
camera map as it stood on the first of every month since January 2024, rebuilt exactly from
its edit history; the registry carries no dates, so the trend is the map's alone.

- **78.6%** of American commutes pass at least one licence-plate reader on the way to work
  (only that leg is routed; cameras face one way, so the trip home passes others); the
  median commute passes three.
- **80.8%** can be routed to *zero* exposure: the fifth whose route passes none, and three
  in four of the rest. The avoiding route adds a median of **2.29 minutes** (an overhead of
  9.71%), or 3.92 minutes for commuters whose usual route passes a camera.
- **The vendor's own records make refusal two-fifths dearer than the volunteer map shows.**
  On the same commutes, the readers only the registry records add a fifth more camera
  encounters, cut the share who can reach zero by 3.5 points and add 0.67 minutes to the
  median detour (paired intervals in the paper). Scored by the travel direction each reader's
  label names instead of as a 60 m disc, the share passing a reader moves by under a point.
- **Where refusal stops being cheap: Georgia.** 93.9% of its commuters pass a reader, only
  37.5% can reach work past none, and avoiding costs a median 13.6 minutes — against 54.3%
  and 7.8 minutes on the volunteer map alone. Whether evasion is cheaper per camera where
  cameras are denser, as the map alone weakly suggested, the merged map does not bear out.
- **What the app avoids.** The app routes around the volunteer map only; the readers that
  only Flock's registry records are not in it. A route planned around the map alone is clean
  of every mapped camera for 84.2% of commuters, but for 35.0% of them it still passes a
  registry reader (44.1% of those whose usual route passes a mapped camera); 54.7% of
  commuters get a route clean of both.
- **The map filled in, and exposure with it.** From 1,112 mapped readers in January 2024 to
  142,257 in September 2026: about fifty a month until DeFlock was founded in October 2024,
  then 1,724 → 4,894 in a single month, then a median 14.1% a month from January 2025 — easing
  to 9.2% a month in 2026, though the number added each month doubled. The share of
  commuters passing one went from 2.2% to 75.3%, crossing a tenth in January 2025, a quarter
  in June, a half in December; against the number of mapped cameras the elasticity is 0.89
  across 35 maps, and the curve is still rising by about two points a month.
- **Refusing them grew dearer, per camera as well as in total.** On the first map of 2024 95%
  of exposed commuters could reach zero, for a median 0.8 minutes; on today's map 79% can, for
  3.1 minutes (75%, for 3.9 minutes, once the registry is added), and the price per camera
  evaded rose from 0.42 to 0.76 minutes. Routed every
  quarter, the share able to reach zero held near 96% through 2024 and near 92% through 2025,
  then fell on every map since October 2025; the extra time and the price per camera rose on
  every routed map since July 2024. Followed from July
  2025 to September 2026, the same 12,647 exposed commutes went from passing 3.2 cameras to
  9.9, and the price per camera on those very trips rose a quarter — partly because the
  detours were being mapped too.
- **Every date is a mapping date, not an installation date.** OpenStreetMap records when a
  camera entered the map (to the second, from the edit history); an installation date exists
  for 47 of the 142,257. Absolute figures are lower bounds that keep rising as the map fills in.

Paper: [`research/paper/trames.pdf`](research/paper/trames.pdf) — build with
`tectonic -X compile research/paper/trames.tex`.

## The merged device map

Every camera source the study uses, resolved to one device list:

```bash
research/scripts/build_supermap.py -o research/out/supermap/alpr_supermap.json \
    --report research/out/analysis_supermap.txt --gzip        # ~4 min cold, ~80 s cached; 523 MB (54 MB gzipped)
```

**389,308 devices in the United States** — 88,754 carried by both OpenStreetMap and Flock's own
device registry, 246,182 by the registry alone, 54,372 mapped but absent from it. 238,561 of them
are licence-plate readers, 140,160 with a surveyed bearing; 180,761 devices read plates and are
either in service or mapped by OpenStreetMap, which records no status. Each device carries where
it is, which sources say so and how far apart they put it, what it is (plate reader, video, audio
detector, speaker, drone, infrastructure), its status, its bearings where anyone surveyed them,
its operator and transparency portal where they are known, its tract and place, and the map
snapshot in which its node first appears. Around each device: the traffic on the road it
watches (HPMS AADT and functional class, for the 241,033 devices within 100 m of a counted
road), the OSM road class a mapped node stands on, the one police portal of the place it stands
in (106,013 devices; the likely operator of a municipal camera, not a recorded one), an operator
inferred from other devices on the same pole (7,281), the fleet number an agency gives it, and
flags for what a registry name says its status is, and for probable duplicates on either side.
Every device also lists the roads it watches: the drivable ways its cone crosses (the same
60 m, 45° sector the router uses, one per surveyed head), or, **where no source records which
way a camera looks, the ways within a 60 m circle of it** — the cone's length, in every
direction, so a camera of unknown bearing is taken to see as far as one with a bearing. A
first pass used 40 ft and left 13% of bearing-less plate readers touching no road, because
cameras stand set back from the carriageways they watch; at 60 m, 435 of 98,401 (0.4%) do.
Of the 5,579 bearing-less devices of any kind still touching no road, most are indoor and
campus video cameras that the registry places at one building's coordinate.
The study's merged map is drawn from it: the in-service plate readers it holds from the registry
alone, 44,059 in the 50 states and DC, each given a 60 m disc
([`cones_from_supermap.py`](research/scripts/cones_from_supermap.py)).
The registry's `rotationAngle` is *not* a bearing — its median
disagreement with a surveyed one is 90°, which is what unrelated angles would give — so it is
carried verbatim and never used as one. The report beside it counts everything, by state, class,
vendor and status; [`build_supermap.py`](research/scripts/build_supermap.py)'s docstring is the
specification.

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

This is the self-hosted path, and the engine the study routes against; the public instance
was retired on 2026-08-29 (see [`server/README.md`](server/README.md)).

GraphHopper resolves `custom_areas.directory` into a spatial index **at graph import
time**. Register the camera cones (157,084 on the 2026-09-22 map, merged into 135,210
polygons) as one area named `alpr`, and a per-request custom model can then reference it with
no geometry in the request at all:

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
./data/fetch.sh all            # LODES + tract attributes, 50 states and DC
../server/.venv/bin/python scripts/build_sample.py --states all --per-state 1200 \
    --frame out/sampling_frame.json --draws out/sample_draws.csv -o out/commutes.csv
./scripts/run-full.sh          # hours, detached, resumable
./scripts/run-history.sh       # the same commutes against earlier camera maps
                               # (built in server/alpr — see "Past-date maps" there)
./scripts/run-analyses.sh      # weighted analysis, vendor, cone radius, trend (the map alone)
./scripts/run-merged.sh        # the merged map: the map's wedges plus a 60 m disc for each
                               # registry reader it lacks, in a graph of its own; the
                               # commutes it exposes routed again (needs the device map above)
./scripts/run-merged-analyses.sh  # merged headline, paired comparison with the map alone,
                               # directed scoring, the figures no other report prints
                               # (paper_numbers.py), then the paper's figures and tables
```

## What leaves the device

Under the default configuration, nothing about where you are. Routes are computed
on-device and the map draws cameras from a downloaded pack.

A camera query carries a bounding box around the current view — which is to say, the
user's location — so those requests **default to denied** and are permitted only while
the active profile uses an online routing engine. They then go to the TRAMES camera service
(`routing.blackflagintel.com/cameras`) or, if it fails, a public Overpass instance, whichever
server the routing itself uses. The gate lives inside the single function that builds the
request rather than at its call sites, so a later change cannot reintroduce the leak by
forgetting a check. Until v1.2.3 this was wrong: a fresh install queried cameras before the
user had opted into anything.

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

The study also reads Flock Safety's device registry as it stood in December 2025, from the
map an independent researcher published at [flocksurveillance.org](https://flocksurveillance.org).
That data is not redistributed here: the device list and the registry's discs are built
locally and gitignored, and the app does not carry them.
