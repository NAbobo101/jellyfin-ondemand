"""The series filter: from raw candidates (provider items) to ONE release per episode.

Everything here is a pure function over dicts (the network stays in TVmaze and the providers, the
disk in the catalog), so the simulation (`cli series-simulate`) and the tests run exactly the code
that runs in production.

Pipeline (every discard keeps its reason; nothing disappears silently):
  1. parse the title       -> series (name/year/country), season, episode(s) or pack, quality
  2. cheap eliminations    -> 0 seeders, no freeleech (private), foreign language, resolution, size
  3. series identity       -> one key `tvdb:N` (ids from the provider, or resolved by name on TVmaze)
  4. canonical episode     -> (season, episode) must exist in TVmaze's episode list
  5. one winner per (series, season, episode): season coherence, source, PROPER, pack vs single, seeds
  6. gaps                  -> what already exists in the local series library is not written

One key per series = a single folder: "Show", "Show 2026" and "Show US" resolved to the same id
land on the same key (alias index in `Resolver`).

A candidate is a dict (providers.base.TorrentItem.candidate()): provider, private, id, title,
category, seeds, published, size, freeleech, ids, files, infohash, torrent_url.
"""

import datetime
import re
from collections import defaultdict

from .release_name import _EP, _EP_X, SOURCE_RANK, normalize, parse_series

parse_title = parse_series     # the series filter's view of a release name

EXCLUDED_CATEGORIES = {"audio", "pc", "ps5", "ps4", "nintendo switch", "music videos", "ebooks", "education", "comics",
                       "mac", "xbox", "games"}


def foreign_reason(c, cfg):
    """Why the release is in a foreign language for this user, or None. A DUAL/MULTI release carries
    the original audio; `allowed_language_tags` are tags that are not foreign to the user."""
    allowed = {t.upper() for t in cfg.get("allowed_language_tags") or ()}
    if (c.get("category") or "").lower() == "foreign" and not allowed:
        return "foreign language (Foreign category)"
    tags = [t for t in c["foreign"] if t not in allowed]
    if tags and not c["multi"]:
        return "foreign language (" + "/".join(tags) + ")"
    return None


# ---------------------------------------------------------------- cheap eliminations
def eliminate(c, cfg):
    """Reason for the discard (before spending a TVmaze request), or None."""
    if (c.get("seeds") or 0) < cfg["seeds_min"]:
        return "no seeders"
    if not c.get("private") and (c.get("seeds") or 0) < cfg.get("public_seeds_min", 2):
        return "too few seeders for a public torrent"
    if c.get("private") and c.get("freeleech") is not None and c["freeleech"] < cfg["freeleech_min"]:
        return "private tracker without freeleech"
    if (reason := foreign_reason(c, cfg)):
        return reason
    if c["resolution"] not in cfg["resolutions"]:
        return f"resolution {c['resolution'] or 'SD'} not accepted"
    cap = cfg["pack_max_gb"] if c["pack"] else cfg["single_max_gb"]
    if c.get("size") and c["size"] > cap * 1e9:
        return f"larger than the cap ({c['size'] / 1e9:.0f} GB > {cap} GB)"
    return None


# ---------------------------------------------------------------- identity
class Resolver:
    """The series of each candidate. `tvmaze`: an object with search/by_tvdb/by_imdb/episodes (ondemand.tvmaze)."""

    def __init__(self, tvmaze, today=None):
        from .tvmaze import summary
        self._summary = summary
        self.tv = tvmaze
        self.today = today or datetime.date.today().isoformat()
        self.index = {}         # "tvdb:N" / "imdb:tt" / "tmdb:N" -> series key
        self.series = {}        # key -> series
        self._names = {}        # (normalized name, year, country) -> (series or None, reason)

    # series = {"key","tvdb","imdb","tmdb","name","year","tvmaze","country"}
    def _register(self, s):
        ids = [f"{k}:{s[k]}" for k in ("tvdb", "imdb", "tmdb") if s.get(k)]
        existing = next((self.index[i] for i in ids if i in self.index), None)
        if existing:
            known = self.series[existing]
            for k, v in s.items():
                if v and not known.get(k):
                    known[k] = v
            key = existing
        else:
            key = ids[0] if ids else f"tvmaze:{s['tvmaze']}"
            self.series[key] = {**s, "key": key}
        for i in ids:
            self.index.setdefault(i, key)
        return self.series[key]

    def by_ids(self, ids, raw_name, year):
        """A candidate with ready ids. TVmaze only gives the original name and the episode list."""
        show = None
        if ids.get("tvdb"):
            show = self.tv.by_tvdb(ids["tvdb"])
        if not show and ids.get("imdb"):
            show = self.tv.by_imdb(ids["imdb"])
        r = self._summary(show) if show else {}
        return self._register({"tvdb": str(ids["tvdb"]) if ids.get("tvdb") else r.get("tvdb"),
                               "imdb": ids.get("imdb") or r.get("imdb"), "tmdb": ids.get("tmdb"),
                               "name": r.get("name") or raw_name, "year": r.get("year") or year,
                               "tvmaze": r.get("id"), "country": r.get("country")})

    def canon(self, series):
        """{(season, episode): airdate} of the series, or None if TVmaze does not know it."""
        if not series.get("tvmaze"):
            return None
        return {(s, e): d for s, e, d in self.tv.episodes(series["tvmaze"])}

    def by_name(self, raw_name, year, country, samples):
        """(series, None) or (None, reason). `samples`: [(season, episode|None, item date)] of every
        candidate with that name — evidence to break ties between namesakes."""
        k = (normalize(raw_name), year, country)
        if k not in self._names:
            self._names[k] = self._resolve_name(raw_name, year, country, samples)
        return self._names[k]

    def _resolve_name(self, raw_name, year, country, samples):
        target = normalize(raw_name)
        found = [self._summary(x["show"]) for x in self.tv.search(raw_name)]
        exact = [s for s in found if normalize(s["name"]) == target]
        if not exact:
            return None, "name not found on TVmaze"
        if year:
            sure = [s for s in exact if s["year"] == year] or [s for s in exact if s["year"] and abs(s["year"] - year) <= 1]
            if not sure:
                return None, f"no series with that name premiered in {year} (premieres: {sorted({s['year'] for s in exact if s['year']})})"
            exact = sure
        if len(exact) > 1 and country:
            exact = [s for s in exact if s["country"] == country] or exact
        if len(exact) > 1:
            exact = self._break_tie(exact, samples)
        if len(exact) != 1:
            ops = ", ".join(f"{s['name']} ({s['year']}) tvdb {s['tvdb']}" for s in exact[:4])
            return None, f"ambiguous between {len(exact)} series: {ops}"
        s = exact[0]
        if not (s["tvdb"] or s["imdb"]):
            return None, "series without tvdb or imdb on TVmaze"
        return self._register({"tvdb": s["tvdb"], "imdb": s["imdb"], "name": s["name"], "year": s["year"],
                               "tvmaze": s["id"], "country": s["country"]}), None

    def _break_tie(self, options, samples):
        """Namesakes without a year: the one that has the season/episode wins and, for an episode, aired
        close to the item's date (±14 days — a new episode is released on the day)."""
        points = []
        for s in options:
            eps = {(a, b): d for a, b, d in self.tv.episodes(s["id"])}
            p = 0
            for season, ep, date in samples:
                if ep is None:
                    p += any(t == season for t, _ in eps)
                elif (d := eps.get((season, ep))) and date:
                    when = datetime.date.fromtimestamp(date)
                    p += abs((datetime.date.fromisoformat(d) - when).days) <= 14
            points.append(p)
        best = max(points)
        return [s for s, p in zip(options, points) if p == best] if best > 0 else options


# ---------------------------------------------------------------- pipeline
def _coverage(c, series, canon, today):
    """Episodes (season, number) the release delivers, or (None, reason)."""
    t = c["season"]
    if not c["pack"]:
        if len(c["episodes"]) > 1:
            return None, "multi-episode file (not supported: playback picks one file per episode)"
        eps = {(t, e) for e in c["episodes"]}
        if canon is not None and not eps <= set(canon):
            return None, "episode not in TVmaze's canonical list"
        return eps, None
    if c.get("files") is not None:                                     # the provider listed the torrent's files
        eps = set()
        for name in c["files"]:
            m = _EP.search(re.sub(r"[._]", " ", name)) or _EP_X.search(name)
            if m and int(m.group(1)) == t:
                eps.add((t, int(m.group(2))))
        if not eps:
            return None, "pack without identifiable episodes in its files"
        if canon is not None:
            eps &= set(canon)
        return eps or None, None if eps else "pack outside TVmaze's canonical list"
    if canon is None:
        return None, "pack without a canonical list to know its episodes"
    eps = {k for k, d in canon.items() if k[0] == t and d and d <= today}   # no file list: assume the aired season
    return (eps, None) if eps else (None, f"season {t} has no aired episodes in the canonical list")


def _rank_key(c, res_pref):
    """Higher is better (compares the tier only: pack vs single and seeds come later)."""
    return (c["resolution"] == res_pref, SOURCE_RANK[c["source"]], c["proper"])


def _choose(covering, res_pref):
    """(winner, [(candidate, reason)]) from a list of candidates covering the same episode."""
    losses = []
    sdr = [c for c in covering if not c["hdr"]]
    if sdr and len(sdr) < len(covering):
        losses += [(c, "HDR/DV with an SDR version available") for c in covering if c["hdr"]]
        covering = sdr
    top = max(_rank_key(c, res_pref) for c in covering)
    tier = [c for c in covering if _rank_key(c, res_pref) == top]
    losses += [(c, "worse quality tier") for c in covering if c not in tier]
    # A private source beats a public one when it has at least half the seeds: cleaner, and no public
    # torrent exposure. Only matters with both in the same quality tier.
    private, public = [c for c in tier if c["private"]], [c for c in tier if not c["private"]]
    if private and public and 2 * max(c["seeds"] for c in private) >= max(c["seeds"] for c in public):
        losses += [(c, "public source lost to a private one with enough seeds") for c in public]
        tier = private
    order = lambda c: (c["seeds"], c["codec"] != "x265", -(c.get("size") or 0))      # seeds, x264, smaller
    packs = sorted((c for c in tier if c["pack"]), key=order, reverse=True)
    singles = sorted((c for c in tier if not c["pack"]), key=order, reverse=True)
    if packs and singles:
        single_wins = singles[0]["seeds"] >= 2 * packs[0]["seeds"]
        winner = singles[0] if single_wins else packs[0]
    else:
        winner = (packs or singles)[0]
    losses += [(c, "lost to a " + ("pack" if winner["pack"] else "single episode") + " with more seeds / pack-vs-single rule")
               for c in tier if c is not winner]
    return winner, losses


def build_catalog(candidates, resolver, cfg, local=None):
    """Candidates -> {"series": {key: {...}}, "discards": [...], "quarantine": {...}}.

    `local`: {"tvdb:N"|"imdb:tt": {(season, episode)}} of what already exists in the local series."""
    local = local or {}
    discards, quarantine = [], {}
    alive = []
    for c in candidates:
        data, reason = parse_title(c["title"])
        if data is None or (c.get("category") or "").lower() in EXCLUDED_CATEGORIES:
            discards.append((c, reason or "category is not a series"))
            continue
        c = {**c, **data}
        if (reason := eliminate(c, cfg)):
            discards.append((c, reason))
            continue
        alive.append(c)

    # identity: some providers carry ids; the rest is grouped by name to break ties with every sample
    by_name = defaultdict(list)
    resolved = []
    for c in alive:
        if c.get("ids"):
            resolved.append((c, resolver.by_ids(c["ids"], c["raw_name"], c["year"])))
        else:
            by_name[(normalize(c["raw_name"]), c["year"], c["country"])].append(c)
    for (_, year, country), group in by_name.items():
        samples = [(c["season"], None if c["pack"] else c["episodes"][0], c["published"]) for c in group]
        series, reason = resolver.by_name(group[0]["raw_name"], year, country, samples)
        if series is None:
            quarantine[group[0]["raw_name"] + (f" ({year})" if year else "")] = {"reason": reason, "candidates": len(group),
                                                                               "example": group[0]["title"]}
            discards += [(c, "series not resolved: " + reason) for c in group]
        else:
            resolved += [(c, series) for c in group]

    # canonical episode + coverage
    by_episode = defaultdict(list)             # (key, season, episode) -> covering candidates
    canons = {}
    for c, series in resolved:
        key = series["key"]
        if key not in canons:
            canons[key] = resolver.canon(series)
        eps, reason = _coverage(c, series, canons[key], resolver.today)
        if eps is None:
            discards.append((c, reason))
            continue
        c["series"], c["covers"] = key, sorted(eps)
        for t, e in eps:
            by_episode[(key, t, e)].append(c)

    # winner per episode, with each season's preferred resolution
    out = {}
    by_season = defaultdict(list)
    for (key, t, e), cands in by_episode.items():
        by_season[(key, t)].append((e, cands))
    for (key, t), eps in by_season.items():
        res_coverage = defaultdict(int)
        for _, cands in eps:
            for r in {c["resolution"] for c in cands}:
                res_coverage[r] += 1
        res_pref = max(res_coverage, key=lambda r: (res_coverage[r], r == "1080p"))
        series = resolver.series[key]
        have = set()
        for ident in (f"tvdb:{series.get('tvdb')}", f"imdb:{series.get('imdb')}", f"tmdb:{series.get('tmdb')}"):
            have |= local.get(ident, set())
        s = out.setdefault(key, {"series": series, "seasons": {}})
        for e, cands in sorted(eps):
            winner, losses = _choose(cands, res_pref)
            s["seasons"].setdefault(t, {})[e] = {"winner": winner, "losses": losses, "local": (t, e) in have,
                                                 "competitors": len(cands)}
    return {"series": out, "discards": discards, "quarantine": quarantine,
            "no_canonical_list": sorted(resolver.series[k]["name"] for k, v in canons.items() if v is None)}


def matches_series(data, series):
    """Is the parsed title of THIS series? Identical original name (ignoring accents/punctuation), same
    year (±1) or none, and a compatible country. It is what tells "Dexter" from "Dexter New Blood" in a
    search by name."""
    if normalize(data["raw_name"]) != normalize(series["name"]):
        return False
    if data["year"] and series.get("year") and abs(data["year"] - series["year"]) > 1:
        return False
    if data["country"] and series.get("country") and data["country"] != series["country"]:
        return False
    return True


def build_for_series(candidates, series, resolver, cfg, local=None):
    """Like `build_catalog`, but for ONE series already identified (on-demand search): candidates of
    another series (same partial name, different year or country) go first, and the remaining ones get
    the series' ids — the identity does not need to be guessed again. Returns the same dictionary."""
    ids = {k: series[k] for k in ("tvdb", "imdb", "tmdb") if series.get(k)}
    accepted, out = [], []
    for c in candidates:
        data, reason = parse_title(c["title"])
        if data is None:
            out.append((c, reason))
        elif not matches_series(data, series):
            out.append((c, "belongs to another series"))
        else:
            accepted.append({**c, "ids": ids})
    r = build_catalog(accepted, resolver, cfg, local)
    r["discards"] += out
    return r


def check(result):
    """The filter's guarantees: a list of problems (empty = ok). Two keys cannot share an id, and
    each series folder must be unique."""
    problems, seen, folders = [], {}, {}
    for key, s in result["series"].items():
        series = s["series"]
        for k in ("tvdb", "imdb", "tmdb"):
            if series.get(k):
                ident = f"{k}:{series[k]}"
                if seen.setdefault(ident, key) != key:
                    problems.append(f"{ident} appears in two series: {seen[ident]} and {key}")
        folder = folder_name(series)
        if folders.setdefault(folder, key) != key:
            problems.append(f"repeated folder: {folder}")
    return problems


def folder_name(series):
    """`Name (Year) [tvdbid-N]`: the id in the folder is what makes Jellyfin merge it with the local series."""
    base = f"{series['name']} ({series['year']})" if series.get("year") else series["name"]
    if series.get("tvdb"):
        return f"{base} [tvdbid-{series['tvdb']}]"
    if series.get("imdb"):
        return f"{base} [imdbid-{series['imdb']}]"
    return base
