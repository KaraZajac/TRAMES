package net.osmand.plus.trames;

/**
 * The OsmAnd network paths TRAMES closes, in one place so the cuts can be audited.
 *
 * <p>Each flag is read by a short guard in the upstream file named beside it
 * ({@code grep -rn TramesPrivacy}), which leaves OsmAnd's own code in place and keeps the fork's
 * diff against upstream small. The settings TRAMES changes for the same reason — the install
 * ID, the welcome screen, the stock-map suggestion — are applied in {@link TramesDefaults}.
 *
 * <p>Two kinds of path are closed, since v1.2.5:
 * <ul>
 *   <li><b>Requests made without a tap.</b> On about one start in five OsmAnd fetches its map
 *   index from download.osmand.net to look for updates; on first start it fetches its help
 *   index from osmand.net; and on phones with Google Play it checks subscriptions with
 *   osmand.net daily and then asks it for sale messages. None of them carries coordinates, but
 *   each shows OsmAnd's servers the phone's IP address and, for most, a random install ID.</li>
 *   <li><b>Features that send coordinates once tapped.</b> "Calculate online" sends the whole
 *   trip. The online photos row sends a place and your last known location, and stays open —
 *   sending again for every later place — once opened. The search screen's nearby places card
 *   is closed with it, because the two share one saved open state.</li>
 * </ul>
 *
 * <p>The Wikipedia data source needs no change: OsmAnd defaults it to offline for builds it
 * treats as paid, which is any package but its own free app's, so Wikipedia lookups read
 * downloaded maps unless a user switches that source to online.
 */
public final class TramesPrivacy {

	/**
	 * {@code AppInitializer.checkMapUpdates}: the background map-index fetch. Its two-day guard
	 * compares milliseconds with {@code 2 * 24 * 60 * 60}, so upstream it is really about three
	 * minutes, gated only by a one-in-five draw on each start.
	 */
	public static final boolean AUTO_MAP_UPDATE_CHECK = false;

	/**
	 * {@code AppInitializer}: preloading OsmAnd's help index, osmand.net/help-structure.json,
	 * on every start until it is cached. The Help screen still loads it when opened.
	 */
	public static final boolean HELP_INDEX_AT_START = false;

	/**
	 * {@code OsmandInAppPurchaseActivity}: Google Play billing, the osmand.net subscription and
	 * promo checks every OsmAnd activity starts on resume, and the sale messages
	 * ({@code DiscountHelper}) they unlock. TRAMES sells nothing.
	 */
	public static final boolean IN_APP_PURCHASES = false;

	/**
	 * {@code RequiredMapsController}: "Calculate online" — the start (or last known location),
	 * every waypoint and the destination, to maptile.osmand.net.
	 */
	public static final boolean CALCULATE_ONLINE = false;

	/**
	 * {@code MenuBuilder}: the online photos row in a place's menu — the place's coordinates and
	 * the user's last known location, to osmand.net, plus Wikimedia queries.
	 */
	public static final boolean ONLINE_PHOTOS = false;

	/**
	 * {@code QuickSearchHistoryListFragment}: the search screen's nearby places card. With the
	 * Wikipedia source online it sends the map's area to osmand.net; with TRAMES's offline
	 * default, opening it fetches OsmAnd's map index to offer Wikipedia maps. Either way it
	 * saves its open state under the online photos row's preference key
	 * ({@code online_photos_menu_collapsed}), so opening it opened that row too.
	 */
	public static final boolean NEARBY_PLACES = false;

	private TramesPrivacy() {
	}
}
