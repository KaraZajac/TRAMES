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

# Declare the toggle next to OsmAnd's own avoid_* switches. default="true" makes camera
# avoidance on out of the box — opt-out, not opt-in. The id must start with "avoid_" so
# RouteParametersFragment files it into the existing "Avoid..." group with no UI code.
PARAM_ANCHOR = ('\t\t<parameter id="avoid_toll" name="Avoid toll roads" '
                'description="Avoid toll roads" type="boolean"/>')
PARAM_NEW = ('\t\t<!-- TRAMES: avoid ALPR (licence-plate reader) cameras, on by default. -->\n'
             '\t\t<parameter id="avoid_alpr" name="Avoid ALPR cameras" '
             'description="Avoid roads watched by licence-plate readers" '
             'type="boolean" default="true"/>')

# The penalty itself, inside <way attribute="priority">. Anchored on the toll *priority*
# rule specifically — there is a second <if param="avoid_toll"> further down under
# <point attribute="obstacle"> for physical toll booths, which must NOT be touched.
RULE_ANCHOR = ('\t\t\t<if param="avoid_toll">\n'
               '\t\t\t\t<select value="0.1" t="toll" v="yes"/>\n'
               '\t\t\t</if>')
RULE_NEW = ('\t\t\t<!-- TRAMES: "alpr=yes" is stamped onto camera-watched ways by\n'
            '\t\t\t     server/offline/tag_ways.py and preserved in the .obf routing section.\n'
            '\t\t\t     0.05 mirrors TramesEngine.BERTH_MULTIPLIERS\' STRONG default, so an\n'
            '\t\t\t     offline route dodges the same cameras as an online one. A priority\n'
            '\t\t\t     multiplier discourages a road; it never blocks it. -->\n'
            '\t\t\t<if param="avoid_alpr">\n'
            '\t\t\t\t<select value="0.05" t="alpr" v="yes"/>\n'
            '\t\t\t</if>')

if "avoid_alpr" in src:
    print("  ok      routing.xml already carries the ALPR rule")
    sys.exit(0)
if check_only:
    print("  MISSING routing.xml has no ALPR rule — run without --check to apply")
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
print("  ok      routing.xml patched with the ALPR avoidance rule (avoid_alpr, 0.05)")
PY
