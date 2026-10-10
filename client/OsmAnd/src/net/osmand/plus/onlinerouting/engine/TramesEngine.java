package net.osmand.plus.onlinerouting.engine;

import androidx.annotation.NonNull;
import androidx.annotation.Nullable;

import java.util.Map;

import static net.osmand.plus.onlinerouting.engine.EngineType.TRAMES_TYPE;

/**
 * The retired TRAMES online engine — GraphHopper with the camera cones baked into the graph,
 * served at routing.blackflagintel.com from 2026-07-25 until 2026-08-29.
 *
 * <p>Since v1.2.4 the app has no online routing of its own: it routes offline, against maps
 * that carry the cameras, and nothing about a trip leaves the phone. This class remains only
 * so that an engine an earlier version saved is read back as a TRAMES engine (see
 * {@link EngineType#TRAMES_TYPE}), which lets {@link net.osmand.plus.trames.TramesDefaults}
 * find it, move any profile that used it to offline routing, and delete it. It cannot be
 * created from the UI, and its standard URL is empty, so even an engine that somehow
 * survived would send nothing anywhere.
 *
 * <p>The routing server in {@code server/} is still what the study routes against; the app
 * no longer talks to one.
 */
public class TramesEngine extends GraphhopperEngine {

	public TramesEngine(@Nullable Map<String, String> params) {
		super(params);
	}

	@NonNull
	@Override
	public OnlineRoutingEngine getType() {
		return TRAMES_TYPE;
	}

	@NonNull
	@Override
	public String getTitle() {
		return "TRAMES (retired)";
	}

	@NonNull
	@Override
	public String getTypeName() {
		return "TRAMES";
	}

	@NonNull
	@Override
	public String getStandardUrl() {
		return "";
	}

	@Override
	public OnlineRoutingEngine newInstance(Map<String, String> params) {
		return new TramesEngine(params);
	}
}
