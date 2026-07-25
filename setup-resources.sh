#!/usr/bin/env bash
# Fetch the OsmAnd-resources tree that the client build reads from ../../resources.
#
#   ./setup-resources.sh              # what the app build needs   (~50 MB)
#   ./setup-resources.sh --with-tests # plus upstream test fixtures (~135 MB)
#   ./setup-resources.sh --full       # the entire upstream repo    (~576 MB)
#   ./setup-resources.sh --update     # refresh an existing checkout
#
# Why this exists: resources/ is 576 MB of third-party assets from
# osmandapp/OsmAnd-resources. Vendoring it would quadruple the size of this
# repository to carry files we do not maintain and cannot usefully diff.
#
# Why it is not a plain clone: the client build only reads about a tenth of that
# tree. A blobless partial clone (--filter=blob:none) plus a sparse checkout
# downloads just the paths listed in NEED below, so the default run costs ~50 MB
# instead of 576 MB. Pass --full if you want the lot.
#
# The paths in NEED are not a guess. They are every `../../resources/...`
# reference in the client's Gradle files:
#     grep -rn '\.\./\.\./resources/' --include='*.gradle' client/
# If a build starts failing on a missing asset, re-run that grep and add what it
# turns up here.
#
# A note on why a missing resources/ is worse than it looks: several of the
# consuming tasks are Gradle `Sync` tasks, which make the destination match the
# source. With the source absent they do not fail — they empty the destination.
# The build then succeeds and produces an app with no rendering styles, no
# routing profiles and no fonts. That is why this script verifies afterwards
# rather than trusting a zero exit code.
set -uo pipefail
cd "$(dirname "$0")"

REPO="https://github.com/osmandapp/OsmAnd-resources.git"
DEST="resources"
BRANCH="master"

# Every subtree the client's Gradle files copy from. Non-cone sparse patterns:
# a trailing slash takes a directory, otherwise it is a file glob.
NEED=(
  "/routing/*.xml"                                   # routing profiles
  "/obf_creation/rendering_types.xml"
  "/poi/"                                            # poi_types.xml, activities.json
  "/rendering_styles/*.xml"                          # map styles
  "/rendering_styles/fonts/"                         # map fonts (.ttf)
  "/rendering_styles/style-icons/map-shaders-vector/"
  "/rendering_styles/style-icons/map-icons-vector/"
  "/rendering_styles/style-icons/poi-icons-vector/"
  "/voice/**/*.js"                                   # voice prompt scripts
  "/bundled_assets.json"
  "/proj/"
  "/color-palette/"
  "/models/"
  "/fonts/"                                          # OsmAnd-telegram
  "/fonts-telegram/"
)
TESTS=( "/test-resources/" )

# Files that must exist afterwards, or the build will silently produce a broken app.
REQUIRED=(
  "routing/routing.xml"
  "poi/poi_types.xml"
  "obf_creation/rendering_types.xml"
  "rendering_styles/default.render.xml"
)

MODE="app"
for a in "$@"; do
  case "$a" in
    --full)       MODE="full" ;;
    --with-tests) MODE="tests" ;;
    --update)     MODE="${MODE}" ; UPDATE=1 ;;
    -h|--help)    sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }

apply_sparse() {
  local patterns=( "${NEED[@]}" )
  [ "$MODE" = "tests" ] && patterns+=( "${TESTS[@]}" )
  if [ "$MODE" = "full" ]; then
    git -C "$DEST" sparse-checkout disable 2>/dev/null
  else
    # --no-cone: cone mode only understands whole directories, and several of
    # these are file globs inside a much larger directory.
    git -C "$DEST" sparse-checkout set --no-cone "${patterns[@]}"
  fi
}

if [ -d "$DEST/.git" ]; then
  echo "resources/ already present — updating"
  apply_sparse
  git -C "$DEST" fetch --depth 1 origin "$BRANCH" || { echo "fetch failed" >&2; exit 1; }
  git -C "$DEST" checkout -q -B "$BRANCH" "origin/$BRANCH" || exit 1
else
  echo "cloning OsmAnd-resources (mode: $MODE)"
  if [ "$MODE" = "full" ]; then
    git clone --depth 1 --branch "$BRANCH" "$REPO" "$DEST" || exit 1
  else
    # blob:none defers file downloads; sparse-checkout then limits which are
    # ever fetched. Together these are the difference between 50 MB and 576 MB.
    git clone --filter=blob:none --no-checkout --depth 1 \
      --branch "$BRANCH" "$REPO" "$DEST" || exit 1
    apply_sparse
    git -C "$DEST" checkout -q "$BRANCH" || exit 1
  fi
fi

missing=0
for f in "${REQUIRED[@]}"; do
  if [ -s "$DEST/$f" ]; then
    printf '  ok      %s\n' "$f"
  else
    printf '  MISSING %s\n' "$f" >&2
    missing=1
  fi
done

if [ "$missing" -ne 0 ]; then
  echo >&2
  echo "Required assets are absent. Do not build against this checkout: the Gradle" >&2
  echo "Sync tasks would empty their destinations and produce an app with no" >&2
  echo "rendering styles or routing profiles, without reporting an error." >&2
  echo "Try: $0 --full" >&2
  exit 1
fi

echo
echo "resources/ ready — $(du -sh "$DEST" 2>/dev/null | cut -f1) (mode: $MODE)"
[ "$MODE" = "app" ] && echo "  upstream tests need fixtures: re-run with --with-tests"
echo "  build the client: cd client && ./trames-build.sh"
