package net.osmand.plus.trames;

import android.util.JsonReader;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;

import net.osmand.IProgress;
import net.osmand.PlatformUtil;
import net.osmand.plus.OsmandApplication;
import net.osmand.plus.utils.AndroidNetworkUtils;

import org.apache.commons.logging.Log;

import java.io.File;
import java.io.FileInputStream;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.List;
import java.util.zip.GZIPInputStream;

/**
 * On-device camera positions, so the map can draw cones with no network.
 *
 * <p><b>Why this exists.</b> {@link TramesCameraSource} only ever asked the network. In
 * airplane mode — or anywhere with no signal — the layer drew nothing, which on a map is
 * indistinguishable from "no cameras here". Meanwhile the offline router was busily
 * avoiding those same cameras. A map that looks clear over surveilled streets is the
 * failure mode this app exists to prevent, and it is the direction of the error that
 * matters: showing nothing reads as safety.
 *
 * <p>The pack is the same snapshot the routing graph and the ALPR-tagged {@code .obf}
 * files were built from, so what the map draws offline is exactly what the offline router
 * avoided. Stripped to position and direction, all 120k US cameras compress to about
 * 1.1 MB — negligible next to the maps it rides along with.
 *
 * <p>Parsed with a streaming {@link JsonReader} into parallel primitive arrays rather than
 * a JSON tree of 120k objects: the tree costs tens of MB transiently and this runs on
 * phones already holding a rendered map. Camera objects are materialised only for the
 * handful inside the current view.
 */
public class TramesCameraStore {

	private static final Log LOG = PlatformUtil.getLog(TramesCameraStore.class);

	/** Hosted next to the maps it accompanies. */
	public static final String PACK_URL = TramesMapDownloader.BASE_URL + "cameras-us.json.gz";

	private static final String PACK_FILE = "trames-cameras.json.gz";

	private final OsmandApplication app;

	/** Parallel arrays, loaded once. null until {@link #ensureLoaded()} succeeds. */
	private static volatile double[] lats;
	private static volatile double[] lons;
	private static volatile String[] dirs;

	public TramesCameraStore(@NonNull OsmandApplication app) {
		this.app = app;
	}

	@NonNull
	public File file() {
		return app.getAppPath(PACK_FILE);
	}

	public boolean isPresent() {
		File f = file();
		return f.exists() && f.length() > 0;
	}

	/** Download the pack. Blocking; returns null on success or an error string. */
	@Nullable
	public String download(@Nullable IProgress progress) {
		File dest = file();
		// gzip=false: the file is stored gzipped and decompressed at parse time, so the
		// transfer must stay byte-for-byte. Asking for transport-level gzip here would
		// hand back a decoded stream and write a .gz that is not gzipped.
		String error = AndroidNetworkUtils.downloadFile(PACK_URL, dest, false, progress);
		if (error == null) {
			invalidate();
		}
		return error;
	}

	/** Drop the parsed copy so the next read picks up a freshly downloaded pack. */
	public static void invalidate() {
		lats = null;
		lons = null;
		dirs = null;
	}

	/**
	 * Cameras inside the given bounds, or an empty list if no pack is present.
	 *
	 * <p>Empty here means "we have nothing to show", which the caller must not present as
	 * "there is nothing here" — see {@link #isPresent()}.
	 */
	@NonNull
	public List<TramesCameraSource.Camera> forBox(double south, double west,
	                                              double north, double east) {
		if (!ensureLoaded()) {
			return new ArrayList<>();
		}
		double[] la = lats, lo = lons;
		String[] dr = dirs;
		if (la == null || lo == null || dr == null) {
			return new ArrayList<>();
		}
		List<TramesCameraSource.Camera> out = new ArrayList<>();
		for (int i = 0; i < la.length; i++) {
			if (la[i] >= south && la[i] <= north && lo[i] >= west && lo[i] <= east) {
				// Synthetic ids: the pack drops OSM ids to stay small, and the only thing
				// downstream needs them for is de-duplicating merged tiles. Index-based ids
				// are unique within the pack, which is all that requires.
				out.add(new TramesCameraSource.Camera(-(long) (i + 1), la[i], lo[i],
						TramesCameraSource.parseDirections(dr[i]), null, null));
			}
		}
		return out;
	}

	/** Parse the pack into memory once. Returns false if absent or unreadable. */
	private boolean ensureLoaded() {
		if (lats != null) {
			return true;
		}
		File f = file();
		if (!f.exists() || f.length() == 0) {
			return false;
		}
		synchronized (TramesCameraStore.class) {
			if (lats != null) {
				return true;
			}
			try (JsonReader reader = new JsonReader(new InputStreamReader(
					new GZIPInputStream(new FileInputStream(f)), "UTF-8"))) {
				int n = 0;
				double[] la = new double[4096];
				double[] lo = new double[4096];
				String[] dr = new String[4096];
				reader.beginObject();
				while (reader.hasNext()) {
					if (!"cams".equals(reader.nextName())) {
						reader.skipValue();
						continue;
					}
					reader.beginArray();
					while (reader.hasNext()) {
						reader.beginArray();
						double lat = reader.nextDouble();
						double lon = reader.nextDouble();
						String dir = reader.hasNext() ? reader.nextString() : "";
						while (reader.hasNext()) {   // tolerate future extra fields
							reader.skipValue();
						}
						reader.endArray();
						if (n == la.length) {
							int cap = n * 2;
							la = java.util.Arrays.copyOf(la, cap);
							lo = java.util.Arrays.copyOf(lo, cap);
							dr = java.util.Arrays.copyOf(dr, cap);
						}
						la[n] = lat;
						lo[n] = lon;
						dr[n] = dir;
						n++;
					}
					reader.endArray();
				}
				reader.endObject();
				lats = java.util.Arrays.copyOf(la, n);
				lons = java.util.Arrays.copyOf(lo, n);
				dirs = java.util.Arrays.copyOf(dr, n);
				LOG.info("TRAMES offline camera pack loaded: " + n + " cameras");
				return n > 0;
			} catch (Exception e) {
				LOG.warn("TRAMES camera pack unreadable: " + e.getMessage());
				// A truncated download would fail here every time; drop it so the next
				// map download re-fetches rather than failing forever on a bad file.
				f.delete();
				return false;
			}
		}
	}
}
