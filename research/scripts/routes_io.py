"""
Reader for the route-geometry sidecar written by run_experiment.py.

The sidecar is gzip, appended and flushed record by record while the experiment runs. A
flush pushes the compressed bytes to disk but does not write an end-of-stream marker, so
`gzip.open(...)` raises EOFError on a file that is still being written — or on one whose
writer was killed. The bytes are intact; only the terminator is missing.

This reader decompresses tolerantly and drops a trailing partial line, so the sidecar can
be analysed while the run is still in progress and survives an interrupted run. That
matters here for the same reason it mattered for the results CSV: this data costs hours
of routing to regenerate.
"""
import json
import zlib


def read_routes(path, skip_bad=True):
    """Yield route records from a (possibly still-open, possibly truncated) sidecar."""
    with open(path, "rb") as fh:
        raw = fh.read()
    dec = zlib.decompressobj(zlib.MAX_WBITS | 16)
    try:
        text = dec.decompress(raw).decode("utf-8", "ignore")
    except zlib.error:
        # Corrupt beyond the first member; nothing reliable to salvage.
        return
    for line in text.splitlines():
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # Only ever the final line, mid-flush. Anything else is a real problem.
            if not skip_bad:
                raise


def index_by_commute(path):
    """{(state, h_tract, w_tract): record} — the same key the results CSV uses."""
    return {(r["state"], r["h_tract"], r["w_tract"]): r for r in read_routes(path)}
