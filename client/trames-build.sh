#!/usr/bin/env bash
# Build TRAMES (OsmAnd fork).
#
#   ./trames-build.sh                       # default: arm64 debug, OpenGL renderer
#   ./trames-build.sh nightlyFreeLegacyArm64Debug
#
# Toolchain notes:
#  - JDK 21 is REQUIRED. Android Gradle Plugin 8.7.3 rejects newer JDKs, and Fedora 44
#    ships only 25/26, so a Temurin tarball lives in ~/.local/jdk-21. The system java
#    will fail with an unsupported-class-file / AGP version error.
#  - The native OpenGL core is NOT built here. OsmAnd downloads prebuilt binaries from
#    its ivy repository, so no NDK, CMake or SWIG is needed — those are only required
#    when developing the C++ core itself.
#  - Prefer a single-ABI variant (Arm64) over Fat: Fat builds every architecture and
#    takes several times as long for no benefit on a modern test device.
#  - GRADLE_JVMARGS below is required, not optional. OsmAnd's gradle.properties has
#    every org.gradle.jvmargs line commented out, so Gradle runs on its small default
#    heap — and with android.enableJetifier=true it must unpack and rewrite the
#    OsmAndCore_android*.aar native archives, which are tens of MB of .so files. That
#    OOMs with "Execution failed for JetifyTransform ... Java heap space", reported as
#    a dependency-resolution failure rather than as a memory problem.
#    Passed on the command line rather than edited into gradle.properties so the fork
#    keeps a minimal diff against upstream OsmAnd and stays cheap to rebase.
#  - Upstream unit tests are SKIPPED (-x test). They cannot pass in this checkout:
#    OsmAnd-java/src/test/resources/ ships only phrases/ and stars/, while the routing
#    and search tests need poi_types.xml and routing fixtures that live in the separate
#    OsmAnd-resources repository. All 8 failures are FileNotFoundException or setup
#    NullPointerException, none are real defects. Clone OsmAnd-resources alongside if
#    you ever want the suite to actually run.
set -uo pipefail
cd "$(dirname "$0")"

export JAVA_HOME="${JAVA_HOME:-$HOME/.local/jdk-21}"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Android/Sdk}"
export ANDROID_SDK_ROOT="$ANDROID_HOME"

GRADLE_JVMARGS="${GRADLE_JVMARGS:--Xmx6g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8}"
VARIANT="${1:-nightlyFreeOpenglArm64Debug}"
[ $# -gt 0 ] && shift          # rest of "$@" becomes extra gradle flags, not a task name
TASK=":OsmAnd:assemble${VARIANT^}"   # gradle wants assembleNightly..., not assemblenightly...

[ -x "$JAVA_HOME/bin/java" ] || { echo "no JDK at $JAVA_HOME" >&2; exit 1; }
[ -d "$ANDROID_HOME/platforms" ] || { echo "no Android SDK at $ANDROID_HOME" >&2; exit 1; }

echo "JDK:     $("$JAVA_HOME/bin/java" -version 2>&1 | head -1)"
echo "SDK:     $ANDROID_HOME"
echo "variant: $VARIANT"
echo "task:    $TASK"

# Re-apply the TRAMES deltas to the upstream resources checkout before Gradle syncs it in.
# Not optional and not a no-op: routing.xml is gitignored at both ends and is overwritten
# by the Sync task on every build, so without this the app builds clean and silently stops
# avoiding cameras. Idempotent — a no-op once applied.
if [ -x ../trames-patch-resources.sh ]; then
  echo "patches:"
  ../trames-patch-resources.sh || { echo "resource patching failed — refusing to build an app that would not avoid cameras" >&2; exit 1; }
fi
echo

mkdir -p ../build-logs
LOG="../build-logs/build-$VARIANT.log"
./gradlew "$TASK" -x test -Dorg.gradle.jvmargs="$GRADLE_JVMARGS" --console=plain "$@" > "$LOG" 2>&1
RC=$?

if [ $RC -eq 0 ]; then
  echo "BUILD OK"
  find OsmAnd/build/outputs/apk -name "*.apk" -newermt "-10 minutes" 2>/dev/null \
    | while read -r apk; do echo "  $(du -h "$apk" | cut -f1)  $apk"; done
else
  echo "BUILD FAILED (rc=$RC) — see $LOG"
  grep -E "FAILURE|What went wrong|error:|Caused by" -A4 "$LOG" | head -40
fi
exit $RC
