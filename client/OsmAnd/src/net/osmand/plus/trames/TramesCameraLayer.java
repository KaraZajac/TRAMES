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
import net.osmand.core.jni.MapMarkersCollection;
import net.osmand.core.jni.PointI;
import net.osmand.core.jni.SwigUtilities;
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
	 * Sizes in DP, scaled by display density at draw time.
	 *
	 * These were originally raw pixels, which made the markers roughly a seventh of their
	 * intended size on a 3x-density phone — barely visible next to OsmAnd's own POI pins.
	 * Anything spatial in a layer has to be multiplied by density.
	 *
	 * The cone is deliberately NOT to scale: a real plate-read range is ~25 m, which at
	 * city zoom is a couple of pixels. It indicates which way a camera looks; it is not a
	 * survey of its coverage.
	 */
	private static final float CONE_DP = 46f;
	private static final float CONE_SPAN_DEG = 45f;
	private static final float ICON_DP = 26f;

	/** Surface-icon keys are a limited resource per marker; 4 covers every real unit. */
	private static final int MAX_CONES = 4;

	private final TramesCameraSource source = new TramesCameraSource();

	private Paint conePaint;
	private Paint bitmapPaint;
	private Bitmap icon;
	private Bitmap coneBitmap;
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

		coneBitmap = buildConeBitmap((int) (CONE_DP * density * 2));
	}

	/**
	 * A wedge pointing north (up), for use as an on-map-surface icon.
	 *
	 * The GL renderer rotates this to each camera's bearing and projects it onto the
	 * ground plane, so it stays glued to the map and lies flat when the view is tilted —
	 * which a canvas-drawn wedge cannot do.
	 */
	@NonNull
	private Bitmap buildConeBitmap(int size) {
		Bitmap bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
		Canvas c = new Canvas(bmp);
		float cx = size / 2f;
		float cy = size / 2f;
		float len = size / 2f;
		Path p = new Path();
		p.moveTo(cx, cy);
		int steps = 10;
		float start = -CONE_SPAN_DEG / 2f;
		for (int i = 0; i <= steps; i++) {
			double rad = Math.toRadians(start + (CONE_SPAN_DEG * i / steps));
			p.lineTo(cx + (float) (len * Math.sin(rad)), cy - (float) (len * Math.cos(rad)));
		}
		p.close();
		c.drawPath(p, conePaint);
		return bmp;
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
			clearMapMarkersCollections();
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
			clearMapMarkersCollections();
			buildMarkers(mapRenderer, cameras);
			builtForCameras = identity;
		}
	}

	private void buildMarkers(@NonNull MapRendererView mapRenderer,
	                          @NonNull List<TramesCameraSource.Camera> cameras) {
		if (icon == null || coneBitmap == null || cameras.isEmpty()) {
			return;
		}
		mapMarkersCollection = new MapMarkersCollection();
		for (TramesCameraSource.Camera cam : cameras) {
			MapMarkerBuilder b = new MapMarkerBuilder();
			b.setPosition(new PointI(MapUtils.get31TileNumberX(cam.lon),
							MapUtils.get31TileNumberY(cam.lat)))
					.setIsAccuracyCircleSupported(false)
					.setBaseOrder(getPointsOrder())
					// Billboard pin: always faces the viewer, so the icon stays legible
					// however the map is rotated or tilted.
					.setPinIcon(NativeUtilities.createSkImageFromBitmap(icon))
					.setPinIconHorisontalAlignment(MapMarker.PinIconHorisontalAlignment.CenterHorizontal)
					.setPinIconVerticalAlignment(MapMarker.PinIconVerticalAlignment.CenterVertical);

			// One surface cone per head, so multi-head units show every direction they
			// actually watch rather than just the first.
			int heads = Math.min(cam.directions.length, MAX_CONES);
			for (int i = 0; i < heads; i++) {
				b.addOnMapSurfaceIcon(SwigUtilities.getOnSurfaceIconKey(i + 1),
						NativeUtilities.createSkImageFromBitmap(coneBitmap));
			}
			MapMarker marker = b.buildAndAddToCollection(mapMarkersCollection);
			for (int i = 0; i < heads; i++) {
				marker.setOnMapSurfaceIconDirection(
						SwigUtilities.getOnSurfaceIconKey(i + 1), cam.directions[i]);
			}
		}
		mapRenderer.addSymbolsProvider(mapMarkersCollection);
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

		float density = tileBox.getDensity();
		float coneLen = CONE_DP * density;
		float rotation = tileBox.getRotate();
		for (TramesCameraSource.Camera cam : cameras) {
			if (cam.lat < bounds.bottom || cam.lat > bounds.top
					|| cam.lon < bounds.left || cam.lon > bounds.right) {
				continue;
			}
			float x = tileBox.getPixXFromLatLon(cam.lat, cam.lon);
			float y = tileBox.getPixYFromLatLon(cam.lat, cam.lon);

			// One wedge per head. Multi-head units genuinely watch several ways at once,
			// so drawing only the first would understate their coverage.
			for (float bearing : cam.directions) {
				// Screen bearings must account for map rotation, or the cones point the
				// wrong way the moment the user rotates the map.
				drawCone(canvas, x, y, bearing - rotation, coneLen);
			}

			if (icon != null) {
				canvas.drawBitmap(icon, x - icon.getWidth() / 2f,
						y - icon.getHeight() / 2f, bitmapPaint);
			}
		}
	}

	private void drawCone(@NonNull Canvas canvas, float x, float y, float bearingDeg,
	                      float length) {
		conePath.reset();
		conePath.moveTo(x, y);
		int steps = 8;
		float start = bearingDeg - CONE_SPAN_DEG / 2f;
		for (int i = 0; i <= steps; i++) {
			// Compass bearing: 0 = north = up = -Y on screen, increasing clockwise.
			double rad = Math.toRadians(start + (CONE_SPAN_DEG * i / steps));
			float px = x + (float) (length * Math.sin(rad));
			float py = y - (float) (length * Math.cos(rad));
			conePath.lineTo(px, py);
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
		clearMapMarkersCollections();
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
