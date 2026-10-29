"""Fallback: an episode's torrent does not play -> the next candidate kept in the archive.

Whether the archived item is dead is decided by `archive.failure` (severe = at once; transient = on the 2nd
failure at least 30 min after the first). Once it is dead, the episodes of that torrent (a pack = several)
switch torrents:
  1. the dead torrent leaves qBittorrent (if it stayed there: an empty swarm) and the lifecycle;
  2. the usual filter (`series_filter.build_for_series`) runs on what the archive has of the series only,
     without the dead items and with no network beyond TVmaze's cache;
  3. each episode gets the new winner (`Database.switch_torrent`): the id and the .strm do not change, the
     subtitle goes back to the placeholder (the real one was matched to another encode) and the next click
     already uses the fallback.
A torrent someone is reading is not switched (if it delivers bytes, it is not dead).
"""

import logging
import threading
import time

from . import archive, series_filter
from .series_ondemand import episode_release, identify
from .subtitles.formats import placeholder_sami, subtitle_path, write_atomic

log = logging.getLogger("fallback")


class Fallback:
    def __init__(self, cfg, db, qbit, tv, catalog=None, metrics=None, service=None):
        self.cfg, self.db, self.qbit, self.tv = cfg, db, qbit, tv
        self.cat, self.metrics, self.service = catalog, metrics, service
        self._lock = threading.Lock()

    def failed(self, rid, reason, severe, background=True):
        """Record the failure of this release's torrent; if the archived item died, switch (in a thread)."""
        r = self.db.one("SELECT torrent_tag FROM releases WHERE id=?", rid)
        if r is None or not r["torrent_tag"]:
            return None                          # a movie: no fallback (yet)
        state = archive.failure(self.db, r["torrent_tag"], reason, severe)
        if state != "dead":
            if state is not None:
                log.info("release %s: failure recorded (%s); one more 30 min or later sends the torrent to the fallback",
                         rid, reason)
            return state
        log.warning("release %s: torrent dead (%s) — looking for a fallback", rid, reason)
        if background:
            threading.Thread(target=self.switch, args=(r["torrent_tag"],), name=f"fallback-{rid}", daemon=True).start()
        else:
            self.switch(r["torrent_tag"])
        return state

    def _reading(self, rids):
        sources = getattr(self.service, "sources", {})
        return any(getattr(sources.get(rid), "readers", 0) > 0 for rid in rids)

    def _drop_torrent(self, tag, rids):
        """The dead torrent leaves qBittorrent (only ours, by tag; never a foreign one) and the lifecycle."""
        try:
            hashes = [t["hash"] for t in self.qbit.info(tag=f"od-{tag}")]
            if hashes:
                self.qbit.delete(hashes, with_files=True)
        except Exception as e:                   # qBittorrent down: the lifecycle catches it later ("vanished")
            log.warning("could not delete the dead torrent od-%s: %s", tag, e)
        marks = ",".join("?" * len(rids))
        self.db.execute(f"UPDATE torrents SET state='removed', removed_at=?, reason='dead torrent (fallback)' "
                        f"WHERE release_id IN ({marks}) AND state<>'removed' AND foreign_=0", time.time(), *rids)
        for rid in rids:
            if self.service is not None:
                self.service.forget(rid)

    def switch(self, tag):
        """Returns how many episodes switched torrents."""
        with self._lock:
            try:
                return self._switch(tag)
            except Exception:
                log.exception("fallback of torrent %s failed", tag)
                return 0

    def _switch(self, tag):
        rels = self.db.all("SELECT r.*, t.key, t.tvdb, t.imdb, t.tmdb, t.name AS series_name, t.year FROM releases r "
                           "JOIN titles t ON t.id=r.title_id WHERE r.torrent_tag=?", tag)
        if not rels:
            return 0
        rids = [r["id"] for r in rels]
        if self._reading(rids):
            log.info("torrent %s has a reader right now: not switching", tag)
            return 0
        self._drop_torrent(tag, rids)
        t = rels[0]
        resolver = series_filter.Resolver(self.tv)
        series = identify(resolver, {k: t[k] for k in ("tvdb", "imdb", "tmdb") if t[k]}, t["series_name"], t["year"])
        if series is None:
            log.warning("fallback: series %s has no tvdb/imdb", t["series_name"])
            return 0
        items = archive.find_series(self.db, series["name"], [t["key"]])
        if series["name"] != t["series_name"]:
            seen = {(i.provider, archive.ident_of(i)) for i in items}
            items += [i for i in archive.find_series(self.db, t["series_name"]) if (i.provider, archive.ident_of(i)) not in seen]
        r = series_filter.build_for_series([i.candidate() for i in items], series, resolver, self.cfg["series"])
        try:
            self.tv.save()
        except Exception:
            pass
        archive.record_result(self.db, r)
        seasons = (r["series"].get(series["key"]) or {}).get("seasons", {})
        switched = 0
        for rel in rels:
            d = seasons.get(rel["season"], {}).get(rel["episode"])
            if d is None:
                log.warning("fallback: %s %s has no other torrent in the archive (stays until the next 'complete')",
                            rel["series_name"], rel["label"])
                continue
            new = episode_release(d["winner"], rel["season"], rel["episode"])
            if new["torrent_tag"] == tag:
                continue
            self.db.switch_torrent(rel["id"], new)
            self._placeholder(rel)
            switched += 1
            log.info("fallback: %s %s -> %s (%s, %d seeds)", rel["series_name"], rel["label"], new["provider"],
                     new["quality"], new["seeds"] or 0)
        if switched and self.metrics is not None:
            self.metrics.inc("fallbacks_used_total", switched)
        return switched

    def _placeholder(self, rel):
        lang = getattr(self.cat, "lang", None)
        if not rel["strm"] or lang is None:
            return
        try:
            write_atomic(subtitle_path(self.cat.to_local(rel["strm"]), lang.iso), placeholder_sami(lang))
        except OSError as e:
            log.warning("fallback: could not rewrite the placeholder subtitle of %s: %s", rel["id"], e)

