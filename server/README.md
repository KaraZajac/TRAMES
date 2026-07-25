# TRAMES server

Routing backend for [TRAMES](https://github.com/KaraZajac/TRAMES) — GraphHopper with
automated licence-plate reader (ALPR) camera cones baked into the routing graph, so
navigation can avoid the cameras that can actually see you.

```
alpr/         OSM/Overpass -> directional camera cones -> GeoJSON
graphhopper/  config, import and serve scripts
```

## Quick start

```sh
python3 -m venv .venv && .venv/bin/pip install shapely

# 1. build camera cones (five regional Overpass queries, ~10 min)
cd alpr && ./fetch-na-cones.sh

# 2. import the graph (North America: ~1h22m, needs ~32 GB heap)
cd ../graphhopper
curl -O https://download.geofabrik.de/north-america-latest.osm.pbf   # 19.2 GB
mv north-america-latest.osm.pbf data/north-america.osm.pbf
./import-detached.sh data/north-america.osm.pbf

# 3. serve (needs ~40 GB heap with cones — see below)
TRAMES_XMX=40g ./serve.sh
./healthcheck.sh
```

Then point the TRAMES app's routing engine at `http://<host>:8989/route`.

## How avoidance works

Cameras are **directional** — one watching northbound traffic says nothing about the
southbound carriageway — so each becomes a cone pointing where it actually looks.
Routing only detours when your path would cross a camera's field of view.

Every cone is unioned into **one** MultiPolygon named `alpr`, resolved into GraphHopper's
spatial index at import. A request then references it with no geometry attached:

```json
{ "priority": [ { "if": "in_alpr", "multiply_by": "0.05" } ] }
```

That keeps the request tiny regardless of camera count (~120k in North America) and
leaves avoidance strength a continuous per-request knob.

**Import-time custom areas being referenceable from per-request custom models is not
documented by GraphHopper.** It was verified experimentally; the whole design depends on
it.

## Measured results

Continental graph, 119,132 cameras → **131,781 cones**:

| Route | Cameras | Extra time | min/camera |
|---|---|---|---|
| Atlanta metro | 8 → **0** | +2.6 min | 0.33 |
| Chicago metro | 6 → **0** | +1.9 min | 0.32 |
| Phoenix metro | 1 → **0** | +0.4 min | 0.38 |
| Wilmington → Dover | 6 → **0** | +5.4 min | 0.89 |
| Atlanta → Athens | 10 → **0** | +13.5 min | 1.35 |

**Dense metros are the cheapest case, not the worst** — grids offer parallel
alternatives, highway corridors don't. The expensive regime is rural/intercity.

Hybrid-mode latency: 20–46 ms up to 1,085 km, 161 ms coast-to-coast.

## Public endpoint

`https://routing.blackflagintel.com/route` — **open, no API key**, path-restricted to
`/route`, `/info`, `/health`.

It is deliberately not token-gated. A token embedded in a public APK is extractable in
seconds, so it was never real security — it only added a setup step. The actual
protection is a systemd blast-radius cap on the routing service:

```ini
CPUQuota=600%     # 6 of 12 cores; Caddy and the other sites always have half the box
MemoryHigh=19G    # soft: kernel reclaims and throttles here
MemoryMax=20G     # hard: only if reclaim fails
```

That matters because this host also serves 13 websites. Without a ceiling, a routing
traffic spike doesn't cost money — it takes those sites down, and the first symptom is a
website failing rather than the router getting busy.

Verified under a 60-request concurrent load: 60/60 succeeded, `memory.events` recorded
**12,116 `high` reclaim events and zero `oom_kill`**, and all 13 sites stayed identical
to baseline. The soft limit absorbed the pressure; the hard ceiling never fired.

Worth adding if it ever gets popular: a Cloudflare rate-limit rule. Caddy has no
rate-limit module built in, and adding one means an `xcaddy` rebuild plus swapping the
binary on a box serving 13 production sites — not worth the risk when the cgroup cap
already contains the damage.

## Things that will bite you

- **Serving the cone graph needs ~40 GB heap.** At 16g the server starts, passes its
  health check, answers a route or two, then returns HTTP 500 on everything with
  `OutOfMemoryError` only in the log. It reads exactly like a routing bug.
- **CH is disabled** (`profiles_ch: []`). Per-request custom models require
  `ch.disable=true`, and CH would freeze avoidance strength at import time. LM (hybrid)
  carries the load.
- **Regenerating cones requires a full re-import.** Custom areas resolve at import;
  editing the GeoJSON alone changes nothing on a running server.
- **The useful `multiply_by` range is 0.3 → 0.01, not 0 → 1.** Above 0.3 the penalty
  can't outweigh a highway. The effective range also shifts with local camera density.
- **Use regional queries, not tiles.** Overpass answers the whole continental US in one
  ~113 s query. A 4° tile grid over North America spends 86% of its requests on empty
  ocean and takes hours to return the same data.

## Data

Camera positions come from OpenStreetMap surveillance tagging
(`man_made=surveillance` + `surveillance:type=ALPR`) via Overpass, largely mapped by the
[DeFlock](https://deflock.me) community. **ODbL** — attribution required, share-alike on
derived databases.

`alpr/build_cones.py` parses what OSM actually contains rather than what the wiki
recommends: the key is `direction` (3,864 uses) not `camera:direction` (21); values
include arc ranges (`338-23`, wrapping past 0°) and semicolon-separated multi-head units
(`0;72;144;216;288`) that a naive parser silently drops — 7,010 of them nationally.

## License

Original work, MIT. Camera and map data are ODbL © OpenStreetMap contributors.
