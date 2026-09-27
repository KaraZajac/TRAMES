#!/usr/bin/env python3
"""
When each ALPR node entered OpenStreetMap, to the second, from the full-history dump.

    python3 node_dates.py --history osm-history/history-latest.osm.pbf \\
        --out osm-history/node_dates.json --monthly osm-history/node_dates_monthly.csv

history_from_planet.py answers "which nodes were ALPRs on date D" for a handful of dates. This
asks the dump the question the other way round — for every node that was ever an ALPR, every
version it has had — and writes the dates that matter per node:

  created         the node's first version, whatever it was tagged then (a traffic signal a
                  mapper later hung a camera on keeps its 2009 creation date)
  first_alpr      the first version tagged as an ALPR: the day the camera entered the map
  last_edit       the latest version of any kind
  versions        how many
  alive           the latest version is visible and still tagged ALPR (as of the dump)
  ended           if not alive: the timestamp of the deletion or retagging
  first_position  where the camera was first mapped, and moved_m how far the latest version
                  has been moved from there

plus a monthly series of how many ALPR nodes the map held on the first of each month, worldwide
and inside the study's state boxes, which needs no Overpass and no per-date extraction.

With --versions, every version of every such node is also written out (JSON lines: id,
timestamp, version, visible, lat, lon, tags), so that the map as it stood on ANY instant is a
filter over that file (snapshot_from_versions.py) and the dump need never be read again.

Same two pyosmium passes as history_from_planet.py (~9 min each on this machine): a TagFilter
pass for the ids, an IdFilter pass for their versions. Node versions carry their own location,
so no location index is built and memory stays small; run it capped all the same:

    systemd-run --user --scope -p MemoryMax=16G -p MemorySwapMax=0 --quiet nice -n 10 python3 node_dates.py ...
"""
import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import re
import time
from collections import defaultdict

import osmium
from osmium.filter import IdFilter, TagFilter

HERE = os.path.dirname(os.path.abspath(__file__))


def is_alpr(tags):
    return tags.get("man_made") == "surveillance" and (tags.get("surveillance:type") or "").lower() == "alpr"


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(a))


def month_starts(first, last):
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        yield dt.datetime(y, m, 1, tzinfo=dt.timezone.utc)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--monthly", default=None, help="CSV of ALPR nodes on the map at each month start")
    ap.add_argument("--versions", default=None, help="JSON lines of every version of every ever-ALPR node")
    ap.add_argument("--boxes", default=os.path.join(HERE, "us_state_boxes.json"))
    ap.add_argument("--from-month", default="2023-01")
    ap.add_argument("--dump-date", default=None, help="YYYY-MM-DD of the dump, if the file name does not say")
    args = ap.parse_args()

    boxes = list(json.load(open(args.boxes))["boxes"].values())
    inside = lambda lat, lon: any(s <= lat <= n and w <= lon <= e for s, w, n, e in boxes)
    # the dump's date: from --dump-date, else from the file name or the .md5 beside it (history-YYMMDD)
    names = [os.path.basename(os.path.realpath(args.history))] + \
        [os.path.basename(p) for p in glob.glob(os.path.join(os.path.dirname(args.history), "history-*.md5"))]
    m = next((re.search(r"history-(\d{6})", n) for n in names if re.search(r"history-(\d{6})", n)), None)
    dump_date = args.dump_date or (f"20{m.group(1)[:2]}-{m.group(1)[2:4]}-{m.group(1)[4:]}" if m else None)

    t0 = time.time()
    ids = set()
    for n in osmium.FileProcessor(args.history, osmium.osm.NODE).with_filter(TagFilter(("man_made", "surveillance"))):
        if (n.tags.get("surveillance:type") or "").lower() == "alpr":
            ids.add(n.id)
    print(f"pass 1: {len(ids):,} nodes were ALPRs at some point ({time.time() - t0:.0f}s)", flush=True)

    t1 = time.time()
    versions = defaultdict(list)
    for n in osmium.FileProcessor(args.history, osmium.osm.NODE).with_filter(IdFilter(ids)):
        loc = n.location
        versions[n.id].append((n.timestamp, n.version, bool(n.visible),
                               loc.lat if loc.valid() else None, loc.lon if loc.valid() else None,
                               {t.k: t.v for t in n.tags}))
    print(f"pass 2: {sum(len(v) for v in versions.values()):,} versions ({time.time() - t1:.0f}s)", flush=True)

    out = {}
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    for vs in versions.values():
        vs.sort(key=lambda x: (x[0], x[1]))
    if args.versions:
        with open(args.versions, "w") as fh:
            for nid in sorted(versions):
                for ts, ver, vis, lat, lon, tags in versions[nid]:
                    fh.write(json.dumps([nid, iso(ts), ver, vis, lat, lon, tags], separators=(",", ":")) + "\n")
        print(f"versions -> {args.versions}", flush=True)
    for nid, vs in versions.items():
        alpr_vs = [v for v in vs if v[2] and is_alpr(v[5])]
        if not alpr_vs:
            continue
        first, last = alpr_vs[0], vs[-1]
        alive = last[2] and is_alpr(last[5])
        rec = {"created": iso(vs[0][0]), "first_alpr": iso(first[0]), "last_edit": iso(last[0]),
               "versions": len(vs), "alive": alive}
        if not alive:
            # the first version after the last ALPR one is the deletion or the retagging
            after = next(v for v in vs if v[0] > alpr_vs[-1][0] or (v[0] == alpr_vs[-1][0] and v[1] > alpr_vs[-1][1]))
            rec["ended"] = iso(after[0])
            rec["ended_by"] = "deleted" if not after[2] else "retagged"
        if first[3] is not None:
            rec["first_position"] = [round(first[3], 7), round(first[4], 7)]
            if last[3] is not None:
                mv = haversine_m(first[3], first[4], last[3], last[4])
                if mv >= 0.5:
                    rec["moved_m"] = round(mv, 1)
        out[str(nid)] = rec

    with open(args.out, "w") as fh:
        json.dump({"source": os.path.basename(os.path.realpath(args.history)), "dump_date": dump_date,
                   "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "note": "every node ever tagged man_made=surveillance + surveillance:type=alpr (any case); "
                           "timestamps UTC; a node created after dump_date is absent",
                   "nodes": out}, fh, separators=(",", ":"))
    print(f"wrote {len(out):,} nodes -> {args.out}", flush=True)

    if args.monthly:
        y, mo = (int(x) for x in args.from_month.split("-"))
        last_month = dt.datetime.strptime(dump_date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc) if dump_date \
            else dt.datetime.now(dt.timezone.utc)
        rows = []
        for at in month_starts(dt.datetime(y, mo, 1, tzinfo=dt.timezone.utc), last_month):
            world = us = 0
            for vs in versions.values():
                cur = None
                for v in vs:
                    if v[0] <= at:
                        cur = v
                    else:
                        break
                if cur and cur[2] and cur[3] is not None and is_alpr(cur[5]):
                    world += 1
                    us += inside(cur[3], cur[4])
            rows.append({"month": at.strftime("%Y-%m"), "alpr_nodes_world": world, "alpr_nodes_us_boxes": us})
        with open(args.monthly, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["month", "alpr_nodes_world", "alpr_nodes_us_boxes"])
            w.writeheader(); w.writerows(rows)
        print(f"monthly series -> {args.monthly} ({rows[0]['month']}..{rows[-1]['month']}, "
              f"{rows[-1]['alpr_nodes_us_boxes']:,} in the US boxes at the end)", flush=True)
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
