# rendering_types delta — registering `alpr` (Phase 2, PROVEN)

For the offline router to avoid ALPR-watched ways, `alpr=yes` has to survive the `.obf`
build into the **routing section**. That takes **two** declarations in OsmAnd's
`rendering_types.xml` (bundled in `OsmAnd-java` / OsmAndMapCreator). One alone is not
enough — that was the entire Phase-2 unknown, now closed by a spike.

## The two lines

```xml
<!-- 1. map type: makes alpr a recognised tag at all (near the toll <type> block ~L313) -->
<type tag="alpr" value="yes" minzoom="9" additional="true" poi="false"/>

<!-- 2. ROUTING type: puts alpr into the .obf routing section so routing.xml t="alpr"
        resolves. THIS is the line that was missing. (in the <routing_type> block, next
        to `<routing_type tag="toll" .../>` ~L7604) -->
<routing_type tag="alpr" mode="amend" base="true"/>
```

Both mirror exactly how `toll` is declared, which is why `toll` avoidance works offline
and `alpr` now does too.

## The proof (tiny-map spike)

Two roads between the same A and B — East (short, ~1112 m) and West (detour, ~1905 m) —
built into a `.obf` with OsmAndMapCreator, routed with `test-routing`:

| East Street tag | `alpr` routing_type registered? | penalty | route taken | distance |
|---|---|---|---|---|
| `alpr=yes` | **no** (map type only) | forced ×0.05 on `alpr` | East (ignored) | 1112 m |
| `alpr=yes` | **yes** | forced ×0.05 on `alpr` | **West (avoided!)** | **1905 m** |
| `toll=yes` (control) | n/a (built in) | forced ×0.05 on `toll` | West (avoided) | 1905 m |

The only change between rows 1 and 2 is the `<routing_type>` line. That is Phase 2.

## Gotchas the spike also surfaced

- **Routing tags come from `MapRenderingTypesEncoder.getRouteTags()`**, which reads the
  `<routing_type>` declarations — *not* a hardcoded list, and *not* from what the
  build-time `routing.xml` references. So the registration is config-only; no OsmAnd-tools
  code change is needed. Good news for maintainability.
- **`test-routing`'s single-route mode ignores `-routingXmlPath`** and uses
  `RoutingConfiguration.getDefault()` (the classpath `routing.xml`), and it passes **no
  routing parameters**. So to test, the penalty has to be in the *bundled* `routing.xml`
  and ungated (or use a real Java harness with an explicit params map). This is a test
  harness quirk, not a product constraint.

## Consequence for the pipeline

Phase 2 is no longer a risk. The real build just needs OsmAndMapCreator run with a
`rendering_types.xml` carrying these two lines, over an extract that `tag_ways.py`
(Phase 1) has stamped with `alpr=yes`. Everything downstream (the `car_alpr` berth
profile) is already drafted and shown here to work.
