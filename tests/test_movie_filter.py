import unittest

from ondemand import config
from ondemand.movie_filter import MovieResolver, build_movies, eliminate, label_of
from ondemand.release_name import parse_movie

CFG = config.DEFAULTS["movies"]


def item(title, seeds=10, private=False, provider="pub", ids=None, size=None, **kw):
    return {"title": title, "seeds": seeds, "private": private, "provider": provider, "ids": ids, "size": size,
            "freeleech": None, "category": "", "kind": None, "torrent_url": f"magnet:?xt=urn:btih:{abs(hash(title)):040d}",
            "infohash": None, "resolutions": None, **kw}


class FakeJellyfin:
    def __init__(self, results=None):
        self.results, self.calls = results or {}, 0

    def remote_search_movie(self, term, year=None, limit=8):
        self.calls += 1
        found = self.results.get(term, [])
        return [r for r in found if r.get("ProductionYear") == year] if year and self.strict_year else found

    strict_year = False

    def imdb_from_tmdb(self, tmdb):
        return {"100": "tt0000100"}.get(str(tmdb))


NOTLD = [{"Name": "A Noite dos Mortos-Vivos", "ProductionYear": 1968, "ProviderIds": {"Tmdb": "10331", "Imdb": "tt0063350"}},
         {"Name": "A Noite dos Mortos-Vivos", "ProductionYear": 1990, "ProviderIds": {"Tmdb": "10727", "Imdb": "tt0100258"}}]


class TestParseMovie(unittest.TestCase):
    def test_names_with_years_inside(self):
        self.assertEqual(parse_movie("2001 A Space Odyssey 1968 1080p BluRay")[0]["raw_name"], "2001 A Space Odyssey")
        d = parse_movie("Blade Runner 2049 (2017) 2160p UHD BluRay x265 HDR-TERMiNAL")[0]
        self.assertEqual((d["raw_name"], d["year"], d["resolution"], d["hdr"]), ("Blade Runner 2049", 2017, "2160p", True))
        self.assertEqual(parse_movie("1917 2019 1080p WEB-DL")[0]["raw_name"], "1917")

    def test_not_a_movie(self):
        self.assertIn("series", parse_movie("Franklin 2024 S01E03 1080p WEB h264-ETHEL")[1])
        self.assertIn("no year", parse_movie("Some Movie 1080p BluRay")[1])

    def test_labels(self):
        self.assertEqual([label_of(r) for r in ("2160p", "1080p", "720p", "480p", None)], ["4K", "1080p", "720p", "SD", "SD"])


class TestEliminate(unittest.TestCase):
    def e(self, title, **kw):
        c = {**item(title, **kw), **parse_movie(title)[0]}
        return eliminate(c, CFG)

    def test_reasons(self):
        self.assertIsNone(self.e("Movie 2020 1080p WEB-DL x264-G"))
        self.assertIn("SD", self.e("Movie 2020 DVDRip x264-G"))
        self.assertIsNone(self.e("Movie 2020 DVDRip x264-G", resolutions=["SD"]))      # provider override
        self.assertIn("public", self.e("Movie 2020 1080p WEB-DL x264-G", seeds=1))
        self.assertIn("freeleech", self.e("Movie 2020 1080p WEB-DL x264-G", private=True, freeleech=0))
        self.assertIn("FRENCH", self.e("Movie 2020 FRENCH 1080p WEB-DL x264-G"))
        self.assertIn("cap", self.e("Movie 2020 1080p WEB-DL x264-G", size=50_000_000_000))
        self.assertIsNone(self.e("Movie 2020 2160p WEB-DL x265-G", size=50_000_000_000))   # the 4K cap is larger


class TestBuild(unittest.TestCase):
    def test_by_imdb_one_winner_per_version_and_720p_only_without_1080p(self):
        ids = {"imdb": "tt4154756"}
        r = build_movies([item("Avengers Infinity War 2018 1080p BluRay x264-A", seeds=100, ids=ids),
                          item("Avengers Infinity War 2018 1080p WEBRip x264-B", seeds=300, ids=ids),
                          item("Avengers Infinity War 2018 720p BluRay x264-C", seeds=999, ids=ids),
                          item("Avengers Infinity War 2018 2160p BluRay x265 HDR-D", seeds=50, ids=ids),
                          item("Avengers Infinity War 2018 2160p BluRay x265-E", seeds=5, ids=ids)],
                         MovieResolver(FakeJellyfin()), CFG)
        title, versions = r["movies"]["tt4154756"]
        self.assertEqual((title["key"], title["name"], title["year"]), ("imdb:tt4154756", "Avengers Infinity War", 2018))
        by = {v["label"]: v for v in versions}
        self.assertEqual(sorted(by), ["1080p", "4K"])
        self.assertEqual(by["1080p"]["name"], "Avengers Infinity War 2018 1080p BluRay x264-A")   # BluRay beats WEBRip
        self.assertEqual(by["4K"]["name"], "Avengers Infinity War 2018 2160p BluRay x265-E")      # SDR beats HDR
        self.assertTrue(any("better main version" in m for _, m in r["discards"]))

    def test_resolved_by_name_and_year_through_tmdb(self):
        jf = FakeJellyfin({"Night of the Living Dead": NOTLD})
        r = build_movies([item("Night of the Living Dead 1968 1080p BluRay x264-G"),
                          item("Night.of.the.Living.Dead.1968.720p.WEB-DL-H"),
                          item("Night of the Living Dead 1990 1080p BluRay x264-G")], MovieResolver(jf), CFG)
        self.assertEqual(sorted(r["movies"]), ["tt0063350", "tt0100258"])
        self.assertEqual([v["label"] for v in r["movies"]["tt0063350"][1]], ["1080p"])
        self.assertEqual(jf.calls, 2)                     # one search per (name, year)

    def test_unknown_year_goes_to_quarantine(self):
        r = build_movies([item("Night of the Living Dead 1975 1080p BluRay x264-G")],
                         MovieResolver(FakeJellyfin({"Night of the Living Dead": NOTLD})), CFG)
        self.assertEqual(r["movies"], {})
        self.assertIn("Night of the Living Dead (1975)", r["quarantine"])

    def test_strict_year_filter_falls_back_to_a_search_without_the_year(self):
        jf = FakeJellyfin({"The Fast And The Furious": [
            {"Name": "Velozes", "ProductionYear": 2001, "ProviderIds": {"Imdb": "tt0232500"}},
            {"Name": "The Fast and the Furious", "ProductionYear": 1954, "ProviderIds": {"Imdb": "tt0047034"}}]})
        jf.strict_year = True                         # like TMDb: year=1955 finds nothing
        r = build_movies([item("The Fast And The Furious 1955 WEB-archive", resolutions=["SD"])], MovieResolver(jf), CFG)
        self.assertEqual(list(r["movies"]), ["tt0047034"])
        self.assertEqual(jf.calls, 2)

    def test_tmdb_only_id_is_converted(self):
        r = build_movies([item("Some Movie 2020 1080p WEB-DL x264-G", ids={"tmdb": "100"})],
                         MovieResolver(FakeJellyfin()), CFG)
        self.assertEqual(list(r["movies"]), ["tt0000100"])

    def test_series_items_are_ignored(self):
        r = build_movies([item("Franklin 2024 S01E03 1080p WEB h264-ETHEL"), item("X 2020 1080p", kind="series")],
                         MovieResolver(FakeJellyfin()), CFG)
        self.assertEqual((r["movies"], r["discards"]), ({}, []))

    def test_private_beats_public_with_half_the_seeds(self):
        ids = {"imdb": "tt1"}
        r = build_movies([item("M 2020 1080p BluRay x264-PUB", seeds=10, ids=ids),
                          item("M 2020 1080p BluRay x264-PRIV", seeds=5, private=True, provider="priv", ids=ids)],
                         MovieResolver(FakeJellyfin()), CFG)
        v = r["movies"]["tt1"][1][0]
        self.assertEqual((v["provider"], v["private"]), ("priv", True))


if __name__ == "__main__":
    unittest.main()
