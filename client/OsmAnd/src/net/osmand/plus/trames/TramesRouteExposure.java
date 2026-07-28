package net.osmand.plus.trames;

import androidx.annotation.NonNull;

import net.osmand.Location;

import java.util.List;

/**
 * Counts how many ALPR cameras can actually see a given route.
 *
 * <p>Distance and time cannot tell a user whether avoidance worked — a longer route is
 * not necessarily a less-surveilled one. This is the number the feature is actually
 * selling, and it mirrors the server-side scorer (server/alpr/score_route.py) so the app
 * and the routing backend agree about what "passed a camera" means.
 *
 * <p>A camera counts as passing you if a route point falls inside its field-of-view
 * wedge. Direction is decisive: a camera watching northbound traffic does not see you on
 * the southbound carriageway, and counting it would overstate exposure and make a good
 * route look bad.
 */
public class TramesRouteExposure {

	/**
	 * Capture radius and half-angle come from {@link TramesGeometry} — the same 60 m / 45°
	 * wedge the router avoids and the map draws, so "seen" here means exactly what "avoided"
	 * meant on the server. This scorer is a per-point approximation of the server's
	 * line-intersects-cone test in {@code score_route.py}: identical geometry, cheaper maths.
	 */
	private static final double CAPTURE_M = TramesGeometry.CONE_RADIUS_M;

	/** Half the capture wedge; the full 45° is what OSM contributors record. */
	private static final double HALF_SPAN_DEG = TramesGeometry.CONE_HALF_SPAN_DEG;

	/** Cameras with no direction tag are treated as watching everything. */
	private static final boolean OMNI_IF_UNKNOWN = true;

	public static class Result {
		public final int seenBy;
		public final int consideredCameras;

		Result(int seenBy, int consideredCameras) {
			this.seenBy = seenBy;
			this.consideredCameras = consideredCameras;
		}
	}

	@NonNull
	public static Result count(@NonNull List<Location> route,
	                           @NonNull List<TramesCameraSource.Camera> cameras) {
		if (route.isEmpty() || cameras.isEmpty()) {
			return new Result(0, cameras.size());
		}
		int seen = 0;
		for (TramesCameraSource.Camera cam : cameras) {
			if (sees(cam, route)) {
				seen++;
			}
		}
		return new Result(seen, cameras.size());
	}

	private static boolean sees(@NonNull TramesCameraSource.Camera cam,
	                            @NonNull List<Location> route) {
		// Cheap latitude gate before the per-point maths: 0.001 deg is ~111 m, safely
		// beyond the 60 m capture radius, so anything outside that band cannot qualify.
		double latPad = 0.001;
		for (Location loc : route) {
			if (Math.abs(loc.getLatitude() - cam.lat) > latPad) {
				continue;
			}
			double dist = haversine(cam.lat, cam.lon, loc.getLatitude(), loc.getLongitude());
			if (dist > CAPTURE_M) {
				continue;
			}
			if (cam.directions.length == 0) {
				if (OMNI_IF_UNKNOWN) {
					return true;
				}
				continue;
			}
			double bearingToPoint = bearing(cam.lat, cam.lon, loc.getLatitude(), loc.getLongitude());
			for (float dir : cam.directions) {
				double delta = Math.abs(angleDiff(bearingToPoint, dir));
				if (delta <= HALF_SPAN_DEG) {
					return true;
				}
			}
		}
		return false;
	}

	/** Signed smallest difference between two bearings, in degrees. */
	private static double angleDiff(double a, double b) {
		double d = (a - b + 540.0) % 360.0 - 180.0;
		return d;
	}

	private static double bearing(double lat1, double lon1, double lat2, double lon2) {
		double p1 = Math.toRadians(lat1);
		double p2 = Math.toRadians(lat2);
		double dl = Math.toRadians(lon2 - lon1);
		double y = Math.sin(dl) * Math.cos(p2);
		double x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
		return (Math.toDegrees(Math.atan2(y, x)) + 360.0) % 360.0;
	}

	private static double haversine(double lat1, double lon1, double lat2, double lon2) {
		double r = 6371000.0;
		double dLat = Math.toRadians(lat2 - lat1);
		double dLon = Math.toRadians(lon2 - lon1);
		double a = Math.sin(dLat / 2) * Math.sin(dLat / 2)
				+ Math.cos(Math.toRadians(lat1)) * Math.cos(Math.toRadians(lat2))
				* Math.sin(dLon / 2) * Math.sin(dLon / 2);
		return 2 * r * Math.asin(Math.min(1.0, Math.sqrt(a)));
	}
}
