"""Series in the search outside the library and in "complete on open".

Two doors to the same work (series_ondemand.research -> register -> write -> Jellyfin):
  - search: cards of series (TMDb, in the library's language) that Jellyfin does not have yet;
    "Add series" searches ALL seasons and episodes available and leaves the whole series in the catalog;
  - complete: when a CATALOG series is opened, look for what is missing (at most once every
    `complete_days`). A local series is never touched.

The research is heavy (one query per season on sources without id search): one at a time, in the
background, with the progress as text in the request. Jellyfin is only touched (writes, refresh) when
nobody is watching, except for a person's explicit request, which takes the same fast path as movies.
"""

import logging
import re
import threading
import time

from . import series_filter, series_ondemand
from .jellyfin import TYPE_SERIES, JellyfinError, item_id
from .tvmaze import TVmazeError

log = logging.getLogger("search-series")

IMDB = re.compile(r"^tt\d{7,9}$")
ITEM_ID = re.compile(r"^[0-9a-f]{32}$")


class SeriesError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _poster(url, size="w342"):
    return url.replace("/t/p/original/", f"/t/p/{size}/") if url else None


class SeriesSearch:
    def __init__(self, db, jellyfin, catalog, cfg, tvmaze, registry, metrics, background=True):
        """cfg: the WHOLE configuration (needs [series] and [search])."""
        self.db, self.jf, self.cat, self.cfg, self.tv, self.metrics = db, jellyfin, catalog, cfg, tvmaze, metrics
        self.registry = registry
        self.s = cfg["search"]
        self.background = background
        self._lock = threading.Lock()
        self._heavy = threading.Semaphore(1)             # one full research at a time
        self._cache = {}                                  # term -> (time, cards before the library filter)
        self._ids = {}                                    # tmdb -> ids (they do not change)
        self._lib = (float("-inf"), set())                # (time, ids of the series Jellyfin already has)
        self._details = {}                                # imdb -> (time, details)
        n = self.db.execute("UPDATE series_searches SET state='error', detail='the service restarted; open the series again' "
                            "WHERE state='searching'").rowcount
        if n:
            log.info("%d series search(es) interrupted by the restart marked as error", n)

    @property
    def series_root(self):
        """The catalog's series folder as Jellyfin sees it."""
        return self.cat.to_jellyfin(self.cat.series) if self.cat.series else None

    # ------------------------------------------------------------ what Jellyfin already has
    def _in_library(self):
        t, ids = self._lib
        if time.monotonic() - t > 300:
            ids = self.jf.all_series_ids()
            self._lib = (time.monotonic(), ids)
        return ids

    @staticmethod
    def _has(ids, library):
        return any(f"{k}:{ids[k]}" in library for k in ("tvdb", "imdb", "tmdb") if ids.get(k))

    def _ids_from_tmdb(self, tmdb):
        if tmdb not in self._ids:
            self._ids[tmdb] = self.jf.series_ids_from_tmdb(tmdb)
        return self._ids[tmdb]

    # ------------------------------------------------------------ search
    def cards(self, term):
        """Series cards from TMDb that are NOT in Jellyfin. No torrent check here (it would be several
        researches per keystroke): availability shows up in the request."""
        key, now = term.lower(), time.monotonic()
        with self._lock:
            c = self._cache.get(key)
        if c and now - c[0] < self.s["cache_min"] * 60:
            found = c[1]
        else:
            try:
                remote = self.jf.remote_search_series(term, limit=self.s["tmdb_max"])
            except JellyfinError as e:
                log.warning("RemoteSearch (series) failed: %s", e)
                return []
            found = []
            for r in remote:
                tmdb = (r.get("ProviderIds") or {}).get("Tmdb")
                try:
                    ids = self._ids_from_tmdb(tmdb) if tmdb else {}
                except JellyfinError:
                    continue
                if not ids.get("Imdb") or not ids.get("Tvdb"):
                    continue                              # without both ids the episodes cannot be checked
                found.append({"kind": "series", "imdb": ids["Imdb"], "tvdb": ids["Tvdb"], "tmdb": tmdb,
                              "name": r.get("Name"), "original_name": r.get("Name"),
                              "year": int(r["PremiereDate"][:4]) if r.get("PremiereDate") else None,
                              "rating": None, "poster": _poster(r.get("ImageUrl")), "qualities": [], "seeds": None})
            with self._lock:
                self._cache = {k: v for k, v in self._cache.items() if now - v[0] < self.s["cache_min"] * 60}
                self._cache[key] = (now, found)
        try:
            have = self._in_library()
        except JellyfinError as e:
            log.warning("library series: %s", e)
            have = set()
        requests = {r["imdb"]: r["state"] for r in self.db.all("SELECT imdb, state FROM requests WHERE kind='series'")}
        return [{**a, "request": requests.get(a["imdb"])} for a in found
                if not self._has({"tvdb": a["tvdb"], "imdb": a["imdb"], "tmdb": a["tmdb"]}, have)]

    # ------------------------------------------------------------ details (read only)
    def details(self, imdb):
        if not IMDB.match(imdb or ""):
            raise SeriesError(400, "invalid IMDb id")
        now = time.monotonic()
        with self._lock:
            c = self._details.get(imdb)
        if c and now - c[0] < self.s["cache_min"] * 60:
            d = c[1]
        else:
            d = self._gather_details(imdb)
            with self._lock:
                self._details = {k: v for k, v in self._details.items() if now - v[0] < self.s["cache_min"] * 60}
                self._details[imdb] = (now, d)
        p = self.db.one("SELECT state, detail FROM requests WHERE imdb=? AND kind='series'", imdb)
        return {**d, "request": p["state"] if p else None}

    def _gather_details(self, imdb):
        try:
            r = self.jf.series_by_imdb(imdb) or {}
        except JellyfinError as e:
            log.warning("details %s: TMDb failed: %s", imdb, e)
            r = {}
        try:
            show = self.tv.by_imdb(imdb)
            eps = self.tv.episodes(show["id"]) if show else []
        except TVmazeError as e:
            log.warning("details %s: TVmaze failed: %s", imdb, e)
            show, eps = None, []
        if not r and not show:
            raise SeriesError(404, "no information about this series")
        today = time.strftime("%Y-%m-%d")
        aired = [(t, n) for t, n, d in eps if t >= 1 and d and d <= today]
        return {
            "kind": "series", "imdb": imdb,
            "name": r.get("Name") or (show or {}).get("name"), "original_name": (show or {}).get("name") or r.get("Name"),
            "year": int(r["PremiereDate"][:4]) if r.get("PremiereDate") else None,
            "overview": (r.get("Overview") or "").strip() or None,
            "genres": (show or {}).get("genres") or [], "rating": ((show or {}).get("rating") or {}).get("average"),
            "seasons": len({t for t, _ in aired}), "episodes": len(aired),
            "status": (show or {}).get("status"),
            "poster": _poster(r.get("ImageUrl"), "w500"), "backdrop": None, "cast": [], "trailer_youtube": None,
            "qualities": [],
        }

    # ------------------------------------------------------------ full research
    def _research(self, ids, progress):
        """Runs the sources and the filter; registers in the database. Returns (series, summary, title_id|None)."""
        resolver = series_filter.Resolver(self.tv)
        series = series_ondemand.identify(resolver, {k: ids[k] for k in ("tvdb", "imdb", "tmdb") if ids.get(k)})
        if series is None:
            raise SeriesError(404, "series without TheTVDB or IMDb id: its episodes cannot be checked")
        local = self.jf.local_episodes(catalog_prefix=self.cat.jellyfin_prefix)
        r, res = series_ondemand.research(series, resolver, self.cfg, self.registry, local,
                                          progress=lambda i, n, name: progress(f"Searching {name} ({i}/{n})…"))
        n = series_ondemand.register(self.cat, r, series)
        self.tv.save()
        tid = self.db.one("SELECT id FROM titles WHERE key=?", series["key"])
        res["registered"] = n
        return series, res, (tid["id"] if tid else None)

    def _write_and_wait(self, tid, series, idle_first=False):
        """Write the folders of the new episodes, make Jellyfin create/refresh the series and return
        (item or None, episodes written). Waits for Jellyfin to count the episodes written now (a series
        that already existed has children from the start)."""
        if idle_first:
            limit = time.monotonic() + 900
            while not self.jf.idle() and time.monotonic() < limit:
                time.sleep(30)
        # only the episodes written NOW count towards the wait (those with a .strm Jellyfin already counted)
        new = self.db.one("SELECT count(*) n FROM releases WHERE title_id=? AND strm IS NULL", tid)["n"]
        folder = self.cat.write_series(tid)
        sid = item_id(TYPE_SERIES, folder)
        before = self._series_item(sid)
        base = (before or {}).get("RecursiveItemCount") or 0
        if before:
            self.jf.refresh_item(sid, recursive=True)
        else:
            root = folder.rsplit("/", 1)[0]
            if not self.jf.refresh_folder(root):
                self.jf.notify_paths([folder])
        limit = time.monotonic() + self.s["item_wait_s"]
        while time.monotonic() < limit:
            found = self._series_item(sid)
            count = found.get("RecursiveItemCount") if found else None
            if found and (found.get("ChildCount") or 0) > 0 and (not new or count is None or count >= base + new):
                return found, new
            if found and not found.get("ChildCount"):
                try:                                     # born without seasons: ask for them to be created
                    self.jf.refresh_item(sid, recursive=True)
                except JellyfinError:
                    pass
            time.sleep(3)
        return (self._series_item(sid) if (before and not new) else None), new

    def _series_item(self, sid):
        # EnableUserData=true: without it Jellyfin omits ChildCount/RecursiveItemCount (they come back None)
        found = self.jf._items(Ids=sid, Fields="ChildCount,RecursiveItemCount,Path,ProviderIds",
                               EnableImages="true", EnableUserData="true")
        return found[0] if found else None

    # ------------------------------------------------------------ request (search outside the library)
    def prepare_request(self, imdb, progress):
        """Runs in the request's thread. Returns (item_id, result text); raises SeriesError."""
        with self._heavy:
            try:
                ids = self._request_ids(imdb)
                series, res, tid = self._research(ids, progress)
            except (TVmazeError, JellyfinError) as e:
                raise SeriesError(502, f"a source is down: {e}") from None
            if not res["episodes_found"]:
                raise SeriesError(404, "no torrent with seeders for this series")
            progress("Building the series in Jellyfin…")
            item, _ = self._write_and_wait(tid, series)
        if item is None:
            raise SeriesError(504, "Jellyfin did not create the series in time; it will show up in a few minutes")
        return item["Id"], f"{res['episodes_found']} of {res['episodes_aired']} episodes"

    def _request_ids(self, imdb):
        for c in self.known_cards(imdb):
            return {"imdb": c["imdb"], "tvdb": c["tvdb"], "tmdb": c["tmdb"]}
        r = self.jf.series_by_imdb(imdb) or {}
        tmdb = (r.get("ProviderIds") or {}).get("Tmdb")
        ids = self._ids_from_tmdb(tmdb) if tmdb else {}
        if not ids.get("Tvdb"):
            raise SeriesError(404, "series without TheTVDB id: its episodes cannot be checked")
        return {"imdb": imdb, "tvdb": ids["Tvdb"], "tmdb": tmdb}

    def known_cards(self, imdb):
        with self._lock:
            for _, found in self._cache.values():
                for a in found:
                    if a["imdb"] == imdb:
                        yield a

    # ------------------------------------------------------------ complete on open
    def complete_status(self, tvdb):
        r = self.db.one("SELECT state, found, detail FROM series_searches WHERE tvdb=?", str(tvdb))
        return {"tvdb": str(tvdb), "state": r["state"], "found": r["found"], "detail": r["detail"]} if r else None

    def complete(self, item_id_, force=False):
        """A catalog series opened by someone: look for what is missing. Returns the status (202 if it started)."""
        if not ITEM_ID.match(item_id_ or ""):
            raise SeriesError(400, "invalid item")
        item = self.jf.item(item_id_)
        root = self.series_root
        if not item or item.get("Type") != "Series" or not root or not (item.get("Path") or "").startswith(root + "/"):
            raise SeriesError(422, "only catalog series are completed (local ones stay as they are)")
        ids = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items() if k in ("Tvdb", "Imdb", "Tmdb") and v}
        if not ids.get("tvdb"):
            raise SeriesError(422, "series without TheTVDB id")
        cur = self.complete_status(ids["tvdb"])
        if cur and cur["state"] == "searching":
            return cur
        last = self.db.one("SELECT last_at, state FROM series_searches WHERE tvdb=?", ids["tvdb"])
        if last and not force:
            age = time.time() - (last["last_at"] or 0)
            limit = 3600 if last["state"] == "error" else self.cfg["series"]["complete_days"] * 86400
            if age < limit:
                return {**self.complete_status(ids["tvdb"]), "recent": True}
        c = self.db.execute(
            "INSERT INTO series_searches(tvdb,imdb,last_at,state,found,detail) VALUES(?,?,?,'searching',0,'Searching…') "
            "ON CONFLICT(tvdb) DO UPDATE SET imdb=excluded.imdb, last_at=excluded.last_at, state='searching', found=0, "
            "detail='Searching…' WHERE series_searches.state<>'searching'", ids["tvdb"], ids.get("imdb"), time.time())
        if c.rowcount == 0:
            return self.complete_status(ids["tvdb"])
        log.info("complete: %s (tvdb %s)", item.get("Name"), ids["tvdb"])
        if self.background:
            threading.Thread(target=self._complete, args=(ids,), name=f"complete-{ids['tvdb']}", daemon=True).start()
        else:
            self._complete(ids)
        return self.complete_status(ids["tvdb"])

    def _mark_complete(self, tvdb, state, found=None, detail=None):
        self.db.execute("UPDATE series_searches SET state=?, found=COALESCE(?, found), detail=?, last_at=? "
                        "WHERE tvdb=?", state, found, detail, time.time(), tvdb)

    def _complete(self, ids):
        tvdb = ids["tvdb"]
        try:
            with self._heavy:
                series, res, tid = self._research(
                    ids, lambda text: self.db.execute("UPDATE series_searches SET detail=? WHERE tvdb=?", text, tvdb))
                written = 0
                if res["registered"] and tid:
                    self._mark_complete(tvdb, "searching", None, "Building the series in Jellyfin…")
                    _, written = self._write_and_wait(tid, series, idle_first=True)
            text = f"{res['episodes_found']} of {res['episodes_aired']} episodes"
            self._mark_complete(tvdb, "ready", written, text)
            self.metrics.inc("search_requests_total", result="complete")
            log.info("complete %s: %s (%d new episodes in the catalog)", tvdb, text, written)
        except (SeriesError, TVmazeError, JellyfinError, OSError) as e:
            self._mark_complete(tvdb, "error", None, str(e))
            log.warning("complete %s: %s", tvdb, e)
        except Exception as e:
            self._mark_complete(tvdb, "error", None, "internal failure")
            log.exception("complete %s failed: %s", tvdb, e)
