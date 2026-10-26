"""Internet Archive: public-domain feature films, as a working example and a safe first test.

    [[providers]]
    name = "internet-archive"
    type = "archive_org"
    private = false
    collection = "feature_films"   # any archive.org movie collection
    rows = 100                     # how many items per import (most downloaded first)

archive.org publishes a .torrent for every item and seeds it itself over HTTP (web seeds),
so these torrents always start. The films are mostly SD: the provider accepts SD on its own
items (`resolutions`), whatever the global [movies] resolutions say. Its torrents carry metadata
files (.xml, .js transcripts) next to the video, so it is `trusted` (no public-torrent file check).
"""

import re

from .base import Http, Provider, TorrentItem

SEARCH = "https://archive.org/advancedsearch.php"
_YEAR = re.compile(r"((?:18|19|20)\d\d)")


class ArchiveOrg(Provider):
    type = "archive_org"

    def __init__(self, name, options=None, secrets=None, context=None):
        options = {"private": False, "trusted": True, "resolutions": ["SD", "720p", "1080p"], **(options or {})}
        super().__init__(name, options, secrets, context)
        self.collection = self.option("collection", "feature_films")
        self.http = Http(pause=float(self.option("pause_s", 1.0)))

    def _search(self, query, rows):
        d = self.http.get_json(SEARCH, {"q": query, "fl[]": ["identifier", "title", "year", "date", "downloads",
                                                             "item_size", "publicdate"],
                                        "sort[]": "downloads desc", "rows": rows, "output": "json"},
                               label=f"{self.name} advancedsearch")
        return [self.stamp(i) for i in (self.to_item(x) for x in (d.get("response") or {}).get("docs") or []) if i]

    def to_item(self, doc):
        ident, title = doc.get("identifier"), (doc.get("title") or "").strip()
        m = _YEAR.search(str(doc.get("year") or doc.get("date") or ""))
        if not ident or not title or not m:
            return None
        # A scene-style name, so the shared parser reads it like any other release.
        clean = re.sub(r"[\s._]+", " ", re.sub(r"\(?\b(18|19|20)\d\d\b\)?", "", title)).strip(" -")
        return TorrentItem(
            title=f"{clean} {m.group(1)} WEB-archive", kind="movie", id=ident,
            torrent_url=f"https://archive.org/download/{ident}/{ident}_archive.torrent",
            size=int(doc["item_size"]) if str(doc.get("item_size") or "").isdigit() else None,
            seeds=10,                    # archive.org web-seeds every item: it always has a source
            category="Movies")

    def feed(self):
        return self._search(f"collection:({self.collection}) AND mediatype:(movies)", int(self.option("rows", 100)))

    def search_movie(self, q):
        name = (q.original_title or q.title or "").replace('"', "")
        if not name:
            return []
        query = f'collection:({self.collection}) AND mediatype:(movies) AND title:("{name}")'
        if q.year:
            query += f" AND year:[{q.year - 1} TO {q.year + 1}]"
        return self._search(query, 20)
