package net.osmand.plus.onlinerouting.engine;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;

import net.osmand.data.LatLon;
import net.osmand.plus.R;
import net.osmand.plus.onlinerouting.EngineParameter;
import net.osmand.plus.onlinerouting.VehicleType;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static net.osmand.plus.onlinerouting.engine.EngineType.TRAMES_TYPE;
import static net.osmand.util.Algorithms.isEmpty;

/**
 * TRAMES routing engine — GraphHopper with ALPR (licence-plate reader) avoidance.
 *
 * <p>Extends {@link GraphhopperEngine} rather than reimplementing it: the response
 * format is plain GraphHopper, so the encoded-polyline parsing, instruction mapping and
 * turn-type translation are all inherited unchanged. Only the request differs.
 *
 * <p><b>Why POST.</b> The avoidance strength is a per-request GraphHopper
 * {@code custom_model}, which cannot travel on a GET query string. The base
 * {@link OnlineRoutingEngine} already supports POST — {@link OrsEngine} does the same —
 * so this needs only {@link #getHTTPMethod()}, {@link #getRequestHeaders()} and
 * {@link #getRequestBody}.
 *
 * <p><b>How avoidance works.</b> The TRAMES GraphHopper instance bakes every known
 * camera's field-of-view cone into the routing graph at import time as a single custom
 * area named {@code alpr}. A request then only has to reference it:
 *
 * <pre>{@code {"priority": [{"if": "in_alpr", "multiply_by": "0.05"}]}}</pre>
 *
 * No geometry travels with the request, so the payload stays tiny regardless of how many
 * cameras exist — there are ~120k in North America — and the strength stays a per-request
 * knob instead of being frozen into the graph.
 *
 * <p><b>Why ch.disable.</b> Contraction Hierarchies bake the weighting into the prepared
 * graph, so a per-request custom model requires dropping out of speed mode. The server
 * has Landmarks (LM) prepared for exactly this, which keeps hybrid-mode routing fast:
 * measured 20-46 ms for routes up to 1,085 km and 161 ms coast-to-coast.
 */
public class TramesEngine extends GraphhopperEngine {

	/**
	 * The custom area baked into the graph at import. Must match the GeoJSON feature id
	 * produced by server/alpr/build_cones.py — if these ever disagree the request still
	 * succeeds and simply avoids nothing, which is the worst kind of failure here.
	 */
	private static final String ALPR_AREA = "alpr";

	/**
	 * Priority multipliers for each avoidance level, weakest to strongest.
	 *
	 * <p>These are NOT evenly spaced, and that is deliberate. Measured against the
	 * continental graph, everything from 1.0 down to about 0.3 leaves most routes
	 * completely unchanged — the penalty simply cannot outweigh a highway. The useful
	 * range is roughly 0.3 to 0.01, so the levels are distributed across that instead of
	 * across 0..1, where two thirds of the control would do nothing.
	 *
	 * <p>Note the effective range shifts with camera density: in a dense metro 0.3
	 * already avoids most cameras, while in sparse rural areas it can still be inert.
	 */
	private static final String[] BERTH_MULTIPLIERS = {
			null,      // OFF      — no custom model at all
			"0.3",     // LIGHT
			"0.1",     // MODERATE
			"0.05",    // STRONG
			"0.01"     // MAXIMUM  — avoids every camera it can reach an alternative around
	};

	/**
	 * On-screen labels, index-aligned with {@link #BERTH_MULTIPLIERS}. Deliberately named
	 * rather than numeric: the multipliers are non-linear and density-dependent, so a raw
	 * "0.05" tells a user nothing useful about what they'll get.
	 */
	public static final int[] BERTH_LABEL_RES = {
			R.string.trames_berth_off,
			R.string.trames_berth_light,
			R.string.trames_berth_moderate,
			R.string.trames_berth_strong,
			R.string.trames_berth_maximum
	};

	private static final int DEFAULT_BERTH = 3;   // STRONG

	/** Default avoidance level, used when nothing has been stored yet. */
	public static int getDefaultBerthLevel() {
		return DEFAULT_BERTH;
	}

	/** Clamp an arbitrary stored value to a valid level index. */
	public static int clampBerthLevel(int level) {
		if (level < 0) {
			return 0;
		}
		return level >= BERTH_MULTIPLIERS.length ? BERTH_MULTIPLIERS.length - 1 : level;
	}

	public TramesEngine(@Nullable Map<String, String> params) {
		super(params);
	}

	@NonNull
	@Override
	public OnlineRoutingEngine getType() {
		return TRAMES_TYPE;
	}

	@NonNull
	@Override
	public String getTitle() {
		return "TRAMES (ALPR avoidance)";
	}

	@NonNull
	@Override
	public String getTypeName() {
		return "TRAMES";
	}

	/**
	 * Public TRAMES routing endpoint. Token-gated — an unauthenticated request gets 403,
	 * so the API key field is not optional. Self-hosters override this with a custom URL;
	 * see https://github.com/KaraZajac/TRAMES-server.
	 */
	@NonNull
	@Override
	public String getStandardUrl() {
		return "https://routing.blackflagintel.com/route";
	}

	@Override
	public OnlineRoutingEngine newInstance(Map<String, String> params) {
		return new TramesEngine(params);
	}

	@Override
	protected void collectAllowedParameters(@NonNull Set<EngineParameter> params) {
		params.add(EngineParameter.KEY);
		params.add(EngineParameter.VEHICLE_KEY);
		params.add(EngineParameter.CUSTOM_NAME);
		params.add(EngineParameter.NAME_INDEX);
		params.add(EngineParameter.CUSTOM_URL);
		params.add(EngineParameter.API_KEY);
		params.add(EngineParameter.TRAMES_BERTH);
	}

	@Override
	protected void collectAllowedVehicles(@NonNull List<VehicleType> vehicles) {
		// Only profiles the TRAMES GraphHopper instance actually has prepared. Offering
		// bike/foot here would produce requests the server rejects with a 400.
		vehicles.add(new VehicleType("car", R.string.routing_engine_vehicle_type_car));
	}

	@NonNull
	@Override
	public String getHTTPMethod() {
		return "POST";
	}

	@NonNull
	@Override
	public Map<String, String> getRequestHeaders() {
		Map<String, String> headers = new HashMap<>();
		headers.put("Content-Type", "application/json");
		// The proxy in front of GraphHopper authenticates with a scoped, revocable
		// token. Same arrangement OVERWATCH uses for its Waze feed: the token is entered
		// in settings and stored on-device, never baked into the APK.
		String apiKey = get(EngineParameter.API_KEY);
		if (!isEmpty(apiKey)) {
			headers.put("X-App-Token", apiKey);
		}
		return headers;
	}

	/**
	 * For POST the endpoint takes everything in the body, so unlike the GET-based
	 * GraphHopper engine no points are appended here. Overriding to a no-op rather than
	 * inheriting matters: the parent would otherwise build a URL carrying a full
	 * duplicate copy of the route request.
	 */
	@Override
	protected void makeFullUrl(@NonNull StringBuilder sb, @NonNull List<LatLon> path,
	                           @Nullable Float startBearing) {
	}

	@Nullable
	@Override
	public String getRequestBody(@NonNull List<LatLon> path, @Nullable Float startBearing)
			throws JSONException {
		JSONObject body = new JSONObject();

		JSONArray points = new JSONArray();
		for (LatLon p : path) {
			// GraphHopper takes [lon, lat] — reversed from OsmAnd's LatLon ordering.
			points.put(new JSONArray().put(p.getLongitude()).put(p.getLatitude()));
		}
		body.put("points", points);

		String vehicle = getVehicleKeyForUrl();
		body.put("profile", isEmpty(vehicle) ? "car" : vehicle);

		// Required for any per-request custom model: CH has the weighting baked in.
		body.put("ch.disable", true);

		// Leave points_encoded at its default (true). The inherited GraphHopper parser
		// reads root.getString("points") as a precision-5 encoded polyline; asking for
		// raw coordinates instead would hand it a JSONObject and break parsing.

		String multiplier = berthMultiplier();
		if (multiplier != null) {
			JSONObject rule = new JSONObject()
					.put("if", "in_" + ALPR_AREA)
					.put("multiply_by", multiplier);
			JSONObject customModel = new JSONObject()
					.put("priority", new JSONArray().put(rule));
			body.put("custom_model", customModel);
		}

		return body.toString();
	}

	/** @return the priority multiplier for the configured level, or null when OFF. */
	@Nullable
	private String berthMultiplier() {
		int level = DEFAULT_BERTH;
		String raw = get(EngineParameter.TRAMES_BERTH);
		if (!isEmpty(raw)) {
			try {
				level = Integer.parseInt(raw);
			} catch (NumberFormatException e) {
				// Fall through to the default rather than failing the route. A bad
				// stored preference should degrade to "still navigates", not "no route".
				level = DEFAULT_BERTH;
			}
		}
		if (level < 0) {
			level = 0;
		} else if (level >= BERTH_MULTIPLIERS.length) {
			level = BERTH_MULTIPLIERS.length - 1;
		}
		return BERTH_MULTIPLIERS[level];
	}
}
