#!/usr/bin/env bash
# Research data acquisition. All sources are public and key-free.
set -uo pipefail
cd "$(dirname "$0")"

echo "[1/3] tract centroids (Census Gazetteer 2024)"
[ -f tracts.zip ] || curl -sL -o tracts.zip \
  "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_tracts_national.zip"
[ -f 2024_Gaz_tracts_national.txt ] || unzip -o -q tracts.zip

echo "[2/3] ACS 5-year tract demographics"
# B19013 median household income; B03002 race/ethnicity (Hispanic-aware, unlike B02001)
for t in b19013 b03002; do
  [ -f "acs_$t.dat" ] || curl -sL -o "acs_$t.dat" \
    "https://www2.census.gov/programs-surveys/acs/summary_file/2022/table-based-SF/data/5YRData/acsdt5y2022-$t.dat"
done

echo "[3/3] LODES8 origin-destination commute flows"
# JT00 = all jobs, main = within-state. Real worker flows, so sampled O/D pairs
# reflect actual commuting rather than uniform random points on a map.
#
#   ./fetch.sh ga tx ca        # named states
#   ./fetch.sh all             # all 50 states + DC
#
# 2022 is the target year. A state without a 2022 file falls back to the latest year
# LODES has for it, and says so: build_sample.py picks up whichever year is on disk.
# curl -f matters — without it a 404 is saved as an HTML error page named .csv.gz,
# and the failure surfaces much later as a gzip error inside the sampler.
ALL="al ak az ar ca co ct de dc fl ga hi id il in ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny nc nd oh ok or pa ri sc sd tn tx ut vt va wa wv wi wy"
[ "${1:-}" = "all" ] && set -- $ALL
mkdir -p lodes
for st in "$@"; do
  ls lodes/${st}_od_main_JT00_*.csv.gz >/dev/null 2>&1 && continue
  got=""
  for yr in 2022 2021 2020 2019 2018 2017 2016 2015; do
    f="lodes/${st}_od_main_JT00_${yr}.csv.gz"
    if curl -sfL -o "$f.part" "https://lehd.ces.census.gov/data/lodes/LODES8/${st}/od/${st}_od_main_JT00_${yr}.csv.gz" \
       && gzip -t "$f.part" 2>/dev/null; then
      mv "$f.part" "$f"; got=$yr; break
    fi
    rm -f "$f.part"
  done
  if [ -z "$got" ]; then echo "   $st FAILED: no od_main file for 2015-2022"
  else echo "   $st $got $(du -h "lodes/${st}_od_main_JT00_${got}.csv.gz" | cut -f1)$([ "$got" != 2022 ] && echo '  (2022 not published; using latest)')"; fi
done
echo "done"
