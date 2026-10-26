"""The catalog on disk: one folder per movie and per library, ONE .strm in each.

    <root>/Movies/Name (Year) [imdbid-tt…]/
        Name (Year) [imdbid-tt…].strm     -> <gateway>/v/<release 1080p (or 720p)>
        Name (Year) [imdbid-tt…].<iso>.smi  (placeholder; becomes the real subtitle)
    <root>/Movies 4K/Name (Year) [imdbid-tt…]/…   (only if there is a 4K release)
    <root>/Series/Name (Year) [tvdbid-N]/Season 01/Name S01E02.strm

Why one version per item (measured): PlaybackInfo's refresh probes ALL the
versions of a movie on every play — with the version picker, one play
downloaded the start of all three; and a probe that fails makes Jellyfin
transcode everything on the CPU. 4K lives in its own library, chosen by
browsing into it.

Writes only with nobody watching and in batches (each new folder becomes a
metadata and image lookup in Jellyfin), and only if the marker file proves
the right disk is mounted — never writes under an empty mount point.
"""

import errno
import logging
import os
import re
import time

from .jellyfin import strm_item_id
from .subtitles.formats import placeholder_sami, subtitle_path, write_atomic

log = logging.getLogger("catalog")

MARKER = ".ondemand-marker"
_FORBIDDEN = re.compile(r'[\\/*?"<>|]')


def clean_name(text):
    """A folder name that works on Linux, Samba and Jellyfin."""
    t = text.replace(":", " -")
    t = _FORBIDDEN.sub("", t)
    t = re.sub(r"\s+", " ", t).strip().rstrip(".")
    return t or "Untitled"


def movie_folder(root, name, year, imdb):
    base = f"{clean_name(name)} ({year})" if year else clean_name(name)
    return os.path.join(root, f"{base} [imdbid-{imdb}]")


def series_folder(root, series):
    """`Name (Year) [tvdbid-N]`: the id in the folder is what makes Jellyfin merge it with a local series."""
    base = f"{clean_name(series['name'])} ({series['year']})" if series.get("year") else clean_name(series["name"])
    if series.get("tvdb"):
        return os.path.join(root, f"{base} [tvdbid-{series['tvdb']}]")
    return os.path.join(root, f"{base} [imdbid-{series['imdb']}]")


def episode_path(folder, name, season, episode):
    """Season 01/Name S01E02.strm: the layout Jellyfin understands without help."""
    return os.path.join(folder, f"Season {season:02d}", f"{clean_name(name)} S{season:02d}E{episode:02d}.strm")


def strm_path(folder):
    return os.path.join(folder, os.path.basename(folder) + ".strm")


def choose_releases(releases):
    """(main, 4k): main = 1080p, else 720p, else SD; 4k = the one labelled 4K, if any."""
    by = {r["label"]: r for r in releases}
    return by.get("1080p") or by.get("720p") or by.get("SD"), by.get("4K")


class Catalog:
    def __init__(self, db, jellyfin, root, gateway_url, movies="Movies", movies_4k=None, series=None,
                 jellyfin_root=None, lang=None):
        """root: where THIS process writes (holds the marker file); jellyfin_root: the same folder
        as Jellyfin sees it (default: the same path). movies/movies_4k/series: sub-folders, each
        one added to a Jellyfin library (None = off). lang: languages.Language for the subtitle
        placeholder (None = no subtitles)."""
        self.db, self.jf, self.lang = db, jellyfin, lang
        self.root = root.rstrip("/")
        self.jf_root = (jellyfin_root or root).rstrip("/")
        sub = lambda s: os.path.join(self.root, s.strip("/")) if s else None
        self.movies, self.movies_4k, self.series = sub(movies), sub(movies_4k), sub(series)
        self.gateway_url = gateway_url.rstrip("/")

    # ------------------------------------------------------------ paths
    def to_jellyfin(self, path):
        """A path under root -> the same path as Jellyfin sees it."""
        return self.jf_root + path[len(self.root):] if path.startswith(self.root) else path

    def to_local(self, path):
        """A path as Jellyfin sees it -> where this process reads/writes it."""
        return self.root + path[len(self.jf_root):] if path.startswith(self.jf_root) else path

    @property
    def jellyfin_prefix(self):
        """What every catalog path starts with, as Jellyfin sees it (to tell catalog from local media)."""
        return self.jf_root + "/"

    def disk_ok(self):
        return os.path.exists(os.path.join(self.root, MARKER))

    def _placeholder(self, strm, rid):
        if self.lang is None:
            return
        sub = subtitle_path(strm, self.lang.iso)
        if not os.path.exists(sub):          # FINAL name from the start (Jellyfin bug #15882)
            write_atomic(sub, placeholder_sami(self.lang))
        self.db.execute("INSERT OR IGNORE INTO subtitles(release_id,state,updated_at) "
                        "VALUES(?, 'placeholder', ?)", rid, time.time())

    # ------------------------------------------------------------ registration
    def register(self, title, versions, local_ids):
        """Store in the database; a title that already exists locally is marked and gets no folder."""
        if not versions:
            return None
        if f"imdb:{title['imdb']}" in local_ids or (title.get("tmdb") and f"tmdb:{title['tmdb']}" in local_ids):
            title = {**title, "state": "duplicate_local"}
        tid = self.db.save_title(title)
        for v in versions:
            self.db.save_release(tid, v)
        return tid

    def register_series(self, series, episodes):
        """Store the series (title of kind 'series', key tvdb:N) and one episode per release. `episodes`:
        [{"label","season","episode",...}] in Database.save_release's format."""
        tid = self.db.save_title({
            "key": series["key"], "kind": "series", "imdb": series.get("imdb"), "tmdb": series.get("tmdb"),
            "tvdb": series.get("tvdb"), "name": series["name"], "year": series.get("year"), "origin": "series"})
        for e in episodes:
            self.db.save_release(tid, e)
        return tid

    # ------------------------------------------------------------ series
    def write_pending_series(self, batch=5, require_idle=True):
        """Write the episodes that have no .strm yet, for up to `batch` series at a time (each new folder
        becomes a metadata and image lookup: Jellyfin has a single queue). Returns how many series."""
        if not self.series:
            return 0
        if not self.disk_ok():
            log.error("marker missing in %s — disk unmounted? nothing written", self.root)
            return 0
        if require_idle and not self.jf.idle():
            return 0
        rows = self.db.all(
            "SELECT t.* FROM titles t WHERE t.kind='series' AND t.state<>'error' AND EXISTS "
            "(SELECT 1 FROM releases r WHERE r.title_id=t.id AND r.strm IS NULL) ORDER BY t.id LIMIT ?", batch)
        folders, n = [], 0
        for t in rows:
            try:
                folders.append(self._write_series(t))
                n += 1
            except OSError as e:
                log.error("could not write series %s: %s", t["name"], e)
                if e.errno in (errno.ENAMETOOLONG, errno.EINVAL, errno.EILSEQ):
                    self.db.execute("UPDATE titles SET state='error' WHERE id=?", t["id"])
        if folders:
            self.jf.notify_paths([self.to_jellyfin(p) for p in folders])
            log.info("%d series written and announced to Jellyfin", n)
        return n

    def write_series(self, tid):
        """This series, now (a search request or "complete"): no batch, no idle wait. Returns the folder
        as Jellyfin sees it. Announcing the folder is up to the caller (it also does a priority refresh)."""
        if not self.series:
            raise OSError("the series catalog is off ([catalog] series = \"\")")
        if not self.disk_ok():
            raise OSError(f"marker missing in {self.root} — disk unmounted?")
        t = self.db.one("SELECT * FROM titles WHERE id=?", tid)
        return self.to_jellyfin(self._write_series(t))

    def _write_series(self, t):
        folder = series_folder(self.series, {"name": t["name"], "year": t["year"], "tvdb": t["tvdb"], "imdb": t["imdb"]})
        eps = self.db.all("SELECT * FROM releases WHERE title_id=? AND strm IS NULL ORDER BY season, episode", t["id"])
        for r in eps:
            strm = episode_path(folder, t["name"], r["season"], r["episode"])
            os.makedirs(os.path.dirname(strm), exist_ok=True)
            write_atomic(strm, f"{self.gateway_url}/v/{r['id']}\n")
            self.db.execute("UPDATE releases SET strm=? WHERE id=?", self.to_jellyfin(strm), r["id"])
            self._placeholder(strm, r["id"])
        self.db.execute("UPDATE titles SET folder=?, state='written', written_at=? WHERE id=?",
                        self.to_jellyfin(folder), time.time(), t["id"])
        return folder

    # ------------------------------------------------------------ movies
    def _write_release(self, root, t, r):
        folder = movie_folder(root, t["name"], t["year"], t["imdb"])
        os.makedirs(folder, exist_ok=True)
        strm = strm_path(folder)
        write_atomic(strm, f"{self.gateway_url}/v/{r['id']}\n")
        self.db.execute("UPDATE releases SET strm=? WHERE id=?", self.to_jellyfin(strm), r["id"])
        self._placeholder(strm, r["id"])
        return folder

    def _write_title(self, t):
        main, uhd = choose_releases(self.db.all("SELECT * FROM releases WHERE title_id=?", t["id"]))
        folders = []
        if main and self.movies:
            folders.append(self._write_release(self.movies, t, main))
        if uhd and self.movies_4k:
            folders.append(self._write_release(self.movies_4k, t, uhd))
        self.db.execute("UPDATE titles SET folder=?, state='written', written_at=? WHERE id=?",
                        self.to_jellyfin(folders[0]) if folders else None, time.time(), t["id"])
        return folders

    def write_pending(self, batch=50, require_idle=True):
        """Write up to `batch` new movies (best rated, then most seeded first). Returns how many."""
        if not self.disk_ok():
            log.error("marker missing in %s — disk unmounted? nothing written", self.root)
            return 0
        if require_idle and not self.jf.idle():
            return 0
        pending = self.db.all("SELECT * FROM titles WHERE state='new' AND kind='movie' "
                              "ORDER BY COALESCE(rating, 0) DESC, "
                              "(SELECT MAX(seeds) FROM releases WHERE title_id=titles.id) DESC LIMIT ?", batch)
        folders, n = [], 0
        for t in pending:
            try:
                folders += self._write_title(t)
                n += 1
            except OSError as e:
                log.error("could not write %s: %s", t["name"], e)
                if e.errno in (errno.ENAMETOOLONG, errno.EINVAL, errno.EILSEQ):
                    # a name the disk refuses: otherwise it would take a slot of every batch forever
                    self.db.execute("UPDATE titles SET state='error' WHERE id=?", t["id"])
        if folders:
            self.jf.notify_paths([self.to_jellyfin(p) for p in folders])
            log.info("%d new titles (%d folders) written and announced to Jellyfin", n, len(folders))
        return n

    def write_title(self, tid):
        """One title, now, requested from the search: no batch, no idle wait (a single folder is light
        for Jellyfin). Returns the .strm of the main version (or of the 4K, if that is all there is)
        as Jellyfin sees it."""
        if not self.disk_ok():
            raise OSError(f"marker missing in {self.root} — disk unmounted?")
        t = self.db.one("SELECT * FROM titles WHERE id=?", tid)
        folders = self._write_title(t)
        # Priority refresh of the library folder (Movies before Movies 4K: the queue is first come,
        # first served); the notice, which takes over a minute, is the fallback.
        for p in folders:
            if not self.jf.refresh_folder(os.path.dirname(self.to_jellyfin(p))):
                self.jf.notify_paths([self.to_jellyfin(p)])
        log.info("%s (%s) written on request (%d folders)", t["name"], t["year"], len(folders))
        return self.main_strm(tid)

    def main_strm(self, tid):
        """.strm (as Jellyfin sees it) of the version that plays: 1080p/720p, or the 4K if that is all."""
        main, uhd = choose_releases(self.db.all("SELECT * FROM releases WHERE title_id=?", tid))
        chosen = main or uhd
        return chosen["strm"] if chosen else None

    # ------------------------------------------------------------ maintenance
    def lock_identified(self, limit=50, min_age_s=300, give_up_h=24):
        """LockData on the items already matched (with IMDb id and poster): every play stops running
        the metadata providers (faster PlaybackInfo, stable name). Locking before the poster arrives
        would stop Jellyfin from fetching the images.

        Only with nobody watching and only the pending ones, by the computed id of each .strm. The
        ready ones are picked before the limit; a title without a poster after `give_up_h` is locked
        with just the IMDb id, or leaves the queue (locked=-1) — otherwise it would be checked forever
        and hold the queue."""
        now = time.time()
        pending = self.db.all(
            "SELECT t.id, t.written_at, r.strm FROM titles t JOIN releases r ON r.title_id=t.id "
            "WHERE t.state='written' AND t.locked=0 AND t.kind='movie' AND r.strm IS NOT NULL AND t.written_at < ? "
            "ORDER BY t.id LIMIT 600", now - min_age_s)
        if not pending or not self.jf.idle():
            return 0
        by_title = {}
        for p in pending:
            by_title.setdefault(p["id"], (p["written_at"] or 0, []))[1].append(p["strm"])
        items = {i["Id"]: i for i in self.jf.items(strm_item_id(p["strm"]) for p in pending)}
        n = gave_up = 0
        for tid, (written_at, paths) in by_title.items():
            if n >= limit:
                break
            found = [items.get(strm_item_id(c)) for c in paths]
            with_imdb = all(i and (i.get("ProviderIds") or {}).get("Imdb") for i in found)
            with_poster = with_imdb and all((i.get("ImageTags") or {}).get("Primary") for i in found)
            old = now - written_at > give_up_h * 3600
            if with_poster or (old and with_imdb):
                for i in found:
                    self.jf.lock(i["Id"])
                self.db.execute("UPDATE titles SET locked=1 WHERE id=?", tid)
                n += 1
            elif old:
                self.db.execute("UPDATE titles SET locked=-1 WHERE id=?", tid)
                gave_up += 1
        if n or gave_up:
            log.info("%d title(s) locked (LockData) after identification%s", n,
                     f"; {gave_up} without IMDb after {give_up_h} h left the queue" if gave_up else "")
        return n
