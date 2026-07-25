# TRAMES

*Latin: byways, side roads.*

Navigation that routes around automated licence-plate readers.

TRAMES answers an ordinary navigation request — an address, a route — with one
additional constraint: prefer paths that no ALPR camera can actually see. Cameras are
modelled as **directional wedges**, not circles, because a reader watching northbound
traffic says nothing about the southbound carriageway, and treating it as a circle makes
the router detour around roads nobody is being read on.

---

## What is here

| Path | What it is |
|---|---|
| `client/` | Android app — a fork of [OsmAnd](https://github.com/osmandapp/OsmAnd) (GPLv3) with an ALPR-avoiding online routing engine, a camera map layer, and a berth selector |
| `server/` | Routing backend — stock GraphHopper 11 plus a preprocessor that turns OSM ALPR data into camera-cone geometry |
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

## How it works

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

```sh
# 0. third-party OsmAnd assets the client build reads (~70 MB)
./setup-resources.sh

# 1. camera geometry (Overpass -> cones)
cd server/alpr && ./fetch-na-cones.sh

# 2. import the graph, then serve  (~82 min for North America)
cd ../graphhopper && ./rebuild-with-cones.sh

# 3. build the app
cd ../../client && ./trames-build.sh
```

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

## Security note

**The routing server has no authentication and no rate limiting.** It binds to localhost
and must not be exposed directly to the internet. Any public deployment needs a reverse
proxy supplying both.

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
