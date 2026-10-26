"""Byte sources the gateway serves to Jellyfin.

A Source answers two questions: up to where, starting at `pos`, the bytes are
already on disk (`available_until`), and wait until byte `pos` arrives
(`wait_for`). The gateway never reads a region the Source has not guaranteed:
in a sparse qBittorrent file, reading a range not yet downloaded returns zeros.

- FileSource: a complete file (finished torrent). Everything is available.
- SimulatedSource: a complete file pretending to be a sequential download (for
  tests and manual trials): start delay, rate, first/last slice early and
  configurable holes.
- TorrentSource (torrent.py): qBittorrent pieces.
"""

import mimetypes
import os
import threading
import time

MIME = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".m4v": "video/mp4",
        ".avi": "video/x-msvideo", ".ts": "video/mp2t", ".webm": "video/webm"}


class Unavailable(Exception):
    """The media cannot be served right now (no seeders, qBittorrent error...).
    The gateway answers 503 — only before the headers have been sent."""


def mime_of(path):
    ext = os.path.splitext(path)[1].lower()
    return MIME.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


class Source:
    name = "?"
    path = None
    size = 0

    def __init__(self):
        self._readers = 0
        self._lock = threading.Lock()
        self.last_access = time.time()     # wall clock: stored in the database (lifecycle)

    @property
    def mime(self):
        return mime_of(self.path)

    def available_until(self, pos):
        """Largest p such that [pos, p) is entirely on disk. p == pos: nothing yet."""
        raise NotImplementedError

    def wait_for(self, pos, timeout, cancelled=lambda: False):
        """Block until byte `pos` is available. False if `timeout` (s) expires
        or `cancelled()` becomes true (client disconnected)."""
        deadline = time.monotonic() + timeout
        while self.available_until(pos) <= pos:
            if cancelled() or time.monotonic() >= deadline:
                return False
            time.sleep(0.5)
        return True

    def open(self):
        return open(self.path, "rb")

    # Open reads: the lifecycle never deletes a torrent someone is reading.
    def reader_started(self):
        with self._lock:
            self._readers += 1
            self.last_access = time.time()

    def reader_finished(self):
        with self._lock:
            self._readers = max(0, self._readers - 1)
            self.last_access = time.time()

    @property
    def readers(self):
        return self._readers


class FileSource(Source):
    def __init__(self, path, name=None):
        super().__init__()
        self.path = path
        self.name = name or os.path.basename(path)
        self.size = os.path.getsize(path)

    def available_until(self, pos):
        return self.size


class SimulatedSource(FileSource):
    """Complete file that pretends to be downloading sequentially.

    delay_s: seconds until the "swarm" delivers the first byte.
    rate: bytes/s of the sequential download after the start.
    edge: bytes at the start and at the end released at the start (first/last piece).
    holes: list of (start, end) that never arrive (to test a stuck seek).
    """

    def __init__(self, path, delay_s=0.0, rate=6_000_000, edge=8 << 20, holes=()):
        super().__init__(path)
        self.t0 = time.monotonic()
        self.delay_s, self.rate, self.edge = delay_s, rate, edge
        self.holes = list(holes)

    def _sequential_until(self):
        t = time.monotonic() - self.t0 - self.delay_s
        if t < 0:
            return 0
        return min(self.size, self.edge + int(t * self.rate))

    def available_until(self, pos):
        t = time.monotonic() - self.t0 - self.delay_s
        if t < 0 or pos >= self.size:
            return pos
        seq = self._sequential_until()
        if pos < seq:
            end = seq
        elif pos >= self.size - self.edge:
            end = self.size          # last slice, released early
        else:
            return pos
        for a, b in self.holes:
            if a <= pos < b:
                return pos
            if pos < a < end:
                end = a
        return end
