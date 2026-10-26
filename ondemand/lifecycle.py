"""Lifecycle of the catalog's torrents (loop every ~30 s).

    trial ──(public: 15 min watched | private: at once)──> committed ──(100 %)──> seeding
      └──(public: stopped early + 10 min with nobody)──> removed (abandoned)
    seeding ──(provider's seed rule met + N days unwatched + nobody reading)──> removed

- "Continue watching": whoever stopped midway has a saved position in Jellyfin.
  Without the torrent, resuming at minute 40 would download from the start again
  (sequential; qBittorrent 4.6 cannot prioritise a piece) and the gateway would give
  up after 60 s. So a saved position holds the seed for `days_with_position` (not 2
  days) and, during the trial, counts as a commitment.

- Private tracker (ratio rules): once started, it goes to the end — a half-downloaded
  torrent that gets deleted risks a hit-and-run. When the VIDEO completes, the other
  files go back to priority 1 (seeding only counts with the torrent at 100 %); not
  before, because changing priorities with the torrent running stalls libtorrent and
  someone is watching.
- Force start while seeding: with the queue of active torrents the seed would stop.
- Playback comes first: while a catalog title plays and still downloads, the other
  downloads (outside the catalog) are throttled; the old limit is stored in the
  database and restored afterwards — even after a restart (reconciliation).
- A torrent that was already the user's ('foreign') never enters here: never deleted.
"""

import logging
import time

from .jellyfin import strm_item_id

log = logging.getLogger("lifecycle")

RECHECK_POSITION_S = 600     # torrent kept for a saved position: ask Jellyfin again every 10 min

# Unknown provider: conservative (private, 7 days, ratio 1.0).
DEFAULT_RULE = {"private": True, "min_seed_hours": 168, "min_ratio": 1.0, "either": False}


class Lifecycle:
    def __init__(self, cfg, db, qbit, jellyfin, service, rules=None):
        """rules(provider_name) -> seed rule dict ({private, min_seed_hours, min_ratio, either}) or None."""
        self.cfg, self.db, self.qbit, self.jf, self.service = cfg, db, qbit, jellyfin, service
        self.c = cfg["lifecycle"]
        self.rules = rules or (lambda name: None)
        self._access_saved = {}      # infohash -> last last_access written (only writes what changed)
        self._kept = set()           # infohash already logged as "kept for a saved position"
        self._position_seen = {}     # infohash -> (query time, largest position in s) — "continue watching"

    def rule(self, provider, private):
        """The provider's seed rule; unknown = conservative. A release marked private is private
        whatever its provider says: never abandoned halfway."""
        r = dict(DEFAULT_RULE)
        r.update(self.rules(provider) or {})
        if private:
            r["private"] = True
        return r

    # ------------------------------------------------------------ who is watching
    def playing(self):
        """{release_id: position in seconds} of what is playing in Jellyfin now. The item id of
        each .strm is computed: no item query to Jellyfin."""
        sessions = self.jf.playing()
        if not sessions:
            return {}
        by_id = {strm_item_id(r["strm"]): r["id"]
                 for r in self.db.all("SELECT id, strm FROM releases WHERE strm IS NOT NULL")}
        seen = {}
        for s in sessions:
            rid = by_id.get(s["NowPlayingItem"].get("Id"))
            if rid:
                pos = ((s.get("PlayState") or {}).get("PositionTicks") or 0) / 1e7
                seen[rid] = max(pos, seen.get(rid, 0))
        return seen

    def saved_position(self, h, group):
        """Largest saved position (s) among the group's items, for any user; 0 = nobody stopped
        midway. None = Jellyfin did not answer: the caller does not delete in this round."""
        seen = self._position_seen.get(h)
        if seen and time.time() - seen[0] < RECHECK_POSITION_S:
            return seen[1]
        marks = ",".join("?" * len(group))
        strms = [r["strm"] for r in self.db.all(f"SELECT strm FROM releases WHERE id IN ({marks}) "
                                                "AND strm IS NOT NULL", *group)]
        try:
            pos = self.jf.positions([strm_item_id(s) for s in strms]) if strms else {}
        except Exception as e:
            log.warning("torrent %s: saved position not queried (%s) — left for the next step", h[:8], e)
            return None
        sec = max(pos.values(), default=0) / 1e7
        self._position_seen[h] = (time.time(), sec)
        return sec

    # ------------------------------------------------------------ transitions
    def _mark(self, h, state, **fields):
        sets = ", ".join(["state=?"] + [f"{k}=?" for k in fields])
        self.db.execute(f"UPDATE torrents SET {sets} WHERE infohash=?", state, *fields.values(), h)

    def _delete(self, row, reason, group=None):
        self.qbit.delete([row["infohash"]], with_files=True)
        self._mark(row["infohash"], "removed", removed_at=time.time(), reason=reason)
        for rid in group or [row["release_id"]]:
            self.service.forget(rid)
        log.info("torrent %s (release %s) deleted: %s", row["infohash"][:8], row["release_id"], reason)

    def _release_rest(self, row):
        """Private: after the video completed, the rest downloads again (torrent at 100 % for
        seeding). True if released (or nothing left); False if the video is not complete yet."""
        files = self.qbit.files(row["infohash"])
        video = next((f for f in files if f["name"] == row["file"]), None)
        if video is None or video["progress"] < 1:
            return False
        rest = [f["index"] for f in files if f["priority"] == 0]
        if rest:
            self.qbit.set_file_priority(row["infohash"], rest, 1)
        self.db.execute("UPDATE torrents SET rest_released=1 WHERE infohash=?", row["infohash"])
        log.info("torrent %s: video complete, %d remaining file(s) released to reach 100 %%",
                 row["infohash"][:8], len(rest))
        return True

    def step(self):
        now = time.time()
        watching = self.playing()
        for rid in watching:
            self.db.execute("UPDATE titles SET last_play=? WHERE id=(SELECT title_id FROM releases WHERE id=?)",
                            now, rid)
        in_qb = {t["hash"]: t for t in self.qbit.info(category=self.cfg["qbittorrent"]["category"])}
        downloading_and_watching = False
        # Series: the episodes of a pack are different releases of the SAME torrent (torrent_tag). Whoever
        # watches or reads any of them holds the whole torrent, and deleting the torrent forgets them all.
        siblings = {}
        for x in self.db.all("SELECT id, torrent_tag FROM releases WHERE torrent_tag IS NOT NULL"):
            siblings.setdefault(x["torrent_tag"], []).append(x["id"])
        rows = self.db.all(
            "SELECT t.*, r.provider, r.private, r.title_id, r.torrent_tag, ti.last_play FROM torrents t "
            "JOIN releases r ON r.id=t.release_id JOIN titles ti ON ti.id=r.title_id "
            "WHERE t.state IN ('trial','committed','seeding')")
        for row in rows:
            h, rid = row["infohash"], row["release_id"]
            qb = in_qb.get(h)
            if qb is None:
                # Confirm one by one: an empty/partial list (qBittorrent restarting, category changed
                # by hand) would mark them all as removed and the lifecycle would drop live torrents.
                qb = self.qbit.one(h)
                if qb is None:
                    self._mark(h, "removed", removed_at=now, reason="vanished from qBittorrent")
                    for r_ in siblings.get(row["torrent_tag"], [rid]):
                        self.service.forget(r_)
                    continue
            group = siblings.get(row["torrent_tag"], [rid])
            sources = [s for s in (self.service.sources.get(g) for g in group) if s is not None]
            reading = any(s.readers > 0 for s in sources)
            access = max((s.last_access for s in sources), default=None)
            group_watching = any(g in watching for g in group)
            if access is not None and self._access_saved.get(h) != access:
                self.db.execute("UPDATE torrents SET last_access=? WHERE infohash=?", access, h)
                self._access_saved[h] = access
            rule = self.rule(row["provider"], row["private"])
            if group_watching and qb["progress"] < 1:
                downloading_and_watching = True
            if not qb.get("force_start"):
                self.qbit.force_start([h], True)        # the queue of active torrents would stop download/seed

            if row["state"] == "trial":
                if qb["progress"] >= 1:                    # completed while still in trial: nothing to abandon
                    if rule["private"] and not row["rest_released"]:
                        # private: the "100 %" is only the video's; committed releases the rest and seeds
                        self._mark(h, "committed", committed_at=now)
                    else:
                        self._mark(h, "seeding", committed_at=now, completed_at=now)
                    continue
                if rule["private"]:
                    self._mark(h, "committed", committed_at=now)
                elif watching.get(rid, 0) >= self.c["commit_minutes"] * 60:
                    self._mark(h, "committed", committed_at=now)
                    log.info("torrent %s: watched for %d min — goes to 100 %%", h[:8], self.c["commit_minutes"])
                elif not group_watching and not reading and \
                        now - (access if access is not None else (row["last_access"] or row["started_at"])) \
                        > self.c["abandon_minutes"] * 60:
                    pos = self.saved_position(h, group)
                    if pos is None:
                        continue
                    if pos > 0:                            # stopped midway to come back later: download to the end
                        self._mark(h, "committed", committed_at=now)
                        log.info("torrent %s: stopped at %d min with a saved position — goes to 100 %%", h[:8], pos // 60)
                    else:
                        self._delete(row, f"abandoned (stopped before {self.c['commit_minutes']} min)", group)
                continue

            if row["state"] == "committed":
                if rule["private"] and not row["rest_released"] and self._release_rest(row):
                    continue                               # the rest is downloading again: 100 % again later
                if qb["progress"] >= 1:
                    self._mark(h, "seeding", completed_at=now)
                    log.info("torrent %s complete: seeding", h[:8])
                continue

            # seeding
            time_ok = qb.get("seeding_time", 0) >= rule["min_seed_hours"] * 3600
            ratio_ok = not rule["min_ratio"] or qb.get("ratio", 0) >= rule["min_ratio"]
            # either: the tracker's rule is "X hours OR ratio Y"; otherwise both
            met = (time_ok or ratio_ok) if rule.get("either") else (time_ok and ratio_ok)
            last = max(row["last_play"] or 0, row["completed_at"] or 0, access or 0)
            idle = now - last > self.c["days_unwatched"] * 86400
            if met and idle and not group_watching and not reading:
                if now - last <= self.c["days_with_position"] * 86400:
                    pos = self.saved_position(h, group)
                    if pos is None or pos > 0:
                        if pos and h not in self._kept:
                            self._kept.add(h)
                            log.info("torrent %s kept: saved position at %d min (up to %d days unwatched)",
                                     h[:8], pos // 60, self.c["days_with_position"])
                        continue
                self._delete(row, "seed rule met and unwatched", group)
        self.throttle_others(downloading_and_watching)

    # ------------------------------------------------------------ playback first
    def throttle_others(self, active):
        saved = {r["infohash"]: r["limit_bps"] for r in self.db.all("SELECT * FROM saved_limits")}
        cat = self.cfg["qbittorrent"]["category"]
        if active:
            limit = self.c["throttle_others_kbs"] * 1024
            new = [t for t in self.qbit.info(filter="downloading")
                   if t.get("category") != cat and t["hash"] not in saved]
            for t in new:
                self.db.execute("INSERT OR IGNORE INTO saved_limits(infohash, limit_bps) VALUES(?,?)",
                                t["hash"], t.get("dl_limit", 0))
            if new:
                self.qbit.limit_download([t["hash"] for t in new], limit)
                log.info("playback first: %d outside download(s) throttled to %d KB/s", len(new),
                         self.c["throttle_others_kbs"])
        elif saved:
            restored = 0
            for h, lim in saved.items():
                try:
                    self.qbit.limit_download([h], lim if lim and lim > 0 else 0)
                except Exception as e:
                    log.warning("could not restore the limit of %s (will retry on the next step): %s", h[:8], e)
                    continue
                # Delete only what was restored: otherwise an error would leave that torrent throttled forever.
                self.db.execute("DELETE FROM saved_limits WHERE infohash=?", h)
                restored += 1
            log.info("playback first: limit restored on %d of %d download(s)", restored, len(saved))
