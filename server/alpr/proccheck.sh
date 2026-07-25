#!/usr/bin/env bash
# Is the cone builder actually running?
#
#   ./proccheck.sh && echo yes || echo no
#
# `pgrep -f build_cones.py` is unreliable here and has produced three separate false
# results in this project: it matches ANY process whose command line contains the
# string — including the shell running the pgrep, a monitor loop watching for that
# same string, and the very guard trying to prevent a double-launch. The bracket trick
# (`[b]uild_cones`) only defeats the first of those.
#
# The fix is to require the process actually be a python interpreter, by reading
# /proc/<pid>/comm rather than trusting the command-line substring.
for pid in $(pgrep -f "build_cones\.py" 2>/dev/null); do
  comm=$(cat "/proc/$pid/comm" 2>/dev/null || echo "")
  case "$comm" in
    python*|Python*) echo "$pid"; exit 0 ;;
  esac
done
exit 1
