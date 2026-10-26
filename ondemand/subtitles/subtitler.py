"""Subtitle on play: fetch, write over the placeholder and tell the player.

Runs in the background as soon as the torrent starts (does not hold PlaybackInfo).
Since starting the torrent takes 5 to 30 s and a subtitle search ~2 s, the real
subtitle is almost always on disk when the player asks for the track. If it
arrives later and the person has the subtitle (index 0) on, the track is toggled
(−1 → 0) so the player fetches the new file — without restarting the video; on
clients without that command, PlayNow at the same position.
Not found: stays 'not_found' and the scheduler tries again later.
"""

import logging
import threading
import time

from ..jellyfin import strm_item_id
from .base import ProviderLimit, extract_srt, score
from .formats import parse_srt, subtitle_path, to_sami, write_atomic

log = logging.getLogger("subtitles")

MIN_LINES = 20        # fewer than this is a trailer's subtitle / garbage
MAX_CANDIDATES = 3    # bad subtitle (no .srt, too short): try the next one by score


class Subtitler:
    def __init__(self, db, jellyfin, provider, lang, to_local=lambda p: p):
        """provider: a subtitle provider (base.py) or None; lang: languages.Language;
        to_local: path as Jellyfin sees it -> path where this process writes."""
        self.db, self.jf, self.provider, self.lang, self.to_local = db, jellyfin, provider, lang, to_local
        self._running = set()
        self._lock = threading.Lock()

    @property
    def source(self):
        return getattr(self.provider, "name", "provider")

    def request(self, rid):
        """Fetch in the background; ignored if a fetch for that release is already running."""
        with self._lock:
            if rid in self._running:
                return
            self._running.add(rid)
        threading.Thread(target=self._run, args=(rid,), name=f"subtitle-{rid}", daemon=True).start()

    def _run(self, rid):
        try:
            self._fetch(rid)
        except Exception as e:
            log.warning("subtitle for %s failed: %s", rid, e)
            self._mark(rid, "not_found", self.source, f"error: {e}")
        finally:
            with self._lock:
                self._running.discard(rid)

    def fetch_now(self, rid):
        """In the caller's thread (queued retries): 'real', 'not_found', 'limit' or
        'running' (a fetch for that release is already running)."""
        with self._lock:
            if rid in self._running:
                return "running"
            self._running.add(rid)
        try:
            return self._fetch(rid)
        except Exception as e:
            log.warning("subtitle for %s failed: %s", rid, e)
            self._mark(rid, "not_found", self.source, f"error: {e}")
            return "not_found"
        finally:
            with self._lock:
                self._running.discard(rid)

    def _mark(self, rid, state, source, detail):
        self.db.execute(
            "INSERT INTO subtitles(release_id,state,source,detail,attempts,updated_at) VALUES(?,?,?,?,1,?) "
            "ON CONFLICT(release_id) DO UPDATE SET state=excluded.state, source=excluded.source, "
            "detail=excluded.detail, attempts=subtitles.attempts+1, updated_at=excluded.updated_at",
            rid, state, source, detail, time.time())

    def fetch(self, rid):
        """True if the release ended up with a real subtitle."""
        return self._fetch(rid) == "real"

    def _fetch(self, rid):
        """'real', 'not_found' or 'limit' (the provider refused: daily quota)."""
        r = self.db.one("SELECT r.id, r.name, r.strm, r.season, r.episode, t.kind, t.imdb, t.name AS title "
                        "FROM releases r JOIN titles t ON t.id=r.title_id WHERE r.id=?", rid)
        if r is None or not r["strm"] or not r["imdb"]:
            return "not_found"
        sub = self.db.one("SELECT state FROM subtitles WHERE release_id=?", rid)
        if sub and sub["state"] == "real":
            return "real"
        if self.provider is None or self.lang is None:
            return "not_found"
        t0 = time.monotonic()
        try:
            if r["kind"] == "series":
                candidates = self.provider.search(r["imdb"], "tv", r["season"], r["episode"])
            else:
                candidates = self.provider.search(r["imdb"])
        except ProviderLimit as e:
            self._mark(rid, "not_found", self.source, str(e))
            log.warning("%s refused (%s): subtitle for %s left for later", self.source, e, rid)
            return "limit"
        if not candidates:
            self._mark(rid, "not_found", self.source, f"no {self.lang.code} subtitle")
            log.info("no %s subtitle on %s for %s (%s)", self.lang.code, self.source, r["title"], r["imdb"])
            return "not_found"
        target = r["name"] or r["title"]
        bad = []
        for cand in sorted(candidates, key=lambda c: score(c, target), reverse=True)[:MAX_CANDIDATES]:
            try:
                ep = (r["season"], r["episode"]) if r["kind"] == "series" else None
                text, name = extract_srt(self.provider.download(cand["url"]), target, ep)
                blocks = parse_srt(text)
                if len(blocks) < MIN_LINES:
                    raise ValueError(f"subtitle with only {len(blocks)} lines ({name})")
            except ValueError as e:      # zip without .srt, a trailer's subtitle: the next candidate
                bad.append(str(e))
                continue
            write_atomic(self.to_local(subtitle_path(r["strm"], self.lang.iso)),
                         to_sami(blocks, self.lang.code, self.lang.name))
            self._mark(rid, "real", self.source, name)
            log.info("%s subtitle for %s (%s): %s, %d lines, %.1fs", self.lang.code, rid, r["title"], name,
                     len(blocks), time.monotonic() - t0)
            self.notify_player(r["strm"])
            return "real"
        raise ValueError("; ".join(bad))

    def notify_player(self, strm):
        """Whoever is already watching this item with the subtitle on now sees the real one. The item
        id comes from the .strm path: only /Sessions, no library listing."""
        try:
            item_id = strm_item_id(strm)
            for s in self.jf.sessions_of_item(item_id):
                ps = s.get("PlayState") or {}
                if ps.get("SubtitleStreamIndex") != 0:      # off (Smart mode) or another track
                    continue
                if "SetSubtitleStreamIndex" in (s.get("SupportedCommands") or []):
                    self.jf.toggle_subtitle(s["Id"], 0)
                else:
                    self.jf.reload(s["Id"], item_id, ps.get("PositionTicks") or 0,
                                   ps.get("MediaSourceId"), ps.get("AudioStreamIndex"), 0)
                try:
                    self.jf.message(s["Id"], self.lang.loaded)
                except Exception:
                    pass
                log.info("player on %s notified (subtitle switched without restarting)", s.get("DeviceName"))
        except Exception as e:
            log.warning("could not notify the player: %s", e)
