package net.osmand.plus.trames;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;

import android.graphics.drawable.Drawable;
import android.os.AsyncTask;

import androidx.annotation.NonNull;
import androidx.appcompat.content.res.AppCompatResources;

import net.osmand.Location;
import net.osmand.core.android.MapRendererView;
import net.osmand.core.jni.MapMarker;
import net.osmand.core.jni.MapMarkerBuilder;
import net.osmand.core.jni.FColorARGB;
import net.osmand.core.jni.MapMarkersCollection;
import net.osmand.core.jni.PointI;
import net.osmand.core.jni.PolygonBuilder;
import net.osmand.core.jni.PolygonsCollection;
import net.osmand.core.jni.QVectorPointI;
import net.osmand.core.jni.ZoomLevel;
import net.osmand.data.LatLon;
import net.osmand.data.QuadRect;
import net.osmand.data.RotatedTileBox;
import net.osmand.plus.OsmandApplication;
import net.osmand.plus.R;
import net.osmand.plus.routing.IRouteInformationListener;
import net.osmand.plus.routing.RoutingHelper;
import net.osmand.data.ValueHolder;
import net.osmand.plus.utils.NativeUtilities;
import net.osmand.plus.views.OsmandMapTileView;
import net.osmand.plus.views.layers.base.OsmandMapLayer;

import net.osmand.util.MapUtils;

import java.util.List;

/**
 * Draws ALPR (licence-plate reader) cameras on the map, with the direction each one
 * actually watches.
 *
 * <p>The direction matters and is the whole point of drawing them this way. A camera
 * watching northbound traffic says nothing about the southbound carriageway, so a plain
 * dot would misrepresent what is actually being avoided — and would make a perfectly
 * sensible route look like it drove straight past a camera it was never seen by.
 *
 * <p>Placed in net.osmand.plus.trames rather than views.layers so every TRAMES-original
 * class sits in one package, keeping the fork's diff against upstream easy to audit.
 */
public class TramesCameraLayer extends OsmandMapLayer implements IRouteInformationListener {

	/** Camera body. Matches the launcher icon's camera dot. */
	private static final int COLOR_CAMERA = 0xFFD9584B;
	/** Field-of-view wedge — translucent so overlapping cones stay readable. */
	private static final int COLOR_CONE = 0x4DD9584B;

	/**
	 * Camera field-of-view geometry, anchored to the ground in real-world metres — so a
	 * cone always covers the same stretch of road and grows/shrinks with zoom.
	 *
	 * <p>The previous build drew a fixed ~46dp screen wedge: identical pixel size at every
	 * zoom, which implied a different real-world coverage each time you zoomed. This is
	 * now a true ground sector — and it is drawn at exactly the geometry the router avoids
	 * and the exposure count uses, from the single source of truth in {@link TramesGeometry}
	 * (60 m / 45°, mirroring the server's build_cones.py). So the wedge on the map is the
	 * wedge the route was planned around; it is not an independent display knob.
	 */
	private static final double CONE_RADIUS_M = TramesGeometry.CONE_RADIUS_M;
	private static final float CONE_SPAN_DEG = (float) TramesGeometry.CONE_SPAN_DEG;

	/** Camera dot icon size in DP — a screen-space point symbol, unlike the ground cones. */
	private static final float ICON_DP = 26f;

	/** Safety cap on sectors per unit; a real multi-head 360° unit has at most ~5. */
	private static final int MAX_HEADS = 8;

	private final TramesCameraSource source = new TramesCameraSource();

	private Paint conePaint;
	private Paint bitmapPaint;
	private Bitmap icon;
	/** Filled ground sectors for the GL renderer — geographic, so they scale with zoom. */
	private PolygonsCollection conesCollection;
	/** Identity of the marker set currently pushed to the GL renderer, to detect staleness. */
	private int builtForCameras = -1;
	private final Path conePath = new Path();

	private boolean enabled = true;
	private OsmandApplication app;
	private RoutingHelper routingHelper;

	public TramesCameraLayer(@NonNull Context ctx) {
		super(ctx);
	}

	@Override
	public void initLayer(@NonNull OsmandMapTileView view) {
		super.initLayer(view);
		OsmandApplication app = view.getApplication();
		routingHelper = app.getRoutingHelper();
		routingHelper.addListener(this);
		this.app = app;
		bitmapPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
		bitmapPaint.setFilterBitmap(true);

		// Rendered into our OWN bitmap rather than via
		// RenderingIcons.getBitmapFromVectorDrawable(): that helper returns a shared
		// static cacheBmp which it eraseColor()s and redraws on every call, so a
		// reference held across frames silently becomes whatever another layer drew last.
		float density = getContext().getResources().getDisplayMetrics().density;
		int px = Math.max(1, (int) (ICON_DP * density));
		Drawable d = AppCompatResources.getDrawable(getContext(), R.drawable.ic_trames_camera);
		if (d != null) {
			icon = Bitmap.createBitmap(px, px, Bitmap.Config.ARGB_8888);
			Canvas c = new Canvas(icon);
			d.setBounds(0, 0, px, px);
			d.draw(c);
		}

		conePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
		conePaint.setStyle(Paint.Style.FILL);
		conePaint.setColor(COLOR_CONE);
	}

	/**
	 * OpenGL path. Markers are placed by the renderer itself, so they stay glued to the
	 * map and behave correctly when tilted into 3D.
	 *
	 * The canvas path below is kept for the legacy (non-GL) renderer, but it is a 2D
	 * approximation: it cannot place icons correctly under perspective, which is exactly
	 * why icons drifted and cones looked wrong in 3D before this existed.
	 */
	@Override
	public void onPrepareBufferImage(Canvas canvas, RotatedTileBox tileBox, DrawSettings settings) {
		super.onPrepareBufferImage(canvas, tileBox, settings);
		MapRendererView mapRenderer = getMapRenderer();
		if (mapRenderer == null) {
			return;
		}
		if (!enabled || tileBox.getZoom() < TramesCameraSource.MIN_ZOOM) {
			clearSymbols();
			builtForCameras = -1;
			return;
		}
		source.ensureLoaded(tileBox.getLatLonBounds(), tileBox.getZoom(), () -> {
			if (view != null) {
				view.refreshMap();
			}
		});
		List<TramesCameraSource.Camera> cameras = source.getCameras();
		int identity = cameras.hashCode();
		if (identity != builtForCameras) {
			clearSymbols();
			buildMarkers(mapRenderer, cameras);
			builtForCameras = identity;
		}
	}

	private void buildMarkers(@NonNull MapRendererView mapRenderer,
	                          @NonNull List<TramesCameraSource.Camera> cameras) {
		if (icon == null || cameras.isEmpty()) {
			return;
		}
		// Two providers: filled ground sectors (geographic, scale with zoom) and the
		// billboard camera pins on top of them.
		PolygonsCollection cones = new PolygonsCollection(ZoomLevel.ZoomLevel1, ZoomLevel.ZoomLevel20);
		FColorARGB fill = NativeUtilities.createFColorARGB(COLOR_CONE);
		int polygonId = 0;
		mapMarkersCollection = new MapMarkersCollection();
		for (TramesCameraSource.Camera cam : cameras) {
			new MapMarkerBuilder()
					.setPosition(point31(cam.lat, cam.lon))
					.setIsAccuracyCircleSupported(false)
					.setBaseOrder(getPointsOrder())
					// Billboard pin: always faces the viewer, so the icon stays legible
					// however the map is rotated or tilted, and sits over the cones.
					.setPinIcon(NativeUtilities.createSkImageFromBitmap(icon))
					.setPinIconHorisontalAlignment(MapMarker.PinIconHorisontalAlignment.CenterHorizontal)
					.setPinIconVerticalAlignment(MapMarker.PinIconVerticalAlignment.CenterVertical)
					.buildAndAddToCollection(mapMarkersCollection);

			// One filled sector per head — multi-head units genuinely watch several ways
			// at once, so drawing only the first would understate their coverage.
			int heads = Math.min(cam.directions.length, MAX_HEADS);
			for (int i = 0; i < heads; i++) {
				new PolygonBuilder()
						.setPolygonId(++polygonId)
						// Higher base order than the pins, so the fill renders beneath them.
						.setBaseOrder(getPointsOrder() + 1)
						.setIsHidden(false)
						.setPoints(sectorPoints31(cam.lat, cam.lon, cam.directions[i]))
						.setFillColor(fill)
						.buildAndAddToCollection(cones);
			}
		}
		mapRenderer.addSymbolsProvider(mapMarkersCollection);
		if (polygonId > 0) {
			mapRenderer.addSymbolsProvider(cones);
			conesCollection = cones;
		}
	}

	/** Clear both providers this layer pushes to the GL renderer. */
	private void clearSymbols() {
		clearMapMarkersCollections();
		MapRendererView mapRenderer = getMapRenderer();
		if (mapRenderer != null && conesCollection != null) {
			mapRenderer.removeSymbolsProvider(conesCollection);
		}
		conesCollection = null;
	}

	@NonNull
	private static PointI point31(double lat, double lon) {
		return new PointI(MapUtils.get31TileNumberX(lon), MapUtils.get31TileNumberY(lat));
	}

	/**
	 * A filled field-of-view sector — apex at the camera, arc at {@link #CONE_RADIUS_M}
	 * metres — as 31-tile points. Built in geographic space so the GL renderer scales it
	 * with zoom and lays it flat on the ground when the view is tilted.
	 */
	@NonNull
	private QVectorPointI sectorPoints31(double lat, double lon, float bearing) {
		QVectorPointI pts = new QVectorPointI();
		pts.add(point31(lat, lon));
		int steps = 14;
		float start = bearing - CONE_SPAN_DEG / 2f;
		for (int i = 0; i <= steps; i++) {
			double b = start + CONE_SPAN_DEG * i / steps;
			LatLon edge = MapUtils.rhumbDestinationPoint(lat, lon, CONE_RADIUS_M, b);
			pts.add(point31(edge.getLatitude(), edge.getLongitude()));
		}
		return pts;
	}

	public void setEnabled(boolean enabled) {
		this.enabled = enabled;
	}

	public boolean isEnabled() {
		return enabled;
	}

	@NonNull
	public TramesCameraSource getSource() {
		return source;
	}

	@Override
	public void onDraw(Canvas canvas, RotatedTileBox tileBox, DrawSettings settings) {
		if (!enabled || bitmapPaint == null) {
			return;
		}
		// In OpenGL mode the markers are drawn by onPrepareBufferImage via the renderer;
		// drawing here as well would double them up and misplace the copies in 3D.
		if (getMapRenderer() != null) {
			return;
		}
		int zoom = tileBox.getZoom();
		if (zoom < TramesCameraSource.MIN_ZOOM) {
			return;
		}

		QuadRect bounds = tileBox.getLatLonBounds();
		// Self-throttling: only actually hits the network when the map has moved far
		// enough, so calling it per frame is safe.
		source.ensureLoaded(bounds, zoom, () -> {
			if (view != null) {
				view.refreshMap();
			}
		});

		List<TramesCameraSource.Camera> cameras = source.getCameras();
		if (cameras.isEmpty()) {
			return;
		}

		for (TramesCameraSource.Camera cam : cameras) {
			if (cam.lat < bounds.bottom || cam.lat > bounds.top
					|| cam.lon < bounds.left || cam.lon > bounds.right) {
				continue;
			}
			// Ground sectors, projected from real lat/lon so they scale with zoom and
			// follow map rotation for free (getPixXFromLatLon already accounts for it).
			for (float bearing : cam.directions) {
				drawConeGeo(canvas, tileBox, cam.lat, cam.lon, bearing);
			}
			if (icon != null) {
				float x = tileBox.getPixXFromLatLon(cam.lat, cam.lon);
				float y = tileBox.getPixYFromLatLon(cam.lat, cam.lon);
				canvas.drawBitmap(icon, x - icon.getWidth() / 2f,
						y - icon.getHeight() / 2f, bitmapPaint);
			}
		}
	}

	/**
	 * Canvas (non-GL) fallback: the same geographic sector as the GL path, projected
	 * corner by corner. Because every vertex is a real ground point, the wedge scales with
	 * zoom and follows map rotation with no explicit rotation maths.
	 */
	private void drawConeGeo(@NonNull Canvas canvas, @NonNull RotatedTileBox tb,
	                         double lat, double lon, float bearing) {
		conePath.reset();
		conePath.moveTo(tb.getPixXFromLatLon(lat, lon), tb.getPixYFromLatLon(lat, lon));
		int steps = 14;
		float start = bearing - CONE_SPAN_DEG / 2f;
		for (int i = 0; i <= steps; i++) {
			double b = start + CONE_SPAN_DEG * i / steps;
			LatLon edge = MapUtils.rhumbDestinationPoint(lat, lon, CONE_RADIUS_M, b);
			conePath.lineTo(tb.getPixXFromLatLon(edge.getLatitude(), edge.getLongitude()),
					tb.getPixYFromLatLon(edge.getLatitude(), edge.getLongitude()));
		}
		conePath.close();
		canvas.drawPath(conePath, conePaint);
	}

	/**
	 * Report how many cameras can see the freshly-calculated route.
	 *
	 * <p>Fetches every camera along the whole route — not just the visible map — so the
	 * count is complete however the user has the map zoomed or panned. Both the fetch and
	 * the scoring run off the UI thread. On a <em>total</em> fetch failure it falls back to
	 * the cameras already loaded for the view, so a network hiccup degrades to a partial
	 * count rather than a false "you're clear"; if it can't check at all, it stays silent
	 * rather than claim zero. Stated as "on this route" rather than "avoided" because a
	 * true avoided-count needs a second unavoided route to compare against.
	 */
	@Override
	public void newRouteIsCalculated(boolean newRoute, ValueHolder<Boolean> showToast) {
		if (!enabled || app == null || routingHelper == null) {
			return;
		}
		List<Location> route = routingHelper.getRoute().getImmutableAllLocations();
		if (route.isEmpty()) {
			return;
		}
		// Whole-route bounding box, padded past the exposure capture radius so a camera
		// sitting just off the end of the route is still fetched and scored.
		double south = 90, north = -90, west = 180, east = -180;
		for (Location loc : route) {
			south = Math.min(south, loc.getLatitude());
			north = Math.max(north, loc.getLatitude());
			west = Math.min(west, loc.getLongitude());
			east = Math.max(east, loc.getLongitude());
		}
		double pad = 0.002;
		final double s = south - pad, n = north + pad, w = west - pad, e = east + pad;

		new AsyncTask<Void, Void, String>() {
			@Override
			protected String doInBackground(Void... voids) {
				// The whole-route set, so exposure covers cameras off the visible map too.
				// null means every tile failed — only then fall back to the view cache, so
				// a network hiccup degrades to the old behaviour, never a false "clear".
				List<TramesCameraSource.Camera> fetched = source.fetchForRouteSync(s, w, n, e);
				List<TramesCameraSource.Camera> use = fetched != null ? fetched : source.getCameras();
				if (use.isEmpty()) {
					// Empty after a successful fetch = genuinely no cameras (say "clear");
					// empty after a failure = we truly couldn't check (say nothing).
					return fetched != null ? app.getString(R.string.trames_exposure_none) : null;
				}
				TramesRouteExposure.Result r = TramesRouteExposure.count(route, use);
				return r.seenBy == 0
						? app.getString(R.string.trames_exposure_none)
						: app.getString(R.string.trames_exposure_count, r.seenBy);
			}

			@Override
			protected void onPostExecute(String msg) {
				if (msg != null) {
					app.showToastMessage(msg);
				}
			}
		}.executeOnExecutor(AsyncTask.THREAD_POOL_EXECUTOR);
	}

	@Override
	public void routeWasCancelled() {
	}

	@Override
	public void routeWasFinished() {
	}

	@Override
	public void destroyLayer() {
		clearSymbols();
		if (routingHelper != null) {
			routingHelper.removeListener(this);
		}
		super.destroyLayer();
	}

	@Override
	public boolean drawInScreenPixels() {
		// Markers keep a constant on-screen size rather than scaling with the map, which
		// is what you want for point symbols.
		return true;
	}
}
