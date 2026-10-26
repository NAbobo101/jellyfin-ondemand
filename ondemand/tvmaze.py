"""Series identity and episode lists: TVmaze's open API (no key).

An RSS item often only carries the release title ("Franklin 2024 S01E03 ..."), so the
series has to be found by name. TVmaze returns the ORIGINAL name, the premiere year and
the ids (`thetvdb`, `imdb`), which Jellyfin's RemoteSearch does not (it answers in the
library's language and without a year). The episode list is the "truth" against which
the filter checks each candidate's (season, episode).

Service limit: 20 requests every 10 s; the default pause stays well below. In-memory cache
and, if `cache_file` is given, a JSON file (a simulation does not repeat hundreds of requests).
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
        self.requests = 0
        if cache_file and os.path.exists(cache_file):
            try:
                with open(cache_file, encoding="utf-8") as f:
                    self.cache = {k: _slim(k, v) for k, v in json.load(f).items()}
            except (OSError, ValueError, AttributeError):
                self.cache = {}

    def _get(self, route, **params):
        with self._lock:
            return self._get_locked(route, **params)

    def _get_locked(self, route, **params):
        key = route + "?" + urllib.parse.urlencode(sorted(params.items()))
        if key in self.cache:
            return self.cache[key]
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
        return data

    def save(self):
        if not self.cache_file:
            return
        with self._lock:
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False)
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
