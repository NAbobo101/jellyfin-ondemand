"""HTTP Range header (RFC 9110).

Only one range per request: that is what Jellyfin's ffmpeg/ffprobe sends. If
more than one arrives, the first is served (no client we know of does that).
"""

# Sentinel: the Range is syntactically valid but cannot be satisfied -> 416.
UNSATISFIABLE = object()


def parse_range(header, size):
    """Return (start, end) with an INCLUSIVE end, None (no Range: whole file)
    or UNSATISFIABLE (reply 416 with `Content-Range: bytes */size`).

    Bad syntax or an unknown unit = Range ignored (None), as the RFC says.
    """
    if not header:
        return None
    unit, sep, spec = header.strip().partition("=")
    if not sep or unit.strip().lower() != "bytes":
        return None
    spec = spec.split(",")[0].strip()
    start, sep, end = spec.partition("-")
    if not sep:
        return None
    start, end = start.strip(), end.strip()
    try:
        if start == "":
            # Suffix "bytes=-N": the last N bytes.
            n = int(end)
            if n <= 0 or size <= 0:
                return UNSATISFIABLE
            return (max(0, size - n), size - 1)
        a = int(start)
        b = int(end) if end else size - 1
    except ValueError:
        return None
    if a < 0:
        return None
    if a >= size:          # before the b < a check: in "bytes=N-" the implicit end is size-1
        return UNSATISFIABLE
    if b < a:
        return None
    return (a, min(b, size - 1))
