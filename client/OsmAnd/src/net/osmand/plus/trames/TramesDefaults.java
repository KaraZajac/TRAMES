package net.osmand.plus.trames;

import androidx.annotation.NonNull;

import net.osmand.PlatformUtil;
import net.osmand.plus.OsmandApplication;
import net.osmand.plus.onlinerouting.OnlineRoutingHelper;
import net.osmand.plus.onlinerouting.engine.OnlineRoutingEngine;
import net.osmand.plus.onlinerouting.engine.TramesEngine;
import net.osmand.plus.routing.RouteService;
import net.osmand.plus.settings.backend.ApplicationMode;
import net.osmand.plus.settings.backend.preferences.CommonPreference;

import org.apache.commons.logging.Log;

/**
 * The out-of-the-box TRAMES configuration: camera-avoiding routing that sends nothing to a
 * server.
 *
 * <p><b>Offline only.</b> Every profile routes with OsmAnd's offline engine against
 * ALPR-tagged maps ({@link TramesMapsDialog}), where camera avoidance comes from the
 * {@code alpr_avoidance} levels baked into {@code routing.xml}, {@code alpr_strong} by
 * default. Until v1.2.4 an online TRAMES engine was seeded as well, pointed at a public
 * server; that server was retired on 2026-08-29, and the app now has no online routing of
 * its own. Avoiding cameras while streaming your itinerary to a host is not privacy.
 *
 * <p><b>Trade-off, stated plainly:</b> offline routing needs a downloaded map. With none
 * present the router cannot produce a route at all — so a fresh install must visit
 * "TRAMES offline maps" in the drawer first. That is a real first-run cost, accepted
 * deliberately: the alternative routes everyone through a server.
 *
 * <p>Runs on every start and is idempotent: {@link #removeRetiredEngine} finds nothing once
 * it has run, and {@link #ensureOfflineDefault} is guarded by its own global preference.
 */
public class TramesDefaults {

	private static final Log LOG = PlatformUtil.getLog(TramesDefaults.class);

	/** Guards the one-time explicit switch to offline routing (see ensureOfflineDefault). */
	private static final String OFFLINE_DEFAULT_PREF = "trames_offline_default_applied";

	private TramesDefaults() {
	}

	public static void ensureSeeded(@NonNull OsmandApplication app) {
		removeRetiredEngine(app);
		ensureOfflineDefault(app);
	}

	/**
	 * Delete every TRAMES online engine an earlier version saved — the seeded one and any a
	 * user configured by hand — and move each profile that routed with one to offline
	 * routing. Such an engine reaches nothing now: the public server is retired, and the app
	 * no longer has TRAMES online routing at all, so a profile left on one could not route.
	 */
	private static void removeRetiredEngine(@NonNull OsmandApplication app) {
		OnlineRoutingHelper helper = app.getOnlineRoutingHelper();
		for (OnlineRoutingEngine engine : helper.getEngines()) {
			if (!(engine instanceof TramesEngine)) {
				continue;
			}
			String key = engine.getStringKey();
			for (ApplicationMode mode : ApplicationMode.allPossibleValues()) {
				if (mode.getRouteService() == RouteService.ONLINE && key != null
						&& key.equals(mode.getRoutingProfile())) {
					mode.setRouteService(RouteService.OSMAND);
					mode.setRoutingProfile(mode.getDefaultRoutingProfile());
				}
			}
			helper.deleteEngine(engine);
			LOG.info("TRAMES: removed the retired online engine " + key);
		}
	}

	/**
	 * Point the car profile at the offline engine explicitly, once.
	 *
	 * <p>On a fresh install this is a no-op in practice — OsmAnd's own default is already
	 * {@link RouteService#OSMAND} — but it is set so the fork's intent doesn't depend on an
	 * upstream default staying put. A profile the user has pointed at an online engine of
	 * their own is a choice they made, and it is left alone.
	 */
	private static void ensureOfflineDefault(@NonNull OsmandApplication app) {
		CommonPreference<Boolean> applied =
				app.getSettings().registerBooleanPreference(OFFLINE_DEFAULT_PREF, false).makeGlobal();
		if (applied.get()) {
			return;
		}
		if (ApplicationMode.CAR.getRouteService() != RouteService.ONLINE) {
			ApplicationMode.CAR.setRouteService(RouteService.OSMAND);
			ApplicationMode.CAR.setRoutingProfile(ApplicationMode.CAR.getDefaultRoutingProfile());
		}
		applied.set(true);
	}
}
