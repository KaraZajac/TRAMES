package net.osmand.plus.trames;

import androidx.annotation.NonNull;

import net.osmand.PlatformUtil;
import net.osmand.plus.OsmandApplication;
import net.osmand.plus.onlinerouting.OnlineRoutingHelper;
import net.osmand.plus.onlinerouting.engine.OnlineRoutingEngine;
import net.osmand.plus.onlinerouting.engine.TramesEngine;
import net.osmand.plus.routing.RouteService;
import net.osmand.plus.settings.backend.ApplicationMode;
import net.osmand.plus.settings.backend.OsmandSettings;
import net.osmand.plus.settings.backend.preferences.CommonPreference;
import net.osmand.plus.settings.enums.RouteCalculationMethod;

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
 * "TRAMES offline maps" first ({@link TramesMapsDialog#promptIfNoMaps} offers it). That is a
 * real first-run cost, accepted deliberately: the alternative routes everyone through a server.
 *
 * <p><b>Standard routing, not fast routing.</b> OsmAnd's fast routing ({@code AUTO}, its
 * default for car and bike) runs on a road network precomputed into the map, at OsmAnd's
 * default costs. TRAMES maps carry none, so on them the router falls back to standard routing,
 * which reads the {@code alpr} tag. A stock OsmAnd map of the same area does carry one, and
 * fast routing could then plan the route on it, where the camera penalty is never applied. So
 * the default is {@code STANDARD_ONLY}. On TRAMES maps alone that changes no route and slows
 * nothing: fast routing found no network there and fell back to standard routing anyway.
 *
 * <p><b>OsmAnd's check-ins, off.</b> See {@link #ensurePrivacyDefaults},
 * {@link #ensureBackupOptOut} and {@link TramesPrivacy}.
 *
 * <p>Runs on every start and is idempotent: {@link #removeRetiredEngine} finds nothing once
 * it has run, the routing default is only an in-memory default (a choice a user saves still
 * wins), and the one-time switches are each guarded by a global preference.
 */
public class TramesDefaults {

	private static final Log LOG = PlatformUtil.getLog(TramesDefaults.class);

	/** Guards the one-time explicit switch to offline routing (see ensureOfflineDefault). */
	private static final String OFFLINE_DEFAULT_PREF = "trames_offline_default_applied";

	/** Guards the one-time privacy switches (see ensurePrivacyDefaults). */
	private static final String PRIVACY_DEFAULTS_PREF = "trames_privacy_defaults_applied";

	/** Guards the one-time backup opt-out (see ensureBackupOptOut), added in v1.2.6. */
	private static final String BACKUP_OPT_OUT_PREF = "trames_backup_opt_out_applied";

	private TramesDefaults() {
	}

	public static void ensureSeeded(@NonNull OsmandApplication app) {
		removeRetiredEngine(app);
		ensureOfflineDefault(app);
		app.getSettings().ROUTE_CALCULATION_METHOD.setDefaultValue(RouteCalculationMethod.STANDARD_ONLY);
		ensurePrivacyDefaults(app);
		ensureBackupOptOut(app);
	}

	/**
	 * Turn off, once, OsmAnd's Android backup ({@code AUTO_BACKUP_ENABLED}, which gates
	 * {@code OsmandBackupAgent}). With the phone's backup on, Android otherwise copies the app's
	 * settings — the last map position and the navigation destination among them — together with
	 * the favourites backup and the map-marker and OSM-note databases, to the phone's backup
	 * account and onto a new phone. Its own guard rather than {@link #ensurePrivacyDefaults}'s,
	 * so that installs which already ran v1.2.5 get it too. Once, because it is a user setting:
	 * turning backup back on is the user's call.
	 */
	private static void ensureBackupOptOut(@NonNull OsmandApplication app) {
		OsmandSettings settings = app.getSettings();
		CommonPreference<Boolean> applied =
				settings.registerBooleanPreference(BACKUP_OPT_OUT_PREF, false).makeGlobal();
		if (applied.get()) {
			return;
		}
		settings.AUTO_BACKUP_ENABLED.set(false);
		applied.set(true);
		LOG.info("TRAMES: turned off OsmAnd's Android backup");
	}

	/**
	 * Turn off, once, the three OsmAnd settings behind its untapped check-ins with OsmAnd's
	 * servers (the code paths behind the rest are closed in {@link TramesPrivacy}):
	 * <ul>
	 *   <li>{@code SEND_UNIQUE_USER_IDENTIFIER}: a random install ID, renewed every three
	 *   months, that OsmAnd adds to its requests ({@code aid}).</li>
	 *   <li>{@code SHOW_OSMAND_WELCOME_SCREEN}: the first-start wizard, shown until any map is
	 *   installed. With a connection it asks osmand.net/api/geo-ip where the phone is, fetches
	 *   the stock map index, and offers the stock map of that region — which carries no
	 *   cameras, so routing on it avoids nothing. {@link TramesMapsDialog#promptIfNoMaps}
	 *   takes its place.</li>
	 *   <li>{@code SHOW_SUGGEST_MAP_DIALOG}: the banner offering a stock map for wherever the
	 *   map shows no installed one, at zooms 9 to 11. To fill it, OsmAnd fetches its map index
	 *   from download.osmand.net with no tap — and before a TRAMES map is installed, that is
	 *   everywhere.</li>
	 * </ul>
	 * Once rather than on every start: the install-ID and suggestion switches are user
	 * settings, and turning them back on is the user's call.
	 */
	private static void ensurePrivacyDefaults(@NonNull OsmandApplication app) {
		OsmandSettings settings = app.getSettings();
		CommonPreference<Boolean> applied =
				settings.registerBooleanPreference(PRIVACY_DEFAULTS_PREF, false).makeGlobal();
		if (applied.get()) {
			return;
		}
		settings.SEND_UNIQUE_USER_IDENTIFIER.set(false);
		settings.SHOW_OSMAND_WELCOME_SCREEN.set(false);
		settings.SHOW_SUGGEST_MAP_DIALOG.set(false);
		applied.set(true);
		LOG.info("TRAMES: turned off OsmAnd's install ID, welcome screen and map suggestions");
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
