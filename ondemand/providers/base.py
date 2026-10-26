"""The provider interface.

A provider turns some source of torrents (an indexer API, an RSS feed, a website) into
`TorrentItem`s. It can do up to three things — implement only what the source supports:

    feed()                  -> items for the periodic catalog import (latest uploads, a category...)
    search_movie(query)     -> items for ONE movie        (search outside the library)
    search_series(query)    -> items for ONE whole series ("Add series", "complete" on open)

Everything after that — parsing the release name, identifying the movie/series, filtering
(seeds, language, resolution, size), picking one winner per movie version or per episode,
writing the catalog, downloading on play, seeding and cleaning up — is the same for every
provider. A provider never downloads the .torrent itself: qBittorrent does, on play.

Configuration (config.toml):

    [[providers]]
    name = "my-indexer"          # unique; stored with every release (picks the seed rule)
    type = "torznab"             # a built-in type (registry.BUILTIN) or "python" + `class`
    private = true               # private tracker: never abandoned halfway, seeds by `seed`
    seed = { min_hours = 72, min_ratio = 1.0, either = true }
    # ...type-specific options; secrets go in secrets.toml under [providers.<name>]
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field

USER_AGENT = "jellyfin-ondemand/1.0"
PUBLIC_TRACKERS = ("udp://tracker.opentrackr.org:1337/announce", "udp://open.stealth.si:80/announce",
                   "udp://tracker.torrent.eu.org:451/announce", "udp://exodus.desync.com:6969/announce")


class ProviderError(Exception):
    """The source failed (HTTP error, timeout, bad answer). The message must not carry a URL or a key."""


@dataclass
class TorrentItem:
    title: str                          # the release name, as the source shows it
    torrent_url: str | None = None      # http(s) link to the .torrent, or a magnet link
    infohash: str | None = None         # when known (lets an existing torrent in qBittorrent be reused)
    size: int | None = None             # bytes
    seeds: int = 0
    published: float | None = None      # unix time
    ids: dict | None = None             # {"imdb": "tt…", "tmdb": "…", "tvdb": "…"} of the MOVIE or the SERIES
    freeleech: int | None = None        # percent (100 = download does not count); None = unknown
    category: str = ""
    kind: str | None = None             # "movie" | "series" | None (guessed from the title)
    files: list | None = None           # file names inside the torrent, when the source lists them
    id: str | None = None               # stable id within the provider (default: infohash, then url, then title)
    provider: str = ""                  # filled in by the registry
    private: bool = True                # filled in by the registry from the provider's config
    resolutions: list | None = field(default=None, repr=False)   # provider override of the accepted resolutions

    def candidate(self):
        """The dict the filters work on."""
        d = asdict(self)
        if d["torrent_url"] is None and self.infohash:
            d["torrent_url"] = magnet(self.infohash, self.title)
        d["id"] = self.id or self.infohash or self.torrent_url or self.title
        d["published"] = self.published or time.time()
        d["infohash"] = self.infohash.lower() if self.infohash else None
        if d["ids"]:
            d["ids"] = clean_ids(d["ids"]) or None
        return d


def clean_ids(ids):
    """Normalise ids: imdb with the "tt" prefix and 7+ digits, tmdb/tvdb as digit strings; zeros dropped."""
    out = {}
    for k in ("imdb", "tmdb", "tvdb"):
        v = str((ids or {}).get(k) or "").strip()
        if k == "imdb":
            digits = v[2:] if v.lower().startswith("tt") else v
            if digits.isdigit() and int(digits) > 0:
                out[k] = f"tt{int(digits):07d}"
        elif v.isdigit() and int(v) > 0:
            out[k] = str(int(v))
    return out


@dataclass
class SeedRule:
    private: bool = True
    min_hours: float = 0
    min_ratio: float = 0
    either: bool = False                # the tracker's rule is "min_hours OR min_ratio"

    def as_lifecycle(self):
        return {"private": self.private, "min_seed_hours": self.min_hours, "min_ratio": self.min_ratio,
                "either": self.either}


@dataclass
class MovieQuery:
    imdb: str | None = None
    tmdb: str | None = None
    title: str = ""                     # in the library's metadata language
    original_title: str | None = None
    year: int | None = None


@dataclass
class SeriesQuery:
    name: str                           # the ORIGINAL name (from TVmaze)
    year: int | None = None
    tvdb: str | None = None
    imdb: str | None = None
    tmdb: str | None = None
    seasons: list = field(default_factory=list)    # aired seasons, from TVmaze

    def queries(self, limit=40):
        """Text queries for sources without id search: one per season ("Name S01") and a general one."""
        return ([f"{self.name} S{s:02d}" for s in sorted(self.seasons)] + [self.name])[:limit]


def magnet(infohash, name="", trackers=PUBLIC_TRACKERS):
    return (f"magnet:?xt=urn:btih:{infohash.lower()}&dn={urllib.parse.quote(name)}"
            + "".join("&tr=" + urllib.parse.quote(t, safe="") for t in trackers))


class Http:
    """A small paced HTTP client for providers: one request at a time, `pause` seconds apart,
    errors as ProviderError without the URL (it may carry a key)."""

    def __init__(self, pause=1.0, timeout=30, headers=None):
        self.pause, self.timeout = pause, timeout
        self.headers = {"User-Agent": USER_AGENT, **(headers or {})}
        self._last = 0.0
        self._lock = threading.Lock()

    def request(self, url, data=None, headers=None, method=None, label=""):
        with self._lock:
            wait = self.pause - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            req = urllib.request.Request(url, data=data, method=method, headers={**self.headers, **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                raise ProviderError(f"HTTP {e.code}{' on ' + label if label else ''}") from None
            except (urllib.error.URLError, OSError) as e:
                raise ProviderError(f"{type(e).__name__}{' on ' + label if label else ''}") from None
            finally:
                self._last = time.monotonic()

    def get(self, url, params=None, **kw):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        return self.request(url, **kw)

    def get_json(self, url, params=None, **kw):
        try:
            return json.loads(self.get(url, params, **kw))
        except ValueError:
            raise ProviderError("the answer is not JSON") from None

    def post_json(self, url, body, **kw):
        h = {"Content-Type": "application/json", **kw.pop("headers", {})}
        try:
            return json.loads(self.request(url, data=json.dumps(body).encode(), headers=h, method="POST", **kw))
        except ValueError:
            raise ProviderError("the answer is not JSON") from None


class Provider:
    """Base class. Subclasses set `type` and override the methods their source supports."""

    type = "base"

    def __init__(self, name, options=None, secrets=None, context=None):
        """options: the [[providers]] table; secrets: [providers.<name>] from secrets.toml;
        context: shared objects (context["qbit"] = the qBittorrent client)."""
        self.name = name
        self.options = dict(options or {})
        self.secrets = dict(secrets or {})
        self.context = context or {}
        self.private = bool(self.options.get("private", True))
        seed = self.options.get("seed") or {}
        self.seed_rule = SeedRule(private=self.private,
                                  min_hours=float(seed.get("min_hours", 0)),
                                  min_ratio=float(seed.get("min_ratio", 1.0 if self.private else 0)),
                                  either=bool(seed.get("either", False)))
        self.resolutions = self.options.get("resolutions")

    # ------------------------------------------------------------ what a provider can do
    def feed(self):
        raise NotImplementedError

    def search_movie(self, query):
        raise NotImplementedError

    def search_series(self, query):
        raise NotImplementedError

    @property
    def capabilities(self):
        """{"feed", "movie", "series"}: the methods this class overrides (and the config did not turn off)."""
        caps = set()
        for cap, method in (("feed", "feed"), ("movie", "search_movie"), ("series", "search_series")):
            if getattr(type(self), method) is not getattr(Provider, method) and self.options.get(cap, True):
                caps.add(cap)
        return caps

    # ------------------------------------------------------------ helpers
    def option(self, key, default=None):
        return self.options.get(key, default)

    def secret(self, key, default=None):
        """A secret from secrets.toml ([providers.<name>]); falls back to the option of the same name."""
        return self.secrets.get(key, self.options.get(key, default))

    def require(self, *keys):
        missing = [k for k in keys if not self.secret(k)]
        if missing:
            raise SystemExit(f"provider {self.name!r} ({self.type}) needs: {', '.join(missing)}")

    def stamp(self, item):
        """Fill in what the registry owns (name, private flag, resolution override)."""
        item.provider, item.private = self.name, self.private
        if self.resolutions and item.resolutions is None:
            item.resolutions = list(self.resolutions)
        return item


_SIZE = re.compile(r"^\s*([\d.,]+)\s*([KMGT]?i?B)\s*$", re.I)


def parse_size(text):
    """"1.4 GB" / "700 MiB" / "123456" -> bytes, or None."""
    if text is None:
        return None
    s = str(text).strip()
    if s.isdigit():
        return int(s)
    m = _SIZE.match(s)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper().replace("I", "")
    return int(n * {"B": 1, "KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30, "TB": 1 << 40}[unit])
