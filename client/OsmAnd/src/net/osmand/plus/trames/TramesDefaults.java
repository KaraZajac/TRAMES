package net.osmand.plus.trames;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;

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
 * Seeds the out-of-the-box TRAMES configuration: camera-avoiding routing that works
 * without sending anything to a server.
 *
 * <p><b>Offline by default, for privacy.</b> The car profile routes with OsmAnd's
 * offline engine against ALPR-tagged maps ({@link TramesMapsDialog}), where camera
 * avoidance comes from the {@code avoid_alpr} rule baked into {@code routing.xml}.
 * Online routing is still available and still avoids cameras, but selecting it by
 * default would be incoherent for this app: every route request would hand a server the
 * user's origin, destination and timing — precisely the movement record the app exists
 * to keep people out of. Avoiding cameras while streaming your itinerary to a host is
 * not privacy, so the private path is the default and the network is opt-in.
 *
 * <p>The online TRAMES engine is still seeded, just not selected. It stays one tap away
 * in the car profile's navigation settings for users who want fresher camera data than
 * their downloaded map, or who have no map for where they are.
 *
 * <p><b>Trade-off, stated plainly:</b> offline routing needs a downloaded map. With none
 * present the router cannot produce a route at all — so a fresh install must visit
 * "TRAMES offline maps" in the drawer first. That is a real first-run cost, accepted
 * deliberately: the alternative silently routes everyone through a server.
 *
 * <p>Each step runs exactly once, guarded by its own global preference. A user who later
 * picks a different engine has made a decision, and re-seeding on every start would
 * fight it. The offline switch is applied to existing installs too, but only where the
 * car profile is still on the online engine <em>this class itself</em> selected in an
 * earlier version — never over a choice the user made.
 *
 * <p>Everything else is already default-on: the camera map layer draws whenever the map
 * is past its minimum zoom, {@code avoid_alpr} defaults to true in the routing config,
 * and a seeded engine with no stored berth routes at
 * {@link TramesEngine#getDefaultBerthLevel()} (STRONG) — the same strength the offline
 * rule uses, so the two paths agree.
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

	/**
	 * Guards the one-time switch to offline-by-default. A separate key from
	 * {@link #SEEDED_PREF} on purpose: installs seeded by an earlier version already have
	 * that flag set, and reusing it would skip the switch for exactly the users who need
	 * it — the ones currently pointed at the online engine.
	 */
	private static final String OFFLINE_DEFAULT_PREF = "trames_offline_default_applied";

	private TramesDefaults() {
	}

	public static void ensureSeeded(@NonNull OsmandApplication app) {
		OnlineRoutingEngine engine = ensureEngine(app);
		ensureOfflineDefault(app, engine);
	}

	/**
	 * Make sure a TRAMES online engine exists, without selecting it. Returns the engine
	 * (existing or newly seeded), or {@code null} if none could be established.
	 */
	@Nullable
	private static OnlineRoutingEngine ensureEngine(@NonNull OsmandApplication app) {
		OnlineRoutingHelper helper = app.getOnlineRoutingHelper();

		// Adopt an existing TRAMES engine before creating one. v1.0.0 users configured
		// theirs by hand, possibly with a custom berth or a self-hosted URL — replacing
		// it with a fresh default would discard that for no benefit.
		for (OnlineRoutingEngine candidate : helper.getEngines()) {
			if (candidate instanceof TramesEngine) {
				return candidate;
			}
		}

		CommonPreference<Boolean> seeded =
				app.getSettings().registerBooleanPreference(SEEDED_PREF, false).makeGlobal();
		if (seeded.get()) {
			// Already seeded once and the engine is gone: the user deleted it. Respect that.
			return null;
		}

		Map<String, String> params = new HashMap<>();
		params.put(EngineParameter.KEY.name(), ENGINE_KEY);
		params.put(EngineParameter.VEHICLE_KEY.name(), "car");
		params.put(EngineParameter.TRAMES_BERTH.name(),
				String.valueOf(TramesEngine.getDefaultBerthLevel()));
		OnlineRoutingEngine engine = new TramesEngine(params);
		helper.saveEngine(engine);
		seeded.set(true);
		return engine;
	}

	/**
	 * Point the car profile at the offline engine, once.
	 *
	 * <p>On a fresh install this is a no-op in practice — OsmAnd's own default is already
	 * {@link RouteService#OSMAND} — but it is set explicitly so the fork's intent doesn't
	 * depend on an upstream default staying put.
	 *
	 * <p>On an upgrade it only moves a profile that an earlier TRAMES version pointed at
	 * the online engine. If the user has since chosen anything else — a different online
	 * engine, BRouter, straight-line — that is their call and it is left alone.
	 */
	private static void ensureOfflineDefault(@NonNull OsmandApplication app,
	                                         @Nullable OnlineRoutingEngine engine) {
		CommonPreference<Boolean> applied =
				app.getSettings().registerBooleanPreference(OFFLINE_DEFAULT_PREF, false).makeGlobal();
		if (applied.get()) {
			return;
		}

		RouteService current = ApplicationMode.CAR.getRouteService();
		boolean onOurSeededEngine = current == RouteService.ONLINE && engine != null
				&& engine.getStringKey().equals(ApplicationMode.CAR.getRoutingProfile());

		if (current != RouteService.ONLINE || onOurSeededEngine) {
			ApplicationMode.CAR.setRouteService(RouteService.OSMAND);
			ApplicationMode.CAR.setRoutingProfile(ApplicationMode.CAR.getDefaultRoutingProfile());
		}

		applied.set(true);
	}
}
