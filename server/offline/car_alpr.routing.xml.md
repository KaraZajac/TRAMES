# `car_alpr` — offline ALPR-avoidance routing profile

The additions that turn OsmAnd's offline car routing into camera-avoiding routing.
These are a **delta against OsmAnd's `routing.xml`** (one big shared file), not a
standalone profile, so they're documented here and merged in at Phase 2.

Everything keys off one custom tag, `alpr=yes`, which `tag_ways.py` (Phase 1) writes
onto the road ways a camera cone covers, and which the `.obf` build must preserve
(Phase 2). Nothing here is exotic — it is a copy of how OsmAnd already avoids toll roads.

## 1. The berth selector — a parameter group

A `group` makes these mutually exclusive, so the user picks exactly one berth, the same
five levels as the online engine. Place among the other car `<parameter>` entries:

```xml
<parameter group="alpr_avoidance" id="alpr_off"
    name="Camera avoidance: Off — fastest route"
    description="Do not avoid licence-plate readers"/>
<parameter group="alpr_avoidance" id="alpr_light"
    name="Camera avoidance: Light"
    description="Avoid readers only where the detour is nearly free"/>
<parameter group="alpr_avoidance" id="alpr_moderate"
    name="Camera avoidance: Moderate"
    description="A balanced trade — dodge most cameras for a modest cost"/>
<parameter group="alpr_avoidance" id="alpr_strong"
    name="Camera avoidance: Strong"
    description="Avoid readers wherever a reasonable alternative exists (default)"/>
<parameter group="alpr_avoidance" id="alpr_max"
    name="Camera avoidance: Maximum"
    description="Avoid every reader it can find a way around"/>
```

## 2. The penalty — inside `<way attribute="priority">`

`priority` is a soft multiplier on the road's effective speed (a value of 0.1 makes the
road ~10× less attractive without blocking it), so this discourages watched roads exactly
the way the server's `{"if":"in_alpr","multiply_by":...}` does. Add alongside the existing
`<if param="avoid_toll">` block:

```xml
<!-- ALPR avoidance. `alpr=yes` is written onto watched ways by
     server/offline/tag_ways.py; the multipliers mirror TramesEngine.BERTH_MULTIPLIERS
     and the server's in_alpr, so online and offline routes agree by construction. -->
<if param="alpr_light">    <select value="0.3"  t="alpr" v="yes"/></if>
<if param="alpr_moderate"> <select value="0.1"  t="alpr" v="yes"/></if>
<if param="alpr_strong">   <select value="0.05" t="alpr" v="yes"/></if>
<if param="alpr_max">      <select value="0.01" t="alpr" v="yes"/></if>
<!-- alpr_off intentionally has no rule: priority stays 1, no avoidance. -->
```

## 3. Declaring the tag

`routing.xml` can only read tags the router knows about. `alpr` must be declared as a
routing tag (the same list that carries `toll`, `surface`, etc.) so `t="alpr"` resolves,
**and** registered in the map-build config (`rendering_types.xml`, routing category) so
the `.obf` build doesn't drop it. That registration is Phase 2 and is the one genuine
unknown — prove it on a tiny extract before building a full region.

## Parity check

| Berth | `TramesEngine` (online) | this profile (offline) |
|---|---|---|
| OFF | `null` | no rule |
| LIGHT | `0.3` | `0.3` |
| MODERATE | `0.1` | `0.1` |
| STRONG | `0.05` | `0.05` |
| MAXIMUM | `0.01` | `0.01` |

Cone geometry is shared too: `alpr=yes` is assigned using the same 60 m / 45° cones as
`build_cones.py` and `TramesGeometry` (v1.1.4). So an offline route and an online route
for the same trip and berth should avoid the same cameras — the only differences are the
underlying router (OsmAnd vs GraphHopper) and map freshness.
