package net.osmand.plus.trames;

import androidx.annotation.NonNull;

import net.osmand.plus.OsmandApplication;
import net.osmand.plus.onlinerouting.EngineParameter;
import net.osmand.plus.onlinerouting.OnlineRoutingHelper;
import net.osmand.plus.onlinerouting.engine.OnlineRoutingEngine;
import net.osmand.plus.onlinerouting.engine.TramesEngine;
import net.osmand.plus.routing.RouteService;
import net.osmand.plus.settings.backend.ApplicationMode;
import net.osmand.plus.settings.backend.preferences.CommonPreference;

import java.util.HashMap;
import java.util.Map;

/**
 * Seeds the out-of-the-box TRAMES configuration on first launch: an online routing
 * engine pointing at the public TRAMES endpoint, and the car profile set to use it.
 *
 * <p>Before this, a fresh install navigated with OsmAnd's offline engine until the user
 * manually created an online routing engine, picked TRAMES as its type, and selected it
 * in the car profile's navigation settings — three screens of setup for the app's whole
 * reason to exist. ALPR avoidance is the product; it must not be opt-in homework.
 *
 * <p>Runs exactly once, guarded by a global preference — not once per missing engine.
 * The distinction matters: a user who deletes the engine or switches the car profile
 * back to offline routing has made a decision, and re-seeding on every start would
 * fight it. Upgraders who already created a TRAMES engine by hand keep it (and their
 * berth choice); the car profile is pointed at the engine they already had.
 *
 * <p>Everything else is already default-on and needs no seeding here: the camera map
 * layer draws whenever the map is past its minimum zoom, and a seeded engine with no
 * stored berth routes at {@link TramesEngine#getDefaultBerthLevel()} (STRONG).
 */
public class TramesDefaults {

	/**
	 * Stable key for the seeded engine, deliberately not {@link OnlineRoutingEngine#generateKey()}:
	 * that one embeds a timestamp, and a stable identity is what makes reseeding checks,
	 * cloud-backup round-trips and future migrations idempotent.
	 */
	public static final String ENGINE_KEY =
			OnlineRoutingEngine.ONLINE_ROUTING_ENGINE_PREFIX + "trames_default";

	private static final String SEEDED_PREF = "trames_defaults_seeded";

	private TramesDefaults() {
	}

	public static void ensureSeeded(@NonNull OsmandApplication app) {
		CommonPreference<Boolean> seeded =
				app.getSettings().registerBooleanPreference(SEEDED_PREF, false).makeGlobal();
		if (seeded.get()) {
			return;
		}

		OnlineRoutingHelper helper = app.getOnlineRoutingHelper();

		// Adopt an existing TRAMES engine before creating one. v1.0.0 users configured
		// theirs by hand, possibly with a custom berth or a self-hosted URL — replacing
		// it with a fresh default would discard that for no benefit.
		OnlineRoutingEngine engine = null;
		for (OnlineRoutingEngine candidate : helper.getEngines()) {
			if (candidate instanceof TramesEngine) {
				engine = candidate;
				break;
			}
		}
		if (engine == null) {
			Map<String, String> params = new HashMap<>();
			params.put(EngineParameter.KEY.name(), ENGINE_KEY);
			params.put(EngineParameter.VEHICLE_KEY.name(), "car");
			params.put(EngineParameter.TRAMES_BERTH.name(),
					String.valueOf(TramesEngine.getDefaultBerthLevel()));
			engine = new TramesEngine(params);
			helper.saveEngine(engine);
		}

		ApplicationMode.CAR.setRouteService(RouteService.ONLINE);
		ApplicationMode.CAR.setRoutingProfile(engine.getStringKey());

		seeded.set(true);
	}
}
