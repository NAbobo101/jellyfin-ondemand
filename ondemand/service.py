"""The jellyfin-ondemand service: HTTP gateway + starting torrents (+ catalog, lifecycle, search).

    python3 -m ondemand.service      # reads $ONDEMAND_CONFIG (/config/config.toml) and $ONDEMAND_SECRETS
"""

import importlib
import logging
import os
import threading
import time
from types import SimpleNamespace

from . import config, languages, movie_filter, series_filter, series_ondemand, torrent
from .catalog import Catalog
from .db import Database
from .gateway import Gateway, Metrics, NotFound
from .jellyfin import Jellyfin
from .lifecycle import Lifecycle
from .pieces import Watcher
from .providers import registry as providers
from .qbit import QBit
from .search import Search, SearchAPI
from .search_series import SeriesSearch
from .sources import Unavailable
from .subtitles.subdl import SubDL
from .subtitles.subtitler import Subtitler
from .tvmaze import TVmaze

log = logging.getLogger("service")


class Service:
    """Resolves /v/<release> -> Source, putting the torrent to download on play.

    Every catalog item has ONE release (the 4K is another item, in the Movies 4K library), so
    every request is for what the person chose. PlaybackInfo's probe MUST succeed: a probe that
    fails makes Jellyfin play the item as a live stream, without knowing the codec — and transcode
    everything on the CPU. So the first request starts the torrent and waits up to
    `first_byte_wait_s` (the Android TV app gives up on PlaybackInfo after 30 s); with the torrent
    active, it waits for the piece up to `play_wait_s`. Probe (ffprobe) and play (ffmpeg) arrive
    alike ("Lavf/…"): they cannot be told apart, and with one release per item they need not be.
    BLOCK: if the probe fails anyway (torrent without seeders), the next request is the ffmpeg of a
    "blind" transcode — which Jellyfin re-creates on every retry of the player (measured: 1.9 GB of
    RAM, swapping). For `block_s` every request for that release gets a 503 at once: the player
    shows an error instead of taking the server down.
    Several almost simultaneous connections for the same release start a single torrent.
    """

    def __init__(self, cfg, db, qbit, jellyfin, metrics, subtitler=None, lang=None, registry=None):
        self.cfg, self.db, self.qbit, self.jf, self.metrics = cfg, db, qbit, jellyfin, metrics
        self.subtitler = subtitler
        self.registry = registry
        self.qcfg = {**cfg["qbittorrent"], "subtitle_pattern": lang.regex if lang else None}
        self.sources = {}         # release_id -> TorrentSource
        self.watchers = {}        # infohash -> Watcher
        self.starting = {}        # release_id -> Event
        self.failures = {}        # release_id -> time of the last failed probe
        self.lock = threading.Lock()

    def forget(self, rid):
        """The lifecycle deleted the torrent (or it vanished): the next play starts from scratch. The
        Watcher only stops if no other release (a torrent with several versions) still uses it."""
        with self.lock:
            s = self.sources.pop(rid, None)
            w = getattr(s, "watcher", None)
            if w is not None and not any(getattr(o, "watcher", None) is w for o in self.sources.values()):
                self.watchers.pop(w.hash, None)
                w.stop()

    def on_failure(self, rid):
        """Counts from the first failure: requests refused during the block do not extend it."""
        now = time.monotonic()
        with self.lock:
            when = self.failures.get(rid)
            if when is not None and now - when <= self.cfg["gateway"]["block_s"]:
                return
            self.failures[rid] = now
        log.warning("probe of %s failed: its requests are blocked for %ds (avoids a blind transcode)",
                    rid, self.cfg["gateway"]["block_s"])

    def resolver(self, rid, request):
        g = self.cfg["gateway"]
        started = getattr(request, "started", None) or time.monotonic()
        gone = None
        with self.lock:
            when = self.failures.get(rid)
            if when is not None and time.monotonic() - when > g["block_s"]:
                self.failures.pop(rid, None)
                when = None
            s = self.sources.get(rid)
            if s is not None and getattr(getattr(s, "watcher", None), "gone", False):
                # Torrent deleted from outside: forget it; the play below adds it again.
                self.sources.pop(rid, None)
                self.watchers.pop(s.watcher.hash, None)
                gone, s = s.watcher.hash, None
        if gone:                      # outside the lock: writing to a slow disk would hold the other requests
            self.db.execute("UPDATE torrents SET state='removed', removed_at=? WHERE infohash=?", time.time(), gone)
            log.info("release %s: the torrent had vanished from qBittorrent; it will be added again", rid)
        if when is not None:
            self.metrics.inc("blocks_total")
            raise Unavailable("the probe failed a moment ago; refusing to avoid a blind transcode")
        if s is not None and s.available_until(0) > 0:
            request.wait = g["play_wait_s"]
            return s
        if s is None and getattr(request, "command", "GET") == "HEAD":
            raise Unavailable("HEAD does not start a torrent")
        # First request (or a torrent that has not delivered the start yet): PlaybackInfo's probe.
        # The first-byte wait discounts the start-up time: the total is what the client tolerates.
        s = s if s is not None else self._ensure(rid)
        request.wait = max(5.0, g["first_byte_wait_s"] - (time.monotonic() - started))
        return s

    def _ensure(self, rid):
        with self.lock:
            s = self.sources.get(rid)
            if s is not None:
                return s
            ev = self.starting.get(rid)
            owner = ev is None
            if owner:
                ev = self.starting[rid] = threading.Event()
        if not owner:
            # Another connection is already starting the torrent: wait as long as the probe would
            # (a shorter wait turned a slow but successful start into a 120 s block).
            ev.wait(timeout=self.cfg["gateway"]["first_byte_wait_s"])
            s = self.sources.get(rid)
            if s is None:
                raise Unavailable("starting the torrent failed")
            return s
        try:
            return self._start(rid)
        finally:
            ev.set()
            with self.lock:
                self.starting.pop(rid, None)

    def _start(self, rid):
        r = self.db.one("SELECT r.*, t.name AS title_name FROM releases r JOIN titles t ON t.id=r.title_id "
                        "WHERE r.id=?", rid)
        if r is None:
            raise NotFound()
        t0 = time.monotonic()
        episode = (r["season"], r["episode"]) if r["season"] is not None and r["episode"] is not None else None
        # files ANOTHER episode of the same pack is reading now: they keep their priority
        keep = set()
        if r["torrent_tag"]:
            with self.lock:
                siblings = [self.sources.get(x["id"]) for x in
                            self.db.all("SELECT id FROM releases WHERE torrent_tag=? AND id<>?", r["torrent_tag"], rid)]
            keep = {s.file.path for s in siblings if s is not None and s.readers > 0}
        try:
            info = torrent.start(self.qbit, rid, r["torrent_url"], self.qcfg, episode=episode,
                                 public=not r["private"] and not (self.registry and self.registry.trusted(r["provider"])),
                                 infohash=r["infohash"], preferred=r["file"],
                                 tag=f"od-{r['torrent_tag']}" if r["torrent_tag"] else None, keep=keep,
                                 min_video_mb=self.cfg["series"]["min_video_mb"] if episode else None)
        except Unavailable:
            raise
        except Exception as e:      # qBittorrent down, network: becomes a 503 before the headers
            raise Unavailable(f"qBittorrent: {e}") from None
        h = info["hash"]
        with self.lock:
            watcher = self.watchers.get(h)
            if watcher is None:
                watcher = self.watchers[h] = Watcher(self.qbit, h)
            source = torrent.TorrentSource(watcher, info["meta"], info["file"], info["path"], info.get("alternatives", ()))
            self.sources[rid] = source
        now = time.time()
        # A title watched again after the lifecycle deleted its torrent: the 'removed' row goes and a new
        # one comes in — with an upsert of last_access only, it stayed 'removed' and the lifecycle never
        # took care of it again (no force start, no abandon: the disk filled up).
        self.db.execute("DELETE FROM torrents WHERE infohash=? AND state='removed'", h)
        self.db.execute(
            "INSERT INTO torrents(infohash,release_id,state,foreign_,path,file,started_at,last_access) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(infohash) DO UPDATE SET last_access=excluded.last_access",
            h, rid, "foreign" if info["foreign"] else "trial", int(info["foreign"]), info["path"],
            info["file"].path, now, now)
        if info.get("switched_target"):
            # the requested episode became the pack's target: the lifecycle gives the rest back when IT completes
            self.db.execute("UPDATE torrents SET file=?, rest_released=0 WHERE infohash=?", info["file"].path, h)
        self.db.execute("UPDATE titles SET last_play=? WHERE id=?", now, r["title_id"])
        self.metrics.inc("starts_total")
        if self.subtitler:
            self.subtitler.request(rid)        # in the background: does not hold PlaybackInfo
        log.info("PLAY %s (%s %s): torrent ready to serve in %.1fs", rid, r["title_name"], r["label"],
                 time.monotonic() - t0)
        return source


# ---------------------------------------------------------------- wiring
def subtitle_provider(cfg, lang):
    """The configured subtitle provider, or None (no language, or no key)."""
    if lang is None:
        return None
    s = cfg["subtitles"]
    name = s.get("provider") or "subdl"
    if name == "subdl":
        key = (cfg["secrets"].get("subdl") or {}).get("api_key")
        if not key:
            log.warning("no SubDL api_key in secrets.toml [subdl]: the catalog keeps the subtitle placeholders")
            return None
        return SubDL(key, lang.subdl)
    module, _, cls = name.partition(":")
    return getattr(importlib.import_module(module), cls)(cfg, lang)


def build(cfg):
    """Every long-lived object, wired. Used by the service and by the CLI."""
    sec = cfg["secrets"]
    db = Database(cfg["general"]["database"])
    qs = sec.get("qbittorrent") or {}
    qbit = QBit(cfg["qbittorrent"]["url"], qs.get("username"), qs.get("password"))
    metrics = Metrics()
    key = (sec.get("jellyfin") or {}).get("api_key")
    if not key:
        raise SystemExit("secrets.toml needs [jellyfin] api_key")
    jf = Jellyfin(cfg["jellyfin"]["url"], key, metrics=metrics, message_header=cfg["jellyfin"]["message_header"])
    lang = languages.get(cfg["subtitles"]["language"])
    c = cfg["catalog"]
    cat = Catalog(db, jf, c["root"], cfg["gateway"]["public_url"], movies=c.get("movies") or None,
                  movies_4k=c.get("movies_4k") or None,
                  series=(c.get("series") or None) if cfg["series"]["enabled"] else None,
                  jellyfin_root=c.get("jellyfin_root") or None, lang=lang)
    registry = providers.build(cfg, db, {"qbit": qbit})
    subtitler = Subtitler(db, jf, subtitle_provider(cfg, lang), lang, to_local=cat.to_local)
    service = Service(cfg, db, qbit, jf, metrics, subtitler, lang, registry)
    tv = TVmaze(cache_file=cfg["series"]["tvmaze_cache"])  # one: the import, requests and "complete" share pace and cache
    return SimpleNamespace(cfg=cfg, db=db, qbit=qbit, metrics=metrics, jf=jf, lang=lang, cat=cat, registry=registry,
                           subtitler=subtitler, service=service, tv=tv)


def watchdog_prom(cfg):
    """Metrics file written by tools/transcode_watchdog.py, next to the database."""
    return os.path.join(os.path.dirname(cfg["general"]["database"]), "transcode-watchdog.prom")


def prepare_metrics(metrics, db, service, cfg):
    """Counters that alerts use are born at 0; gauges of the current state are computed on every scrape."""
    for name in ("starts_total", "blocks_total", "first_byte_timeouts_total", "piece_waits_total", "searches_total"):
        metrics.zero(name)
    for result in ("ready", "error", "limit"):
        metrics.zero("search_requests_total", result=result)

    def current_state(m):
        m.set("active_streams", sum(1 for s in list(service.sources.values()) if s.readers > 0))
        m.clear("torrents")
        for r in db.all("SELECT state, count(*) n FROM torrents WHERE state<>'removed' GROUP BY state"):
            m.set("torrents", r["n"], state=r["state"])
        m.set("catalog_pending", db.one("SELECT count(*) n FROM titles WHERE state='new'")["n"])
    metrics.collectors.append(current_state)
    metrics.files.append(watchdog_prom(cfg))


def _loop(name, interval_s, fn, delay_s=5):
    """Run `fn` forever every `interval_s`; an error becomes a log line, never takes the service down."""
    def run():
        time.sleep(delay_s)
        while True:
            try:
                fn()
            except Exception:
                log.exception("task %s failed", name)
            time.sleep(interval_s)
    threading.Thread(target=run, name=name, daemon=True).start()


# ---------------------------------------------------------------- the periodic import
def import_movies(app, candidates):
    """Movie filter -> database. A title that exists locally is marked and never written; the
    library (a full listing) is only read when there is something new to register."""
    r = movie_filter.build_movies(candidates, movie_filter.MovieResolver(app.jf), app.cfg["movies"])
    known = {row["key"] for row in app.db.all("SELECT key FROM titles")}
    new = [imdb for imdb in r["movies"] if f"imdb:{imdb}" not in known]
    ids = app.jf.local_ids(catalog_prefix=app.cat.jellyfin_prefix) if new else set()
    for title, versions in r["movies"].values():
        app.cat.register(title, versions, ids)
    log.info("movies: %d candidates, %d movies (%d new), %d in quarantine",
             len(candidates), len(r["movies"]), len(new), len(r["quarantine"]))
    return len(r["movies"])


def import_series(app, candidates):
    """Series filter -> database. The catalog writes the .strm files bit by bit, with nobody watching.
    An episode that already exists in a local series is not registered (only gaps are filled)."""
    sc = app.cfg["series"]
    local = app.jf.local_episodes(catalog_prefix=app.cat.jellyfin_prefix)
    try:
        r = series_filter.build_catalog(candidates, series_filter.Resolver(app.tv), sc, local)
    finally:
        app.tv.save()
    for problem in series_filter.check(r):
        log.error("series: %s — nothing registered this round", problem)
        return 0
    n_series = n_eps = 0
    for s in r["series"].values():
        eps = [series_ondemand.episode_release(e["winner"], t, n) for t, season in s["seasons"].items()
               for n, e in season.items() if not e["local"]]
        if eps:
            app.cat.register_series(s["series"], eps)
            n_series += 1
            n_eps += len(eps)
    log.info("series: %d candidates, %d series and %d episodes registered, %d in quarantine (%d TVmaze requests)",
             len(candidates), n_series, n_eps, len(r["quarantine"]), app.tv.requests)
    return n_eps


def import_catalog(app):
    """Every provider's feed, once, through both filters."""
    items, warnings = app.registry.feed()
    if warnings:
        log.warning("import: %s", "; ".join(warnings))
    cands = [i.candidate() for i in items]
    if app.cfg["movies"]["enabled"]:
        import_movies(app, [c for c in cands if c["kind"] != "series"])
    if app.cfg["series"]["enabled"] and app.cat.series:
        import_series(app, [c for c in cands if c["kind"] != "movie"])


def retry_subtitles(cfg, db, subtitler, pause_s=5):
    """Releases that were played and stayed without a subtitle: a new attempt, ONE at a time with a
    pause (one thread per release at once triggers the provider's 429, and every refusal used up an
    attempt). Stops at the first provider limit: the rest waits for the next round."""
    s = cfg["subtitles"]
    for r in db.all("SELECT release_id FROM subtitles WHERE state='not_found' AND attempts < ? "
                    "AND updated_at < ? ORDER BY updated_at", s["max_attempts"], time.time() - s["interval_h"] * 3600):
        if subtitler.fetch_now(r["release_id"]) == "limit":
            log.info("subtitles: provider at its limit; the others wait for the next round")
            break
        time.sleep(pause_s)


def schedule(app):
    cfg, c = app.cfg, app.cfg["catalog"]
    lifecycle = Lifecycle(cfg, app.db, app.qbit, app.jf, app.service, rules=app.registry.seed_rule)
    _loop("lifecycle", cfg["lifecycle"]["interval_s"], lifecycle.step)
    _loop("catalog", c["interval_min"] * 60, lambda: (app.cat.write_pending(c["batch"]), app.cat.lock_identified(),
                                                      app.cat.write_pending_series(c["series_batch"])))
    # Hourly; whether a release may try again is decided by its age (interval_h). With the loop at the
    # same period as the age, attempts fell every 2× interval.
    if app.lang is not None:
        _loop("subtitles", 3600, lambda: retry_subtitles(cfg, app.db, app.subtitler), 120)
    if len(app.registry.with_capability("feed")):
        _loop("import", c["import_interval_h"] * 3600, lambda: import_catalog(app), 60)
    log.info("scheduler: lifecycle every %ds, catalog every %d min, import every %d h from %d provider(s)",
             cfg["lifecycle"]["interval_s"], c["interval_min"], c["import_interval_h"],
             len(app.registry.with_capability("feed")))


def main():
    cfg = config.load()
    logging.basicConfig(level=getattr(logging, str(cfg["general"]["log_level"]).upper(), logging.INFO),
                        format="[%(asctime)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    app = build(cfg)
    q = cfg["qbittorrent"]

    def wait_for_qbittorrent():
        """In the background: search and the catalog do not depend on qBittorrent, so the service starts
        without it (after a power cut qBittorrent may come up later). Only playback needs it."""
        attempt = 0
        while True:
            try:
                app.qbit.ensure_category(q["category"], q["save_path"])
                log.info("qBittorrent %s ok; category %s -> %s", app.qbit.version(), q["category"], q["save_path"])
                return
            except Exception as e:
                attempt += 1
                if attempt == 1 or attempt % 30 == 0:
                    log.warning("qBittorrent not answering yet (%s); retrying every 10 s", e)
                time.sleep(10)
    threading.Thread(target=wait_for_qbittorrent, name="wait-qbittorrent", daemon=True).start()
    schedule(app)
    prepare_metrics(app.metrics, app.db, app.service, cfg)
    s = cfg["search"]
    if s["enabled"]:
        # Its own port, API only (no /v/): the one a reverse proxy publishes next to Jellyfin.
        series = (SeriesSearch(app.db, app.jf, app.cat, cfg, app.tv, app.registry, app.metrics)
                  if cfg["series"]["enabled"] and app.cat.series else None)
        api = SearchAPI(("0.0.0.0", s["port"]), Search(app.db, app.jf, app.cat, app.registry, cfg, app.metrics,
                                                       series=series), cfg["abr"]["root"])
        threading.Thread(target=api.serve_forever, name="search-api", daemon=True).start()
        log.info("search API listening on :%d", s["port"])
    g = cfg["gateway"]
    srv = Gateway(("0.0.0.0", g["port"]), app.service.resolver, g["video_clients"], g["admin_clients"],
                  first_byte_wait=g["first_byte_wait_s"], stall_wait=g["stall_wait_s"], metrics=app.metrics,
                  on_failure=app.service.on_failure)
    log.info("gateway listening on :%d", g["port"])
    srv.serve_forever()


if __name__ == "__main__":
    main()
