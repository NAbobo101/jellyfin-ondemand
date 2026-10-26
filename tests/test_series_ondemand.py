import os
import tempfile
import unittest

from ondemand import archive
from ondemand.db import Database
from ondemand.providers import TorrentItem, magnet
from ondemand.series_filter import Resolver, build_for_series, check, matches_series, parse_title
from ondemand.series_ondemand import episode_release
from tests.fakes import NOW, FakeTV, rss
from tests.test_series_filter import CFG, TODAY

SERIES = {"key": "tvdb:79349", "tvdb": "79349", "imdb": "tt0773262", "tmdb": "1405", "name": "Dexter", "year": 2006,
          "country": "US", "tvmaze": 10}
CFG_PUB = {**CFG, "public_seeds_min": 2}


def pub(title, seeds=50, size=2_000_000_000, h=None, provider="public-index"):
    h = h or (format(abs(hash(title)), "x") * 3)[:40].ljust(40, "0")
    return {"provider": provider, "private": False, "freeleech": None, "ids": None, "id": h, "title": title,
            "category": "TV / HD", "seeds": seeds, "published": NOW, "size": size, "infohash": h,
            "torrent_url": magnet(h, title), "files": None}


class TestExactName(unittest.TestCase):
    def data(self, title):
        return parse_title(title)[0]

    def test_dexter_is_not_new_blood(self):
        self.assertTrue(matches_series(self.data("Dexter S01E01 1080p BluRay x264-G"), SERIES))
        self.assertTrue(matches_series(self.data("Dexter 2006 S01E01 1080p BluRay x264-G"), SERIES))
        self.assertFalse(matches_series(self.data("Dexter New Blood S01E01 1080p WEB x264-G"), SERIES))
        self.assertFalse(matches_series(self.data("Dexter Resurrection 2025 S01E01 1080p WEB x264-G"), SERIES))
        self.assertFalse(matches_series(self.data("Dexter 2021 S01E01 1080p WEB x264-G"), SERIES))     # another year
        self.assertFalse(matches_series(self.data("Dexter UK S01E01 1080p WEB x264-G"), SERIES))        # another country


class TestBuildForSeries(unittest.TestCase):
    def build(self, candidates, local=None):
        return build_for_series(candidates, SERIES, Resolver(FakeTV(), today=TODAY), CFG_PUB, local)

    def test_only_the_requested_series_and_one_winner_per_episode(self):
        r = self.build([pub("Dexter S01E01 1080p BluRay x264-AAA", seeds=40), pub("Dexter S01E01 1080p WEBRip x264-BBB", seeds=90),
                        pub("Dexter S01E02 1080p BluRay x264-AAA", seeds=30),
                        pub("Dexter New Blood S01E01 1080p WEB x264-CCC", seeds=500)])
        self.assertEqual(list(r["series"]), ["tvdb:79349"])
        eps = r["series"]["tvdb:79349"]["seasons"][1]
        self.assertEqual(sorted(eps), [1, 2])
        self.assertEqual(eps[1]["winner"]["group"], "AAA")                # BluRay beats a worse source, even with fewer seeds
        self.assertTrue(any(m == "belongs to another series" for _, m in r["discards"]))
        self.assertEqual(check(r), [])

    def test_huge_multi_season_pack_and_size_cap(self):
        r = self.build([pub("Dexter (2006) Season 1-8 S01-S08 (1080p BluRay x265 HEVC)", size=188_000_000_000),
                        pub("Dexter S01 1080p BluRay x265-ZZZ", size=80_000_000_000),
                        pub("Dexter S01E03 1080p BluRay x264-AAA", seeds=12)])
        eps = r["series"]["tvdb:79349"]["seasons"][1]
        self.assertEqual(sorted(eps), [3])                                   # the 80 GB pack is over the 60 cap
        reasons = " | ".join(m for _, m in r["discards"])
        self.assertIn("multi-season", reasons)
        self.assertIn("cap", reasons)

    def test_public_with_few_seeds_falls(self):
        r = self.build([pub("Dexter S01E01 1080p BluRay x264-AAA", seeds=1)])
        self.assertEqual(r["series"], {})
        self.assertIn("too few seeders", r["discards"][0][1])

    def test_private_archive_and_public_compete_by_the_usual_rules(self):
        r = self.build([rss("Dexter S01E01 1080p BluRay x264-TLG", seeds=60), pub("Dexter S01E01 1080p BluRay x264-PUB", seeds=40)])
        e = r["series"]["tvdb:79349"]["seasons"][1][1]
        self.assertEqual(e["winner"]["group"], "TLG")
        self.assertEqual(len(e["losses"]), 1)

    def test_episode_release_shares_the_tag_only_between_episodes_of_the_same_torrent(self):
        c = {"provider": "feed-tracker", "id": "999", "title": "A S01 1080p", "resolution": "1080p", "source": "WEB-DL",
             "codec": "x264", "seeds": 3, "private": True, "torrent_url": "u"}
        a, b = episode_release(c, 1, 1), episode_release(c, 1, 2)
        self.assertEqual(a["torrent_tag"], b["torrent_tag"])
        self.assertNotEqual(a["torrent_tag"], episode_release({**c, "id": "1000"}, 1, 1)["torrent_tag"])
        self.assertEqual((a["label"], b["label"]), ("S01E01", "S01E02"))


class TestArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, title, seeds=5, url="https://tracker.example/rss/download/1/KEY/x.torrent", ident=None):
        return TorrentItem(title=title, torrent_url=url, seeds=seeds, id=ident or title, provider="rss", private=True)

    def test_keeps_parsable_items_refreshes_seeds_and_finds_by_name(self):
        items = [self.item("Dexter S01E01 1080p BluRay x264-AAA"), self.item("Project Hail Mary 2026 1080p BluRay x264-ANARCHY"),
                 self.item("Dexter S01E02 1080p BluRay x264-AAA", url=None), self.item("Dexter New Blood S01E01 1080p WEB x264-B"),
                 self.item("garbage without anything")]
        self.assertEqual(archive.record(self.db, items), 3)                  # no link and no parse stay out
        items[0].seeds = 40
        self.assertEqual(archive.record(self.db, items), 0)
        found = archive.find(self.db, "rss", "Dexter", "series")
        self.assertEqual([(i.title, i.seeds, i.private, i.provider) for i in found],
                         [("Dexter S01E01 1080p BluRay x264-AAA", 40, True, "rss")])
        self.assertEqual(len(archive.find(self.db, "rss", "Dexter New Blood")), 1)
        self.assertEqual(archive.find(self.db, "rss", "Project Hail Mary", "movie")[0].kind, "movie")

    def test_retention(self):
        archive.record(self.db, [self.item("Dexter S01E01 1080p BluRay x264-AAA")])
        self.db.execute("UPDATE archive SET seen_at=?", 1.0)
        archive.record(self.db, [])
        self.assertEqual(self.db.one("SELECT count(*) n FROM archive")["n"], 0)


if __name__ == "__main__":
    unittest.main()
