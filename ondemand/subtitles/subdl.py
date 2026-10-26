"""SubDL (subdl.com) subtitle provider.

API: GET api.subdl.com/api/v1/subtitles (api_key, imdb_id, languages, type=movie|tv,
season_number, episode_number); the download is dl.subdl.com + url (a .zip). The
returned url ALREADY CARRIES the api_key in the query: never log a URL.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from .base import ProviderLimit

USER_AGENT = "jellyfin-ondemand/1.0"


class SubDL:
    API = "https://api.subdl.com/api/v1/subtitles"
    DL = "https://dl.subdl.com"
    MAX_ZIP = 5 << 20
    name = "subdl"

    def __init__(self, api_key, language, timeout=20):
        """language: the SubDL code (languages.Language.subdl, e.g. "BR_PT", "EN", "ES")."""
        self.api_key, self.language, self.timeout = api_key, language, timeout

    def _open(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code in (429, 403):
                raise ProviderLimit(f"SubDL HTTP {e.code}") from None
            raise

    def search(self, imdb, kind="movie", season=None, episode=None):
        p = {"api_key": self.api_key, "imdb_id": imdb, "languages": self.language, "subs_per_page": 30, "type": kind}
        if kind == "tv":
            p.update(season_number=season, episode_number=episode)
        with self._open(self.API + "?" + urllib.parse.urlencode(p)) as r:
            d = json.load(r)
        if not d.get("status"):
            return []
        return [{"releases": s.get("releases") or [], "name": s.get("name") or "", "url": s["url"],
                 "hi": bool(s.get("hi")), "comment": s.get("comment") or ""}
                for s in d.get("subtitles") or [] if s.get("url")]

    def download(self, url):
        with self._open(self.DL + url) as r:
            data = r.read(self.MAX_ZIP + 1)
        if len(data) > self.MAX_ZIP:
            raise ValueError("subtitle zip too large")
        return data
