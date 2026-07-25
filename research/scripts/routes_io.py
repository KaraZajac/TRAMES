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
    """
    Yield route records from a (possibly still-open, possibly truncated) sidecar.

    The file can hold SEVERAL concatenated gzip members, not one: every process that
    opens it in append mode starts a new member, so the main experiment contributes one
    and each backfill pass another. A single `decompressobj` stops at the end of the
    first member and reports success, which silently dropped 2,537 backfilled routes —
    the analysis ran on 15,043 of 17,580 while looking complete. Decode members in a
    loop, following `unused_data`, until the input is consumed.
    """
    with open(path, "rb") as fh:
        raw = fh.read()

    offset = 0
    while offset < len(raw):
        dec = zlib.decompressobj(zlib.MAX_WBITS | 16)
        try:
            text = dec.decompress(raw[offset:]).decode("utf-8", "ignore")
        except zlib.error:
            # Corrupt or truncated mid-member; emit nothing further.
            return
        for line in text.splitlines():
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # Only ever the final line of a member, mid-flush.
                if not skip_bad:
                    raise
        consumed = len(raw) - offset - len(dec.unused_data)
        if consumed <= 0:
            return
        offset += consumed


def index_by_commute(path):
    """{(state, h_tract, w_tract): record} — the same key the results CSV uses."""
    return {(r["state"], r["h_tract"], r["w_tract"]): r for r in read_routes(path)}
