package net.osmand.plus.trames;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;

import android.graphics.drawable.Drawable;

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
	 * cone always covers the same stretch of road and grows/shrinks with zoom, the way
	 * the DeFlock map draws it.
	 *
	 * <p>The previous build drew a fixed ~46dp screen wedge: identical pixel size at every
	 * zoom, which meant it implied a different real-world coverage each time you zoomed,
	 * and read as far too small next to DeFlock's ground-anchored cones.
	 *
	 * <p>These two numbers are the knobs. {@code CONE_RADIUS_M} is how far down the road a
	 * reader is shown watching; {@code CONE_SPAN_DEG} is how wide the wedge is. They are a
	 * display convention, not a survey — OSM gives us only the bearing a camera faces, not
	 * its lens or range.
	 */
	private static final double CONE_RADIUS_M = 90.0;
	private static final float CONE_SPAN_DEG = 55f;

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
	 * Only counts cameras already loaded for the current view, so a long route reaching
	 * outside the fetched area is undercounted. Stated as "on this route" rather than
	 * "avoided" for that reason, and because a true avoided-count needs a second
	 * unavoided route to compare against — which would double every routing request.
	 */
	@Override
	public void newRouteIsCalculated(boolean newRoute, ValueHolder<Boolean> showToast) {
		if (!enabled || app == null || routingHelper == null) {
			return;
		}
		List<TramesCameraSource.Camera> cameras = source.getCameras();
		if (cameras.isEmpty()) {
			return;
		}
		List<Location> route = routingHelper.getRoute().getImmutableAllLocations();
		if (route.isEmpty()) {
			return;
		}
		TramesRouteExposure.Result r = TramesRouteExposure.count(route, cameras);
		String msg = r.seenBy == 0
				? app.getString(R.string.trames_exposure_none)
				: app.getString(R.string.trames_exposure_count, r.seenBy);
		app.runInUIThread(() -> app.showToastMessage(msg));
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
