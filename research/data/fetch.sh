#!/usr/bin/env bash
# Research data acquisition. All sources are public and key-free.
set -uo pipefail
cd "$(dirname "$0")"

echo "[1/4] tract centroids (Census Gazetteer 2024)"
[ -f tracts.zip ] || curl -sL -o tracts.zip \
  "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_tracts_national.zip"
[ -f 2024_Gaz_tracts_national.txt ] || unzip -o -q tracts.zip

echo "[2/4] ACS 5-year tract demographics"
# B19013 median household income; B03002 race/ethnicity (Hispanic-aware, unlike B02001);
# B08301 means of transportation to work (who drives, for the commute-mode check)
for t in b19013 b03002 b08301; do
  [ -f "acs_$t.dat" ] || curl -sL -o "acs_$t.dat" \
    "https://www2.census.gov/programs-surveys/acs/summary_file/2022/table-based-SF/data/5YRData/acsdt5y2022-$t.dat"
done

echo "[3/4] LODES8 origin-destination commute flows"
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

echo "[4/4] TIGER/Line 2022 tract, block-group and place boundaries (siting analysis)"
# 2022 geography matches the ACS 2022 5-year tables above, Connecticut's planning-region
# codes included. Full-resolution TIGER/Line rather than the generalized cartographic
# files: siting.py measures distances to boundaries in tens of metres.
declare -A FIPS=([al]=01 [ak]=02 [az]=04 [ar]=05 [ca]=06 [co]=08 [ct]=09 [de]=10 [dc]=11 [fl]=12
  [ga]=13 [hi]=15 [id]=16 [il]=17 [in]=18 [ia]=19 [ks]=20 [ky]=21 [la]=22 [me]=23 [md]=24
  [ma]=25 [mi]=26 [mn]=27 [ms]=28 [mo]=29 [mt]=30 [ne]=31 [nv]=32 [nh]=33 [nj]=34 [nm]=35
  [ny]=36 [nc]=37 [nd]=38 [oh]=39 [ok]=40 [or]=41 [pa]=42 [ri]=44 [sc]=45 [sd]=46 [tn]=47
  [tx]=48 [ut]=49 [vt]=50 [va]=51 [wa]=53 [wv]=54 [wi]=55 [wy]=56)
mkdir -p tiger
for st in "$@"; do
  for layer in TRACT:tract BG:bg PLACE:place; do
    f="tiger/tl_2022_${FIPS[$st]}_${layer##*:}.zip"
    [ -f "$f" ] && continue
    if curl -sfL -o "$f.part" "https://www2.census.gov/geo/tiger/TIGER2022/${layer%%:*}/${f#tiger/}" \
       && unzip -tq "$f.part" >/dev/null 2>&1; then mv "$f.part" "$f"
    else rm -f "$f.part"; echo "   $st ${layer##*:} FAILED"; fi
  done
done
echo "done"
