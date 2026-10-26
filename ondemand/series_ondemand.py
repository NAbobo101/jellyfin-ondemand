"""A whole series on demand: gathers everything that exists of it and leaves it in the catalog.

Used by the search outside the library ("Add series") and when a catalog series is opened
("complete"). Does not talk to Jellyfin nor write to disk: it returns what the filter
(series_filter.py) decided and, if asked, registers it in the database; the Catalog writes
the folders.

Sources: every provider that can search series (and the archive of the feed-only ones), all
through the SAME filter (exact series name, TVmaze's canonical list, one winner per episode).
"""

import hashlib
import logging

from . import series_filter
from .providers import SeriesQuery

log = logging.getLogger("series")


def episode_release(c, t, e):
    """Winning candidate -> release (Database.save_release's format) of one episode. The torrent_tag is
    shared by every episode of the same torrent (pack), because an RSS item's infohash is unknown before
    the add."""
    tag = hashlib.md5(f"{c['provider']}:{c['id']}".encode()).hexdigest()[:10]
    return {"label": f"S{t:02d}E{e:02d}", "season": t, "episode": e, "name": c["title"],
            "quality": c["resolution"], "source_type": c["source"], "codec": c["codec"], "size": c.get("size"),
            "seeds": c["seeds"], "infohash": c.get("infohash"), "torrent_url": c.get("torrent_url"),
            "private": c["private"], "provider": c["provider"], "torrent_tag": tag}


def identify(resolver, ids, name=None, year=None):
    """The filter's series from ids (Tvdb/Imdb/Tmdb): ORIGINAL name and episode list come from TVmaze."""
    if not (ids.get("tvdb") or ids.get("imdb")):
        return None
    return resolver.by_ids({k: ids[k] for k in ("tvdb", "imdb", "tmdb") if ids.get(k)}, name or "", year)


def seasons_of(resolver, series):
    canon = resolver.canon(series) or {}
    return sorted({t for t, _ in canon if t >= 1})


def gather(series, registry, progress=None, seasons=None):
    """(candidates, warnings). A source that is down does not take the others down: it becomes a warning."""
    q = SeriesQuery(name=series["name"], year=series.get("year"), tvdb=series.get("tvdb"), imdb=series.get("imdb"),
                    tmdb=series.get("tmdb"), seasons=list(seasons or []))
    items, warnings = registry.search_series(q, progress)
    return [i.candidate() for i in items], warnings


def research(series, resolver, cfg, registry, local=None, progress=None):
    """Runs every source and the filter for ONE series. Returns (filter result, summary)."""
    seasons = seasons_of(resolver, series)
    cands, warnings = gather(series, registry, progress, seasons)
    r = series_filter.build_for_series(cands, series, resolver, cfg["series"], local)
    return r, summary(r, series, resolver, len(cands), warnings)


def summary(r, series, resolver, n_candidates, warnings=()):
    """Numbers to show the user: how many aired episodes the series has and how many were found."""
    canon = resolver.canon(series) or {}
    today = resolver.today
    aired = {k for k, d in canon.items() if k[0] >= 1 and d and d <= today}
    s = (r["series"].get(series["key"]) or {}).get("seasons", {})
    found = {(t, e) for t, eps in s.items() for e in eps}
    return {"candidates": n_candidates, "episodes_aired": len(aired), "episodes_found": len(found),
            "new": sum(1 for t, eps in s.items() for e in eps.values() if not e["local"]),
            "seasons_found": sorted(s), "missing": sorted(aired - found), "warnings": list(warnings)}


def register(cat, r, series):
    """Store in the database what the filter chose (episodes that already exist locally do not go in).
    Returns how many episodes were registered."""
    s = r["series"].get(series["key"])
    if not s:
        return 0
    eps = [episode_release(e["winner"], t, n) for t, season in s["seasons"].items()
           for n, e in season.items() if not e["local"]]
    if eps:
        cat.register_series(s["series"], eps)
    return len(eps)
