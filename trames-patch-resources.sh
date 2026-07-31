#!/usr/bin/env bash
# Apply the TRAMES deltas to the upstream OsmAnd resources checkout.
#
#   ./trames-patch-resources.sh          # apply (idempotent)
#   ./trames-patch-resources.sh --check  # report status only, change nothing
#
# Why this exists: the ALPR avoidance rule lives in routing.xml, which we do NOT own.
# `resources/` is a checkout of osmandapp/OsmAnd-resources, and the client build Syncs
# `resources/routing/` into OsmAnd-java (see client/OsmAnd-java/build.gradle). Both the
# source and the destination are gitignored (client/.gitignore: routing*.xml), so an edit
# to either is untracked AND gets overwritten — by `setup-resources.sh --update` upstream,
# or by the Gradle Sync on the very next build. The rule would silently vanish and the app
# would quietly stop avoiding cameras: the worst possible failure for this fork, because
# routing still succeeds, it just stops doing the one thing the app is for.
#
# So the delta is kept here, in a tracked script, and re-applied at build time
# (client/trames-build.sh calls this). Same approach as build_maps.py's patch_mapcreator(),
# which patches rendering_types.xml in the MapCreator jars for the same reason.
set -uo pipefail
cd "$(dirname "$0")"

RT="resources/routing/routing.xml"
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

[ -f "$RT" ] || {
  echo "  MISSING $RT — run ./setup-resources.sh first" >&2
  exit 1
}

python3 - "$RT" "$CHECK" <<'PY'
import sys, xml.dom.minidom

path, check_only = sys.argv[1], sys.argv[2] == "1"
src = open(path, encoding="utf-8").read()

# Five mutually exclusive levels rather than one on/off switch. A shared `group` is what
# makes them exclusive: OsmAnd collects same-group boolean parameters into a
# LocalRoutingParameterGroup and renders it as one row showing the current level, tapping
# through to a picker — the same treatment the built-in "Driving style" group gets. That
# is entirely OsmAnd's own machinery; the fork adds no UI code for it.
#
# The multipliers mirror TramesEngine.BERTH_MULTIPLIERS exactly, so a given level means
# the same thing online and offline. They are NOT evenly spaced: measured on the
# continental graph, everything from 1.0 down to ~0.3 leaves most routes unchanged, so
# the useful range is 0.3..0.01 and the levels are distributed across that instead of
# across 0..1, where most of the control would do nothing.
#
# alpr_strong is default="true": camera avoidance is on out of the box at the same
# strength the online engine defaults to. Opt-out, not opt-in.
PARAM_ANCHOR = ('\t\t<parameter id="avoid_toll" name="Avoid toll roads" '
                'description="Avoid toll roads" type="boolean"/>')
PARAM_NEW = (
    '\t\t<!-- TRAMES: ALPR (licence-plate reader) avoidance level. -->\n'
    '\t\t<parameter group="alpr_avoidance" id="alpr_off" name="Off" '
    'description="Do not avoid licence-plate readers" type="boolean"/>\n'
    '\t\t<parameter group="alpr_avoidance" id="alpr_light" name="Light" '
    'description="Avoid readers only where the detour is nearly free" type="boolean"/>\n'
    '\t\t<parameter group="alpr_avoidance" id="alpr_moderate" name="Moderate" '
    'description="A balanced trade between detour and exposure" type="boolean"/>\n'
    '\t\t<parameter group="alpr_avoidance" id="alpr_strong" name="Strong" '
    'description="Avoid readers wherever a reasonable alternative exists" '
    'type="boolean" default="true"/>\n'
    '\t\t<parameter group="alpr_avoidance" id="alpr_max" name="Maximum" '
    'description="Avoid every reader it can find a way around" type="boolean"/>')

# The penalties themselves, inside <way attribute="priority">. Anchored on the toll
# *priority* rule specifically — there is a second <if param="avoid_toll"> further down
# under <point attribute="obstacle"> for physical toll booths, which must NOT be touched.
RULE_ANCHOR = ('\t\t\t<if param="avoid_toll">\n'
               '\t\t\t\t<select value="0.1" t="toll" v="yes"/>\n'
               '\t\t\t</if>')
RULE_NEW = ('\t\t\t<!-- TRAMES: "alpr=yes" is stamped onto camera-watched ways by\n'
            '\t\t\t     server/offline/tag_ways.py and preserved in the .obf routing section.\n'
            '\t\t\t     A priority multiplier discourages a road; it never blocks it, so a\n'
            '\t\t\t     watched road stays usable when it is the only way through.\n'
            '\t\t\t     alpr_off deliberately has no rule: priority stays 1. -->\n'
            '\t\t\t<if param="alpr_light">\n'
            '\t\t\t\t<select value="0.3" t="alpr" v="yes"/>\n'
            '\t\t\t</if>\n'
            '\t\t\t<if param="alpr_moderate">\n'
            '\t\t\t\t<select value="0.1" t="alpr" v="yes"/>\n'
            '\t\t\t</if>\n'
            '\t\t\t<if param="alpr_strong">\n'
            '\t\t\t\t<select value="0.05" t="alpr" v="yes"/>\n'
            '\t\t\t</if>\n'
            '\t\t\t<if param="alpr_max">\n'
            '\t\t\t\t<select value="0.01" t="alpr" v="yes"/>\n'
            '\t\t\t</if>')

if "alpr_avoidance" in src:
    print("  ok      routing.xml already carries the ALPR avoidance levels")
    sys.exit(0)
if check_only:
    print("  MISSING routing.xml has no ALPR levels — run without --check to apply")
    sys.exit(1)

for name, anchor in (("parameter", PARAM_ANCHOR), ("priority rule", RULE_ANCHOR)):
    if src.count(anchor) != 1:
        # Upstream moved the ground under us. Fail loudly: a silently unpatched
        # routing.xml ships an app that looks fine and avoids nothing.
        print(f"  FAILED  {name} anchor found {src.count(anchor)}x (expected 1) — "
              f"upstream routing.xml changed; update the anchors in this script", file=sys.stderr)
        sys.exit(1)

src = src.replace(PARAM_ANCHOR, PARAM_ANCHOR + "\n" + PARAM_NEW, 1)
src = src.replace(RULE_ANCHOR, RULE_ANCHOR + "\n" + RULE_NEW, 1)

open(path, "w", encoding="utf-8").write(src)
try:
    xml.dom.minidom.parse(path)
except Exception as e:
    print(f"  FAILED  patched routing.xml is not well-formed: {e}", file=sys.stderr)
    sys.exit(1)
print("  ok      routing.xml patched: alpr_avoidance levels (off/light/moderate/strong/max, default strong)")
PY
