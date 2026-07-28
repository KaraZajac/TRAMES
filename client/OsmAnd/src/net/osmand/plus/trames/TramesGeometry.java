package net.osmand.plus.trames;

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
 */
public final class TramesGeometry {

	private TramesGeometry() {
	}

	/** Cone length in metres. Mirrors {@code build_cones.py} {@code DEFAULT_RADIUS_M}. */
	public static final double CONE_RADIUS_M = 60.0;

	/** Full field-of-view width in degrees. Mirrors {@code build_cones.py} {@code DEFAULT_SPAN_DEG}. */
	public static final double CONE_SPAN_DEG = 45.0;

	/** Half the field of view, for point-in-wedge tests. */
	public static final double CONE_HALF_SPAN_DEG = CONE_SPAN_DEG / 2.0;
}
