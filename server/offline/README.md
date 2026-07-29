# TRAMES offline routing — ALPR-baked maps (Path A)

Offline navigation that still avoids licence-plate readers, with no server in the
loop. The online engine bakes camera cones into a GraphHopper graph; this does the
same thing for OsmAnd's **offline** router by baking an `alpr=yes` tag onto the road
ways a camera watches, inside custom `.obf` map files, and penalising that tag in a
routing profile.

This is the "correct" offline design (from the offline-routing discussion): it keeps
the **soft, continuous berth** — OFF / LIGHT / MODERATE / STRONG / MAXIMUM — rather than
the hard road-blocking of OsmAnd's impassable-roads feature, and it reuses the exact
same geometry and multipliers as the online engine.

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

Same numbers as `TramesEngine.BERTH_MULTIPLIERS`, and — since v1.1.4 — the same 60 m / 45°
cone geometry (`TramesGeometry` / `build_cones.py`). Online and offline would agree by
construction. The profile is drafted in `car_alpr.routing.xml.md`.

## The pipeline

```
alpr.geojson (cones, from ../alpr/build_cones.py)
        +
region .osm.pbf (Geofabrik)
        │
        ▼  tag_ways.py   ── snap cones to ways, write alpr=yes onto watched ways
region-alpr.osm.pbf
        │
        ▼  OsmAndMapCreator (+ ALPR-aware rendering_types so the tag survives)
region-alpr.obf            ── ships to the phone
        +
car_alpr routing profile   ── penalises alpr=yes by berth
        │
        ▼
offline route that avoids cameras, no server
```

## Phases

- [x] **0 · Feasibility.** Proven: routing.xml applies a soft, tunable penalty to a custom
      way tag via a parameter group. Berth maps 1:1 to the online multipliers. (This doc +
      `car_alpr.routing.xml.md`.)
- [x] **1 · Cone→way tagging** (`tag_ways.py`) — **done + validated end-to-end.** Reads
      `alpr.geojson` + an OSM extract, tags every road way whose geometry intersects a cone
      with `alpr=yes` (two-pass pyosmium; shapely STRtree for the intersect), emits a tagged
      extract. On the two-road spike map it tagged only the watched road, and feeding its
      output through the Phase-2 build gave the avoided route (1904.8 m) while the untagged
      control stayed on the short road (1112.1 m) — Phases 1+2 proven together. Needs
      `pyosmium` + `shapely` (now installed).
- [x] **2 · Tag preservation — PROVEN.** The make-or-break unknown is closed by a
      spike: registering `alpr` as a `<routing_type>` (plus a `<type>`) in
      `rendering_types.xml` puts it into the `.obf` routing section, and the offline
      router then avoids `alpr=yes` ways — a two-road test map rerouted 1112 m → 1905 m
      once the penalty applied, and stayed on the short road without it. Exact recipe and
      proof table in `rendering_types.delta.md`. It's config-only; no OsmAnd-tools code
      change needed.
- [ ] **3 · `.obf` build.** Run OsmAndMapCreator on the tagged extract with the
      ALPR-aware config. **Toolchain:** OsmAndMapCreator — *not present*, needs setup
      (Java 21 is available). Start with one metro (e.g. the DC/Delaware area the paper
      used), not the continent.
- [ ] **4 · Client.** A "TRAMES (offline)" driving profile: OsmAnd offline engine +
      `car_alpr` routing + the berth exposed as the same selector. Auto-fall back to it
      when the server is unreachable; keep the exposure count working against on-device
      camera data.
- [ ] **5 · Distribution.** Host ALPR-tagged `.obf` on astrophage; in-app download/import;
      a freshness/version stamp (maps go stale as cameras are added — same problem the
      graph re-import has, at bigger file sizes).

## Open questions / risks

- ~~**Tag preservation (Phase 2)** is the make-or-break unknown.~~ **Resolved** — two
  `rendering_types.xml` declarations (`<type>` + `<routing_type>`) register `alpr` cleanly
  and the offline router avoids it. See `rendering_types.delta.md`. The rest is engineering.
- **Map size + staleness.** OsmAnd regional maps are large; ALPR-tagged variants double the
  download story (users fetch our maps instead of standard ones), and they age as DeFlock
  adds cameras. A per-region rebuild cadence is needed.
- **Shipping the routing profile.** Whether `car_alpr` can be bundled in the app or must be
  a user-imported routing file — affects Phase 4 UX.
- **Build cost.** A continental `.obf` build is heavy; the paper's metro extracts are the
  right size to prototype and validate against known camera positions.

## Status

Phases **0, 1, and 2 done** — the offline pipeline is proven from cone geometry to an
avoided offline route on a spike map (`tag_ways.py` → OsmAndMapCreator with the two
`rendering_types` lines → OsmAnd offline router reroutes around `alpr=yes`). What remains is
**scale and delivery**, not feasibility:

- [x] **Phase 3 — done + validated on real Delaware.** `cones_from_cameras.py` built 505
  cone parts from 529 real cameras (local `cameras.json`, no Overpass); `tag_ways.py`
  tagged 1,183 of 138,180 roads in 15.6 s; OsmAndMapCreator built the `.obf` with the two
  `rendering_types` lines and the `alpr` tag is confirmed in its routing section. Routing
  proof: **7 of 12 routes across camera-dense Wilmington rerouted** on the tagged map vs
  the untagged control, detours up to **+758 m** — realistic, cheap avoidance, exactly the
  online behaviour. (Spike used a jar patched with the two lines; the US batch should carry
  them as a proper `rendering_types.xml` override.)
- **Phase 4** — client: a "TRAMES (offline)" driving profile (OsmAnd engine + the
  `car_alpr` berth params), auto-fallback when the server is unreachable, exposure count
  against on-device camera data.
- **Phase 5** — distribution: host the ALPR-tagged `.obf` on astrophage, in-app
  download/import, a freshness stamp.
