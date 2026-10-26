"""RSS feeds that qBittorrent already reads (its own RSS reader), read through the WebUI API.

Many private trackers only offer an RSS feed with your passkey in it. Register the feed in
qBittorrent (RSS tab, no auto-download rule) inside a folder, and this provider reads the
articles from qBittorrent: the passkey stays with qBittorrent, which also fetches the
.torrent on play.

    [[providers]]
    name = "my-rss"
    type = "qbit_rss"
    folder = "ondemand"            # the qBittorrent RSS folder
    feeds = ["My Tracker"]         # feed names (a name also matches "My Tracker 24h" etc.)
    private = true
    seed = { min_ratio = 1.0 }

An RSS feed has no search, so its items are kept in the archive (archive.py) on every import
and searched there by name — the archive grows from the day the feed is added.
"""

import re
import time
from email.utils import parsedate_to_datetime

from .base import Provider, TorrentItem

_SEEDERS = re.compile(r"Seeders:\s*(\d+)", re.I)
_CATEGORY = re.compile(r"Category:\s*(.*?)\s*-\s*Seeders", re.I)


def _date(text):
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError):
        return None


def articles(tree, folder, feeds):
    """Articles of the matching feeds, without repeating the same torrent across similar feeds
    ("X" and "X 24h" often share ids)."""
    seen, out = set(), []
    node = tree.get(folder) if folder else tree
    for name, feed in (node or {}).items():
        if not isinstance(feed, dict) or "articles" not in feed:
            continue
        if feeds and not any(name == f or name.startswith(f) for f in feeds):
            continue
        for a in feed["articles"]:
            ident = a.get("id") or a.get("title")
            if ident in seen:
                continue
            seen.add(ident)
            out.append(a)
    return out


class QBitRSS(Provider):
    type = "qbit_rss"

    def __init__(self, name, options=None, secrets=None, context=None):
        super().__init__(name, options, secrets, context)
        self.folder = self.option("folder", "ondemand")
        self.feeds = list(self.option("feeds", []))
        self.seeders = re.compile(self.option("seeders_pattern"), re.I) if self.option("seeders_pattern") else _SEEDERS
        self.category = re.compile(self.option("category_pattern"), re.I) if self.option("category_pattern") else _CATEGORY

    def feed(self):
        qbit = self.context.get("qbit")
        if qbit is None:
            return []
        out = []
        for a in articles(qbit.rss_items(), self.folder, self.feeds):
            desc = a.get("description") or ""
            seeds, cat = self.seeders.search(desc), self.category.search(desc)
            out.append(self.stamp(TorrentItem(
                title=a.get("title") or "", torrent_url=a.get("torrentURL") or a.get("link"),   # carries the passkey
                seeds=int(seeds.group(1)) if seeds else int(self.option("default_seeds", 0)),
                category=(cat.group(1) if cat else a.get("category")) or "",
                published=_date(a.get("date")) or time.time(), id=str(a.get("id") or a.get("title")))))
        return out
