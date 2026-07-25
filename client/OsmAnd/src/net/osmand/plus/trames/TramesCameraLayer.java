package net.osmand.plus.trames;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;

import androidx.annotation.NonNull;

import net.osmand.data.QuadRect;
import net.osmand.data.RotatedTileBox;
import net.osmand.plus.views.OsmandMapTileView;
import net.osmand.plus.views.layers.base.OsmandMapLayer;

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
public class TramesCameraLayer extends OsmandMapLayer {

	/** Camera body. Matches the launcher icon's camera dot. */
	private static final int COLOR_CAMERA = 0xFFD9584B;
	/** Field-of-view wedge — translucent so overlapping cones stay readable. */
	private static final int COLOR_CONE = 0x33D9584B;

	/**
	 * Drawn cone length in pixels. Deliberately NOT to scale: a real plate-read range is
	 * ~25 m, which at city zoom is a couple of pixels and invisible. This is a legibility
	 * indicator of which way a camera looks, not a survey of its exact coverage.
	 */
	private static final float CONE_PX = 34f;
	private static final float CONE_SPAN_DEG = 45f;
	private static final float DOT_RADIUS_PX = 4.5f;

	private final TramesCameraSource source = new TramesCameraSource();

	private Paint dotPaint;
	private Paint conePaint;
	private Paint outlinePaint;
	private final Path conePath = new Path();

	private boolean enabled = true;

	public TramesCameraLayer(@NonNull Context ctx) {
		super(ctx);
	}

	@Override
	public void initLayer(@NonNull OsmandMapTileView view) {
		super.initLayer(view);
		dotPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
		dotPaint.setStyle(Paint.Style.FILL);
		dotPaint.setColor(COLOR_CAMERA);

		outlinePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
		outlinePaint.setStyle(Paint.Style.STROKE);
		outlinePaint.setStrokeWidth(1.5f);
		// A white ring keeps the marker legible over both the dark road casing and pale
		// map background; without it the dots vanish on red-tinted areas.
		outlinePaint.setColor(Color.WHITE);

		conePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
		conePaint.setStyle(Paint.Style.FILL);
		conePaint.setColor(COLOR_CONE);
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
		if (!enabled || dotPaint == null) {
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
				float screenBearing = bearing - rotation;
				drawCone(canvas, x, y, screenBearing);
			}

			canvas.drawCircle(x, y, DOT_RADIUS_PX, dotPaint);
			canvas.drawCircle(x, y, DOT_RADIUS_PX, outlinePaint);
		}
	}

	private void drawCone(@NonNull Canvas canvas, float x, float y, float bearingDeg) {
		conePath.reset();
		conePath.moveTo(x, y);
		int steps = 6;
		float start = bearingDeg - CONE_SPAN_DEG / 2f;
		for (int i = 0; i <= steps; i++) {
			// Compass bearing: 0 = north = up = -Y on screen, increasing clockwise.
			double rad = Math.toRadians(start + (CONE_SPAN_DEG * i / steps));
			float px = x + (float) (CONE_PX * Math.sin(rad));
			float py = y - (float) (CONE_PX * Math.cos(rad));
			conePath.lineTo(px, py);
		}
		conePath.close();
		canvas.drawPath(conePath, conePaint);
	}

	@Override
	public boolean drawInScreenPixels() {
		// Markers keep a constant on-screen size rather than scaling with the map, which
		// is what you want for point symbols.
		return true;
	}
}
