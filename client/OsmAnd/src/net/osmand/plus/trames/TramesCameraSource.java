package net.osmand.plus.trames;

import android.os.AsyncTask;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;

import net.osmand.data.LatLon;
import net.osmand.data.QuadRect;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Supplies ALPR (licence-plate reader) camera positions to the map layer and the route
 * exposure count, from the on-device camera pack ({@link TramesCameraStore}).
 *
 * <p><b>It never uses the network.</b> Until v1.2.4 this class could also ask a server — the
 * TRAMES camera service, or a public Overpass instance — for the cameras in a bounding box
 * around the current view, which is to say for the user's location; it was allowed to only
 * while a profile routed online. The app no longer routes online, and the pack is the same
 * snapshot the offline maps were built from, so the map shows exactly the cameras the
 * router avoids. With no network path left, there is no location for a camera lookup to
 * leak.
 *
 * <p>Tag handling follows what OSM actually contains rather than what the wiki recommends
 * — see {@link #parseDirections}. Measured over 3,899 cameras in one metro:
 * {@code direction} appears 3,864 times against 21 for {@code camera:direction}.
 */
public class TramesCameraSource {

	/**
	 * Below this zoom the visible area covers thousands of cameras — too many to read. The
	 * layer draws nothing lower.
	 */
	public static final int MIN_ZOOM = 12;

	/** Re-read once the map has moved roughly this far from the last read's centre. */
	private static final double REFETCH_DEG = 0.05;

	/** Padding around the visible box so panning slightly doesn't trigger a re-read. */
	private static final double PAD_DEG = 0.03;

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

	/** The on-device pack. Set by the layer; while it is null, there is nothing to draw. */
	@Nullable
	private volatile TramesCameraStore store;

	public void setStore(@Nullable TramesCameraStore store) {
		this.store = store;
	}

	/**
	 * True when nothing can supply cameras — no pack on disk. Distinct from "read the pack and
	 * there are none here": the UI must be able to tell an empty area from an unanswerable
	 * question, because a blank map over surveilled streets reads as safety.
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
	 * Read the cameras for the given view from the pack, if the map has moved far enough to
	 * warrant it. Cheap and safe to call on every frame — it self-throttles.
	 */
	public void ensureLoaded(@NonNull QuadRect visibleBox, int zoom, @Nullable Runnable onLoaded) {
		if (zoom < MIN_ZOOM || fetching.get()) {
			return;
		}
		TramesCameraStore local = store;
		if (local == null || !local.isPresent()) {
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
				return local.forBox(south, west, north, east);
			}

			@Override
			protected void onPostExecute(List<Camera> result) {
				cameras = result;
				lastCentre = new LatLon(cLat, cLon);
				fetching.set(false);
				if (onLoaded != null) {
					onLoaded.run();
				}
			}
		}.executeOnExecutor(AsyncTask.THREAD_POOL_EXECUTOR);
	}

	/**
	 * Every camera within the given bounds, for scoring a whole route rather than just the
	 * visible map. Synchronous — call it off the UI thread. Independent of the view cache,
	 * so it never disturbs what the map is currently drawing.
	 *
	 * <p>Returns {@code null} when there is no pack: an empty list means the route genuinely
	 * passes no known cameras, which the caller must not confuse with "we could not check".
	 */
	@Nullable
	public List<Camera> fetchForRouteSync(double south, double west, double north, double east) {
		TramesCameraStore pack = store;
		return pack != null && pack.isPresent() ? pack.forBox(south, west, north, east) : null;
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
