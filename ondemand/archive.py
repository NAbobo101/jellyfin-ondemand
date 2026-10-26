"""The archive: every item a feed-only provider announced, kept to be looked up by name later.

RSS feeds have no search and only show the latest items. Keeping each new item that passes
builds, on its own, an index that grows from the day the provider was added. Searching a
series or a movie then also looks here, through the same filters as everything else.

`torrent_url` may carry a passkey: as with releases, it only goes to the database, never to a log.
Seeds are those of the last sighting: they get old over time (see `find`).
"""

import json
import time

from .providers.base import TorrentItem
from .release_name import normalize, parse_movie, parse_series

RETENTION_DAYS = 365


def _name_and_kind(title, kind=None):
    if kind != "movie":
        d, _ = parse_series(title)
        if d:
            return normalize(d["raw_name"]), "series"
    if kind != "series":
        d, _ = parse_movie(title)
        if d:
            return normalize(d["raw_name"]), "movie"
    return None, None


def record(db, items):
    """Store the items not seen before (those whose name parses) and refresh seeds/sighting of the known
    ones. Returns how many are new."""
    now = time.time()
    new = 0
    c = db.con()
    with c:
        for it in items:
            name, kind = _name_and_kind(it.title, it.kind)
            if name is None or not (it.torrent_url or it.infohash):
                continue
            ident = it.id or it.infohash or it.torrent_url
            if c.execute("SELECT 1 FROM archive WHERE provider=? AND id=?", (it.provider, ident)).fetchone():
                c.execute("UPDATE archive SET seeds=?, seen_at=? WHERE provider=? AND id=?",
                          (it.seeds, now, it.provider, ident))
                continue
            data = {k: getattr(it, k) for k in ("title", "torrent_url", "infohash", "size", "seeds", "published",
                                                "ids", "freeleech", "category", "files", "id", "private", "resolutions")}
            c.execute("INSERT INTO archive(provider,id,title,norm_name,kind,seeds,published,item,seen_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (it.provider, ident, it.title, name, kind, it.seeds, it.published, json.dumps(data), now))
            new += 1
        c.execute("DELETE FROM archive WHERE seen_at < ?", (now - RETENTION_DAYS * 86400,))
    return new


def find(db, provider, name, kind=None):
    """Archived items of one provider for a name (normalized), as TorrentItems. Seeds older than a
    week are the ones from the day of the announcement and may not hold any more: playback confirms."""
    sql = "SELECT * FROM archive WHERE provider=? AND norm_name=?"
    args = [provider, normalize(name)]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    out = []
    for r in db.all(sql, *args):
        it = TorrentItem(**json.loads(r["item"]))
        it.provider, it.seeds, it.kind = provider, r["seeds"] or 0, r["kind"]
        out.append(it)
    return out
