package net.osmand.plus.plugins.aistracker;

import net.osmand.shared.aistracker.AisLatLon;
import net.osmand.shared.aistracker.AisObjType;
import net.osmand.shared.aistracker.AisObject;

import static net.osmand.shared.aistracker.AisObjType.AIS_AIRPLANE;
import static net.osmand.shared.aistracker.AisObjType.AIS_ATON;
import static net.osmand.shared.aistracker.AisObjType.AIS_ATON_VIRTUAL;
import static net.osmand.shared.aistracker.AisObjType.AIS_LANDSTATION;
import static net.osmand.shared.aistracker.AisObjType.AIS_SART;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.PointF;

import androidx.annotation.NonNull;

import net.osmand.core.android.MapRendererView;
import net.osmand.core.jni.MapMarkersCollection;
import net.osmand.core.jni.PointI;
import net.osmand.core.jni.SingleSkImage;
import net.osmand.core.jni.VectorLinesCollection;
import net.osmand.data.LatLon;
import net.osmand.data.PointDescription;
import net.osmand.data.RotatedTileBox;
import net.osmand.plus.ChartPointsHelper;
import net.osmand.plus.plugins.PluginsHelper;
import net.osmand.plus.plugins.aistracker.AisTrackerPlugin.AisDataManager.AisObjectListener;
import net.osmand.plus.utils.NativeUtilities;
import net.osmand.plus.views.OsmandMapTileView;
import net.osmand.plus.views.layers.ContextMenuLayer.IContextMenuProvider;
import net.osmand.plus.views.layers.MapSelectionResult;
import net.osmand.plus.views.layers.MapSelectionRules;
import net.osmand.plus.views.layers.base.OsmandMapLayer;
import net.osmand.util.Algorithms;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class AisTrackerLayer extends OsmandMapLayer implements IContextMenuProvider, AisObjectListener {

	public static final int START_ZOOM = 6;
	public static final int START_ZOOM_SHOW_SHAPE = 16;
	public static final int START_ZOOM_SHOW_DIRECTION = 10;

	private static final long AIS_RENDER_REFRESH_INTERVAL_MS = 1000L;

	private final AisTrackerPlugin plugin = PluginsHelper.requirePlugin(AisTrackerPlugin.class);
	private final Paint bitmapPaint = new Paint();
	private final Map<Integer, AisObjectDrawable> objectDrawables = new ConcurrentHashMap<>();

	private MapMarkersCollection markersCollection;
	private VectorLinesCollection vectorLinesCollection;
	private Bitmap aisRestBitmap;
	private SingleSkImage aisRestImage;
	private float textScale = 1f;
	private int lastRenderZoom = -1;
	private long lastRenderRefreshTimeMs;

	public AisTrackerLayer(@NonNull Context context) {
		super(context);
	}

	@Override
	public void initLayer(@NonNull OsmandMapTileView view) {
		super.initLayer(view);

		bitmapPaint.setAntiAlias(true);
		bitmapPaint.setFilterBitmap(true);
		bitmapPaint.setStrokeWidth(4);
		bitmapPaint.setColor(Color.DKGRAY);

		float density = 5;
		int pointColor = 0xFFFFFFFF;
		ChartPointsHelper pointsHelper = new ChartPointsHelper(getContext());
		aisRestBitmap = pointsHelper.createXAxisPointBitmap(pointColor, density);
	}

	@Override
	public void cleanupResources() {
		MapRendererView mapRenderer = getMapRenderer();
		if (mapRenderer != null && markersCollection != null && vectorLinesCollection != null) {
			markersCollection.removeAllMarkers();
			vectorLinesCollection.removeAllLines();
			mapRenderer.removeSymbolsProvider(markersCollection);
			mapRenderer.removeSymbolsProvider(vectorLinesCollection);
			aisRestImage = null;
		}
		objectDrawables.clear();
		lastRenderZoom = -1;
		lastRenderRefreshTimeMs = 0;
	}

	@Override
	public void onAisObjectReceived(@NonNull AisObject ais) {
		int mmsi = ais.getMmsi();
		boolean own = isOwnObject(ais);
		AisObjectDrawable drawable = objectDrawables.get(mmsi);
		if (drawable == null) {
			if (isOwnObjectHidden(ais)) {
				return; // exclude own AIS object from list
			}
			drawable = new AisObjectDrawable(plugin, ais);
			drawable.setOwnObject(own);
			objectDrawables.put(ais.getMmsi(), drawable);
		} else {
			if (isOwnObjectHidden(ais)) {
				this.onAisObjectRemoved(ais);
				return;
			} else {
				drawable.setOwnObject(own);
				drawable.set(ais);
			}
		}
		if (getMapRenderer() != null && !drawable.hasAisRenderData() && aisRestImage != null
				&& markersCollection != null && vectorLinesCollection != null) {
			drawable.createAisRenderData(getBaseOrder(), bitmapPaint, markersCollection, vectorLinesCollection, aisRestImage);
		}
		drawable.updateAisRenderData(getTileView(), bitmapPaint);
	}

	@Override
	public void onAisObjectRemoved(@NonNull AisObject ais) {
		if (getMapRenderer() != null && markersCollection != null && vectorLinesCollection != null) {
			AisObjectDrawable drawable = objectDrawables.get(ais.getMmsi());
			if (drawable != null) {
				drawable.clearAisRenderData(markersCollection, vectorLinesCollection);
			}
		}
		objectDrawables.remove(ais.getMmsi());
	}

	private boolean isOwnObject(@NonNull AisObject ais) {
		return ais.getMmsi() == plugin.AIS_OWN_MMSI.get();
	}

	private boolean isOwnObjectHidden(@NonNull AisObject ais) {
		return isOwnObject(ais) && !plugin.AIS_DISPLAY_OWN_POSITION.get();
	}

	public void refreshOwnObjectVisibility() {
		for (AisObject ais : plugin.getAisObjects()) {
			AisObjectDrawable drawable = objectDrawables.get(ais.getMmsi());
			if (isOwnObjectHidden(ais)) {
				if (drawable != null) {
					onAisObjectRemoved(ais);
				}
			} else if (drawable != null) {
				drawable.setOwnObject(isOwnObject(ais));
				drawable.updateAisRenderData(getTileView(), bitmapPaint);
			} else {
				onAisObjectReceived(ais);
			}
		}
	}

	public boolean isLocationVisible(RotatedTileBox tileBox, double lat, double lon) {
		//noinspection SimplifiableIfStatement
		if (tileBox == null) {
			return false;
		}
		return tileBox.containsLatLon(lat, lon);
	}

	@Override
	public void onDraw(Canvas canvas, RotatedTileBox tileBox, DrawSettings settings) {

	}

	@Override
	public void onPrepareBufferImage(Canvas canvas, RotatedTileBox tileBox, DrawSettings settings) {
		super.onPrepareBufferImage(canvas, tileBox, settings);
		MapRendererView mapRenderer = getMapRenderer();

		float textScale = getTextScale();
		boolean textScaleChanged = this.textScale != textScale;
		this.textScale = textScale;
		if (textScaleChanged) {
			plugin.getAisImagesCache().clearCache();
		}

		List<AisObject> aisObjects = plugin.getAisObjects();
		if (mapRenderer != null) {
			if (mapActivityInvalidated || mapRendererChanged || textScaleChanged) {
				cleanupResources();

				if (aisRestImage == null) {
					aisRestImage = NativeUtilities.createSkImageFromBitmap(aisRestBitmap);
				}
				markersCollection = new MapMarkersCollection();
				vectorLinesCollection = new VectorLinesCollection();
				mapRenderer.addSymbolsProvider(markersCollection);
				mapRenderer.addSymbolsProvider(vectorLinesCollection);

				for (AisObject ais : plugin.getAisObjects()) {
					if (isOwnObjectHidden(ais)) {
						continue;
					}
					AisObjectDrawable drawable = new AisObjectDrawable(plugin, ais);
					drawable.setOwnObject(isOwnObject(ais));
					objectDrawables.put(ais.getMmsi(), drawable);
					drawable.createAisRenderData(getBaseOrder(), bitmapPaint,
							markersCollection, vectorLinesCollection, aisRestImage);
					drawable.updateAisRenderData(getTileView(), bitmapPaint);
				}
				updateNativeRenderRefreshState();
			} else if (shouldRefreshNativeRenderData()) {
				refreshNativeRenderData(aisObjects);
			}
			mapActivityInvalidated = false;
			mapRendererChanged = false;
		} else if (tileBox.getZoom() >= START_ZOOM) {
			for (AisObject ais : aisObjects) {
				AisObjectDrawable drawable = objectDrawables.get(ais.getMmsi());
				if (drawable != null && ais.getPosition() != null && isLocationVisible(tileBox, ais.getPosition().getLatitude(), ais.getPosition().getLongitude())) {
					drawable.draw(bitmapPaint, canvas, tileBox);
				}
			}
		}
	}

	private boolean shouldRefreshNativeRenderData() {
		OsmandMapTileView tileView = getTileView();
		if (getMapRenderer() == null || tileView == null || objectDrawables.isEmpty()) {
			return false;
		}
		long now = System.currentTimeMillis();
		return tileView.getZoom() != lastRenderZoom
				|| now - lastRenderRefreshTimeMs >= AIS_RENDER_REFRESH_INTERVAL_MS;
	}

	private void refreshNativeRenderData(@NonNull List<AisObject> aisObjects) {
		for (AisObject ais : aisObjects) {
			AisObjectDrawable drawable = objectDrawables.get(ais.getMmsi());
			if (drawable != null) {
				drawable.updateAisRenderData(getTileView(), bitmapPaint);
			}
		}
		updateNativeRenderRefreshState();
	}

	private void updateNativeRenderRefreshState() {
		OsmandMapTileView tileView = getTileView();
		lastRenderZoom = tileView != null ? tileView.getZoom() : -1;
		lastRenderRefreshTimeMs = System.currentTimeMillis();
	}

	@Override
	public boolean drawInScreenPixels() {
		return true;
	}

	@Override
	public void collectObjectsFromPoint(@NonNull MapSelectionResult result, @NonNull MapSelectionRules rules) {
		PointF point = result.getPoint();
		RotatedTileBox tileBox = result.getTileBox();
		List<AisObject> aisObjects = plugin.getAisObjects();
		if (Algorithms.isEmpty(aisObjects) || tileBox.getZoom() < START_ZOOM) {
			return;
		}
		MapRendererView mapRenderer = getMapRenderer();
		float radius = getScaledTouchRadius(getApplication(), tileBox.getDefaultRadiusPoi()) * TOUCH_RADIUS_MULTIPLIER;
		List<PointI> touchPolygon31 = null;
		if (mapRenderer != null) {
			touchPolygon31 = NativeUtilities.getPolygon31FromPixelAndRadius(mapRenderer, point, radius);
			if (touchPolygon31 == null) {
				return;
			}
		}
		for (AisObject object : aisObjects) {
			if (isOwnObjectHidden(object)) {
				continue;
			}
			AisLatLon latLon = object.getPosition();
			if (latLon != null) {
				double lat = latLon.getLatitude();
				double lon = latLon.getLongitude();

				boolean add = mapRenderer != null
						? NativeUtilities.isPointInsidePolygon(lat, lon, touchPolygon31)
						: tileBox.isLatLonNearPixel(lat, lon, point.x, point.y, radius);
				if (add) {
					result.collect(object, this);
				}
			}
		}
	}

	@Override
	public LatLon getObjectLocation(Object o) {
		if (o instanceof AisObject) {
			AisLatLon pos = ((AisObject) o).getPosition();
			if (pos != null) {
				return new LatLon(pos.getLatitude(), pos.getLongitude());
			}
		}
		return null;
	}

	@Override
	public PointDescription getObjectName(Object o) {
		if (o instanceof AisObject ais) {
			AisObjType objectClass = ais.getObjectClass();
			if (ais.getShipName() != null) {
				return new PointDescription("AIS object", ais.getShipName() +
						(isSignalLost(ais) ? " (signal lost)" : ""));
			} else if (objectClass == AIS_LANDSTATION) {
				return new PointDescription("AIS object", "Land Station with MMSI " + ais.getMmsi());
			} else if (objectClass == AIS_AIRPLANE) {
				return new PointDescription("AIS object", "Airplane with MMSI " +
						ais.getMmsi() + (isSignalLost(ais) ? " (signal lost)" : ""));
			} else if ((objectClass == AIS_ATON) || (objectClass == AIS_ATON_VIRTUAL)) {
				return new PointDescription("AIS object", "Aid to Navigation");
			} else if (objectClass == AIS_SART) {
				return new PointDescription("AIS object", "SART (Search and Rescue Transmitter)");
			}
			return new PointDescription("AIS object",
					"AIS object with MMSI " + ais.getMmsi() +
							(isSignalLost(ais) ? " (signal lost)" : ""));
		}
		return null;
	}

    private boolean isSignalLost(AisObject ais) {
        return ais.isLost(plugin.getVesselLostTimeoutInMinutes()) && ais.isMovable() && !ais.isVesselAtRest();
    }
}
