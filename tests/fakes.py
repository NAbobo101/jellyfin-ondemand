"""Shared test doubles: a TVmaze with made-up data (the ids are real ones) and candidate builders."""

import datetime

from ondemand.release_name import normalize

NOW = datetime.datetime(2026, 10, 20, 12).timestamp()


def show(id_, name, year, tvdb, imdb, country="US"):
    return {"id": id_, "name": name, "premiered": f"{year}-01-01" if year else None, "language": "English",
            "status": "Running", "externals": {"thetvdb": tvdb, "imdb": imdb}, "network": {"country": {"code": country}}}


class FakeTV:
    """Same interface as ondemand.tvmaze.TVmaze, with fake data."""

    def __init__(self):
        self.shows = {s["id"]: s for s in [
            show(1, "Franklin", 1997, 78150, "tt0203254", "CA"), show(2, "Franklin", 2024, 417280, "tt18351584"),
            show(3, "Franklin", 2025, 459949, "tt36279637"),
            show(4, "Doc", 2001, 84765, "tt0283724"), show(5, "Doc", 2025, 433263, "tt26748321"),
            show(6, "Doc", 2026, 473735, "tt36992503"),
            show(7, "A Different World", 1987, 76877, "tt0092339"), show(8, "A Different World", 2026, 463765, "tt33081352"),
            show(9, "Phony", 2026, 455844, "tt21952924"), show(10, "Dexter", 2006, 79349, "tt0773262"),
            show(11, "The Simpsons", 1989, 71663, "tt0096697"), show(12, "No IDs", 2020, None, None),
            show(13, "The Office", 2005, 73244, "tt0386676"), show(14, "The Office", 2001, 78107, "tt0290978", "GB"),
            show(15, "Dexter: New Blood", 2021, 403047, "tt14164730"),
        ]}
        self.eps = {
            2: [(1, n, f"2024-0{n}-01") for n in range(1, 4)],
            5: [(1, n, f"2025-0{n}-10") for n in range(1, 4)],
            6: [(1, n, f"2026-10-{10 + n:02d}") for n in range(1, 4)],
            4: [(1, 1, "2001-03-01")],
            8: [(1, n, f"2026-0{n}-01") for n in range(1, 4)],
            9: [(1, 1, "2026-10-13"), (1, 2, "2026-10-19")],
            10: [(s, n, f"{2005 + s}-10-{n:02d}") for s in (1, 2) for n in range(1, 13)],
            11: [(38, 1, "2026-10-05"), (38, 2, "2026-10-12"), (38, 3, "2026-10-19")],
            13: [(2, 1, "2005-09-01")], 14: [(2, 1, "2002-09-01")],
        }
        self.requests = 0

    def search(self, name):
        self.requests += 1
        return [{"score": 1.0, "show": s} for s in self.shows.values() if normalize(name) in normalize(s["name"])]

    def by_tvdb(self, tvdb):
        return next((s for s in self.shows.values() if str(s["externals"]["thetvdb"]) == str(tvdb)), None)

    def by_imdb(self, imdb):
        return next((s for s in self.shows.values() if s["externals"]["imdb"] == imdb), None)

    def episodes(self, id_):
        return self.eps.get(id_, [])

    def save(self):
        pass


def rss(title, seeds=5, category="Episodes HD", published=NOW, provider="feed-tracker", private=True):
    """A feed item: only the title and seeders, no ids (like most RSS feeds)."""
    return {"provider": provider, "private": private, "freeleech": None, "size": None, "ids": None, "id": title,
            "title": title, "category": category, "seeds": seeds, "published": published, "files": None,
            "torrent_url": f"https://tracker.example/rss/download/{abs(hash(title))}/KEY/x.torrent", "infohash": None}


def with_ids(title, files, seeds=1, tvdb=79349, size=30_000_000_000, fl=100, provider="id-tracker"):
    """An API item that carries the series ids and the torrent's file list (UNIT3D style)."""
    return {"provider": provider, "private": True, "freeleech": fl, "size": size, "id": title, "title": title,
            "category": "TV", "seeds": seeds, "published": NOW, "files": files, "infohash": None,
            "ids": {"tvdb": str(tvdb), "imdb": "tt0773262", "tmdb": "1405"},
            "torrent_url": "https://ids.example/torrent/download/1.KEY"}


def public(title, seeds=50, size=None, infohash="c" * 40):
    return {"provider": "public-index", "private": False, "freeleech": None, "size": size, "ids": None, "id": title,
            "title": title, "category": "TV", "seeds": seeds, "published": NOW, "files": None, "infohash": infohash,
            "torrent_url": f"magnet:?xt=urn:btih:{infohash}"}
