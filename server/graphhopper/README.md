# TRAMES routing backend

GraphHopper 11.0. Phase 1 status: **working, proven against the stock OsmAnd request
shape.** No ALPR data yet — that's phase 2.

```sh
./import.sh data/delaware.osm.pbf   # build the graph
./serve.sh                          # serve on localhost:8989
```

Requires a JDK 17+ (JDK 25 verified working — note this is *not* the JDK the OsmAnd
Android build needs, which is pinned to 17/21 by AGP 8.7.3).

## Profiles

| Profile | Prep | Purpose |
|---|---|---|
| `car` | CH + LM | Baseline "fastest route". CH makes it fast. |
| `car_alpr` | LM only | Camera-avoidant. LM (hybrid) so the custom model stays tunable. |

`car_alpr` has no CH preparation, so requests **must** pass `ch.disable=true` or
GraphHopper returns `Cannot find CH preparation for the requested profile`. This is
intentional: CH bakes the weighting into the graph, which would make the avoidance
strength fixed at import time.

## Wiring up stock OsmAnd (no fork needed)

This is the phase-1 validation path — real turn-by-turn navigation over
camera-avoidant routes with zero Android code written.

1. OsmAnd → Settings → Navigation → **Online routing engines** → *Add*
2. Type: **Graphhopper**
3. Custom URL: `http://<host>:8989/route`
4. Vehicle: **Custom**, key: `&profile=car_alpr&ch.disable=true`

OsmAnd's `OnlineRoutingEngine.isCustomParameterizedValue()` detects the `&`-params
and appends that fragment to the URL verbatim, so both settings reach GraphHopper.

OsmAnd only consumes the route *geometry* — it re-derives every turn instruction,
street name and voice prompt from its own offline maps via "Attach to the roads".
So this backend only has to return a sane polyline.

## Verified phase-1 results (Delaware extract)

Wilmington → Dover:

| Profile | Distance | Time |
|---|---|---|
| `car` | 106.59 km | 77.2 min |
| `car_alpr` | 126.13 km | 128.2 min |

0 of 1084 route points fall inside the avoid polygon. Response shape matches
`GraphhopperEngine.parseServerResponse()`: `paths` root array, `points` as an encoded
polyline, and instructions carrying `distance`/`text`/`street_name`/`time`/`interval`/`sign`.

Hybrid-mode (LM) request latency on this extract: **38–135 ms** for a 106 km route.
Needs re-benchmarking at North America scale.

## Known finding: the avoidance "slider" is not smooth

Sweeping `multiply_by` against a single large avoid polygon:

| multiply_by | Distance | Time | Points in zone |
|---|---|---|---|
| 1.0 / 0.8 / 0.5 | 106.59 km | 77.2 min | 162 |
| 0.2 / 0.05 / 0.01 | 126.13 km | 128.2 min | 0 |

It's a cliff, not a gradient — the network offered only two real choices. With many
small camera cones instead of one blob the tradeoff space should be much smoother,
but **the berth slider's feel is an empirical question to settle in phase 2**, not a
given.

## Caveat: `car_alpr_demo.json` is a placeholder

`custom_models/car_alpr_demo.json` hardcodes a rectangle over the DE-1 corridor purely
to prove the mechanism. Phase 2 replaces it: cone-snap DeFlock/OSM ALPR points onto the
ways they actually watch (respecting `camera:direction`), write an `alpr_exposure`
encoded value at import via a custom TagParser, and reduce the custom model to a single
rule with no per-request geometry.

## North America import

Use `import-detached.sh`, not `import.sh`, for anything continental:

```sh
./import-detached.sh data/north-america.osm.pbf   # setsid; survives shell death
tail -f logs/import-run.log
./healthcheck.sh 600 && TRAMES_XMX=12g ./serve.sh
```

A multi-hour import started as a child of an ordinary shell dies when that shell's
session is reaped. The first North America attempt was lost exactly that way — killed
during subnetwork marking, ~28 minutes in, with **no OOM and no error in the log**,
which makes it look like a crash when it isn't. `import-detached.sh` uses `setsid` so
the JVM becomes its own session leader (verify with `ps -o pid,sid,pgid` — SID should
equal PID).

Measured timings on the 19.2 GB extract (16 cores, `-Xmx32g`):

| Stage | Time |
|---|---|
| pass1 | 6 min 33 s |
| pass2 | 20 min 17 s |
| Hilbert sort (103M edges + 82.9M nodes) | ~53 s |

Resulting graph: **82,932,093 nodes / 103,104,339 edges**, 14 GB graph cache.

| Stage | Time |
|---|---|
| pass1 | 6 min 35 s |
| pass2 | 20 min 19 s |
| Hilbert sort | ~53 s |
| Subnetwork marking | 1 min 53 s |
| LM preparation | 52 min 08 s |
| Flush | 10 s |
| **total** | **~1 h 22 min** |

LM peaked at **28.1 GB of the 32 GB heap**. CH preparation on top of that would very
likely have exceeded it — another reason `profiles_ch` stays empty for now.

## Continental latency benchmark

Hybrid mode (LM, `ch.disable=true`), served with `-Xmx16g`, median of 3 warm runs:

| Route | Distance | `car` | `car_alpr` |
|---|---|---|---|
| Atlanta urban | 12 km | 20 ms | 19 ms |
| Atlanta → Athens | 114 km | 31 ms | 30 ms |
| Atlanta → Nashville | 399 km | 24 ms | 25 ms |
| Atlanta → Chicago | 1,085 km | 46 ms | 46 ms |
| NYC → LA | 4,428 km | 161 ms | 145 ms |

**Verdict: the continuous berth slider is viable.** Hybrid mode holds well under 200 ms
even coast-to-coast, so there's no need to fall back to a handful of discrete
CH-prepared profiles. Per-request `custom_model` tuning stays on the table.

(`car` and `car_alpr` agree on distance here because the demo avoid-polygon is in
Delaware and these routes are not — as expected until phase 2 supplies real data.)

19.2 GB extract (Geofabrik, 2026-07-23). Keep `TRAMES_XMX` at roughly
`available RAM - 8g` — a JVM that swaps during landmark computation turns an
hours-long import into a days-long one.

`profiles_lm` uses `preparation_profile: car` so `car_alpr` borrows `car`'s landmarks
rather than computing its own. Verified on Delaware to produce byte-identical routes
(106.59 km / 126.13 km) while running one LM preparation instead of two. This is only
valid while every custom model multiplies priority *down*; see the config comment.

Note `car_alpr` on a North America graph behaves identically to `car` outside the
hardcoded Delaware test polygon — that's expected until phase 2 supplies real data.

## Not yet done

- Real ALPR data (`car_alpr_demo.json` is a placeholder rectangle)
- Caddy config / deployment to api.blackflagintel.com
- No auth or rate limiting — do not expose port 8989
