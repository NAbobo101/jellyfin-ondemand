"""An example provider for a made-up JSON API — copy it to your plugins folder and adapt it.

The site answers:
    GET https://torrents.example/api/latest            -> {"results": [ {...}, ... ]}
    GET https://torrents.example/api/search?imdb=tt…   -> the same shape
with items like
    {"name": "Night of the Living Dead 1968 1080p BluRay x264-GRP", "magnet": "magnet:?xt=…",
     "seeders": 42, "size": 4000000000, "imdb": "tt0063350", "type": "movie", "added": 1760000000}

config.toml:
    [[providers]]
    name = "example-site"
    type = "python"
    class = "json_api_provider:JsonApiProvider"
    url = "https://torrents.example"
    private = false
"""

from ondemand.providers import Provider, TorrentItem
from ondemand.providers.base import Http


class JsonApiProvider(Provider):
    type = "json_api"

    def __init__(self, name, options=None, secrets=None, context=None):
        super().__init__(name, options, secrets, context)
        self.require("url")                               # stops at start-up if the option is missing
        self.base = self.option("url").rstrip("/")
        self.http = Http(pause=1.0)                       # one request per second, errors without the URL

    def _items(self, data):
        out = []
        for r in data.get("results") or []:
            out.append(self.stamp(TorrentItem(            # stamp() fills in the provider name and private flag
                title=r["name"], torrent_url=r.get("magnet"), seeds=int(r.get("seeders") or 0),
                size=r.get("size"), ids={"imdb": r.get("imdb")}, published=r.get("added"),
                kind={"movie": "movie", "tv": "series"}.get(r.get("type")))))
        return out

    def feed(self):                                       # the periodic catalog import
        return self._items(self.http.get_json(self.base + "/api/latest", label=self.name))

    def search_movie(self, query):                        # "search outside the library"
        if not query.imdb:
            return []
        return self._items(self.http.get_json(self.base + "/api/search", {"imdb": query.imdb}, label=self.name))

    # No search_series(): this site cannot search a series. Series from its feed are still kept in the
    # archive and found by name when someone adds a series.
