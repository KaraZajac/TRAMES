package net.osmand.plus.trames;

import android.os.AsyncTask;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;

import net.osmand.PlatformUtil;
import net.osmand.data.LatLon;
import net.osmand.data.QuadRect;

import org.apache.commons.logging.Log;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Fetches ALPR (licence-plate reader) camera positions for display on the map.
 *
 * <p>Primary source is the TRAMES server's own camera endpoint, which serves the exact
 * snapshot the routing graph's cones were built from — so the map shows the cameras the
 * router is actually avoiding, not a fresher OSM state the graph has never seen. It
 * speaks Overpass's protocol, so the public Overpass instances remain as fallbacks and
 * the fetch path below cannot tell them apart. That fallback is what keeps the layer
 * alive for a self-hoster running a router with no camera endpoint — and it is not
 * hypothetical the other way round either: on 2026-07-28 both public instances answered
 * 504 to this exact query for a full day, which on the old Overpass-only path meant a
 * silent, clean-looking map over surveilled streets.
 *
 * <p>Tag handling follows what OSM actually contains rather than what the wiki recommends
 * — see TramesCameraSource#parseDirections. Measured over 3,899 cameras in one metro:
 * {@code direction} appears 3,864 times against 21 for {@code camera:direction}.
 */
public class TramesCameraSource {

	private static final Log LOG = PlatformUtil.getLog(TramesCameraSource.class);

	private static final String[] ENDPOINTS = {
			"https://routing.blackflagintel.com/cameras",
			"https://overpass-api.de/api/interpreter",
			"https://overpass.kumi.systems/api/interpreter"
	};

	/**
	 * Below this zoom the visible area covers thousands of cameras — enough to be both
	 * unreadable and abusive to a free community API. The layer draws nothing lower.
	 */
	public static final int MIN_ZOOM = 12;

	private static final int TIMEOUT_MS = 25_000;
	private static final int QUERY_TIMEOUT_S = 20;

	/** Refetch once the map has moved roughly this far from the last query centre. */
	private static final double REFETCH_DEG = 0.05;

	/** Padding around the visible box so panning slightly doesn't trigger a refetch. */
	private static final double PAD_DEG = 0.03;

	/** Tile size for whole-route camera fetches — kept under the server's per-query span cap. */
	private static final double ROUTE_TILE_DEG = 2.0;

	/** Backstop so a pathologically long route can't fan out into dozens of queries. */
	private static final int ROUTE_MAX_TILES = 16;

	public static class Camera {
		public final long osmId;
		public final double lat;
		public final double lon;
		/** Bearings in degrees clockwise from north; empty when the camera has no direction. */
		public final float[] directions;
		@Nullable
		public final String operator;
		@Nullable
		public final String brand;

		Camera(long osmId, double lat, double lon, float[] directions,
		       @Nullable String operator, @Nullable String brand) {
			this.osmId = osmId;
			this.lat = lat;
			this.lon = lon;
			this.directions = directions;
			this.operator = operator;
			this.brand = brand;
		}
	}

	private final AtomicBoolean fetching = new AtomicBoolean(false);
	private volatile List<Camera> cameras = Collections.emptyList();
	private volatile LatLon lastCentre;
	private volatile long lastFetchFailedAt;

	/**
	 * On-device fallback. Set by the app so the layer keeps working with no network; when
	 * null the source is network-only (its behaviour before the offline pack existed).
	 */
	@Nullable
	private volatile TramesCameraStore store;

	public void setStore(@Nullable TramesCameraStore store) {
		this.store = store;
	}

	/** Decides whether this source may talk to the network at all. */
	public interface NetworkPolicy {
		boolean allowed();
	}

	/**
	 * Default DENY, and deliberately so.
	 *
	 * <p>A camera query is a bounding box around what the user is looking at — which is to
	 * say, where they are. Sending that to our server (or worse, to a public Overpass
	 * instance) is exactly the kind of movement record this app exists to prevent, and on
	 * a fresh install it happened before the user had opted into anything at all.
	 *
	 * <p>So the source cannot reach the network unless something explicitly permits it,
	 * and the failure mode of a mis-wired policy is a map with no cameras rather than a
	 * silent location leak. See {@link #fetch} for the single choke point this gates.
	 */
	private volatile NetworkPolicy networkPolicy = () -> false;

	public void setNetworkPolicy(@NonNull NetworkPolicy policy) {
		this.networkPolicy = policy;
	}

	private boolean networkAllowed() {
		NetworkPolicy p = networkPolicy;
		return p != null && p.allowed();
	}

	/**
	 * True when nothing can currently supply cameras — no network answer and no pack on
	 * disk. Distinct from "fetched successfully and there are none here": the UI must be
	 * able to tell an empty area from an unanswerable question, because a blank map over
	 * surveilled streets reads as safety.
	 */
	public boolean isUnknown() {
		TramesCameraStore s = store;
		return cameras.isEmpty() && (s == null || !s.isPresent());
	}

	@NonNull
	public List<Camera> getCameras() {
		return cameras;
	}

	/**
	 * Fetch cameras for the given view if the map has moved far enough to warrant it.
	 * Cheap and safe to call on every frame — it self-throttles.
	 */
	public void ensureLoaded(@NonNull QuadRect visibleBox, int zoom, @Nullable Runnable onLoaded) {
		if (zoom < MIN_ZOOM || fetching.get()) {
			return;
		}
		// Nothing can answer: no permission to ask, and no pack to read. Return before
		// spawning a task rather than after — on a fresh install with offline routing this
		// is the common path, and it must cost nothing and send nothing.
		TramesCameraStore local = store;
		if (!networkAllowed() && (local == null || !local.isPresent())) {
			return;
		}
		// Back off for a minute after a failure. Overpass is a free community service and
		// hammering it on every pan after an outage is exactly how an IP gets blocked.
		if (System.currentTimeMillis() - lastFetchFailedAt < 60_000) {
			return;
		}
		double cLat = (visibleBox.top + visibleBox.bottom) / 2;
		double cLon = (visibleBox.left + visibleBox.right) / 2;
		LatLon centre = lastCentre;
		if (centre != null
				&& Math.abs(centre.getLatitude() - cLat) < REFETCH_DEG
				&& Math.abs(centre.getLongitude() - cLon) < REFETCH_DEG) {
			return;
		}
		if (!fetching.compareAndSet(false, true)) {
			return;
		}
		final double south = Math.min(visibleBox.top, visibleBox.bottom) - PAD_DEG;
		final double north = Math.max(visibleBox.top, visibleBox.bottom) + PAD_DEG;
		final double west = Math.min(visibleBox.left, visibleBox.right) - PAD_DEG;
		final double east = Math.max(visibleBox.left, visibleBox.right) + PAD_DEG;

		new AsyncTask<Void, Void, List<Camera>>() {
			@Override
			protected List<Camera> doInBackground(Void... voids) {
				List<Camera> net = fetch(south, west, north, east);
				if (net != null) {
					return net;
				}
				// No network answer: fall back to the on-device pack rather than leaving
				// the map blank. Same snapshot the offline router avoids, so offline the
				// map and the route agree exactly.
				TramesCameraStore s = store;
				return s != null ? s.forBox(south, west, north, east) : null;
			}

			@Override
			protected void onPostExecute(List<Camera> result) {
				if (result != null) {
					cameras = result;
					lastCentre = new LatLon(cLat, cLon);
				} else {
					lastFetchFailedAt = System.currentTimeMillis();
				}
				fetching.set(false);
				if (onLoaded != null) {
					onLoaded.run();
				}
			}
		}.executeOnExecutor(AsyncTask.THREAD_POOL_EXECUTOR);
	}

	@Nullable
	private List<Camera> fetch(double south, double west, double north, double east) {
		// The single choke point for every camera request, gated here rather than at the
		// call sites so a new caller cannot accidentally reintroduce the leak. The bounding
		// box below IS the user's location; it does not leave the device unless the policy
		// says so.
		if (!networkAllowed()) {
			return null;
		}
		String query = "[out:json][timeout:" + QUERY_TIMEOUT_S + "];"
				+ "(node[\"man_made\"=\"surveillance\"][\"surveillance:type\"=\"ALPR\"]"
				+ "(" + south + "," + west + "," + north + "," + east + "););out body;";
		for (String endpoint : ENDPOINTS) {
			HttpURLConnection conn = null;
			try {
				conn = (HttpURLConnection) new URL(endpoint).openConnection();
				conn.setConnectTimeout(TIMEOUT_MS);
				conn.setReadTimeout(TIMEOUT_MS);
				conn.setRequestMethod("POST");
				conn.setDoOutput(true);
				conn.setRequestProperty("User-Agent", "TRAMES (map camera layer)");
				conn.setRequestProperty("Content-Type", "application/x-www-form-urlencoded");
				try (OutputStream os = conn.getOutputStream()) {
					os.write(("data=" + URLEncoder.encode(query, "UTF-8")).getBytes("UTF-8"));
				}
				if (conn.getResponseCode() / 100 != 2) {
					continue;
				}
				StringBuilder sb = new StringBuilder();
				try (java.io.BufferedReader r = new java.io.BufferedReader(
						new java.io.InputStreamReader(conn.getInputStream(), "UTF-8"))) {
					String line;
					while ((line = r.readLine()) != null) {
						sb.append(line);
					}
				}
				String body = sb.toString();
				// Overpass answers 200 with a {"remark": "...timed out..."} body when it
				// gives up, and elements is then empty. Treating that as "no cameras here"
				// would silently show a clean map over a surveilled area.
				if (body.contains("\"remark\"")
						&& (body.contains("timed out") || body.contains("runtime error"))) {
					continue;
				}
				return parse(body);
			} catch (Exception e) {
				LOG.warn("TRAMES camera fetch failed on " + endpoint + ": " + e.getMessage());
			} finally {
				if (conn != null) {
					conn.disconnect();
				}
			}
		}
		return null;
	}

	/**
	 * Fetch every camera within the given bounds, for scoring a whole route rather than
	 * just the visible map. Synchronous — call it off the UI thread. Independent of the
	 * view cache, so it never disturbs what the map is currently drawing.
	 *
	 * <p>Bounds larger than the server's per-query span cap are split into tiles and the
	 * results unioned by OSM id. Returns {@code null} only if <em>every</em> tile failed:
	 * an empty list means the route genuinely passes no mapped cameras, which the caller
	 * must not confuse with a network failure — the difference is a real "you're clear"
	 * versus "we could not check".
	 */
	@Nullable
	public List<Camera> fetchForRouteSync(double south, double west, double north, double east) {
		// Offline the whole route is answerable from the pack in one pass — no tiling, no
		// per-query span cap, and no undercount on a long route. Preferred whenever the
		// pack is present and the network is not answering.
		TramesCameraStore pack = store;

		int rows = Math.max(1, (int) Math.ceil((north - south) / ROUTE_TILE_DEG));
		int cols = Math.max(1, (int) Math.ceil((east - west) / ROUTE_TILE_DEG));
		if ((long) rows * cols > ROUTE_MAX_TILES) {
			// Pathologically long route: one capped query beats firing dozens. The server
			// truncates, so this can undercount — acceptable at that scale, and still far
			// better than the old visible-view-only count.
			List<Camera> one = fetch(south, west, north, east);
			if (one != null) {
				return one;
			}
			return pack != null && pack.isPresent() ? pack.forBox(south, west, north, east) : null;
		}
		LinkedHashMap<Long, Camera> merged = new LinkedHashMap<>();
		boolean anySuccess = false;
		for (int r = 0; r < rows; r++) {
			for (int c = 0; c < cols; c++) {
				double s = south + r * ROUTE_TILE_DEG;
				double n = Math.min(north, s + ROUTE_TILE_DEG);
				double w = west + c * ROUTE_TILE_DEG;
				double e = Math.min(east, w + ROUTE_TILE_DEG);
				List<Camera> tile = fetch(s, w, n, e);
				if (tile != null) {
					anySuccess = true;
					for (Camera cam : tile) {
						merged.put(cam.osmId, cam);
					}
				}
			}
		}
		if (anySuccess) {
			return new ArrayList<>(merged.values());
		}
		// Every tile failed. The pack answers the whole box at once, so an offline
		// exposure count is complete rather than partial.
		return pack != null && pack.isPresent() ? pack.forBox(south, west, north, east) : null;
	}

	@NonNull
	private List<Camera> parse(@NonNull String json) throws Exception {
		JSONObject root = new JSONObject(json);
		JSONArray elements = root.optJSONArray("elements");
		List<Camera> out = new ArrayList<>();
		if (elements == null) {
			return out;
		}
		for (int i = 0; i < elements.length(); i++) {
			JSONObject el = elements.optJSONObject(i);
			if (el == null || !"node".equals(el.optString("type"))) {
				continue;
			}
			double lat = el.optDouble("lat", Double.NaN);
			double lon = el.optDouble("lon", Double.NaN);
			if (Double.isNaN(lat) || Double.isNaN(lon)) {
				continue;
			}
			JSONObject tags = el.optJSONObject("tags");
			String raw = null;
			String operator = null;
			String brand = null;
			if (tags != null) {
				raw = tags.optString("direction", null);
				if (raw == null || raw.isEmpty()) {
					raw = tags.optString("camera:direction", null);
				}
				operator = emptyToNull(tags.optString("operator", null));
				brand = emptyToNull(tags.optString("brand", null));
				if (brand == null) {
					brand = emptyToNull(tags.optString("manufacturer", null));
				}
			}
			out.add(new Camera(el.optLong("id"), lat, lon, parseDirections(raw), operator, brand));
		}
		return out;
	}

	@Nullable
	private static String emptyToNull(@Nullable String s) {
		return s == null || s.isEmpty() ? null : s;
	}

	/**
	 * Parse an OSM direction value into zero or more bearings.
	 *
	 * <p>Handles the four shapes that actually occur:
	 * <pre>
	 *   "137"                single bearing
	 *   "144-189"            arc range -> its midpoint
	 *   "338-23"             arc wrapping past 0 degrees
	 *   "320;190"            multi-head unit -> two bearings
	 *   "0;72;144;216;288"   five-head 360 unit -> five bearings
	 * </pre>
	 *
	 * <p>The semicolon case matters for more than tidiness: multi-head units are exactly
	 * the ones covering both carriageways, so dropping them would draw a single arrow on
	 * a camera that in fact watches every direction.
	 */
	@NonNull
	static float[] parseDirections(@Nullable String raw) {
		if (raw == null || raw.isEmpty()) {
			return new float[0];
		}
		String[] tokens = raw.split(";");
		List<Float> out = new ArrayList<>(tokens.length);
		for (String token : tokens) {
			String t = token.trim();
			if (t.isEmpty()) {
				continue;
			}
			int dash = t.indexOf('-', 1);           // from 1: a leading '-' is a negative bearing
			try {
				if (dash > 0) {
					float a = Float.parseFloat(t.substring(0, dash).trim());
					float b = Float.parseFloat(t.substring(dash + 1).trim());
					float span = ((b - a) % 360f + 360f) % 360f;
					out.add(((a + span / 2f) % 360f + 360f) % 360f);
				} else {
					out.add((Float.parseFloat(t) % 360f + 360f) % 360f);
				}
			} catch (NumberFormatException e) {
				// Cardinal names and junk (serial numbers do appear in this field) are
				// simply skipped — the camera is still drawn, just without an arrow.
			}
		}
		float[] arr = new float[out.size()];
		for (int i = 0; i < arr.length; i++) {
			arr[i] = out.get(i);
		}
		return arr;
	}
}
