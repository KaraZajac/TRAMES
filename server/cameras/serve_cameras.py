#!/usr/bin/env python3
"""Serve ALPR camera positions to the TRAMES map layer, Overpass-compatibly.

    python3 serve_cameras.py [--port 8991] [--data cameras.json]

Why this exists: the map layer originally queried the public Overpass instances
directly from the phone, and on 2026-07-28 both spent the day answering 504 to
the ALPR query while trivial queries worked — leaving a silent, clean-looking
map over surveilled streets. The camera snapshot was already sitting on this
box (it is what the routing graph's cones are built from), so serve it from
here and let Overpass be the fallback instead of the single point of failure.
A pleasant consequence: the map now displays the same snapshot the router is
actually avoiding, instead of a fresher OSM state the graph has never seen.

Speaks just enough of Overpass's protocol that the client needs no new fetch
path — the SAME request works against this service and against real Overpass:

    POST /cameras   body: data=<urlencoded Overpass QL>   (the bbox is parsed
                    out of the QL; every other part of the query is ignored)
    GET  /cameras?bbox=<south>,<west>,<north>,<east>      (for humans/curl)
    GET  /health                                          (load-state probe)

Response is Overpass-shaped: {"osm3s": {...}, "elements": [{"type": "node",
"id", "lat", "lon", "tags": {...}}]} with exactly the five tags the client
renders. Elements are pre-serialized at load time and answers are assembled by
joining byte strings — the whole continent (120,838 cameras, ~14 MB) holds in
~122 MB RSS measured, which matters on a box where GraphHopper owns 18 of 22 GB.

No auth, no rate limiting of its own — bind localhost only, front with Caddy,
same posture as the routing endpoint (see the systemd unit's blast-radius cap).
Refresh: rebuild cameras.json (build_cameras_json.py), scp it over, then
`systemctl reload trames-cameras` (SIGHUP) — no restart, no dropped requests.
"""

import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GRID_DEG = 0.25          # index cell size; a phone-zoom bbox touches ~4 cells
MAX_SPAN_DEG = 5.0       # largest bbox side served; the client sends ~0.2
MAX_ELEMENTS = 20_000    # hard cap on elements per answer

# One parenthesized numeric 4-tuple is the bbox; nothing else in the client's
# QL matches this shape. Scientific notation accepted because Java renders
# doubles that way below 1e-3.
BBOX_RE = re.compile(
    r"\(\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*,"
    r"\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*,"
    r"\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*,"
    r"\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*\)"
)


class CameraStore:
    """Grid-bucketed camera index over pre-serialized element JSON."""

    def __init__(self, path):
        self.path = path
        self.load()

    def load(self):
        t0 = time.monotonic()
        with open(self.path) as f:
            data = json.load(f)
        grid = {}
        count = 0
        for el in data.get("elements", ()):
            lat, lon = el["lat"], el["lon"]
            rendered = json.dumps(
                {"type": "node", **el}, separators=(",", ":")
            ).encode()
            key = (int(lat // GRID_DEG), int(lon // GRID_DEG))
            grid.setdefault(key, []).append((lat, lon, rendered))
            count += 1
        # Swap both atomically enough for our purposes: readers grab the
        # attribute once per request, so a mid-reload request just answers
        # wholly from the old or wholly from the new snapshot.
        self.grid = grid
        self.count = count
        self.osm_base = (data.get("osm3s") or {}).get("timestamp_osm_base", "unknown")
        print(f"loaded {count} cameras (osm_base {self.osm_base}) "
              f"in {time.monotonic() - t0:.1f}s", flush=True)

    def query(self, south, west, north, east):
        grid = self.grid
        out = []
        gy0, gy1 = int(south // GRID_DEG), int(north // GRID_DEG)
        gx0, gx1 = int(west // GRID_DEG), int(east // GRID_DEG)
        for gy in range(gy0, gy1 + 1):
            for gx in range(gx0, gx1 + 1):
                for lat, lon, rendered in grid.get((gy, gx), ()):
                    if south <= lat <= north and west <= lon <= east:
                        out.append(rendered)
                        if len(out) > MAX_ELEMENTS:
                            return out[:MAX_ELEMENTS], True
        return out, False


STORE = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "trames-cameras"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):  # journald gets one line per request
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _send(self, code, payload, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Data-Timestamp", STORE.osm_base)
        self.end_headers()
        self.wfile.write(body)

    def _bad(self, msg):
        self._send(400, {"error": msg})

    # -- request handling -------------------------------------------------

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path in ("/health", "/cameras/health"):
            self._send(200, {"cameras": STORE.count, "osm_base": STORE.osm_base})
            return
        if url.path != "/cameras":
            self._send(404, {"error": "not found"})
            return
        bbox = urllib.parse.parse_qs(url.query).get("bbox", [""])[0]
        parts = bbox.split(",")
        if len(parts) != 4:
            self._bad("expected ?bbox=south,west,north,east")
            return
        try:
            self._answer(*map(float, parts))
        except ValueError:
            self._bad("bbox values must be numbers")

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/cameras":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if not 0 < length <= 65536:
            self._bad("missing or oversized body")
            return
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        m = BBOX_RE.search(form.get("data", [""])[0])
        if not m:
            self._bad("no (south,west,north,east) bbox found in query")
            return
        self._answer(*map(float, m.groups()))

    def _answer(self, south, west, north, east):
        south, north = max(south, -90.0), min(north, 90.0)
        west, east = max(west, -180.0), min(east, 180.0)
        if south > north or west > east:
            self._bad("inverted bbox")
            return
        if north - south > MAX_SPAN_DEG or east - west > MAX_SPAN_DEG:
            self._bad(f"bbox side exceeds {MAX_SPAN_DEG} degrees")
            return
        elements, truncated = STORE.query(south, west, north, east)
        head = {
            "version": 0.6,
            "generator": "TRAMES camera service",
            "osm3s": {
                "timestamp_osm_base": STORE.osm_base,
                "copyright": (
                    "The data included in this document is from "
                    "www.openstreetmap.org. The data is made available under ODbL."
                ),
            },
        }
        if truncated:
            # Word chosen so the client's Overpass-failure heuristic ("timed
            # out" / "runtime error") does NOT treat a capped answer as an
            # outage and discard it.
            head["remark"] = f"truncated to {MAX_ELEMENTS} elements"
        prefix = json.dumps(head, separators=(",", ":"))[:-1].encode()
        body = prefix + b',"elements":[' + b",".join(elements) + b"]}"
        self._send(200, body)


def main():
    global STORE
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--port", type=int, default=8991)
    ap.add_argument("--data", default=os.path.join(here, "cameras.json"))
    args = ap.parse_args()

    STORE = CameraStore(args.data)
    signal.signal(signal.SIGHUP, lambda *_: STORE.load())

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serving on 127.0.0.1:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
