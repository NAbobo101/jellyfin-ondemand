"""Torznab: Prowlarr, Jackett and anything else that speaks the Torznab API.

This is the easiest way to add sources: configure your indexers in Prowlarr (or Jackett)
and point one [[providers]] entry at each indexer's Torznab URL — no code.

    [[providers]]
    name = "my-indexer"
    type = "torznab"
    url = "http://prowlarr:9696/1/api"       # Prowlarr: <prowlarr>/<indexer id>/api
    private = true                            # what the indexer is; drives the seed rule
    seed = { min_hours = 0, min_ratio = 1.0 }
    movie_categories = [2000]                 # Torznab standard: 2000 movies, 5000 TV
    series_categories = [5000]
    # secrets.toml: [providers.my-indexer] api_key = "..."

The download link Torznab returns is handed to qBittorrent as is — qBittorrent must be able
to reach it (with Prowlarr's proxied links, qBittorrent must reach Prowlarr).
"""

import email.utils
import logging
import xml.etree.ElementTree as ET

from .base import Http, Provider, ProviderError, TorrentItem, clean_ids

log = logging.getLogger("providers.torznab")

NS = "{http://torznab.com/schemas/2015/feed}"


def _date(text):
    try:
        return email.utils.parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError):
        return None


def parse_feed(xml_bytes, movie_categories=(), series_categories=()):
    """Torznab XML -> [TorrentItem] (provider/private not filled yet)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise ProviderError("the answer is not Torznab XML") from None
    if root.tag == "error":
        raise ProviderError(f"indexer error {root.get('code')}: {root.get('description')}")
    out = []
    for it in root.iter("item"):
        attrs = {}
        for a in it.findall(f"{NS}attr"):
            attrs.setdefault(a.get("name"), []).append(a.get("value"))
        attr = lambda k: (attrs.get(k) or [None])[0]
        title = (it.findtext("title") or "").strip()
        enclosure = it.find("enclosure")
        link = (enclosure.get("url") if enclosure is not None else None) or it.findtext("link")
        mag = attr("magneturl")
        if link and link.startswith("magnet:"):
            link, mag = None, mag or link
        if not title or not (link or mag or attr("infohash")):
            continue
        cats = {int(c) for c in (attrs.get("category") or []) if str(c).isdigit()}
        cats |= {int(c.text) for c in it.findall("category") if (c.text or "").strip().isdigit()}
        kind = None
        if any(c // 1000 * 1000 in series_categories or c in series_categories for c in cats):
            kind = "series"
        elif any(c // 1000 * 1000 in movie_categories or c in movie_categories for c in cats):
            kind = "movie"
        dvf = attr("downloadvolumefactor")
        try:
            freeleech = round((1 - float(dvf)) * 100) if dvf is not None else None
        except ValueError:
            freeleech = None
        size = attr("size") or it.findtext("size") or (enclosure.get("length") if enclosure is not None else None)
        seeds = attr("seeders")
        out.append(TorrentItem(
            title=title, torrent_url=link or mag, infohash=(attr("infohash") or "").lower() or None,
            size=int(size) if str(size or "").isdigit() and int(size) > 0 else None,
            seeds=int(seeds) if str(seeds or "").isdigit() else 0, published=_date(it.findtext("pubDate")),
            ids=clean_ids({"imdb": attr("imdbid") or attr("imdb"), "tmdb": attr("tmdbid"), "tvdb": attr("tvdbid")}) or None,
            freeleech=freeleech, category=",".join(str(c) for c in sorted(cats)), kind=kind,
            id=it.findtext("guid") or None))
    return out


class Torznab(Provider):
    type = "torznab"

    def __init__(self, name, options=None, secrets=None, context=None):
        super().__init__(name, options, secrets, context)
        self.require("url", "api_key")
        self.url = self.option("url").rstrip("/")
        self.movie_categories = list(self.option("movie_categories", [2000]))
        self.series_categories = list(self.option("series_categories", [5000]))
        self.id_search = bool(self.option("id_search", True))   # imdbid/tvdbid queries (most indexers support them)
        self.http = Http(pause=float(self.option("pause_s", 1.0)), timeout=float(self.option("timeout_s", 60)))

    def _query(self, **params):
        params = {"apikey": self.secret("api_key"), **{k: v for k, v in params.items() if v not in (None, "")}}
        data = self.http.get(self.url, params, label=f"{self.name} t={params.get('t')}")
        items = parse_feed(data, self.movie_categories, self.series_categories)
        return [self.stamp(i) for i in items]

    def _cats(self, cats):
        return ",".join(str(c) for c in cats)

    def feed(self):
        """The indexer's latest items in the movie and TV categories (Torznab "RSS mode": a search without q)."""
        cats = self.movie_categories + self.series_categories
        return self._query(t="search", cat=self._cats(cats), limit=self.option("feed_limit", 100))

    def search_movie(self, q):
        if self.id_search and q.imdb:
            found = self._query(t="movie", imdbid=q.imdb.removeprefix("tt"), cat=self._cats(self.movie_categories))
            if found:
                return found
        text = " ".join(x for x in (q.original_title or q.title, str(q.year or "")) if x).strip()
        return self._query(t="search", q=text, cat=self._cats(self.movie_categories)) if text else []

    def search_series(self, q):
        out, seen = [], set()

        def add(items):
            for i in items:
                key = i.infohash or i.torrent_url or i.title
                if key not in seen:
                    seen.add(key)
                    out.append(i)
        cats = self._cats(self.series_categories)
        if self.id_search and (q.tvdb or q.imdb):
            ids = {"tvdbid": q.tvdb} if q.tvdb else {"imdbid": (q.imdb or "").removeprefix("tt")}
            for s in sorted(q.seasons) or [None]:
                try:
                    add(self._query(t="tvsearch", cat=cats, season=s, **ids))
                except ProviderError as e:
                    log.warning("%s: tvsearch by id failed (%s) — falling back to text", self.name, e)
                    break
            if out:
                return out
        for text in q.queries():
            add(self._query(t="search", q=text, cat=cats))
        return out
