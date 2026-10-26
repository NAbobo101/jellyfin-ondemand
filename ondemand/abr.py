"""Adaptive bitrate (ABR): a video's pre-generated HLS ladder, served on the same origin as Jellyfin.

tools/generate-abr.sh writes <abr root>/<itemId>/ with a master.m3u8 of 4 qualities (1080p at 6
and 3.5 Mbit/s, 720p at 2, 480p at 0.9) cut at the same instants. web/abr.js (JavaScript Injector)
swaps the play's source for that master: jellyfin-web's hls.js measures the bandwidth on every
segment and switches quality without pausing.

    GET /ondemand/api/abr                                 {"items": [itemId…]}  (token in a header)
    GET /ondemand/api/abr/<itemId>                        {"available": bool}   (token in a header)
    GET /ondemand/abr/t/<token>/<itemId>/master.m3u8      the ladder
    GET /ondemand/abr/t/<token>/<itemId>/v0/index.m3u8 …  playlists, init_N.mp4, segNNNNN.m4s

The Jellyfin session token goes IN THE PATH because hls.js requests the segments by relative URL,
without jellyfin-web's headers: every request inherits the token and is checked (with Search.user's
10-minute cache). Only files with known names; no "..".
"""

import os
import re

ITEM = r"([0-9a-f]{32})"
LIST_ROUTE = "/api/abr"
INFO_ROUTE = re.compile(r"^/api/abr/" + ITEM + r"$")
FILE_ROUTE = re.compile(r"^/abr/t/([A-Za-z0-9_-]{4,128})/" + ITEM
                        + r"/(master\.m3u8|v[0-3]/(?:index\.m3u8|init(?:_[0-3])?\.mp4|seg\d{5}\.m4s))$")
TYPES = {".m3u8": "application/vnd.apple.mpegurl", ".mp4": "video/mp4", ".m4s": "video/iso.segment"}
CHUNK = 256 * 1024


def items(root):
    """itemIds with a ready ladder (the .tmp folder of a generation in progress does not count)."""
    if not root:
        return []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return sorted(n for n in names if re.fullmatch(ITEM, n) and available(root, n))


def available(root, item_id):
    return bool(root) and os.path.isfile(os.path.join(root, item_id, "master.m3u8"))


def file(root, item_id, relative):
    """(path, content type) of a file of the ladder, or None if it does not exist."""
    if not root:
        return None
    path = os.path.join(root, item_id, relative)
    if not os.path.isfile(path):
        return None
    return path, TYPES[os.path.splitext(path)[1]]
