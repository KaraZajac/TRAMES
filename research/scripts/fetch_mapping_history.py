#!/usr/bin/env python3
"""
Monthly count of mapped ALPR nodes in each state, from OSM history (the ohsome API).

    python3 fetch_mapping_history.py -o out/mapped_alpr_monthly_by_state.csv

This is the mapping history, not a deployment history: OSM records when a camera was mapped,
and installation dates are on 47 of 142,991 nodes. It dates when volunteer coverage reached
each state — the context every trend figure needs. ohsome answers aggregate counts over time
but refuses feature extraction on its public instance (HTTP 403), which is why the camera sets
themselves come from Overpass (server/alpr/fetch-history.sh). Validated: its continental count
at the July snapshot's timestamp is 120,778 against our 120,757, the gap being the 21 nodes
tagged lowercase `alpr` that July's case-sensitive fetch missed. Its data lags live OSM by
about two months.
"""
import argparse
import csv
import json
import os
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FILTER = "man_made=surveillance and surveillance:type in (ALPR, alpr) and type:node"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default=None, help="default: the last month ohsome has data for")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    import importlib.util
    spec = importlib.util.spec_from_file_location("trend", os.path.join(HERE, "trend.py"))
    CODES = importlib.util.module_from_spec(spec); spec.loader.exec_module(CODES)
    CODES = CODES.CODES

    meta = json.load(urllib.request.urlopen("https://api.ohsome.org/v1/metadata", timeout=60))
    end = args.end or meta["extractRegion"]["temporalExtent"]["toTimestamp"][:7] + "-01"
    fc = json.load(open(os.path.join(os.path.dirname(HERE), "paper", "figures", "us-states.json")))
    geoms = {CODES[f["properties"]["name"]]: f["geometry"] for f in fc["features"] if f["properties"]["name"] in CODES}
    # One state per request. All 51 polygons in one request make ohsome answer HTTP 500, and
    # Alaska's outline crosses the date line, which ohsome rejects as outside its extract —
    # so Alaska is asked by bounding box (the same one fetch-na-cones.sh uses).
    rows = []
    for code in sorted(geoms):
        params = {"time": f"{args.start}/{end}/P1M", "filter": FILTER}
        if code == "ak":
            params["bboxes"] = "-170,51,-129,72"
        else:
            params["bpolys"] = json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "id": code, "properties": {}, "geometry": geoms[code]}]})
        req = urllib.request.Request("https://api.ohsome.org/v1/elements/count",
                                     data=urllib.parse.urlencode(params).encode(),
                                     headers={"User-Agent": "TRAMES research (mapping-history counts)"})
        res = json.load(urllib.request.urlopen(req, timeout=900))
        for x in res["result"]:
            rows.append((x["timestamp"][:7], code, int(x["value"])))
        print(f"  {code}: {res['result'][-1]['value']:.0f} mapped by {res['result'][-1]['timestamp'][:7]}", flush=True)
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["month", "state", "mapped_alpr"])
        w.writerows(sorted(rows))
    print(f"{len(geoms)} states -> {args.out} (ohsome data through "
          f"{meta['extractRegion']['temporalExtent']['toTimestamp']})")

if __name__ == "__main__":
    main()
