"""Torrent pieces: which part of the file is already on disk.

qBittorrent 4.6 has no per-piece priority API; what can be done is read
`pieceStates` (0 = missing, 1 = downloading, 2 = downloaded) and only hand
Jellyfin the contiguous run of complete pieces. One Watcher per torrent does
that polling and wakes every waiting thread — several readers (probe,
transcode, subtitle extraction) do not multiply the API calls.
"""

import logging
import threading
import time

log = logging.getLogger("pieces")

COMPLETE = 2


class PieceMap:
    """Translates a position inside the file -> torrent piece."""

    def __init__(self, piece_size, offset, size):
        self.piece_size, self.offset, self.size = piece_size, offset, size
        self.first = offset // piece_size
        self.last = (offset + size - 1) // piece_size

    def piece(self, pos):
        return (self.offset + pos) // self.piece_size

    def available_until(self, pos, states):
        """Largest p such that [pos, p) of the file is in complete pieces."""
        if pos >= self.size or not states:
            return pos
        p = self.piece(pos)
        n = len(states)
        while p <= self.last and p < n and states[p] == COMPLETE:
            p += 1
        if p > self.last:
            return self.size
        end = p * self.piece_size - self.offset
        return end if end > pos else pos

    def fraction_complete(self, states):
        if not states:
            return 0.0
        span = states[self.first:self.last + 1]
        return sum(1 for s in span if s == COMPLETE) / max(1, len(span))


class Watcher:
    """Reads a torrent's pieceStates and wakes whoever waits for a piece.

    Polls every `interval` s while someone is waiting; every `idle` s otherwise
    (a new waiter wakes the loop at once if the last read is stale); stops for
    good when the files of interest complete. "Complete" means the pieces of the
    files being served (`interest`), not the whole torrent: in a multi-file
    torrent the rest stays at priority 0 and would never reach 100 %.
    """

    def __init__(self, qbit, infohash, interval=1.0, idle=30.0):
        self.qbit, self.hash, self.interval, self.idle = qbit, infohash, interval, idle
        self.states = []
        self.complete = False
        self.gone = False         # the torrent was removed from qBittorrent (externally)
        self.cond = threading.Condition()
        self._spans = set()       # (first piece, last piece) of each file being served
        self._waiting = 0
        self._stop = False
        self._read_at = 0.0
        self._last_warning = 0.0
        self._thread = threading.Thread(target=self._loop, name=f"watcher-{infohash[:8]}", daemon=True)
        self._thread.start()

    def interest(self, first, last):
        with self.cond:
            self._spans.add((first, last))

    def _is_complete(self, st):
        if not st:
            return False
        if not self._spans:
            return all(s == COMPLETE for s in st)
        return all(p < len(st) and st[p] == COMPLETE for a, b in self._spans for p in range(a, b + 1))

    def _loop(self):
        while not self._stop:
            self._read_at = time.monotonic()     # time of the attempt (errors too: no tight loop)
            try:
                st = self.qbit.piece_states(self.hash)
                with self.cond:
                    self.complete = self._is_complete(st)
                    self.states = [] if self.complete else st    # complete: the map is no longer needed
                    self.cond.notify_all()
                if self.complete:
                    log.info("torrent %s complete — watcher finished", self.hash[:8])
                    return
            except Exception as e:   # qBittorrent restarting, network: try again
                if getattr(e, "code", None) == 404:
                    # Torrent deleted in qBittorrent (by the user or by the lifecycle): it will not come back.
                    log.warning("torrent %s vanished from qBittorrent — watcher finished", self.hash[:8])
                    with self.cond:
                        self.gone = True
                        self.cond.notify_all()
                    return
                if time.monotonic() - self._last_warning > 60:
                    log.warning("pieceStates for %s failed: %s", self.hash[:8], e)
                    self._last_warning = time.monotonic()
            with self.cond:
                # Wake every `interval` (without calling the API) to see whether someone started
                # waiting; read when the last read expired: `interval` with waiters, `idle` without.
                while not self._stop:
                    left = self._read_at + (self.interval if self._waiting else self.idle) - time.monotonic()
                    if left <= 0:
                        break
                    self.cond.wait(timeout=min(left, self.interval))

    def poke(self):
        """Ask for a read now."""
        with self.cond:
            self.cond.notify_all()

    def wait(self, condition, timeout, cancelled=lambda: False):
        deadline = time.monotonic() + timeout
        with self.cond:
            self._waiting += 1
            if time.monotonic() - self._read_at > self.interval:
                self.cond.notify_all()      # stale read: the loop reads now (without hammering the API)
            try:
                while not condition():
                    if self.complete:
                        return condition()
                    if self.gone or self._stop:
                        return False
                    left = deadline - time.monotonic()
                    if left <= 0 or cancelled():
                        return False
                    self.cond.wait(timeout=min(1.0, left))
                return True
            finally:
                self._waiting -= 1

    def stop(self):
        self._stop = True
        self.poke()
