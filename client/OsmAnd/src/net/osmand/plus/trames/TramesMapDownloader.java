package net.osmand.plus.trames;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;

import net.osmand.IProgress;
import net.osmand.plus.OsmandApplication;
import net.osmand.plus.utils.AndroidNetworkUtils;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.util.ArrayList;
import java.util.List;

import static net.osmand.util.Algorithms.isEmpty;

/**
 * Downloads TRAMES's ALPR-tagged offline maps and installs them so OsmAnd renders and
 * routes on them immediately.
 *
 * <p><b>Why a bespoke downloader.</b> OsmAnd's own map catalogue is an XML index served
 * from {@code download.osmand.net}, wired through {@code DownloadResources} /
 * {@code DownloadActivityType} and gated by a free-download counter. Our maps aren't
 * there and never will be — they carry an extra {@code alpr=yes} routing tag (written by
 * {@code server/offline/tag_ways.py}, preserved in the {@code .obf} routing section) that
 * the {@code car_alpr} profile penalises. Stock OsmAnd maps lack that tag, so avoidance
 * offline <em>requires</em> ours. Rather than graft a second index source onto OsmAnd's
 * catalogue plumbing, this reads a tiny JSON manifest from our host and fetches each
 * {@code .obf} straight into the maps directory — the same install path
 * {@code ObfImportTask} uses for a shared file, minus the file picker.
 *
 * <p><b>Install tail.</b> A downloaded {@code .obf} dropped at the maps-dir root is picked
 * up by a full re-index ({@code ResourceManager.reloadIndexes}) with no app restart; the
 * download UI's "installed" state is then refreshed. Identical to
 * {@code ObfImportTask.doInBackground}.
 *
 * <p>Stateless and self-contained in the {@code net.osmand.plus.trames} style: it takes an
 * {@link OsmandApplication} and reaches existing subsystems through its getters. Network
 * calls block, so run {@link #fetchManifest()} and {@link #downloadMap} off the UI thread.
 */
public class TramesMapDownloader {

	/**
	 * Public map host. Self-hosters who build their own ALPR maps can point a fork here;
	 * kept as a constant (not a preference) for parity with {@link
	 * net.osmand.plus.onlinerouting.engine.TramesEngine#getStandardUrl()}.
	 */
	public static final String BASE_URL = "https://maps.blackflagintel.com/";

	/** The catalogue: {@code {version, base_url, maps:[{name,file,size,date}, ...]}}. */
	public static final String MANIFEST_URL = BASE_URL + "manifest.json";

	private final OsmandApplication app;

	public TramesMapDownloader(@NonNull OsmandApplication app) {
		this.app = app;
	}

	/** One downloadable state from the manifest. */
	public static class TramesMap {
		/** Region slug, e.g. {@code "california"}. */
		public final String name;
		/** File name as hosted and stored, e.g. {@code "california-alpr.obf"}. */
		public final String file;
		/** Size in bytes (0 if unknown) — for the UI and a freshness/size hint. */
		public final long size;
		/** Build date {@code YYYY-MM-DD} (empty if unknown) — maps age as cameras are added. */
		public final String date;

		TramesMap(@NonNull String name, @NonNull String file, long size, @NonNull String date) {
			this.name = name;
			this.file = file;
			this.size = size;
			this.date = date;
		}
	}

	/**
	 * Fetch and parse the manifest.
	 *
	 * @return the available maps, or {@code null} if the host is unreachable or the JSON is
	 * malformed. Callers should treat {@code null} as "offline / try later", not an error to
	 * surface loudly — this is a background convenience, not a critical path.
	 */
	@Nullable
	public List<TramesMap> fetchManifest() {
		String json = AndroidNetworkUtils.sendRequest(app, MANIFEST_URL, null,
				"Fetching TRAMES map list", false, false);
		if (isEmpty(json)) {
			return null;
		}
		try {
			JSONArray arr = new JSONObject(json).getJSONArray("maps");
			List<TramesMap> maps = new ArrayList<>(arr.length());
			for (int i = 0; i < arr.length(); i++) {
				JSONObject m = arr.getJSONObject(i);
				maps.add(new TramesMap(
						m.getString("name"),
						m.getString("file"),
						m.optLong("size", 0),
						m.optString("date", "")));
			}
			return maps;
		} catch (JSONException e) {
			return null;
		}
	}

	/** The on-device path a map installs to (whether or not it exists yet). */
	@NonNull
	public File localFile(@NonNull TramesMap map) {
		return app.getAppPath(map.file);
	}

	/** True once the map is present on device. */
	public boolean isInstalled(@NonNull TramesMap map) {
		File f = localFile(map);
		return f.exists() && f.length() > 0;
	}

	/**
	 * Download one map into the maps directory and re-index so it renders + routes at once.
	 * Blocking — call off the UI thread.
	 *
	 * @param map      the map to fetch
	 * @param progress optional progress sink for the byte transfer (may be {@code null})
	 * @return {@code null} on success, or a human-readable error string on failure. On
	 * failure the partial file is left for {@link AndroidNetworkUtils} to have cleaned; a
	 * later retry overwrites it.
	 */
	@Nullable
	public String downloadMap(@NonNull TramesMap map, @Nullable IProgress progress) {
		File dest = app.getAppPath(map.file);
		File parent = dest.getParentFile();
		if (parent != null) {
			parent.mkdirs();
		}
		String error = AndroidNetworkUtils.downloadFile(BASE_URL + map.file, dest, false, progress);
		if (error == null) {
			// Same tail as ObfImportTask: full re-scan picks up the new .obf, then the
			// download screen's installed-state cache is refreshed.
			app.getResourceManager().reloadIndexes(IProgress.EMPTY_PROGRESS, new ArrayList<>());
			app.getDownloadThread().updateLoadedFiles();
		}
		return error;
	}
}
