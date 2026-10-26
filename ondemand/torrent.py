"""Gateway <-> qBittorrent bridge: put the torrent to download and serve its video.

The order avoids the known libtorrent/qBittorrent 4.6 pitfalls:
1. add PAUSED, tagged od-<id>, in the ondemand category, sequential + first/last
   piece, no ratio/time limit, "Original" layout. By URL: qBittorrent fetches
   the .torrent itself (through whatever network path it uses, e.g. a VPN).
2. wait until it shows up under the tag (add does not return the hash);
3. export the .torrent -> real offsets (pad files included);
4. file priorities WHILE STILL PAUSED (changing them with the torrent running
   stalls libtorrent for 10–60 s);
5. force start (skips the 3-download queue) — and NOT resume (that turns force off).

Re-entry (container restarted, source forgotten): an already configured torrent
is neither paused nor gets its priorities redone — pausing drops the peers of a
running download and, on a private tracker, would reset to 0 the rest of the
files the lifecycle already released.

A torrent that already existed in qBittorrent (the user's own) is found by
infohash when the provider knows it, and reused — marked so the lifecycle NEVER
deletes it. The .torrent is never fetched from here: the container may not share
qBittorrent's network path (VPN), and private tracker URLs carry the passkey.
"""

import logging
import os
import re
import time

from .bencode import read_torrent
from .pieces import PieceMap
from .qbit import QBitError
from .sources import Source, Unavailable

log = logging.getLogger("torrent")

VIDEO = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".webm")
SUBTITLE = (".srt", ".smi", ".ass", ".ssa", ".sub", ".vtt")
DANGEROUS = (".exe", ".scr", ".lnk", ".bat", ".cmd", ".msi", ".vbs", ".js", ".jar", ".rar", ".zip", ".7z", ".iso")
PAUSED = ("pausedDL", "pausedUP", "stoppedDL", "stoppedUP")


def to_local(qbit_path, path_map):
    """Path as qBittorrent sees it -> path inside this container.
    `path_map` = {qbit prefix: local prefix}; the longest matching prefix wins."""
    for prefix in sorted(path_map, key=len, reverse=True):
        p = prefix.rstrip("/") + "/"
        if qbit_path.startswith(p):
            return path_map[prefix].rstrip("/") + "/" + qbit_path[len(p):]
    raise Unavailable(f"path outside the configured [qbittorrent] path_map: {qbit_path}")


class TorrentSource(Source):
    def __init__(self, watcher, meta, file, local_path, alternatives=()):
        """alternatives: other places where the file may be (qBittorrent's incomplete
        folder; it moves the file to save_path on completion)."""
        super().__init__()
        self.watcher, self.file = watcher, file
        self.path = local_path
        self.alternatives = [c for c in alternatives if c != local_path]
        self.name = os.path.basename(local_path)
        self.size = file.size
        self.map = PieceMap(meta.piece_size, file.offset, file.size)
        watcher.interest(self.map.first, self.map.last)

    def available_until(self, pos):
        if self.watcher.complete:
            return self.size if pos < self.size else pos
        return self.map.available_until(pos, self.watcher.states)

    def wait_for(self, pos, timeout, cancelled=lambda: False):
        return self.watcher.wait(lambda: self.available_until(pos) > pos, timeout, cancelled)

    def open(self):
        for c in [self.path] + self.alternatives:
            for name in (c, c + ".!qB"):
                try:
                    return open(name, "rb")    # no exists() first: the .!qB rename fits in between
                except FileNotFoundError:
                    continue
        raise Unavailable("the file does not exist on disk yet")


def choose_video(meta, episode=None, preferred=None):
    """Largest video in the torrent; in a season pack, the file of the episode (season, number);
    with `preferred`, the file whose name ends like that (torrent with several versions)."""
    videos = [f for f in meta.real_files if f.path.lower().endswith(VIDEO)
              and "sample" not in os.path.basename(f.path).lower()]
    if preferred:
        videos = [f for f in videos if f.path.endswith(preferred)]
    if episode:
        s, e = episode
        pattern = re.compile(rf"(s0*{s}[ ._-]*e0*{e}(?!\d)|\b0*{s}x0*{e}(?!\d))", re.I)
        videos = [f for f in videos if pattern.search(os.path.basename(f.path))]
    if not videos:
        raise Unavailable("no matching video file in the torrent")
    return max(videos, key=lambda f: f.size)


def check_safety(meta, min_video_mb):
    """For public providers: nothing executable/archived and a plausible video size."""
    bad = [f.path for f in meta.real_files if f.path.lower().endswith(DANGEROUS)]
    if bad:
        raise Unavailable(f"torrent refused: suspicious file {os.path.basename(bad[0])}")
    largest = max((f.size for f in meta.real_files if f.path.lower().endswith(VIDEO)), default=0)
    if largest < min_video_mb * 1_000_000:
        raise Unavailable("torrent refused: video too small (fake?)")


def _qbit_file(qb_files, meta, f):
    """torrents/files entry of the video: by name; if the layout changed the name (a user's
    torrent with a different sub-folder), by position among the real files with the same size."""
    found = next((x for x in qb_files if x["name"] == f.path), None)
    if found is None:
        idx = next((i for i, a in enumerate(meta.real_files) if a is f or a.path == f.path), None)
        found = next((x for x in qb_files if x["index"] == idx and x.get("size") == f.size), None)
    if found is None:
        raise Unavailable("could not match the torrent's file to qBittorrent's file list")
    return found


def _export(qbit, h, until):
    """.torrent from qBittorrent. Magnet: wait for the metadata until `until` (monotonic) (409 =
    not there yet; 4.6.3 also answers 409 while it checks the files right after the add)."""
    while True:
        try:
            return qbit.export(h)
        except QBitError as e:
            if e.code != 409:
                raise
            if time.monotonic() >= until:
                raise Unavailable("the magnet's metadata did not arrive in time (no seeders?)") from None
            time.sleep(0.5)


def start(qbit, release_id, torrent_url, cfg, episode=None, public=False, infohash=None, preferred=None,
          tag=None, keep=(), min_video_mb=None):
    """Put the release's torrent to download. Returns a dict with hash, meta, file,
    path (local), alternatives and 'foreign' (True = a torrent that already existed: never delete).

    `cfg` is the [qbittorrent] section (save_path, path_map, category, timeouts) plus an optional
    compiled `subtitle_pattern` (subtitles in the configured language get top priority).

    Known infohash: looked up directly by hash — if it is already in qBittorrent without the
    ondemand tag, it is the user's own torrent: reuse it and never delete it.

    Series: `episode` = (season, number) picks its file inside the pack, and `tag` is shared by the
    episodes of the same torrent (an RSS item's infohash is unknown before the add). A pack already
    downloading: the requested episode jumps the queue (the other episodes' incomplete files go to 0,
    except those in `keep`, which someone else is reading); the lifecycle gives the rest back when
    the episode completes.

    Magnet: a PAUSED torrent never fetches metadata in qBittorrent 4.6.3 (measured: 90 s stuck in
    pausedDL, size 0). So the add goes UNPAUSED with stopCondition=MetadataReceived: the metadata
    arrives in seconds and qBittorrent stops by itself, without downloading anything — from then on
    it is the same flow as a .torrent (priorities while paused, force start)."""
    category = cfg.get("category", "ondemand")
    tag = tag or f"od-{release_id}"
    deadline = time.monotonic() + cfg.get("torrent_wait_s", 20) + 10     # deadline for the whole start
    foreign = new = False
    found = qbit.info(tag=tag)
    if not found and infohash:
        existing = qbit.one(infohash.lower())
        if existing:
            found = [existing]
            # the category tag (not this release's tag): a torrent with several versions is ours too
            foreign = category not in (existing.get("tags") or "").split(",")
    magnet = (torrent_url or "").startswith("magnet:")
    if not found:
        extra = {"stopCondition": "MetadataReceived"} if magnet else {}
        qbit.add(url=torrent_url, savepath=cfg["save_path"], category=category,
                 tags=f"{category},{tag}", paused=not magnet, sequentialDownload=True,
                 firstLastPiecePrio=True, autoTMM=False, contentLayout="Original",
                 ratioLimit=-1, seedingTimeLimit=-1, **extra)
        new = True
        limit = min(deadline, time.monotonic() + cfg.get("torrent_wait_s", 20))
        while time.monotonic() < limit and not found:
            time.sleep(0.5)
            found = qbit.info(tag=tag) or (qbit.info(hashes=infohash.lower()) if infohash else [])
        if not found:
            # The tracker did not deliver the .torrent, or it already exists in qBittorrent without
            # our tag and with no known infohash (RSS item): nothing is fetched from here.
            raise Unavailable("qBittorrent did not queue the torrent (tracker down, or it already "
                              f"exists in qBittorrent without the {category} tag)")
    t = found[0]
    h = t["hash"]
    meta = read_torrent(_export(qbit, h, time.monotonic() + (cfg.get("metadata_wait_s", 40) if magnet else 0)))
    deadline = max(deadline, time.monotonic() + 10)   # waiting for metadata does not eat the priorities' deadline
    try:
        if public:
            check_safety(meta, min_video_mb or cfg.get("min_video_mb", 300))
        f = choose_video(meta, episode, preferred)
    except Unavailable:
        if new:
            # Refused before any priority was set: a torrent left behind would later be "re-entered"
            # downloading every file, and nothing in the database would ever clean it up.
            qbit.delete([h], with_files=True)
        raise
    qb_files = qbit.files(h)
    video = _qbit_file(qb_files, meta, f)

    # Pack already downloading: the requested episode must jump the queue (sequential follows file order)
    others = [x for x in qb_files if x["index"] != video["index"] and x.get("priority", 0) > 0
              and x.get("progress", 0) < 1 and x["name"] not in keep and not x["name"].lower().endswith(SUBTITLE)]
    switch_target = bool(episode) and not new and video.get("progress", 0) < 1 and bool(others)
    if not foreign and t.get("progress", 0) < 1 and (new or video.get("priority") != 1 or switch_target):
        # File priority only with the torrent PAUSED (running, libtorrent stalls 10–60 s).
        # Right after the add it may still be in checkingResumeData/metaDL or already running.
        if t.get("state") not in PAUSED:
            qbit.pause([h])
            while time.monotonic() < deadline:
                time.sleep(0.25)
                t = qbit.one(h) or t
                if t.get("state") in PAUSED:
                    break
        pattern = cfg.get("subtitle_pattern")
        subs = [x["index"] for x in qb_files
                if x["name"].lower().endswith(SUBTITLE) and pattern is not None and pattern.search(x["name"])]
        # NEVER priority 7 on the video: in sequential mode libtorrent ignores priorities, EXCEPT 7,
        # which jumps ahead of the order — the whole video at 7 becomes "rarest first" and the start
        # of the file does not arrive (measured). Video at 1 (normal); qBittorrent's "first/last
        # piece" option is what uses 7, for the start and the end.
        qbit.set_file_priority(h, [video["index"]], 1)
        qbit.set_file_priority(h, subs, 7)          # subtitle in the wanted language: tiny, arrives first
        # The rest stays out while the video downloads (sequential would follow file order). A
        # private torrent gets the rest back at 1 when the video completes (lifecycle.py): seeding
        # only counts with the torrent at 100%. Only for a freshly added torrent: on re-entry the
        # rest may be what the lifecycle already released, or another version's video playing now.
        if new:
            rest = [x["index"] for x in qb_files if x["index"] != video["index"] and x["index"] not in subs]
            qbit.set_file_priority(h, rest, 0)
        elif switch_target:
            qbit.set_file_priority(h, [x["index"] for x in others], 0)
    if not foreign:
        if not new:                                       # the add already turns both on
            qbit.set_sequential(h, True, t)
            qbit.set_first_last(h, True, t)
        # setForceStart already resumes in forced mode. Do NOT call resume afterwards: resume puts
        # the torrent back in normal mode and turns force start off (3-download queue).
        qbit.force_start([h], True)
    # Real name on disk (qBittorrent layout/renaming); with the incomplete folder on, the file
    # sits in download_path until complete and then moves to save_path.
    bases = [b for b in (t.get("download_path"), t["save_path"]) if b]
    paths = [to_local(os.path.join(b, video["name"]), cfg.get("path_map") or {}) for b in bases]
    log.info("release %s -> %s (%s, %.2f GB, piece %d KiB)%s%s", release_id, h[:8], os.path.basename(f.path),
             f.size / 1e9, meta.piece_size // 1024, " [foreign]" if foreign else "", "" if new else " [re-entry]")
    return {"hash": h, "meta": meta, "file": f, "path": paths[0], "alternatives": paths[1:],
            "foreign": foreign, "new": new, "switched_target": switch_target and not foreign}
