"""Search outside the library: movies and series a person looks for and Jellyfin does not have.

Jellyfin's search only looks in its own database (the apps call /Items?searchTerm=) and accepts no
outside results. web/search.js (JavaScript Injector plugin) adds a section to the search page that
calls this API. A requested movie becomes a catalog .strm — an item like any other, on every
device — and the browser starts playing when it is born.

    GET  /ondemand/api/search?q=…        results that are NOT in Jellyfin
    POST /ondemand/api/request {imdb, kind}  writes the title now (202)
    GET  /ondemand/api/request/<imdb>    preparing | ready (+ item_id) | error
    GET  /ondemand/api/details/<imdb>    overview, poster, qualities… — the window that opens on a
                                         card; writes nothing
    POST /ondemand/api/complete {item_id}  a catalog series was opened: look for missing episodes
    GET  /ondemand/api/complete/<tvdb>
    GET  /ondemand/api/health

It may arrive through a reverse proxy that keeps or strips the /ondemand prefix: routes work both
ways. The caller sends its own Jellyfin session token, checked against /Users/Me: only logged-in
users search and request.

Movies: the term goes to TMDb through Jellyfin's RemoteSearch (in the library's language); each
result becomes an IMDb id and, with `check_availability`, every provider is asked for it (through
the same movie filter as the catalog). Only movies with at least one usable release become cards.
"""

import http.server
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from socketserver import ThreadingMixIn

from . import abr, movie_filter
from .jellyfin import JellyfinError, strm_item_id
from .providers import MovieQuery
from .search_series import SeriesError

log = logging.getLogger("search")

IMDB = re.compile(r"^tt\d{7,9}$")
REQUEST_ROUTE = re.compile(r"^/api/request/(tt\d{7,9})$")
DETAILS_ROUTE = re.compile(r"^/api/details/([^/]+)$")
COMPLETE_ROUTE = re.compile(r"^/api/complete/(\d{1,10})$")
LABEL_ORDER = ("SD", "720p", "1080p", "4K")
TOKEN_IN_AUTH = re.compile(r'Token="?([^",\s]+)"?')
PREFIX = "/ondemand"


class SearchError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _tmdb_poster(url, size="w342"):
    """RemoteSearch returns the poster at its original size (MBs); for a card w342 is enough."""
    return url.replace("/t/p/original/", f"/t/p/{size}/") if url else None


class Search:
    def __init__(self, db, jellyfin, catalog, registry, cfg, metrics, background=True, series=None):
        """cfg: the WHOLE configuration. background=False: requests run at once (tests).
        series: SeriesSearch (search_series.py) or None."""
        self.db, self.jf, self.cat, self.registry, self.metrics = db, jellyfin, catalog, registry, metrics
        self.cfg, self.s, self.movies_cfg = cfg, cfg["search"], cfg["movies"]
        self.series = series
        self.background = background
        self._lock = threading.Lock()
        self._cache = {}              # term -> (time, cards before the library filter)
        self._details = {}            # imdb -> (time, details without the request state)
        self._versions = {}           # imdb -> (time, (title, versions))
        self._users = {}              # token -> (time, user)
        self._tmdb_imdb = {}          # tmdb -> imdb (does not change)
        self._english = {}            # tmdb -> English name (the one release groups use)
        # (time, {imdb: item}) of the local movies (outside the catalog). Starts at -inf: with 0.0 the cache
        # looked fresh until monotonic() passed 30 min, and right after a boot the search missed the locals.
        self._local = (float("-inf"), {})
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="search")
        n = self.db.execute("UPDATE requests SET state='error', detail='the service restarted; request again' "
                            "WHERE state='preparing'").rowcount
        if n:
            log.info("%d request(s) interrupted by the restart marked as error", n)

    # ------------------------------------------------------------ who asks
    def user(self, token):
        if not token or len(token) > 200:
            return None
        now = time.monotonic()
        with self._lock:
            c = self._users.get(token)
        if c and now - c[0] < 600:
            return c[1]
        u = self.jf.user_from_token(token)
        if u:
            with self._lock:
                self._users = {k: v for k, v in self._users.items() if now - v[0] < 600}
                self._users[token] = (now, u)
        return u

    # ------------------------------------------------------------ what Jellyfin already has
    def _local_movies(self):
        """{imdb: item} of the local movies (outside the catalog): the only library listing the search
        makes, refreshed every `library_cache_min` (a new local movie is rare)."""
        t, local = self._local
        if time.monotonic() - t > self.s["library_cache_min"] * 60:
            local = self.jf.local_movies(self.cat.jellyfin_prefix)
            self._local = (time.monotonic(), local)
        return local

    def in_library(self):
        """IMDb ids of everything Jellyfin's native search already shows: the local movies and the titles
        written to the catalog (these come from the database, without asking Jellyfin)."""
        written = {r["imdb"] for r in self.db.all("SELECT imdb FROM titles WHERE state='written' AND kind='movie'")}
        return written | set(self._local_movies())

    def _existing_item(self, imdb):
        """Jellyfin's item for this movie, if there is one: the local one, or the catalog's by the
        computed id of the main .strm (a small query, by id)."""
        local = self._local_movies().get(imdb)
        if local:
            return local
        t = self.db.one("SELECT id FROM titles WHERE imdb=? AND state='written' AND kind='movie'", imdb)
        strm = self.cat.main_strm(t["id"]) if t else None
        found = self.jf.items([strm_item_id(strm)]) if strm else []
        return found[0] if found else None

    # ------------------------------------------------------------ availability (providers)
    def _imdb(self, tmdb):
        if tmdb not in self._tmdb_imdb:
            self._tmdb_imdb[tmdb] = self.jf.imdb_from_tmdb(tmdb)
        return self._tmdb_imdb[tmdb]

    def _english_name(self, tmdb):
        if tmdb and tmdb not in self._english:
            try:
                r = self.jf.movie_by_tmdb(tmdb, language="en") or {}
            except JellyfinError:
                r = {}
            self._english[tmdb] = r.get("Name")
        return self._english.get(tmdb)

    def versions(self, imdb, remote=None):
        """(title, versions) of a movie across every provider, through the movie filter; cached."""
        now = time.monotonic()
        with self._lock:
            c = self._versions.get(imdb)
        if c and now - c[0] < self.s["cache_min"] * 60:
            return c[1]
        remote = remote or self.jf.movie_by_imdb(imdb) or {}
        tmdb = (remote.get("ProviderIds") or {}).get("Tmdb")
        english = self._english_name(tmdb)
        year = remote.get("ProductionYear")
        q = MovieQuery(imdb=imdb, tmdb=tmdb, title=remote.get("Name") or "", original_title=english, year=year)
        items, warnings = self.registry.search_movie(q)
        target = {"imdb": imdb, "tmdb": tmdb, "name": english or remote.get("Name"), "year": year,
                  "names": [english, remote.get("Name")]}
        (title, vs), _ = movie_filter.build_for_movie([i.candidate() for i in items], target, self.movies_cfg)
        result = (title, vs)
        with self._lock:
            self._versions = {k: v for k, v in self._versions.items() if now - v[0] < self.s["cache_min"] * 60}
            self._versions[imdb] = (now, result)
        log.info("availability %s: %d item(s) from providers -> %s%s", imdb, len(items),
                 ", ".join(v["label"] for v in vs) or "nothing usable", f" (warnings: {warnings})" if warnings else "")
        return result

    # ------------------------------------------------------------ search
    def search(self, term):
        term = " ".join((term or "").split())[:100]
        if len(term) < 2:
            return []
        self.metrics.inc("searches_total")
        key, now = term.lower(), time.monotonic()
        with self._lock:
            c = self._cache.get(key)
        if c and now - c[0] < self.s["cache_min"] * 60:
            cards = c[1]
        else:
            cards = self._movie_cards(term)
            with self._lock:
                self._cache = {k: v for k, v in self._cache.items() if now - v[0] < self.s["cache_min"] * 60}
                self._cache[key] = (now, cards)
        in_lib = self.in_library()
        requests = {r["imdb"]: r["state"] for r in self.db.all("SELECT imdb, state FROM requests WHERE kind='movie'")}
        movies = [{**c, "request": requests.get(c["imdb"])} for c in cards if c["imdb"] not in in_lib]
        return (movies + self._series(term))[: self.s["max_results"]]

    def _series(self, term):
        """Series cards (TMDb) outside Jellyfin; a failure here never takes the movie search down."""
        if self.series is None or IMDB.match(term):
            return []
        try:
            return self.series.cards(term)
        except Exception as e:
            log.warning("series search failed: %s", e)
            return []

    def _card(self, remote):
        """TMDb result -> card, if some provider has a usable release (or availability is not checked)."""
        try:
            ids = remote.get("ProviderIds") or {}
            imdb = ids.get("Imdb") or (self._imdb(ids["Tmdb"]) if ids.get("Tmdb") else None)
            if not imdb:
                return None
            title, versions = (None, [])
            if self.s.get("check_availability", True):
                title, versions = self.versions(imdb, remote)
                if not versions:
                    return None
        except JellyfinError as e:
            log.warning("TMDb -> IMDb failed: %s", e)
            return None
        return {"kind": "movie", "imdb": imdb, "name": remote.get("Name"),
                "original_name": (title or {}).get("name") or remote.get("Name"), "year": remote.get("ProductionYear"),
                "rating": None, "poster": _tmdb_poster(remote.get("ImageUrl")),
                "qualities": sorted({v["label"] for v in versions}, key=LABEL_ORDER.index),
                "seeds": max((v.get("seeds") or 0 for v in versions), default=None)}

    def _movie_cards(self, term):
        try:
            if IMDB.match(term):
                r = self.jf.movie_by_imdb(term)
                remote = [r] if r else []
            else:
                remote = self.jf.remote_search_movie(term, limit=self.s["tmdb_max"])
        except JellyfinError as e:
            log.warning("RemoteSearch (TMDb) failed: %s", e)
            return []
        cards, seen = [], set()
        for c in self._pool.map(self._card, remote):
            if c and c["imdb"] not in seen:
                seen.add(c["imdb"])
                cards.append(c)
        log.info("search %r: %d on TMDb -> %d card(s)", term, len(remote), len(cards))
        return cards

    # ------------------------------------------------------------ details (read only)
    def details(self, imdb, kind="movie"):
        """The movie's window (or the series', with kind="series"): TMDb through Jellyfin (name, overview,
        poster) plus the qualities the providers have, cached for `cache_min`."""
        if not IMDB.match(imdb or ""):
            raise SearchError(400, "invalid IMDb id")
        if kind == "series":
            if self.series is None:
                raise SearchError(404, "series are off")
            return self.series.details(imdb)
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
        p = self.status(imdb)
        return {**d, "request": p["state"] if p else None}

    def _gather_details(self, imdb):
        try:
            r = self.jf.movie_by_imdb(imdb) or {}
        except JellyfinError as e:
            log.warning("details %s: TMDb failed: %s", imdb, e)
            r = {}
        if not r:
            raise SearchError(404, "no information about this movie")
        title, versions = self.versions(imdb, r)
        return {
            "kind": "movie", "imdb": imdb,
            "name": r.get("Name"), "original_name": (title or {}).get("name") or r.get("Name"),
            "year": r.get("ProductionYear"),
            "overview": (r.get("Overview") or "").strip() or None,
            "runtime_min": None, "genres": [], "rating": None, "cast": [], "trailer_youtube": None,
            "poster": _tmdb_poster(r.get("ImageUrl"), "w500"), "backdrop": None,
            "qualities": sorted({v["label"] for v in versions}, key=LABEL_ORDER.index),
        }

    # ------------------------------------------------------------ request
    def status(self, imdb):
        r = self.db.one("SELECT state, item_id, detail FROM requests WHERE imdb=?", imdb)
        return {"imdb": imdb, "state": r["state"], "item_id": r["item_id"], "detail": r["detail"]} if r else None

    def request(self, imdb, user, kind="movie"):
        if not IMDB.match(imdb or ""):
            raise SearchError(400, "invalid IMDb id")
        if kind not in ("movie", "series") or (kind == "series" and self.series is None):
            raise SearchError(400, "invalid kind")
        cur = self.status(imdb)
        if cur and cur["state"] == "preparing":
            return cur
        if cur and cur["state"] == "ready" and (self._series_exists(cur) if kind == "series" else self._existing_item(imdb)):
            return cur
        if not (user.get("Policy") or {}).get("IsAdministrator"):
            n = self.db.one("SELECT count(*) n FROM requests WHERE user_id=? AND requested_at>? AND imdb<>?",
                            user["Id"], time.time() - 86400, imdb)["n"]
            if n >= self.s["daily_limit"]:
                self.metrics.inc("search_requests_total", result="limit")
                raise SearchError(429, f"limit of {self.s['daily_limit']} requests per day")
        now = time.time()
        c = self.db.execute(
            "INSERT INTO requests(imdb,username,user_id,requested_at,state,updated_at,kind) VALUES(?,?,?,?,'preparing',?,?) "
            "ON CONFLICT(imdb) DO UPDATE SET username=excluded.username, user_id=excluded.user_id, "
            "requested_at=excluded.requested_at, state='preparing', item_id=NULL, detail=NULL, "
            "updated_at=excluded.updated_at, kind=excluded.kind WHERE requests.state<>'preparing'",
            imdb, user.get("Name"), user["Id"], now, now, kind)
        if c.rowcount == 0:                # the same title requested twice at the same time
            return self.status(imdb)
        log.info("request for %s (%s) by %s", imdb, kind, user.get("Name"))
        target = self._prepare_series if kind == "series" else self._prepare
        if self.background:
            threading.Thread(target=target, args=(imdb,), name=f"request-{imdb}", daemon=True).start()
        else:
            target(imdb)
        return self.status(imdb)

    def _series_exists(self, req):
        try:
            return bool(req.get("item_id") and self.jf.item(req["item_id"]))
        except JellyfinError:
            return False

    def _prepare_series(self, imdb):
        """Research the whole series (every season) and leave it in the catalog; progress goes in `detail`."""
        t0 = time.monotonic()

        def progress(text):
            self.db.execute("UPDATE requests SET detail=?, updated_at=? WHERE imdb=? AND state='preparing'",
                            text, time.time(), imdb)
        try:
            item_id, text = self.series.prepare_request(imdb, progress)
            self._mark(imdb, "ready", item_id=item_id, detail=text)
            log.info("series request %s ready in %.0fs: %s", imdb, time.monotonic() - t0, text)
        except SeriesError as e:
            self._mark(imdb, "error", detail=str(e))
            log.warning("series request %s: %s", imdb, e)
        except Exception as e:
            self._mark(imdb, "error", detail="internal failure")
            log.exception("series request %s failed: %s", imdb, e)

    def _mark(self, imdb, state, item_id=None, detail=None):
        self.db.execute("UPDATE requests SET state=?, item_id=?, detail=?, updated_at=? WHERE imdb=?",
                        state, item_id, detail, time.time(), imdb)
        self.metrics.inc("search_requests_total", result=state)

    def _prepare(self, imdb):
        t0 = time.monotonic()
        try:
            item = self._existing_item(imdb)
            if item is None:                 # the normal case: write it and wait for Jellyfin to create the item
                title, versions = self.versions(imdb)
                if not versions:
                    raise SearchError(404, "no provider has a usable torrent of this movie")
                tid = self.cat.register({**title, "origin": "request"}, versions, local_ids=set())
                strm = self.cat.write_title(tid)
                if not strm:
                    raise SearchError(404, "no version of this movie fits the catalog")
                item = self._wait_item(strm)
                if item is None:
                    raise SearchError(504, "Jellyfin did not create the item in time; try again in a few minutes")
            self._mark(imdb, "ready", item_id=item["Id"])
            log.info("request %s ready in %.0fs: item %s (%s)", imdb, time.monotonic() - t0, item["Id"], item.get("Name"))
        except SearchError as e:
            self._mark(imdb, "error", detail=str(e))
            log.warning("request %s: %s", imdb, e)
        except Exception as e:
            self._mark(imdb, "error", detail="internal failure")
            log.exception("request %s failed: %s", imdb, e)

    def _wait_item(self, strm):
        """The .strm's item, by computed id (one small query every 3 s). It is born in seconds, but
        without metadata (the folder's raw name) until Jellyfin's queue reaches it: once born it gets
        a high-priority refresh and waits up to `metadata_wait_s` for the poster (a sign TMDb answered)."""
        target = strm_item_id(strm)
        t0 = time.monotonic()
        limit = t0 + self.s["item_wait_s"]
        born = None
        while time.monotonic() < limit:
            found = self.jf.items([target])
            item = found[0] if found else None
            if item:
                if (item.get("ImageTags") or {}).get("Primary"):
                    log.info("item %s with metadata %.0fs after the notice", item["Id"], time.monotonic() - t0)
                    return item
                if born is None:
                    born = time.monotonic()
                    log.info("item %s born without metadata %.0fs after the notice; priority refresh",
                             item["Id"], born - t0)
                    try:
                        self.jf.refresh_item(item["Id"])
                    except JellyfinError as e:
                        log.warning("refresh of item %s failed: %s", item["Id"], e)
                elif time.monotonic() - born > self.s["metadata_wait_s"]:
                    log.info("item %s without a poster after %ds: plays anyway", item["Id"], self.s["metadata_wait_s"])
                    return item
            time.sleep(3)
        return None


# ---------------------------------------------------------------- HTTP

class SearchAPI(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, search, abr_root=""):
        super().__init__(address, APIHandler)
        self.search = search
        self.abr_root = abr_root


class APIHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "jellyfin-ondemand"
    sys_version = ""
    timeout = 30

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        if path.startswith(PREFIX + "/"):
            path = path[len(PREFIX):]
        b = self.server.search
        try:
            if path == "/api/health":
                return self._json(200, {"ok": True})
            m = abr.FILE_ROUTE.match(path)
            if method == "GET" and m:
                # hls.js sends no header: the token comes in the path (see ondemand/abr.py)
                if b.user(m.group(1)) is None:
                    return self._json(401, {"error": "invalid Jellyfin session; sign in again"})
                found = abr.file(self.server.abr_root, m.group(2), m.group(3))
                return self._file(*found) if found else self._json(404, {"error": "nothing here"})
            user = b.user(self._token())
            if user is None:
                return self._json(401, {"error": "invalid Jellyfin session; sign in again"})
            if method == "GET" and path == "/api/search":
                term = urllib.parse.parse_qs(url.query).get("q", [""])[0]
                return self._json(200, {"results": b.search(term)})
            if method == "POST" and path == "/api/request":
                body = self._body()
                return self._json(202, b.request(str(body.get("imdb", "")), user, str(body.get("kind") or "movie")))
            if method == "POST" and path == "/api/complete":
                if b.series is None:
                    raise SearchError(404, "series are off")
                body = self._body()
                admin = (user.get("Policy") or {}).get("IsAdministrator")
                return self._json(202, b.series.complete(str(body.get("item_id", "")), force=bool(body.get("force")) and admin))
            if method == "GET" and path == abr.LIST_ROUTE:
                return self._json(200, {"items": abr.items(self.server.abr_root)})
            m = abr.INFO_ROUTE.match(path)
            if method == "GET" and m:
                return self._json(200, {"available": abr.available(self.server.abr_root, m.group(1))})
            m = COMPLETE_ROUTE.match(path)
            if method == "GET" and m and b.series is not None:
                s = b.series.complete_status(m.group(1))
                return self._json(200, s) if s else self._json(404, {"error": "no search for this series"})
            m = DETAILS_ROUTE.match(path)
            if method == "GET" and m:
                kind = urllib.parse.parse_qs(url.query).get("kind", ["movie"])[0]
                return self._json(200, b.details(m.group(1), kind))
            m = REQUEST_ROUTE.match(path)
            if method == "GET" and m:
                s = b.status(m.group(1))
                return self._json(200, s) if s else self._json(404, {"error": "no request for this title"})
            return self._json(404, {"error": "nothing here"})
        except (SearchError, SeriesError) as e:
            return self._json(e.code, {"error": str(e)})
        except Exception:
            log.exception("API: %s %s failed", method, path)
            return self._json(500, {"error": "internal error"})

    def _token(self):
        """X-Emby-Token (what web/search.js sends) or the Token= of jellyfin-web's Authorization."""
        t = self.headers.get("X-Emby-Token") or self.headers.get("X-MediaBrowser-Token")
        if t:
            return t.strip()
        m = TOKEN_IN_AUTH.search(self.headers.get("Authorization", ""))
        return m.group(1) if m else None

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            raise SearchError(413, "request too large")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise SearchError(400, "invalid JSON") from None
        if not isinstance(body, dict):
            raise SearchError(400, "invalid JSON")
        return body

    def _file(self, path, ctype):
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        # a segment never changes; a playlist may be redone if the ladder is regenerated
        self.send_header("Cache-Control", "private, max-age=86400" if ctype != abr.TYPES[".m3u8"] else "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(abr.CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
