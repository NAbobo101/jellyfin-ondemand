"""The archive: every item a provider ever showed, with its address and what the filter decided about it.

Why: RSS feeds have no search and only show the latest items, and any source may take a torrent down. If
only each episode's winner were kept (as a release), a series item that fell into quarantine (a season TVmaze
did not list yet, an ambiguous name, TVmaze down) would never be seen again once it left the feed, and a dead
torrent would have nothing to replace it.

So every item is kept (table `archive`, key provider + id within the provider):
  - what the source said (the TorrentItem as JSON: title, seeds, size, ids, files, `torrent_url` — which
    may carry a passkey: it only goes to the database, NEVER to a log);
  - `tag` = the same hash as the releases' `torrent_tag` (series_ondemand.episode_release): links the item
    to the episodes it delivers;
  - for series, the state: new -> winner | fallback | quarantine | discarded, and dead (playback proved
    it useless).

The import (service.import_series) filters what the feeds show now plus the archive's `pending` items;
quarantine comes back with a back-off (1, 2, 4… up to 30 days). Seeds are those of the last sighting: they
get old over time (playback confirms). Movies use the archive only to search feed-only providers by name.
"""

import hashlib
import json
import time

from .providers.base import TorrentItem
from .release_name import normalize, parse_movie, parse_series

RETENTION_DAYS = 365          # except items of a series in the catalog, which stay
MAX_BACKOFF_DAYS = 30
FAILURES_FOR_DEAD = 2         # a failure that may be transient (cold magnet, tracker down) needs 2…
FAILURE_GAP_S = 1800          # …at least 30 min apart (a cold first probe sometimes takes over 60 s)
FAILURE_VALID_S = 7 * 86400   # an older failure no longer counts

# A pack may win one episode and lose another: the strongest state wins.
STRENGTH = {"winner": 3, "fallback": 2, "quarantine": 1, "discarded": 0}
FIELDS = ("title", "torrent_url", "infohash", "size", "seeds", "published", "ids", "freeleech", "category", "files",
          "id", "private", "resolutions")


def tag_of(provider, ident):
    """Shared by every episode of the same torrent: an RSS item's infohash is unknown before the add."""
    return hashlib.md5(f"{provider}:{ident}".encode()).hexdigest()[:10]


def ident_of(it):
    return it.id or it.infohash or it.torrent_url


def temporary(reason):
    """A discard that may change on its own (TVmaze learns the season, the name stops being ambiguous)."""
    return reason.startswith("series not resolved") or "canonical list" in reason


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


def record(db, items, now=None):
    """Store the items not seen before (those whose name parses) and refresh seeds/sighting/address of the
    known ones. Returns how many are new. Also applies the retention."""
    now = now or time.time()
    new = 0
    c = db.con()
    with c:
        for it in items:
            name, kind = _name_and_kind(it.title, it.kind)
            if name is None or not (it.torrent_url or it.infohash):
                continue
            ident = ident_of(it)
            data = json.dumps({k: getattr(it, k) for k in FIELDS})
            if c.execute("SELECT 1 FROM archive WHERE provider=? AND id=?", (it.provider, ident)).fetchone():
                c.execute("UPDATE archive SET seeds=?, seen_at=?, item=? WHERE provider=? AND id=?",
                          (it.seeds, now, data, it.provider, ident))
                continue
            c.execute("INSERT INTO archive(provider,id,tag,title,norm_name,kind,seeds,published,item,seen_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (it.provider, ident, tag_of(it.provider, ident), it.title, name, kind, it.seeds, it.published,
                       data, now))
            new += 1
        c.execute("DELETE FROM archive WHERE seen_at < ? AND (series_key IS NULL OR series_key NOT IN "
                  "(SELECT key FROM titles WHERE kind='series'))", (now - RETENTION_DAYS * 86400,))
    return new


def _item(r):
    it = TorrentItem(**json.loads(r["item"]))
    it.provider, it.seeds, it.kind = r["provider"], r["seeds"] or 0, r["kind"]
    return it


def find(db, provider, name, kind=None):
    """Archived items of one provider for a name (normalized), as TorrentItems (movie search of feed-only
    providers). Seeds older than a week are the ones from the day of the announcement: playback confirms."""
    sql = "SELECT * FROM archive WHERE provider=? AND norm_name=?"
    args = [provider, normalize(name)]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return [_item(r) for r in db.all(sql, *args)]


def find_series(db, name, keys=()):
    """Everything the archive has of a series (normalized name or series key such as `tvdb:N`), from every
    provider, except the dead ones. What a series search and the fallback add to the fresh results."""
    keys = [k for k in keys if k]
    sql = "SELECT * FROM archive WHERE kind='series' AND state<>'dead' AND (norm_name=?"
    sql += f" OR series_key IN ({','.join('?' * len(keys))}))" if keys else ")"
    return [_item(r) for r in db.all(sql, normalize(name), *keys)]


def pending(db, limit_names=None, now=None, priority=()):
    """Archived series items the import should (re)evaluate: the `new` ones and the `quarantine` ones whose
    turn came — and, with them, every live item of the same series (the filter picks a season's resolution
    looking at the whole season). `limit_names` bounds TVmaze requests and memory; names in `priority`
    (the catalog's series, normalized) go first, then the most recently seen. Already schedules the next
    attempt of those that went in (back-off 1, 2, 4… days up to 30): the filter's result decides the state.
    Returns candidate dicts (the filters' format)."""
    now = now or time.time()
    names = db.all("SELECT norm_name, max(seen_at) v FROM archive WHERE kind='series' AND state IN ('new','quarantine') "
                   "AND COALESCE(retry_at, 0) <= ? GROUP BY norm_name", now)
    priority = set(priority)
    names = sorted(names, key=lambda r: (r["norm_name"] in priority, r["v"]), reverse=True)
    chosen = [r["norm_name"] for r in (names if limit_names is None else names[:limit_names])]
    out, seen = [], set()
    c = db.con()
    with c:
        for i in range(0, len(chosen), 200):                  # SQLite's parameter limit
            batch = chosen[i:i + 200]
            marks = ",".join("?" * len(batch))
            due = c.execute(f"SELECT provider, id, attempts, series_key FROM archive WHERE kind='series' "
                            f"AND norm_name IN ({marks}) AND state IN ('new','quarantine') AND COALESCE(retry_at, 0) <= ?",
                            (*batch, now)).fetchall()
            for d in due:
                backoff = min(MAX_BACKOFF_DAYS, 2 ** d["attempts"]) * 86400
                c.execute("UPDATE archive SET attempts=attempts+1, retry_at=?, evaluated_at=? WHERE provider=? AND id=?",
                          (now + backoff, now, d["provider"], d["id"]))
            keys = sorted({d["series_key"] for d in due if d["series_key"]})
            sql = (f"SELECT * FROM archive WHERE kind='series' AND state NOT IN ('dead','discarded') "
                   f"AND (norm_name IN ({marks})")
            sql += f" OR series_key IN ({','.join('?' * len(keys))}))" if keys else ")"
            for r in c.execute(sql, (*batch, *keys)).fetchall():
                if (r["provider"], r["id"]) not in seen:
                    seen.add((r["provider"], r["id"]))
                    out.append(_item(r).candidate())
    return out


def record_result(db, r, now=None):
    """State of every item that went through the series filter (`build_catalog`/`build_for_series`).
    Dead stays dead; an item of another series (search by name) is not touched."""
    now = now or time.time()
    decision = {}                    # (provider, id) -> (state, reason, series key, season)

    def put(c, state, reason, key=None, season=None):
        if "provider" not in c or "id" not in c:
            return
        k = (c["provider"], str(c["id"]))
        if k not in decision or STRENGTH[state] > STRENGTH[decision[k][0]]:
            decision[k] = (state, reason, key or c.get("series"), season or c.get("season"))

    for key, s in r["series"].items():
        for t, eps in s["seasons"].items():
            for e, d in eps.items():
                put(d["winner"], "winner", f"S{t:02d}E{e:02d}", key, t)
                for c, reason in d["losses"]:
                    put(c, "fallback", reason, key, t)
    for c, reason in r["discards"]:
        if reason == "belongs to another series":
            continue
        put(c, "quarantine" if temporary(reason) else "discarded", reason)
    c = db.con()
    with c:
        for (provider, ident), (state, reason, key, season) in decision.items():
            c.execute("UPDATE archive SET state=?, reason=?, series_key=COALESCE(?, series_key), season=COALESCE(?, season), "
                      "evaluated_at=?, retry_at=CASE WHEN ?='quarantine' THEN max(COALESCE(retry_at, 0), ?) ELSE retry_at END "
                      "WHERE provider=? AND id=? AND state<>'dead'",
                      (state, reason, key, season, now, state, now + 86400, provider, ident))
    return len(decision)


def failure(db, tag, reason, severe, now=None):
    """The item's torrent did not play. `severe` (fake, no video for the episode) = dead at once; the rest
    (cold magnet, tracker down, empty swarm) only on the 2nd failure at least 30 min after the first.
    Returns the state afterwards ('dead' or the one before), or None if the archive does not know the torrent."""
    now = now or time.time()
    r = db.one("SELECT provider, id, state, failures, failed_at FROM archive WHERE tag=?", tag)
    if r is None:
        return None
    if r["state"] == "dead":
        return "dead"
    failures = r["failures"] if r["failed_at"] and now - r["failed_at"] < FAILURE_VALID_S else 0
    if not severe and r["failed_at"] and now - r["failed_at"] < FAILURE_GAP_S:
        return r["state"]                    # the same playback attempt: does not count twice
    failures += 1
    dead = severe or failures >= FAILURES_FOR_DEAD
    db.execute("UPDATE archive SET failures=?, failed_at=?, state=CASE WHEN ? THEN 'dead' ELSE state END, "
               "reason=CASE WHEN ? THEN ? ELSE reason END WHERE provider=? AND id=?",
               failures, now, int(dead), int(dead), f"did not play: {reason}", r["provider"], r["id"])
    return "dead" if dead else r["state"]


def migrate(db):
    """Once: archive rows from before the states get their tag, and the torrents that are already episode
    releases go into the archive as `winner`, with the series' ids — so every episode has a fallback chain.
    The provider's id is rebuilt from the release (infohash, then url) and only counts if its tag matches
    the release's `torrent_tag`. Archived rows of a release's torrent get the series key (they stay `new`:
    the first import re-evaluates them)."""
    if db.kv_get("archive_states_migrated"):
        return
    now = time.time()
    c = db.con()
    with c:
        for r in c.execute("SELECT provider, id FROM archive WHERE tag IS NULL").fetchall():
            c.execute("UPDATE archive SET tag=? WHERE provider=? AND id=?", (tag_of(r["provider"], r["id"]), r["provider"], r["id"]))
        rels = c.execute("SELECT r.provider, r.torrent_tag, r.name, r.torrent_url, r.infohash, r.seeds, r.size, r.private, "
                         "r.season, r.created_at, t.key, t.tvdb, t.imdb, t.tmdb FROM releases r "
                         "JOIN titles t ON t.id=r.title_id WHERE t.kind='series' AND r.torrent_tag IS NOT NULL "
                         "GROUP BY r.torrent_tag").fetchall()
        for r in rels:
            c.execute("UPDATE archive SET series_key=COALESCE(series_key, ?) WHERE tag=?", (r["key"], r["torrent_tag"]))
            if c.execute("SELECT 1 FROM archive WHERE tag=?", (r["torrent_tag"],)).fetchone():
                continue
            ident = next((o for o in (r["infohash"], r["torrent_url"]) if o and tag_of(r["provider"], o) == r["torrent_tag"]), None)
            d, _ = parse_series(r["name"] or "")
            if ident is None or d is None:
                continue
            ids = {k: r[k] for k in ("tvdb", "imdb", "tmdb") if r[k]} or None
            it = TorrentItem(title=r["name"], torrent_url=r["torrent_url"], infohash=r["infohash"], size=r["size"],
                             seeds=r["seeds"] or 0, ids=ids, kind="series", id=ident, private=bool(r["private"]))
            c.execute("INSERT INTO archive(provider,id,tag,title,norm_name,kind,seeds,item,series_key,season,state,seen_at) "
                      "VALUES(?,?,?,?,?,'series',?,?,?,?,'winner',?)",
                      (r["provider"], ident, r["torrent_tag"], r["name"], normalize(d["raw_name"]), r["seeds"],
                       json.dumps({k: getattr(it, k) for k in FIELDS}), r["key"], r["season"], r["created_at"] or now))
    db.kv_set("archive_states_migrated", now)
