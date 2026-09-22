# TRAMES ALPR preprocessor

Turns OSM/DeFlock license-plate-reader data into GraphHopper custom-area geometry, so
routing can avoid the cameras that can actually see you.

```sh
../.venv/bin/python build_cones.py \
    --bbox 38.40,-75.80,39.85,-75.03 \
    -o ../graphhopper/custom_areas/alpr.geojson

../.venv/bin/python score_route.py \
    --from 39.7391,-75.5398 --to 38.9108,-75.4277 \
    --cones ../graphhopper/custom_areas/alpr.geojson
```

Then re-import the graph (custom areas are resolved at import time) and route with:

```json
{ "priority": [ { "if": "in_alpr", "multiply_by": "0.05" } ] }
```

## Why cones, and why one merged area

Cameras are **directional**. One watching northbound traffic says nothing about the
southbound carriageway, so treating each as a circle makes the router dodge roads
nobody is being read on. Each camera becomes a wedge pointing where it actually looks.

All wedges union into a **single** MultiPolygon with id `alpr`. GraphHopper resolves
custom areas into a spatial index at import, so a per-request custom model can then
reference `in_alpr` with **no geometry in the request at all** — which keeps the
avoidance strength a continuous per-request knob and needs no custom Java or forked
GraphHopper. One feature per camera would instead require an `in_<id>` clause per
camera, which is unusable at real density.

## Tag handling is driven by measurement, not the wiki

| Finding | Measured |
|---|---|
| Key is `direction`, **not** `camera:direction` | 3,864 vs 21 (Atlanta metro, 3,899 cameras) |
| Directional coverage | 99.6% |
| Default cone span 45° | 94 of 111 arc-ranges span exactly 45° |
| Flock identified by `manufacturer` | 90% populated; `operator` only 4.9% |

`direction` values come in four shapes, all handled:

| Value | Meaning | Cones |
|---|---|---|
| `137` | single bearing | 1 |
| `144-189` | explicit arc | 1 (span 45) |
| `338-23` | arc wrapping past 0° | 1 (span via modulo) |
| `320;190` | back-to-back pair | 2 |
| `0;72;144;216;288` | 5-head 360° unit | 5 |
| `199-269;325-35` | two arcs | 2 |

The `;` multi-value case matters for correctness, not tidiness: multi-head units are
exactly the ones covering **both** carriageways, so dropping them sends you past the
cameras that are hardest to evade. In Delaware they are 25 of 529 nodes — parsing them
raises 502 cones to 580.

## Continental build (2026-09-22) — the refresh

Same five regional queries, ~5 minutes. Against the July snapshot the paper was written
on, two months of DeFlock mapping added a fifth again:

| | 2026-07-24 | **2026-09-22** | change |
|---|---|---|---|
| ALPR nodes | 120,838 | **142,991** | +18.3% |
| Cameras with a usable bearing → cones | 119,132 → 131,781 | 140,043 → **157,084** | +19.2% cones |
| Multi-head units | 7,010 | 9,146 | |
| Directional coverage | 98.7% | 98.0% | |
| Flock Safety share (substring, any vendor key) | 82.5% | 80.0% | Axis tripled: 1,052 → 3,327 |
| Union | 114,172 parts | **135,210 parts** | |

Rebuild the study's cone-set variants (vendor split, radius sweep) from the same cached
tiles with `./build-variants.sh` — they go to `variants/`, never into `custom_areas/`,
because GraphHopper would bake every geojson it finds there into the graph.

### What the refresh taught — three changes to `build_cones.py`

- **The match is case-insensitive now** (`["surveillance:type"~"^alpr$",i]`): 19 readers
  were tagged `alpr`, and an exact match on `ALPR` silently dropped every one of them.
- **A stale mirror is a failed answer.** `overpass-api.de` timed out on the Mexico box;
  the fallback `overpass.kumi.systems` answered promptly from a database dated
  **2026-05-06**. That box overlaps the whole southern US below 33°N, and regions were
  merged last-wins, so ~13,600 cameras across Texas, Arizona and Florida reverted to
  four-month-old tags and ~400 nodes deleted since May came back as ghosts. Nothing
  failed; the only symptom was the oldest-tile timestamp that `build_cameras_json.py`
  prints. `MAX_AGE_HOURS` (48 h) now rejects such an answer and moves on, overlapping
  regions merge **newest-database-wins**, and a >24 h spread between cached tiles is
  flagged. Check the tile stamps after every fetch anyway.
- **Endpoint order is `overpass-api.de`, then DeFlock's own mirror, then kumi.**
  `overpass.deflock.org` measured seconds behind live and fast; kumi was the stale one
  and later stopped answering at all.

## Continental build (2026-07-24)

Five regional queries — continental US, Alaska, Hawaii, Canada, Mexico — in ~10 minutes:

| | |
|---|---|
| ALPR nodes | **120,838** |
| Cameras kept → cones | 119,132 → **131,781** |
| Multi-head units | **7,010** (contributing 12,649 extra cones) |
| Skipped | 1,589 no-direction, 117 unparseable |
| Union | 114,172 parts, valid |
| Output | 55 MB GeoJSON |

`unary_union` handled 131,781 cones without difficulty — no per-tile merging needed.

### Use regions, not tiles

An earlier version swept North America as a 4° grid of 450 tiles. **86% of those
requests hit empty ocean** and the run was on track for ~2.5 hours. Overpass answers
the entire continental US — 120,757 cameras, 44 MB — in a **single 113-second query**.
The selectivity of the query, not the area, is what governs cost here.

### Why not DeFlock's published data

[`FoggedLens/deflock-data`](https://github.com/FoggedLens/deflock-data) runs the
hourly pipeline behind `maps.deflock.org`, and its README states it "pulls raw camera
data (US + Canada) **from Overpass**" — the same upstream this script uses. Going
direct is fresher and has no CDN dependency. Concretely:

- `deflockhopper_maps/public/cameras-us.json.gz` was committed **2026-04-12**: 61,768
  cameras against 120,757 live, missing ~49% — including real Flock units tagged
  `surveillance:zone=traffic`. It is a stale bundled fallback, not the live dataset.
- The live endpoints are **Cloudflare-gated for non-browser clients** despite the
  README promising "no API key": `data.dontgetflocked.com/cameras.geojson.gz` → 403,
  `tiles.dontgetflocked.com/cameras-us-hourly.json` → challenge page. OVERWATCH hit
  the same wall on `cdn.deflock.me`.

Worth borrowing later: their [`tiles/cameras/layers.json`](https://github.com/FoggedLens/deflock-data)
has reference MapLibre layer definitions including direction-cone rendering config,
useful when TRAMES draws cones in-app.

## Verified results (Delaware, 529 cameras → 580 cones)

Wilmington → Dover, scored with `score_route.py`:

| `multiply_by` | Distance | Time | Cameras passed | Avoided | Extra time |
|---|---|---|---|---|---|
| baseline | 106.59 km | 77.2 min | **6** | — | — |
| 0.5 / 0.3 | 106.59 km | 77.2 min | 6 | 0 | 0.0 |
| 0.1 | 108.24 km | 80.1 min | 3 | 3 | +2.9 min |
| 0.05 | 109.22 km | 80.9 min | 1 | 5 | +3.7 min |
| 0.01 | 110.23 km | 82.6 min | **0** | **6** | +5.4 min |

**Zero cameras for +5.4 min on a 77-min drive**, at ~0.9 min per camera avoided.

This resolves the phase-1 concern that avoidance was a cliff rather than a gradient.
That was an artefact of testing against one large polygon; with many small cones the
router has real intermediate choices and the response is smooth.

## Continental validation (2026-07-24, 131,781 cones on the NA graph)

`validate_metros.py` against the live continental graph:

| Route | Cameras | Avoided | Extra time | min/camera |
|---|---|---|---|---|
| Atlanta metro (dense) | 8 → **0** | 100% | +2.6 min | 0.33 |
| Atlanta → Athens | 10 → **0** | 100% | +13.5 min | 1.35 |
| Nashville metro | 0 → 0 | — | 0.0 | — |
| Wilmington → Dover | 6 → **0** | 100% | +5.4 min | 0.89 |
| Chicago metro | 6 → **0** | 100% | +1.9 min | 0.32 |
| Phoenix metro | 1 → **0** | 100% | +0.4 min | 0.38 |

**Dense metros are the cheapest case, not the worst.** 0.26–0.33 min per camera in
Atlanta and Chicago against 1.35–2.34 on the long intercity route. Dense street grids
supply parallel alternatives; highway corridors do not. The expensive regime is
rural/intercity — the opposite of the pre-test assumption.

Delaware reproduced its sandbox numbers exactly (106.59 km/6 cameras → 110.23/0,
+5.4 min), which makes it a usable regression check on any future rebuild.

## Serving the cone graph needs ~40 GB heap

`-Xmx16g` is enough for the cone-less continental graph and **not** enough with cones.
The failure is nasty because it doesn't look like a memory problem: the server starts
cleanly, passes its health check, answers one or two routes, then returns **HTTP 500 on
every subsequent request** with `java.lang.OutOfMemoryError: Java heap space` buried in
the server log. It reads as a routing bug. Budget 14 GB graph (RAM_STORE) + the
114k-part custom-area index + per-request working set.

## Tuning note: the useful slider range is 0.3 → 0.01

Everything from 1.0 down to 0.3 is a dead zone — the penalty has to get fairly strong
before it outweighs a highway. A UI slider should map its travel onto **0.3 → 0.01**
(probably logarithmically), not linearly onto 0 → 1, or most of the control will do
nothing.

## Since validated, and still open

- Dense-metro behaviour: validated on the continental graph above and across 17,580
  commutes in the study (`research/`) — metros are the cheap case.
- `--radius`: swept over 30/45/60/90 m in the paper (section 7.3, `build-variants.sh`
  produces the sets). Exposure is mildly sensitive; the avoidance headline degrades
  above 60 m.
- Still open: no dedup against DeFlock's own feed. This reads OSM via Overpass only —
  now with DeFlock's Overpass mirror as a fallback endpoint, which is the same upstream.
