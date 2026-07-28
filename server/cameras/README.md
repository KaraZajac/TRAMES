# TRAMES camera service

Serves ALPR camera positions to the app's map layer from the same snapshot the
routing graph's cones were built from. Exists because the map layer's original
data path — the phone querying public Overpass instances directly — spent
2026-07-28 answering 504 on both instances while the routing endpoint sat on a
box already holding all 120,838 camera positions. The public Overpass instances
remain in the client as fallback; this service is simply first in line.

Display and avoidance now come from the **same dataset**: the map shows exactly
the cameras the router is steering around, not a fresher OSM state the graph
has never seen.

```
build_cameras_json.py    region_cache tiles -> one deduped, slimmed cameras.json
serve_cameras.py         bbox lookups over it, speaking Overpass's protocol
trames-cameras.service   systemd unit (hardened, blast-radius capped)
```

## Protocol

The client needs no separate code path for this service — its exact Overpass
request works here:

```sh
# what the app sends (Overpass QL, bbox parsed out of it, rest ignored)
curl -X POST https://routing.blackflagintel.com/cameras \
  --data-urlencode 'data=[out:json][timeout:20];(node["man_made"="surveillance"]["surveillance:type"="ALPR"](33.6,-84.6,33.9,-84.2););out body;'

# the human form
curl 'https://routing.blackflagintel.com/cameras?bbox=33.6,-84.6,33.9,-84.2'
```

Answers are Overpass-shaped (`osm3s` header, `elements` of `node`s) carrying
only the five tags the layer renders: `direction`, `camera:direction`,
`operator`, `brand`, `manufacturer`. Bbox sides are capped at 5°, answers at
20,000 elements (a capped answer carries a `remark` that deliberately avoids
the client's failure-heuristic words).

## Deploy / refresh

```sh
# on the laptop, from server/cameras/
python3 build_cameras_json.py                       # region_cache -> cameras.json
scp cameras.json serve_cameras.py astrophage:/opt/trames/cameras/
ssh astrophage systemctl reload trames-cameras      # SIGHUP: reload in place
```

First-time install: copy `trames-cameras.service` to `/etc/systemd/system/`,
`systemctl enable --now trames-cameras`, and add `/cameras` to the Caddy site
(`/etc/caddy/Caddyfile.d/routing.blackflagintel.com.caddyfile`):

```caddyfile
	@cameras path /cameras
	handle @cameras {
		reverse_proxy 127.0.0.1:8991
	}
```

Data freshness is whatever the last `fetch-na-cones.sh` run produced — the
`osm3s.timestamp_osm_base` in every answer (and the `X-Data-Timestamp` header)
says exactly how fresh that is. Refreshing cameras.json without re-importing
the routing graph is fine: the map drifts slightly ahead of the cones, which
beats the map lying dark.

Camera data © OpenStreetMap contributors, ODbL; largely mapped by the
[DeFlock](https://deflock.me) community.
