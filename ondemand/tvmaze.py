"""Series identity and episode lists: TVmaze's open API (no key).

An RSS item often only carries the release title ("Franklin 2024 S01E03 ..."), so the
series has to be found by name. TVmaze returns the ORIGINAL name, the premiere year and
the ids (`thetvdb`, `imdb`), which Jellyfin's RemoteSearch does not (it answers in the
library's language and without a year). The episode list is the "truth" against which
the filter checks each candidate's (season, episode).

Service limit: 20 requests every 10 s; the default pause stays well below. In-memory cache
and, if `cache_file` is given, a JSON file (a simulation does not repeat hundreds of requests).

Validity: an episode list is good for `VALIDITY["episodes"]` — a cache that never expires freezes the
list on the day of the first request, and a new season then never gets past the filter (seen in
practice: a season aired weeks earlier, dozens of archived releases, all in quarantine). Searches and
lookups last longer, empty answers (404, a search with nothing) shorter. Expired + TVmaze down = the old
answer is used. An entry nobody asked for in `DROP_DAYS` leaves on `save` (the cache does not grow forever).
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("tvmaze")

BASE = "https://api.tvmaze.com/"
EPISODE_FIELDS = ("season", "number", "airdate")
USER_AGENT = "jellyfin-ondemand/1.0"
VALIDITY = {"episodes": 2 * 86400, "other": 7 * 86400, "empty": 86400}
DROP_DAYS = 90
WHEN = "__when__"              # in the file: {key: time of the request}


def _slim(key, data):
    """An episode list keeps only what `episodes()` reads. The whole answer (overview, images, links)
    made the cache reach 37 MB = ~190 MB in memory once loaded (an OOM loop in a 192 MB container)."""
    if "/episodes?" in key and isinstance(data, list):
        return [{c: e.get(c) for c in EPISODE_FIELDS} for e in data]
    return data


class TVmazeError(Exception):
    pass


class TVmaze:
    def __init__(self, base=BASE, pause=0.7, timeout=20, cache_file=None):
        self.base, self.pause, self.timeout = base, pause, timeout
        self.cache_file = cache_file
        self._last = 0.0
        self._lock = threading.RLock()      # the import job, requests and the API share the object and the pace
        self.cache = {}
        self.when = {}
        self.requests = 0
        if cache_file and os.path.exists(cache_file):
            try:
                with open(cache_file, encoding="utf-8") as f:
                    data = json.load(f)
                self.when = data.pop(WHEN, None) or {}
                self.cache = {k: _slim(k, v) for k, v in data.items()}
                # A cache from before validity existed: episode lists are born expired (they are what froze);
                # searches and lookups count from the file's date.
                mtime = os.path.getmtime(cache_file)
                for k in self.cache:
                    self.when.setdefault(k, mtime - VALIDITY["episodes"] - 1 if "/episodes?" in k else mtime)
            except (OSError, ValueError, AttributeError):
                self.cache, self.when = {}, {}

    def _get(self, route, **params):
        with self._lock:
            return self._get_locked(route, **params)

    def _get_locked(self, route, **params):
        key = route + "?" + urllib.parse.urlencode(sorted(params.items()))
        if key in self.cache:
            if not self._expired(key):
                return self.cache[key]
            try:
                return self._fetch(route, key)
            except TVmazeError as e:            # down: the old answer serves until the next time
                log.warning("TVmaze: %s; using the stored (expired) answer", e)
                return self.cache[key]
        return self._fetch(route, key)

    def _expired(self, key):
        data = self.cache[key]
        validity = VALIDITY["empty"] if not data else VALIDITY["episodes" if "/episodes?" in key else "other"]
        return time.time() - self.when.get(key, 0.0) > validity

    def _fetch(self, route, key):
        wait = self.pause - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(self.base + key, headers={"User-Agent": USER_AGENT})
        data = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    data = None          # "does not exist" is an answer too: it goes to the cache
                    break
                if e.code == 429 and attempt < 2:
                    time.sleep(5)
                    continue
                raise TVmazeError(f"HTTP {e.code} on {route}") from None
            except (urllib.error.URLError, OSError, ValueError) as e:
                if attempt < 2:
                    time.sleep(2)
                    continue
                raise TVmazeError(f"{route}: {type(e).__name__}") from None
        self._last = time.monotonic()
        self.requests += 1
        self.cache[key] = data = _slim(key, data)
        self.when[key] = time.time()
        return data

    def save(self):
        if not self.cache_file:
            return
        with self._lock:
            limit = time.time() - DROP_DAYS * 86400
            for k in [k for k in self.cache if self.when.get(k, 0.0) < limit]:
                del self.cache[k]                          # a request nobody repeated in DROP_DAYS
            self.when = {k: v for k, v in self.when.items() if k in self.cache}
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({**self.cache, WHEN: self.when}, f, ensure_ascii=False)
            os.replace(tmp, self.cache_file)

    # ------------------------------------------------------------ queries
    def search(self, name):
        """[{score, show}] from `search/shows` (most relevant first)."""
        return self._get("search/shows", q=name) or []

    def by_tvdb(self, tvdb):
        return self._get("lookup/shows", thetvdb=str(tvdb))

    def by_imdb(self, imdb):
        return self._get("lookup/shows", imdb=imdb)

    def episodes(self, tvmaze_id):
        """[(season, number, airdate 'YYYY-MM-DD' or None)] without specials."""
        eps = self._get(f"shows/{tvmaze_id}/episodes") or []
        return [(e["season"], e["number"], e.get("airdate") or None) for e in eps if e.get("number") is not None]


def summary(show):
    """Only what the filter uses from a TVmaze show."""
    ext = show.get("externals") or {}
    country = ((show.get("network") or {}).get("country") or (show.get("webChannel") or {}).get("country") or {}).get("code")
    return {"id": show["id"], "name": show["name"], "year": int(show["premiered"][:4]) if show.get("premiered") else None,
            "tvdb": str(ext["thetvdb"]) if ext.get("thetvdb") else None, "imdb": ext.get("imdb"),
            "country": country, "language": show.get("language"), "status": show.get("status")}
