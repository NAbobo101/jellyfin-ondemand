import datetime
import unittest

from ondemand import series_filter
from ondemand.release_name import normalize
from ondemand.series_filter import Resolver, build_catalog, check, eliminate, parse_title
from tests.fakes import FakeTV, rss, with_ids

CFG = {"seeds_min": 1, "freeleech_min": 100, "resolutions": ["1080p", "720p"], "pack_max_gb": 60, "single_max_gb": 8}
TODAY = "2026-10-20"


def build(candidates, local=None, tv=None, cfg=CFG):
    return build_catalog(candidates, Resolver(tv or FakeTV(), today=TODAY), cfg, local)


class TestParse(unittest.TestCase):
    def p(self, title):
        d, reason = parse_title(title)
        self.assertIsNone(reason, title)
        return d

    def test_episode_with_year_and_quality(self):
        d = self.p("Franklin 2024 S01E03 1080p WEB h264-ETHEL")
        self.assertEqual((d["raw_name"], d["year"], d["season"], d["episodes"], d["pack"]), ("Franklin", 2024, 1, [3], False))
        self.assertEqual((d["resolution"], d["source"], d["codec"], d["group"]), ("1080p", "WEB-DL", "x264", "ETHEL"))

    def test_pack_and_language(self):
        d = self.p("Cyberpunk Edgerunners S02 1080p NF WEB-DL DUAL DDP5 1 H 264-FLUX")
        self.assertTrue(d["pack"] and d["multi"])
        self.assertEqual((d["season"], d["episodes"]), (2, []))
        self.assertEqual(self.p("Mayor of Kingstown S02 NORDiC 2160p SKST WEB-DL H 265-NORViNE")["foreign"], ["NORDIC"])

    def test_multi_episode_and_range(self):
        self.assertEqual(self.p("The Office US S02E01E02 1080p")["episodes"], [1, 2])
        self.assertEqual(self.p("The Office US S02E01E02 1080p")["country"], "US")
        self.assertEqual(self.p("Show.Name.2024.S03E04-E06.720p.HDTV.x264-GRP")["episodes"], [4, 5, 6])

    def test_hdr_is_not_confused_with_sdr_nor_web_dl(self):
        self.assertTrue(self.p("Tear Along the Dotted Line S01E06 DV 2160p WEB H265-CROiSSANTS")["hdr"])
        self.assertFalse(self.p("El Chapo S03 REPACK 1080p NF WEB-DL H.265 SDR DD")["hdr"])
        self.assertTrue(self.p("El Chapo S03 REPACK 1080p NF WEB-DL H.265 SDR DD")["proper"])

    def test_sources(self):
        self.assertEqual(self.p("A S01E01 1080p WEBRip x265-G")["source"], "WEBRip")
        self.assertEqual(self.p("A S01E01 1080p BluRay x264-G")["source"], "BluRay")
        self.assertEqual(self.p("A S01E01 720p HDTV x264-G")["source"], "HDTV")
        self.assertIsNone(self.p("A S01E01 720p x264-FENiX")["source"])

    def test_not_a_series(self):
        self.assertEqual(parse_title("The Daily Show 2026 10 19 Guest 1080p WEB h264-EDITH")[1], "daily show (by date)")
        self.assertIn("not a series", parse_title("Project Hail Mary 2026 IMAX 1080p BluRay x264-ANARCHY")[1])
        self.assertIn("multi-season", parse_title("Show S01-S03 1080p BluRay")[1])


class TestNormalize(unittest.TestCase):
    def test_apostrophe_accent_and_ampersand(self):
        self.assertEqual(normalize("It's Always Sunny"), normalize("Its Always Sunny"))
        self.assertEqual(normalize("Amélie & Co."), "amelie and co")


class TestEliminate(unittest.TestCase):
    def e(self, title, cfg=CFG, **extra):
        d, _ = parse_title(title)
        return eliminate({**d, "seeds": 5, "private": True, "freeleech": None, "size": None, "category": "", **extra}, cfg)

    def test_reasons(self):
        self.assertEqual(self.e("A S01E01 1080p WEB h264-G", seeds=0), "no seeders")
        self.assertIn("freeleech", self.e("A S01E01 1080p WEB h264-G", freeleech=50))
        self.assertIn("GERMAN", self.e("A S01E01 GERMAN 1080p WEB h264-G"))
        self.assertIn("Foreign", self.e("A S01E01 1080p WEB h264-G", category="Foreign"))
        self.assertIn("2160p", self.e("A S01E01 2160p WEB h265-G"))
        self.assertIn("SD", self.e("A S01E01 HDTV x264-G"))
        self.assertIn("cap", self.e("A S01E01 1080p WEB h264-G", size=9_000_000_000))
        self.assertIn("cap", self.e("A S01 1080p WEB h264-G", size=61_000_000_000))
        self.assertIn("public", self.e("A S01E01 1080p WEB h264-G", private=False, seeds=1))

    def test_passes(self):
        self.assertIsNone(self.e("A S01E01 1080p WEB h264-G"))
        self.assertIsNone(self.e("A S01E01 GERMAN DUAL 1080p WEB h264-G"))     # DUAL carries the original
        self.assertIsNone(self.e("A S01 1080p WEB h264-G", size=30_000_000_000))

    def test_allowed_language_tags(self):
        cfg = {**CFG, "allowed_language_tags": ["spanish", "LATINO"]}
        self.assertIsNone(self.e("A S01E01 SPANISH 1080p WEB h264-G", cfg))
        self.assertIsNone(self.e("A S01E01 1080p WEB h264-G", cfg, category="Foreign"))
        self.assertIn("GERMAN", self.e("A S01E01 GERMAN 1080p WEB h264-G", cfg))


class TestIdentity(unittest.TestCase):
    def test_year_breaks_namesake_ties(self):
        for title, tvdb in [("Franklin 2024 S01E03 1080p WEB h264-X", "417280"), ("Doc 2025 S01E01 1080p WEB h264-X", "433263"),
                            ("A Different World 2026 S01E02 1080p WEB h264-X", "463765")]:
            r = build([rss(title)])
            self.assertEqual(list(r["series"]), [f"tvdb:{tvdb}"], title)

    def test_without_year_the_episode_date_breaks_the_tie(self):
        # "Doc" without a year: the only one that aired S01E02 in the item's week is the 2026 one
        r = build([rss("Doc S01E02 1080p WEB h264-X", published=datetime.datetime(2026, 10, 12).timestamp())])
        self.assertEqual(list(r["series"]), ["tvdb:473735"])

    def test_ambiguous_goes_to_quarantine(self):
        r = build([rss("Doc S01E01 1080p WEB h264-X", published=datetime.datetime(2020, 1, 1).timestamp())])
        self.assertEqual(r["series"], {})
        self.assertIn("ambiguous", r["quarantine"]["Doc"]["reason"])

    def test_country_breaks_the_tie(self):
        r = build([rss("The Office US S02E01 1080p WEB h264-X", published=datetime.datetime(2005, 9, 1).timestamp())])
        self.assertEqual(list(r["series"]), ["tvdb:73244"])

    def test_not_found_and_year_that_does_not_match(self):
        r = build([rss("Name That Does Not Exist S01E01 1080p WEB h264-X"), rss("Franklin 2010 S01E01 1080p WEB h264-X")])
        self.assertEqual(r["series"], {})
        self.assertEqual(len(r["quarantine"]), 2)
        self.assertTrue(any("no series" in q["reason"] for q in r["quarantine"].values()))

    def test_series_without_tvdb_or_imdb_is_left_out(self):
        r = build([rss("No IDs S01E01 1080p WEB h264-X")])
        self.assertIn("without tvdb or imdb", r["quarantine"]["No IDs"]["reason"])

    def test_episode_outside_the_canonical_list(self):
        r = build([rss("Phony S01E09 1080p WEB h264-X"), rss("Phony S01E02 1080p WEB h264-X")])
        self.assertEqual(sorted(r["series"]["tvdb:455844"]["seasons"][1]), [2])
        self.assertTrue(any("canonical list" in m for _, m in r["discards"]))


class TestChoice(unittest.TestCase):
    def winner(self, r, key, t, e):
        return r["series"][key]["seasons"][t][e]["winner"]

    def test_six_releases_become_one_winner_per_episode(self):
        r = build([rss("Phony S01E02 1080p WEB h264-ETHEL", seeds=0), rss("Phony S01E02 PROPER 1080p WEB H264-GLHF", seeds=56),
                   rss("Phony S01E01 DV HDR 2160p WEB H265-CAKES", seeds=1), rss("Phony S01E01 720p x264-FENiX", seeds=11),
                   rss("Phony S01E02 DV HDR 2160p WEB h265-ETHEL", seeds=1), rss("Phony S01E02 1080p x265-ELiTE", seeds=16)])
        eps = r["series"]["tvdb:455844"]["seasons"][1]
        self.assertEqual(sorted(eps), [1, 2])
        self.assertEqual(self.winner(r, "tvdb:455844", 1, 1)["group"], "FENiX")      # the only one left (4K out)
        self.assertEqual(self.winner(r, "tvdb:455844", 1, 2)["group"], "GLHF")       # PROPER WEB-DL beats a x265 re-encode
        self.assertEqual(check(r), [])

    def test_season_coherence_and_a_gap_with_720p(self):
        r = build([rss("The Simpsons S38E01 720p WEB H264-iNSiDiOUS", seeds=20), rss("The Simpsons S38E02 720p WEB H264-iNSiDiOUS", seeds=9),
                   rss("The Simpsons S38E03 720p WEB H264-iNSiDiOUS", seeds=18), rss("The Simpsons S38E03 1080p x265-ELiTE", seeds=73)])
        # 720p covers 3 episodes, 1080p only 1: the season stays in 720p even with the 1080p far more seeded
        self.assertEqual({self.winner(r, "tvdb:71663", 38, e)["resolution"] for e in (1, 2, 3)}, {"720p"})

    def test_pack_vs_single(self):
        files = [f"Dexter (2006) S01E{n:02d} (1080p BDRip x265).mkv" for n in range(1, 13)]
        pack = with_ids("Dexter S01 1080p BluRay DD+ 5.1 x265-Vialle", files, seeds=3)
        r = build([pack, rss("Dexter S01E01 1080p BluRay x264-AAA", seeds=5)])
        self.assertTrue(self.winner(r, "tvdb:79349", 1, 1)["pack"])             # 5 < 2×3: the pack wins
        r = build([pack, rss("Dexter S01E01 1080p BluRay x264-AAA", seeds=6)])
        self.assertFalse(self.winner(r, "tvdb:79349", 1, 1)["pack"])            # 6 ≥ 2×3: the single wins
        self.assertTrue(self.winner(r, "tvdb:79349", 1, 2)["pack"])             # the other 11 stay in the pack
        self.assertEqual(sorted(r["series"]["tvdb:79349"]["seasons"][1]), list(range(1, 13)))

    def test_pack_without_file_list_covers_only_what_aired(self):
        r = build([rss("The Simpsons S38 1080p WEB-DL H 264-X", seeds=30)])
        self.assertEqual(sorted(r["series"]["tvdb:71663"]["seasons"][38]), [1, 2, 3])

    def test_hdr_only_loses_when_there_is_sdr(self):
        r = build([rss("Phony S01E01 HDR 1080p WEB H265-A", seeds=50), rss("Phony S01E01 1080p WEB H264-B", seeds=2),
                   rss("Phony S01E02 HDR 1080p WEB H265-A", seeds=50)])
        eps = r["series"]["tvdb:455844"]["seasons"][1]
        self.assertEqual(eps[1]["winner"]["group"], "B")
        self.assertEqual(eps[2]["winner"]["group"], "A")                        # without SDR, HDR is what there is

    def test_x264_breaks_equal_seeds(self):
        r = build([rss("Phony S01E01 1080p WEB H265-A", seeds=5), rss("Phony S01E01 1080p WEB H264-B", seeds=5)])
        self.assertEqual(self.winner(r, "tvdb:455844", 1, 1)["group"], "B")

    def test_private_beats_public_with_half_the_seeds(self):
        pub = {**rss("Phony S01E01 1080p WEB H264-PUB", seeds=10), "private": False, "provider": "public-index"}
        r = build([pub, rss("Phony S01E01 1080p WEB H264-PRIV", seeds=5)])
        self.assertEqual(self.winner(r, "tvdb:455844", 1, 1)["group"], "PRIV")
        r = build([pub, rss("Phony S01E01 1080p WEB H264-PRIV", seeds=4)])
        self.assertEqual(self.winner(r, "tvdb:455844", 1, 1)["group"], "PUB")


class TestMerge(unittest.TestCase):
    def test_same_series_by_ids_and_by_name_becomes_one(self):
        pack = with_ids("Dexter S01 1080p BluRay x265-V", [f"Dexter S01E{n:02d}.mkv" for n in range(1, 13)])
        r = build([pack, rss("Dexter 2006 S01E03 1080p WEB h264-X")])
        self.assertEqual(list(r["series"]), ["tvdb:79349"])
        self.assertEqual(check(r), [])

    def test_pack_without_episodes_in_its_files_falls(self):
        r = build([with_ids("Dexter S01 1080p BluRay x265-V", ["synopsis.nfo"])])
        self.assertEqual(r["series"], {})
        self.assertTrue(any("without identifiable episodes" in m for _, m in r["discards"]))

    def test_only_fills_gaps_of_local_series(self):
        r = build([rss("Phony S01E01 1080p WEB H264-B"), rss("Phony S01E02 1080p WEB H264-B")], local={"tvdb:455844": {(1, 1)}})
        eps = r["series"]["tvdb:455844"]["seasons"][1]
        self.assertTrue(eps[1]["local"])
        self.assertFalse(eps[2]["local"])

    def test_folder_name_carries_the_tvdb_id(self):
        r = build([rss("Phony S01E01 1080p WEB H264-B")])
        self.assertEqual(series_filter.folder_name(r["series"]["tvdb:455844"]["series"]), "Phony (2026) [tvdbid-455844]")

    def test_check_flags_a_duplicate_key(self):
        r = build([rss("Phony S01E01 1080p WEB H264-B")])
        other = dict(r["series"]["tvdb:455844"])
        r["series"]["imdb:tt21952924"] = other
        self.assertTrue(check(r))


if __name__ == "__main__":
    unittest.main()
