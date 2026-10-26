"""UNIT3D: the API of private trackers running the UNIT3D software.

    [[providers]]
    name = "my-unit3d"
    type = "unit3d"
    url = "https://tracker.example"           # the site's base URL
    private = true
    seed = { min_hours = 72, min_ratio = 1.0, either = true }   # e.g. "72 h OR ratio 1.0"
    movie_categories = [1]                    # the tracker's category ids (see its upload page)
    series_categories = [2]
    feed_pages = 200                          # feed: read the categories page by page (25 per page)
    # secrets.toml: [providers.my-unit3d] api_token = "..."

The `download_link` the API returns already carries your RSS key: it is stored with the
release and only qBittorrent fetches it. The token travels in a header, never in a URL.

Quirk seen in the wild: a listing page can fail with HTTP 500 because of one torrent in it.
When a page fails, its items are requested one by one and only the broken one is skipped.
"""

import datetime
import logging
import re

from .base import Http, Provider, ProviderError, TorrentItem, clean_ids

log = logging.getLogger("providers.unit3d")


def _freeleech(t):
    m = re.match(r"\s*(\d+)", str(t.get("freeleech") or "0"))
    return int(m.group(1)) if m else 0


def _published(t):
    try:
        return datetime.datetime.fromisoformat(str(t.get("created_at")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def to_item(t, kind=None):
    """An API torrent (its `attributes` plus `id`) -> TorrentItem, or None without hash/link."""
    if not t.get("info_hash") or not t.get("download_link"):
        return None
    return TorrentItem(
        title=(t.get("name") or "").strip(), torrent_url=t["download_link"], infohash=t["info_hash"].lower(),
        size=int(t["size"]) if str(t.get("size") or "").isdigit() else None, seeds=int(t.get("seeders") or 0),
        published=_published(t), freeleech=_freeleech(t), category=str(t.get("category") or ""), kind=kind,
        ids=clean_ids({"imdb": t.get("imdb_id"), "tmdb": t.get("tmdb_id"), "tvdb": t.get("tvdb_id")}) or None,
        files=[f["name"] for f in t.get("files") or [] if isinstance(f, dict) and f.get("name")] or None,
        id=str(t.get("id")))


class Unit3D(Provider):
    type = "unit3d"

    def __init__(self, name, options=None, secrets=None, context=None):
        super().__init__(name, options, secrets, context)
        self.require("url", "api_token")
        self.base = self.option("url").rstrip("/") + "/api/"
        self.movie_categories = list(self.option("movie_categories", []))
        self.series_categories = list(self.option("series_categories", []))
        self.per_page = int(self.option("per_page", 25))
        self.http = Http(pause=float(self.option("pause_s", 1.5)), timeout=float(self.option("timeout_s", 60)),
                         headers={"Authorization": "Bearer " + self.secret("api_token"), "Accept": "application/json"})

    def _filter(self, **params):
        d = self.http.get_json(self.base + "torrents/filter", params, label=f"{self.name} torrents/filter")
        return [{**x["attributes"], "id": x.get("id")} for x in d.get("data") or []]

    def _page(self, cid, page, per_page):
        return self._filter(**{"categories[]": cid, "perPage": per_page, "page": page})

    def category(self, cid, max_pages=200):
        """Every torrent in the category. A page that fails -> item by item."""
        items, pp = [], self.per_page
        for page in range(1, max_pages + 1):
            try:
                batch = self._page(cid, page, pp)
            except ProviderError as e:
                log.warning("%s: category %s, page %d: %s — asking for its items one by one", self.name, cid, page, e)
                batch, end = [], False
                for k in range((page - 1) * pp + 1, page * pp + 1):
                    try:
                        one = self._page(cid, k, 1)
                    except ProviderError:
                        continue                   # the torrent that breaks the listing is left out
                    if not one:
                        end = True
                        break
                    batch += one
                items += batch
                if end:
                    break
                continue
            if not batch:
                break
            items += batch
        return items

    def _items(self, raw, kind):
        return [self.stamp(i) for i in (to_item(t, kind) for t in raw) if i]

    def feed(self):
        out = []
        for kind, cats in (("movie", self.movie_categories), ("series", self.series_categories)):
            for cid in cats:
                out += self._items(self.category(cid, int(self.option("feed_pages", 200))), kind)
        return out

    def search_movie(self, q):
        if not q.imdb or not self.movie_categories:
            return []
        return self._items(self._filter(**{"imdbId": q.imdb.removeprefix("tt"), "categories[]": self.movie_categories,
                                           "perPage": 50}), "movie")

    def search_series(self, q):
        if not q.tvdb or not self.series_categories:
            return []
        return self._items(self._filter(**{"tvdbId": q.tvdb, "categories[]": self.series_categories,
                                           "perPage": 50}), "series")
