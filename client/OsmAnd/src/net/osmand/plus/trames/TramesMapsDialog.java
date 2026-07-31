package net.osmand.plus.trames;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;

import net.osmand.plus.OsmandApplication;
import net.osmand.plus.R;

import java.io.File;
import java.util.List;
import java.util.Locale;

/**
 * The in-app catalogue of TRAMES's ALPR-tagged offline maps: pick a state, download it,
 * and the offline router starts avoiding cameras there.
 *
 * <p>Deliberately a plain dialog rather than a fragment in OsmAnd's download screen. That
 * screen is built around {@code DownloadResources} / {@code IndexItem} and the
 * download.osmand.net catalogue; our maps come from a different host with a different
 * (much simpler) manifest, and grafting them into that model would mean faking index items
 * for a catalogue that doesn't exist upstream. A self-contained dialog over
 * {@link TramesMapDownloader} keeps the whole feature in the {@code trames} package.
 *
 * <p>Progress is measured by polling the destination file's size against the size the
 * manifest advertises, rather than by counting {@code IProgress} deltas. The delta units
 * depend on {@code Algorithms.streamCopy}'s divider, which is an implementation detail we
 * would silently mis-scale if it ever changed; a file on disk versus a known byte count
 * cannot drift.
 *
 * <p>All network work runs on plain threads with results posted back to the main looper —
 * downloads here are minutes long for a big state, well past what a short-lived task
 * abstraction is meant to hold.
 */
public class TramesMapsDialog {

	private TramesMapsDialog() {
	}

	/** Open the catalogue. Fetches the manifest first, so it shows a spinner immediately. */
	public static void show(@NonNull Activity activity) {
		OsmandApplication app = (OsmandApplication) activity.getApplication();
		TramesMapDownloader downloader = new TramesMapDownloader(app);

		AlertDialog loading = new AlertDialog.Builder(activity)
				.setView(messageView(activity, activity.getString(R.string.trames_maps_loading)))
				.setCancelable(true)
				.show();

		new Thread(() -> {
			List<TramesMapDownloader.TramesMap> maps = downloader.fetchManifest();
			post(() -> {
				dismiss(loading);
				if (activity.isFinishing()) {
					return;
				}
				if (maps == null || maps.isEmpty()) {
					toast(activity, activity.getString(R.string.trames_maps_unavailable));
					return;
				}
				showList(activity, downloader, maps);
			});
		}, "trames-manifest").start();
	}

	private static void showList(@NonNull Activity activity,
	                             @NonNull TramesMapDownloader downloader,
	                             @NonNull List<TramesMapDownloader.TramesMap> maps) {
		CharSequence[] labels = new CharSequence[maps.size()];
		for (int i = 0; i < maps.size(); i++) {
			TramesMapDownloader.TramesMap m = maps.get(i);
			// "California — 2.0 GB" / "· installed" so the state of each row is obvious
			// without a custom adapter.
			labels[i] = prettyName(m.name) + " — " + humanSize(m.size)
					+ (downloader.isInstalled(m) ? "  ·  " + activity.getString(R.string.trames_maps_installed) : "");
		}
		new AlertDialog.Builder(activity)
				.setTitle(R.string.trames_maps_title)
				.setItems(labels, (dialog, which) -> confirm(activity, downloader, maps.get(which)))
				.setNegativeButton(R.string.shared_string_close, null)
				.show();
	}

	private static void confirm(@NonNull Activity activity,
	                            @NonNull TramesMapDownloader downloader,
	                            @NonNull TramesMapDownloader.TramesMap map) {
		boolean installed = downloader.isInstalled(map);
		String msg = activity.getString(installed
				? R.string.trames_maps_redownload_prompt
				: R.string.trames_maps_download_prompt, prettyName(map.name), humanSize(map.size));
		new AlertDialog.Builder(activity)
				.setTitle(prettyName(map.name))
				.setMessage(msg)
				.setPositiveButton(R.string.shared_string_download, (d, w) -> download(activity, downloader, map))
				.setNegativeButton(R.string.shared_string_cancel, null)
				.show();
	}

	private static void download(@NonNull Activity activity,
	                             @NonNull TramesMapDownloader downloader,
	                             @NonNull TramesMapDownloader.TramesMap map) {
		TextView text = new TextView(activity);
		text.setText(activity.getString(R.string.trames_maps_downloading, prettyName(map.name)));
		ProgressBar bar = new ProgressBar(activity, null, android.R.attr.progressBarStyleHorizontal);
		bar.setMax(100);
		bar.setIndeterminate(map.size <= 0);   // no advertised size → nothing to scale against

		LinearLayout box = box(activity);
		box.addView(text);
		box.addView(bar);

		AlertDialog dialog = new AlertDialog.Builder(activity)
				.setView(box)
				.setCancelable(false)   // cancelling mid-write would leave a truncated .obf
				.show();

		File dest = downloader.localFile(map);
		Handler handler = new Handler(Looper.getMainLooper());
		boolean[] done = {false};

		// Poll the file on disk — see class javadoc for why not IProgress deltas.
		Runnable poll = new Runnable() {
			@Override
			public void run() {
				if (done[0]) {
					return;
				}
				if (map.size > 0 && dest.exists()) {
					int pct = (int) Math.min(100, dest.length() * 100 / map.size);
					bar.setProgress(pct);
					text.setText(activity.getString(R.string.trames_maps_downloading, prettyName(map.name))
							+ "  " + pct + "%");
				}
				handler.postDelayed(this, 500);
			}
		};
		handler.postDelayed(poll, 500);

		new Thread(() -> {
			String error = downloader.downloadMap(map, null);
			post(() -> {
				done[0] = true;
				handler.removeCallbacks(poll);
				dismiss(dialog);
				if (activity.isFinishing()) {
					return;
				}
				toast(activity, error == null
						? activity.getString(R.string.trames_maps_ready, prettyName(map.name))
						: activity.getString(R.string.trames_maps_failed, error));
			});
		}, "trames-download").start();
	}

	// ---- small helpers -------------------------------------------------------------

	/** "district-of-columbia" -> "District Of Columbia". */
	private static String prettyName(@NonNull String slug) {
		StringBuilder sb = new StringBuilder(slug.length());
		for (String part : slug.split("-")) {
			if (part.isEmpty()) {
				continue;
			}
			if (sb.length() > 0) {
				sb.append(' ');
			}
			sb.append(Character.toUpperCase(part.charAt(0))).append(part.substring(1));
		}
		return sb.length() == 0 ? slug : sb.toString();
	}

	private static String humanSize(long bytes) {
		if (bytes <= 0) {
			return "?";
		}
		double mb = bytes / (1024d * 1024d);
		return mb >= 1024
				? String.format(Locale.US, "%.1f GB", mb / 1024d)
				: String.format(Locale.US, "%.0f MB", mb);
	}

	private static LinearLayout box(@NonNull Activity activity) {
		LinearLayout box = new LinearLayout(activity);
		box.setOrientation(LinearLayout.VERTICAL);
		int pad = (int) (16 * activity.getResources().getDisplayMetrics().density);
		box.setPadding(pad, pad, pad, pad);
		return box;
	}

	private static View messageView(@NonNull Activity activity, @NonNull String message) {
		LinearLayout box = box(activity);
		TextView tv = new TextView(activity);
		tv.setText(message);
		tv.setGravity(Gravity.CENTER);
		box.addView(tv);
		box.addView(new ProgressBar(activity));
		return box;
	}

	private static void post(@NonNull Runnable r) {
		new Handler(Looper.getMainLooper()).post(r);
	}

	private static void dismiss(@Nullable AlertDialog dialog) {
		if (dialog != null && dialog.isShowing()) {
			try {
				dialog.dismiss();
			} catch (IllegalArgumentException e) {
				// Activity went away underneath us; nothing left to dismiss.
			}
		}
	}

	private static void toast(@NonNull Activity activity, @NonNull String message) {
		Toast.makeText(activity, message, Toast.LENGTH_LONG).show();
	}
}
