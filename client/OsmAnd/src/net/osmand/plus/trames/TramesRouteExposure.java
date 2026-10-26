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
 * <p>A camera counts as passing you if the route's line crosses its field-of-view wedge
 * anywhere. Direction is decisive: a camera watching northbound traffic does not see you on
 * the southbound carriageway, and counting it would overstate exposure and make a good
 * route look bad.
 *
 * <p>Until v1.2.7 only the route's points were tested, and a road's points can lie well
 * apart: a 60 m wedge across a straight stretch fits between two of them. On 150 Northern
 * Virginia commutes (October 2026), 7 of the 111 routes the readout called clear at the
 * Strong level, then the default, crossed a wedge.
 */
public class TramesRouteExposure {

	/**
	 * A camera with no usable facing (2,948 of the 142,991 in the September 2026 pack) has no
	 * cone, so the router does not avoid it. The readout counts it as watching every direction
	 * within the capture radius, so a route that passes close by one is never reported clean.
	 */
	private static final boolean OMNI_IF_UNKNOWN = true;

	public static class Result {
		public final int seenBy;
		public final int consideredCameras;

		Result(int seenBy, int consideredCameras) {
			this.seenBy = seenBy;
			this.consideredCameras = consideredCameras;
		}
	}

	/**
	 * Capture radius and wedge widths come from {@link TramesGeometry} — the same 60 m wedges
	 * the router avoids and the map draws, so "seen" here means exactly what "avoided" meant.
	 */
	@NonNull
	public static Result count(@NonNull List<Location> route,
	                           @NonNull List<TramesCameraSource.Camera> cameras) {
		if (route.isEmpty() || cameras.isEmpty()) {
			return new Result(0, cameras.size());
		}
		double[] lat = new double[route.size()];
		double[] lon = new double[route.size()];
		for (int i = 0; i < lat.length; i++) {
			Location loc = route.get(i);
			lat[i] = loc.getLatitude();
			lon[i] = loc.getLongitude();
		}
		int seen = 0;
		for (TramesCameraSource.Camera cam : cameras) {
			if (TramesGeometry.seesLine(cam.lat, cam.lon, cam.directions, cam.spans,
					OMNI_IF_UNKNOWN, lat, lon)) {
				seen++;
			}
		}
		return new Result(seen, cameras.size());
	}
}
