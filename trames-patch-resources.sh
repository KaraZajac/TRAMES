#!/usr/bin/env bash
# Apply the TRAMES deltas to the upstream OsmAnd resources checkout.
#
#   ./trames-patch-resources.sh          # apply (idempotent)
#   ./trames-patch-resources.sh --check  # report status only, change nothing
#
# Why this exists: the ALPR avoidance rules live in routing.xml, which we do NOT own.
# `resources/` is a checkout of osmandapp/OsmAnd-resources, and the client build Syncs
# `resources/routing/` into OsmAnd-java (see client/OsmAnd-java/build.gradle). Both the
# source and the destination are gitignored (client/.gitignore: routing*.xml), so an edit
# to either is untracked AND gets overwritten — by `setup-resources.sh --update` upstream,
# or by the Gradle Sync on the very next build. The rules would silently vanish and the app
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
import re, sys, xml.dom.minidom

path, check_only = sys.argv[1], sys.argv[2] == "1"
src = open(path, encoding="utf-8").read()

# Car, bicycle and pedestrian all get the same control: a camera is a camera whether you
# are driving, riding or walking past it, and a user who picks "Maximum" in one profile
# means the same thing in the others.
#
# public_transport is deliberately absent, and not from neglect — its routingProfile has
# no <way attribute="priority"> block at all, because transit routing picks scheduled
# lines rather than weighting roads. There is nothing to penalise: you cannot reroute a
# bus around a camera.
PROFILES = ("car", "bicycle", "pedestrian")

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
PARAMS = (
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
    'description="Avoid every reader it can find a way around" type="boolean"/>\n'
)

# Placed FIRST inside <way attribute="priority">, and that position is load-bearing.
# GeneralRouter's RouteAttributeContext.evaluate() returns the value of the FIRST rule
# that matches and stops — priorities do not accumulate. Anywhere further down, a watched
# road that also happens to carry smoothness=bad, tracktype=grade5 or access=destination
# would match one of those rules first and escape camera avoidance entirely, silently and
# only on some roads. Being first means "this way is watched" always wins while a level is
# selected, which is the whole product.
RULES = (
    '\t\t\t<!-- TRAMES: "alpr=yes" is stamped onto camera-watched ways by\n'
    '\t\t\t     server/offline/tag_ways.py and preserved in the .obf routing section.\n'
    '\t\t\t     FIRST in this block on purpose: evaluate() takes the first matching rule\n'
    '\t\t\t     and stops, so anywhere later a watched road carrying smoothness/access/\n'
    '\t\t\t     tracktype tags would match those instead and skip avoidance.\n'
    '\t\t\t     A priority multiplier discourages a road; it never blocks it, so a watched\n'
    '\t\t\t     road stays usable when it is the only way through.\n'
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
    '\t\t\t</if>\n'
)

if "alpr_avoidance" in src:
    have = sorted({m for m in PROFILES
                   if re.search(r'<routingProfile name="%s" baseProfile=.*?</routingProfile>' % m,
                                src, re.S) and "alpr_avoidance" in
                   re.search(r'<routingProfile name="%s" baseProfile=.*?</routingProfile>' % m,
                             src, re.S).group(0)})
    print(f"  ok      routing.xml already carries ALPR levels for: {', '.join(have)}")
    sys.exit(0)
if check_only:
    print("  MISSING routing.xml has no ALPR levels — run without --check to apply")
    sys.exit(1)

out = src
for prof in PROFILES:
    # The real profile block is the top-level `name=X baseProfile=X` one; there are also
    # nested <routingProfile> stubs earlier in the file used for other purposes.
    pat = re.compile(r'\n\t<routingProfile name="%s" baseProfile="%s".*?\n\t</routingProfile>'
                     % (prof, prof), re.S)
    m = pat.search(out)
    if not m:
        # Fail loudly: a silently unpatched profile ships an app that looks fine and
        # avoids nothing on that mode.
        print(f"  FAILED  no <routingProfile name=\"{prof}\" baseProfile=\"{prof}\"> — "
              f"upstream routing.xml changed; update this script", file=sys.stderr)
        sys.exit(1)
    blk = m.group(0)

    # Parameters: immediately before the profile's first real <parameter ...> line, which
    # keeps them grouped with the other user-facing switches for that mode.
    pm = re.search(r'\n(\t\t<parameter (?!.*<!--)[^\n]*/>)', blk)
    if not pm:
        print(f"  FAILED  {prof}: no <parameter> line to anchor on", file=sys.stderr)
        sys.exit(1)
    blk = blk[:pm.start(1)] + PARAMS + blk[pm.start(1):]

    # Rules: first thing inside <way attribute="priority">. See the RULES comment.
    om = re.search(r'\n(\t\t<way attribute="priority">\n)', blk)
    if not om:
        print(f"  FAILED  {prof}: no <way attribute=\"priority\"> block", file=sys.stderr)
        sys.exit(1)
    blk = blk[:om.end(1)] + RULES + blk[om.end(1):]

    out = out[:m.start()] + blk + out[m.end():]

open(path, "w", encoding="utf-8").write(out)
try:
    xml.dom.minidom.parse(path)
except Exception as e:
    print(f"  FAILED  patched routing.xml is not well-formed: {e}", file=sys.stderr)
    sys.exit(1)
print(f"  ok      routing.xml patched: alpr_avoidance levels in {', '.join(PROFILES)} "
      f"(off/light/moderate/strong/max, default strong)")
PY
