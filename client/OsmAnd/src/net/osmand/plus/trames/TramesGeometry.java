package net.osmand.plus.trames;

import java.util.Arrays;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The one place the ALPR camera field-of-view geometry is defined on the client, so the
 * map cones and the route-exposure count can never disagree with each other — or with the
 * router.
 *
 * <p>These values mirror the server's routing cones in {@code server/alpr/build_cones.py}
 * ({@code DEFAULT_RADIUS_M = 60}, {@code DEFAULT_SPAN_DEG = 45}) — the exact geometry the
 * GraphHopper graph is built with and the geometry the paper scores against. That is why
 * they are what they are, and not a display preference: the wedge the map draws is the
 * wedge the router avoided and the wedge "N cameras can see this route" is counted with.
 *
 * <p><b>If you change these, change {@code build_cones.py} too and re-import the graph</b>
 * — otherwise the client would model a different camera than the server routes around.
 *
 * <p>Plain Java on purpose (no Android types), so the parsing and the route test can be
 * checked against the cone builder off the device.
 */
public final class TramesGeometry {

	private TramesGeometry() {
	}

	/** Cone length in metres. Mirrors {@code build_cones.py} {@code DEFAULT_RADIUS_M}. */
	public static final double CONE_RADIUS_M = 60.0;

	/**
	 * Field-of-view width in degrees for a camera mapped with a single bearing. Mirrors
	 * {@code build_cones.py} {@code DEFAULT_SPAN_DEG}. A camera mapped with an arc keeps the
	 * arc's own width, as the builder gives it.
	 */
	public static final double CONE_SPAN_DEG = 45.0;

	/** Metres per degree of latitude: the flat projection build_cones.py draws its cones in. */
	private static final double M_PER_DEG = 111320.0;

	/** The 16 compass points, in 22.5-degree steps from north — as the cone builder reads them. */
	private static final String[] COMPASS_POINTS = {"N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
			"S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"};

	/** An arc range, exactly as build_cones.py's {@code ARC_RE} matches one. */
	private static final Pattern ARC = Pattern.compile("^\\s*(\\d+(?:\\.\\d+)?)\\s*-\\s*(\\d+(?:\\.\\d+)?)\\s*$");

	/** A camera's heads: the bearing each one faces, and the full width it watches. */
	public static final class Heads {

		static final Heads NONE = new Heads(new float[0], new float[0]);

		/** Degrees clockwise from north, one per head; empty when the camera has no direction. */
		public final float[] bearings;
		/** Full width of each head's view in degrees, parallel to {@link #bearings}; 360 is all round. */
		public final float[] spans;

		Heads(float[] bearings, float[] spans) {
			this.bearings = bearings;
			this.spans = spans;
		}
	}

	/**
	 * Parse an OSM direction value into zero or more heads, each with its width.
	 *
	 * <p>Handles the shapes that actually occur:
	 * <pre>
	 *   "137"                single bearing             -> 45° wide
	 *   "144-189"            arc range                  -> centred on its midpoint, as wide as the arc
	 *   "338-23"             arc wrapping past 0 degrees
	 *   "0-360"              arc that closes on itself  -> all round
	 *   "320;190"            multi-head unit            -> two heads
	 *   "0;72;144;216;288"   five-head 360 unit         -> five heads
	 *   "NE", "wsw"          compass point              -> its bearing, 45° wide
	 * </pre>
	 *
	 * <p>The semicolon case matters for more than tidiness: multi-head units are exactly
	 * the ones covering both carriageways, so dropping them would draw a single arrow on
	 * a camera that in fact watches every direction.
	 *
	 * <p>Every value must come out as the cone builder that tags the maps reads it
	 * ({@code server/alpr/build_cones.py}, {@code parse_directions}), or the map and the
	 * route readout disagree with the router about what a camera watches. Until v1.2.5
	 * compass points were skipped here though the builder reads them (401 cameras of the
	 * September 2026 pack). Until v1.2.7 every head was drawn and counted 45° wide: the 638
	 * cameras of that pack mapped with a wider or narrower arc were not, and the 31 mapped
	 * all round ("0-360") were drawn as a wedge facing north.
	 */
	public static Heads parseHeads(String raw) {
		if (raw == null || raw.isEmpty()) {
			return Heads.NONE;
		}
		String[] tokens = raw.split(";");
		float[] bearings = new float[tokens.length];
		float[] spans = new float[tokens.length];
		int n = 0;
		for (String token : tokens) {
			String t = token.trim();
			if (t.isEmpty()) {
				continue;
			}
			// Doubles, as the builder's Python floats: junk such as "4555555555" lands on the
			// same bearing only if no digits are lost on the way.
			Matcher arc = ARC.matcher(t);
			if (arc.matches()) {
				double a = Double.parseDouble(arc.group(1));
				double b = Double.parseDouble(arc.group(2));
				double span = ((b - a) % 360.0 + 360.0) % 360.0;
				if (span == 0 && a != b) {
					bearings[n] = (float) (a % 360.0);
					spans[n++] = 360f;
					continue;
				}
				if (span == 0) {
					span = CONE_SPAN_DEG;
				}
				bearings[n] = (float) ((a + span / 2.0) % 360.0);
				spans[n++] = (float) span;
				continue;
			}
			try {
				bearings[n] = (float) ((Double.parseDouble(t) % 360.0 + 360.0) % 360.0);
				spans[n++] = (float) CONE_SPAN_DEG;
			} catch (NumberFormatException e) {
				float compass = compassBearing(t);
				if (compass >= 0) {
					bearings[n] = compass;
					spans[n++] = (float) CONE_SPAN_DEG;
				}
				// Anything else is junk (serial numbers do appear in this field) and is
				// skipped — the camera is still drawn, just without a wedge.
			}
		}
		return n == 0 ? Heads.NONE : new Heads(Arrays.copyOf(bearings, n), Arrays.copyOf(spans, n));
	}

	/** "NE" -> 45, "wsw" -> 247.5; -1 for anything that is not one of the 16 compass points. */
	static float compassBearing(String token) {
		String key = token.toUpperCase(Locale.ROOT).replace(" ", "");
		for (int i = 0; i < COMPASS_POINTS.length; i++) {
			if (COMPASS_POINTS[i].equals(key)) {
				return i * 22.5f;
			}
		}
		return -1;
	}

	/**
	 * Whether a camera's field of view meets a route anywhere along it — on the lines
	 * between the route's points as well as at them. Computed in the flat projection, centred
	 * on the camera, that build_cones.py draws the router's cones in, so a hit here is a hit
	 * on the wedge the router avoided (to within the few centimetres by which the builder's
	 * eight-sided arc falls inside the true one).
	 *
	 * @param allRoundIfNoBearing count a camera with no heads as watching every direction
	 *                            within {@link #CONE_RADIUS_M}
	 */
	public static boolean seesLine(double camLat, double camLon, float[] bearings, float[] spans,
	                               boolean allRoundIfNoBearing, double[] lat, double[] lon) {
		int n = Math.min(lat.length, lon.length);
		if (n == 0 || (bearings.length == 0 && !allRoundIfNoBearing)) {
			return false;
		}
		double mLat = M_PER_DEG;
		double mLon = Math.max(M_PER_DEG * Math.cos(Math.toRadians(camLat)), 1.0);
		double padLat = CONE_RADIUS_M / mLat;
		double padLon = CONE_RADIUS_M / mLon;
		// One segment per pair of consecutive points; a one-point route is one point.
		for (int i = 0, segments = Math.max(n - 1, 1); i < segments; i++) {
			int j = Math.min(i + 1, n - 1);
			if (Math.min(lat[i], lat[j]) > camLat + padLat || Math.max(lat[i], lat[j]) < camLat - padLat
					|| Math.min(lon[i], lon[j]) > camLon + padLon || Math.max(lon[i], lon[j]) < camLon - padLon) {
				continue;
			}
			double ax = (lon[i] - camLon) * mLon;
			double ay = (lat[i] - camLat) * mLat;
			double bx = (lon[j] - camLon) * mLon;
			double by = (lat[j] - camLat) * mLat;
			if (bearings.length == 0) {
				if (segmentMeetsDisc(ax, ay, bx, by, CONE_RADIUS_M)) {
					return true;
				}
				continue;
			}
			for (int h = 0; h < bearings.length; h++) {
				boolean hit = spans[h] >= 360f
						? segmentMeetsDisc(ax, ay, bx, by, CONE_RADIUS_M)
						: segmentMeetsWedge(ax, ay, bx, by, CONE_RADIUS_M, bearings[h], spans[h] / 2.0);
				if (hit) {
					return true;
				}
			}
		}
		return false;
	}

	/** Segment A-B against the disc of radius r about the origin (metres, x east, y north). */
	private static boolean segmentMeetsDisc(double ax, double ay, double bx, double by, double r) {
		double dx = bx - ax;
		double dy = by - ay;
		double len2 = dx * dx + dy * dy;
		double t = len2 == 0 ? 0 : Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2));
		double px = ax + t * dx;
		double py = ay + t * dy;
		return px * px + py * py <= r * r;
	}

	/**
	 * Segment A-B against the sector with its apex at the origin, radius r, facing
	 * {@code bearing} and opening {@code half} degrees either side. The sector is a closed
	 * region, so the segment meets it if an end lies inside or the segment crosses its
	 * boundary: one of the two straight edges, or the arc.
	 */
	private static boolean segmentMeetsWedge(double ax, double ay, double bx, double by,
	                                         double r, double bearing, double half) {
		if (inWedge(ax, ay, r, bearing, half) || inWedge(bx, by, r, bearing, half)) {
			return true;
		}
		double left = Math.toRadians(bearing - half);
		double right = Math.toRadians(bearing + half);
		if (segmentsMeet(ax, ay, bx, by, 0, 0, r * Math.sin(left), r * Math.cos(left))
				|| segmentsMeet(ax, ay, bx, by, 0, 0, r * Math.sin(right), r * Math.cos(right))) {
			return true;
		}
		// The arc: where the segment crosses the circle, is the crossing inside the wedge?
		double dx = bx - ax;
		double dy = by - ay;
		double qa = dx * dx + dy * dy;
		if (qa == 0) {
			return false;
		}
		double qb = 2 * (ax * dx + ay * dy);
		double qc = ax * ax + ay * ay - r * r;
		double disc = qb * qb - 4 * qa * qc;
		if (disc < 0) {
			return false;
		}
		double root = Math.sqrt(disc);
		double t1 = (-qb - root) / (2 * qa);
		double t2 = (-qb + root) / (2 * qa);
		return (t1 >= 0 && t1 <= 1 && withinAngle(ax + t1 * dx, ay + t1 * dy, bearing, half))
				|| (t2 >= 0 && t2 <= 1 && withinAngle(ax + t2 * dx, ay + t2 * dy, bearing, half));
	}

	private static boolean inWedge(double x, double y, double r, double bearing, double half) {
		double d2 = x * x + y * y;
		return d2 <= r * r && (d2 == 0 || withinAngle(x, y, bearing, half));
	}

	/** Is the direction from the origin to (x, y) within {@code half} degrees of {@code bearing}? */
	private static boolean withinAngle(double x, double y, double bearing, double half) {
		double toPoint = Math.toDegrees(Math.atan2(x, y));     // clockwise from north
		return Math.abs((toPoint - bearing + 540.0) % 360.0 - 180.0) <= half;
	}

	/** Do the closed segments A-B and C-D share a point (crossing, touching or overlapping)? */
	private static boolean segmentsMeet(double ax, double ay, double bx, double by,
	                                    double cx, double cy, double dx, double dy) {
		double d1 = side(cx, cy, dx, dy, ax, ay);
		double d2 = side(cx, cy, dx, dy, bx, by);
		double d3 = side(ax, ay, bx, by, cx, cy);
		double d4 = side(ax, ay, bx, by, dx, dy);
		if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))) {
			return true;
		}
		return (d1 == 0 && between(cx, cy, dx, dy, ax, ay)) || (d2 == 0 && between(cx, cy, dx, dy, bx, by))
				|| (d3 == 0 && between(ax, ay, bx, by, cx, cy)) || (d4 == 0 && between(ax, ay, bx, by, dx, dy));
	}

	/** Which side of the line O-P the point Q lies on: the sign of the cross product. */
	private static double side(double ox, double oy, double px, double py, double qx, double qy) {
		return (px - ox) * (qy - oy) - (py - oy) * (qx - ox);
	}

	/** For a point Q on the line O-P: does it lie between O and P? */
	private static boolean between(double ox, double oy, double px, double py, double qx, double qy) {
		return Math.min(ox, px) <= qx && qx <= Math.max(ox, px)
				&& Math.min(oy, py) <= qy && qy <= Math.max(oy, py);
	}
}
