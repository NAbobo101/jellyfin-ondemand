"""The movie filter: from raw candidates (provider items) to at most one release per movie version.

Versions: "1080p" (or "720p" when there is no 1080p, or "SD" when there is neither) goes to the
Movies library; "4K" (2160p) goes to Movies 4K. Pure functions over dicts, like the series filter.

Pipeline (every discard keeps its reason):
  1. parse the title      -> name, year, resolution, source, codec, HDR, language tags
  2. cheap eliminations   -> seeds, freeleech (private), foreign language, resolution, size
  3. identity             -> IMDb id from the provider, or by name + year through Jellyfin's
                             TMDb RemoteSearch (MovieResolver); unsure = quarantine
  4. one winner per (movie, version): SDR over HDR, source, PROPER, private vs public, seeds
"""

import logging
from collections import defaultdict

from .release_name import SOURCE_RANK, normalize, parse_movie
from .series_filter import foreign_reason

log = logging.getLogger("movies")

LABELS = {"2160p": "4K", "1080p": "1080p", "720p": "720p", None: "SD", "576p": "SD", "480p": "SD"}
MAIN_ORDER = ("1080p", "720p", "SD")       # only the best of these is kept, next to the 4K


def label_of(resolution):
    return LABELS.get(resolution, "SD")


def eliminate(c, cfg):
    if (c.get("seeds") or 0) < cfg["seeds_min"]:
        return "no seeders"
    if not c.get("private") and (c.get("seeds") or 0) < cfg.get("public_seeds_min", 2):
        return "too few seeders for a public torrent"
    if c.get("private") and c.get("freeleech") is not None and c["freeleech"] < cfg["freeleech_min"]:
        return "private tracker without freeleech"
    if (reason := foreign_reason(c, cfg)):
        return reason
    accepted = c.get("resolutions") or cfg["resolutions"]
    res = c["resolution"] if c["resolution"] in ("2160p", "1080p", "720p") else "SD"
    if res not in accepted:
        return f"resolution {res} not accepted"
    cap = cfg.get("max_gb_4k", 80) if res == "2160p" else cfg.get("max_gb", 40)
    if c.get("size") and c["size"] > cap * 1e9:
        return f"larger than the cap ({c['size'] / 1e9:.0f} GB > {cap} GB)"
    return None


class MovieResolver:
    """IMDb id of a movie from its name and year, through Jellyfin's RemoteSearch (TMDb, the plugin's key).

    RemoteSearch answers in the library's language, so names cannot be compared; the year can. TMDb
    ranks by relevance: the first result with the same year (±1 only if nothing matches exactly) wins.
    No such result = quarantine."""

    def __init__(self, jellyfin):
        self.jf = jellyfin
        self._cache = {}

    def by_name(self, raw_name, year):
        k = (normalize(raw_name), year)
        if k not in self._cache:
            self._cache[k] = self._resolve(raw_name, year)
        return self._cache[k]

    def _resolve(self, raw_name, year):
        try:
            found = self.jf.remote_search_movie(raw_name, year=year)
        except Exception as e:
            return None, f"TMDb search failed: {type(e).__name__}"
        exact = [r for r in found if r.get("ProductionYear") == year]
        near = [r for r in found if r.get("ProductionYear") and abs(r["ProductionYear"] - year) <= 1]
        best = (exact or near or [None])[0]
        if best is None:
            return None, f"no TMDb movie named like that from {year}"
        ids = best.get("ProviderIds") or {}
        imdb = ids.get("Imdb")
        if not imdb and ids.get("Tmdb"):
            try:
                imdb = self.jf.imdb_from_tmdb(ids["Tmdb"])
            except Exception:
                imdb = None
        if not imdb:
            return None, "TMDb match without an IMDb id"
        return {"imdb": imdb, "tmdb": ids.get("Tmdb"), "year": best.get("ProductionYear") or year}, None

    def by_tmdb(self, tmdb):
        try:
            return self.jf.imdb_from_tmdb(tmdb)
        except Exception:
            return None


def _rank(c, prefer_sdr):
    return (not (prefer_sdr and c["hdr"]), SOURCE_RANK[c["source"]], c["proper"])


def _choose(cands, prefer_sdr):
    """(winner, losses) among candidates for the same movie version."""
    top = max(_rank(c, prefer_sdr) for c in cands)
    tier = [c for c in cands if _rank(c, prefer_sdr) == top]
    losses = [(c, "worse quality tier") for c in cands if c not in tier]
    private, public = [c for c in tier if c["private"]], [c for c in tier if not c["private"]]
    if private and public and 2 * max(c["seeds"] for c in private) >= max(c["seeds"] for c in public):
        losses += [(c, "public source lost to a private one with enough seeds") for c in public]
        tier = private
    winner = max(tier, key=lambda c: (c["seeds"], c["codec"] != "x265", -(c.get("size") or 0)))
    losses += [(c, "fewer seeds") for c in tier if c is not winner]
    return winner, losses


def version(c):
    """A winning candidate -> a release in Database.save_release's format."""
    return {"label": label_of(c["resolution"]), "quality": c["resolution"] or "SD", "source_type": c["source"],
            "codec": c["codec"], "size": c.get("size"), "seeds": c["seeds"], "infohash": c.get("infohash"),
            "torrent_url": c.get("torrent_url"), "private": c["private"], "provider": c["provider"], "name": c["title"]}


def build_movies(candidates, resolver, cfg):
    """Candidates -> {"movies": {imdb: (title, [versions])}, "discards": [...], "quarantine": {...}}."""
    discards, quarantine, alive = [], {}, []
    for c in candidates:
        if c.get("kind") == "series":
            continue
        data, reason = parse_movie(c["title"])
        if data is None:
            if c.get("kind") == "movie":
                discards.append((c, reason))
            continue
        c = {**c, **data}
        if (reason := eliminate(c, cfg)):
            discards.append((c, reason))
            continue
        alive.append(c)

    by_imdb = defaultdict(list)
    meta = {}
    unresolved = defaultdict(list)
    for c in alive:
        ids = c.get("ids") or {}
        imdb = ids.get("imdb") or (resolver.by_tmdb(ids["tmdb"]) if ids.get("tmdb") and resolver else None)
        if imdb:
            by_imdb[imdb].append(c)
            meta.setdefault(imdb, {"tmdb": ids.get("tmdb"), "year": c["year"], "name": c["raw_name"]})
        else:
            unresolved[(normalize(c["raw_name"]), c["year"])].append(c)
    for (_, year), group in unresolved.items():
        found, reason = resolver.by_name(group[0]["raw_name"], year) if resolver else (None, "no resolver")
        if found is None:
            quarantine[f"{group[0]['raw_name']} ({year})"] = {"reason": reason, "candidates": len(group),
                                                              "example": group[0]["title"]}
            discards += [(c, "movie not resolved: " + reason) for c in group]
            continue
        by_imdb[found["imdb"]] += group
        meta.setdefault(found["imdb"], {"tmdb": found.get("tmdb"), "year": found["year"], "name": group[0]["raw_name"]})

    out = {}
    for imdb, cands in by_imdb.items():
        by_label = defaultdict(list)
        for c in cands:
            by_label[label_of(c["resolution"])].append(c)
        main = next((lab for lab in MAIN_ORDER if lab in by_label), None)
        keep = [lab for lab in (main, "4K") if lab and lab in by_label]
        discards += [(c, f"a better main version exists ({main})") for lab in by_label if lab not in keep
                     for c in by_label[lab]]
        versions = []
        for lab in keep:
            winner, losses = _choose(by_label[lab], cfg.get("prefer_sdr", True))
            discards += losses
            versions.append(version(winner))
        m = meta[imdb]
        title = {"key": f"imdb:{imdb}", "imdb": imdb, "tmdb": m.get("tmdb"), "name": m["name"], "year": m["year"],
                 "kind": "movie", "origin": versions[0]["provider"]}
        out[imdb] = (title, versions)
    return {"movies": out, "discards": discards, "quarantine": quarantine}


def build_for_movie(candidates, target, cfg):
    """Like `build_movies`, but for ONE movie already identified (search outside the library). `target`:
    {"imdb", "tmdb", "name", "names": [names to accept], "year"}. Items that carry another IMDb id, or
    whose name/year do not match, go first; the rest get the movie's ids. Returns (title, versions) or
    (None, []) and the discards."""
    names = {normalize(n) for n in target.get("names") or () if n}
    accepted, out = [], []
    for c in candidates:
        ids = c.get("ids") or {}
        if ids.get("imdb"):
            if ids["imdb"] != target["imdb"]:
                out.append((c, "another movie (IMDb id)"))
                continue
        else:
            data, reason = parse_movie(c["title"])
            if data is None:
                out.append((c, reason))
                continue
            if normalize(data["raw_name"]) not in names or (target.get("year") and abs(data["year"] - target["year"]) > 1):
                out.append((c, "another movie (name/year)"))
                continue
        accepted.append({**c, "ids": {"imdb": target["imdb"], "tmdb": target.get("tmdb")}})
    r = build_movies(accepted, None, cfg)
    found = r["movies"].get(target["imdb"])
    if found and target.get("name"):
        found[0].update(name=target["name"], year=target.get("year") or found[0]["year"])
    return found or (None, []), r["discards"] + out
