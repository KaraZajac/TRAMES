package net.osmand.plus.onlinerouting.engine;

import androidx.annotation.NonNull;

import net.osmand.util.Algorithms;

public class EngineType {

	/**
	 * TRAMES: the retired TRAMES online engine. Deliberately not in {@link #values()}, so it
	 * can no longer be chosen for a new engine; still recognised by name, so an engine an
	 * earlier version saved is read back as what it is and TramesDefaults can remove it.
	 * Without this, {@link #getTypeByName} would fall back to GraphHopper and quietly leave
	 * a profile pointed at a third-party server.
	 */
	public static final OnlineRoutingEngine TRAMES_TYPE = new TramesEngine(null);
	public static final OnlineRoutingEngine GRAPHHOPPER_TYPE = new GraphhopperEngine(null);
	public static final OnlineRoutingEngine OSRM_TYPE = new OsrmEngine(null);
	public static final OnlineRoutingEngine ORS_TYPE = new OrsEngine(null);
	public static final OnlineRoutingEngine GPX_TYPE = new GpxEngine(null);

	private static OnlineRoutingEngine[] enginesTypes;

	public static OnlineRoutingEngine[] values() {
		if (enginesTypes == null) {
			enginesTypes = new OnlineRoutingEngine[]{
					GRAPHHOPPER_TYPE,
					OSRM_TYPE,
					ORS_TYPE,
					GPX_TYPE
			};
		}
		return enginesTypes;
	}

	@NonNull
	public static OnlineRoutingEngine getTypeByName(@NonNull String typeName) {
		if (Algorithms.objectEquals(TRAMES_TYPE.getTypeName(), typeName)) {
			return TRAMES_TYPE;
		}
		for (OnlineRoutingEngine type : values()) {
			if (Algorithms.objectEquals(type.getTypeName(), typeName)) {
				return type;
			}
		}
		return values()[0];
	}

}
