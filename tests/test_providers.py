import json
import os
import tempfile
import unittest
from unittest import mock

from ondemand.db import Database
from ondemand.providers import MovieQuery, Provider, ProviderError, SeriesQuery, TorrentItem
from ondemand.providers.archive_org import ArchiveOrg
from ondemand.providers.base import clean_ids, parse_size
from ondemand.providers.qbit_rss import QBitRSS
from ondemand.providers.registry import Registry, build
from ondemand.providers.torznab import Torznab, parse_feed
from ondemand.providers.unit3d import Unit3D

TORZNAB = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
<channel>
 <item>
  <title>Night of the Living Dead 1968 1080p BluRay x264-GRP</title>
  <guid>https://indexer.example/details/1</guid>
  <link>https://indexer.example/download/1?key=SECRET</link>
  <pubDate>Mon, 19 Oct 2026 12:00:00 +0000</pubDate>
  <enclosure url="https://indexer.example/download/1?key=SECRET" length="4000000000" type="application/x-bittorrent"/>
  <torznab:attr name="category" value="2040"/>
  <torznab:attr name="seeders" value="42"/>
  <torznab:attr name="infohash" value="ABCDEF0123456789ABCDEF0123456789ABCDEF01"/>
  <torznab:attr name="imdbid" value="0063350"/>
  <torznab:attr name="downloadvolumefactor" value="0"/>
 </item>
 <item>
  <title>Dexter S01 1080p BluRay x265-GRP</title>
  <guid>2</guid>
  <link>magnet:?xt=urn:btih:1111111111111111111111111111111111111111</link>
  <size>30000000000</size>
  <category>5040</category>
  <torznab:attr name="seeders" value="7"/>
  <torznab:attr name="tvdbid" value="79349"/>
 </item>
 <item><title>no link at all</title></item>
</channel></rss>"""


class TestBase(unittest.TestCase):
    def test_clean_ids_and_size(self):
        self.assertEqual(clean_ids({"imdb": "63350", "tmdb": "0", "tvdb": " 79349 "}), {"imdb": "tt0063350", "tvdb": "79349"})
        self.assertEqual(clean_ids({"imdb": "tt0063350"}), {"imdb": "tt0063350"})
        self.assertEqual((parse_size("1.5 GB"), parse_size("700 MiB"), parse_size("12"), parse_size("x")),
                         (int(1.5 * (1 << 30)), 700 << 20, 12, None))

    def test_candidate_builds_a_magnet_and_an_id(self):
        c = TorrentItem(title="X 2020 1080p", infohash="ABC", provider="p").candidate()
        self.assertTrue(c["torrent_url"].startswith("magnet:?xt=urn:btih:abc"))
        self.assertEqual((c["id"], c["infohash"]), ("ABC", "abc"))

    def test_capabilities_and_seed_rule(self):
        class FeedOnly(Provider):
            type = "x"

            def feed(self):
                return []
        p = FeedOnly("a", {"private": True, "seed": {"min_hours": 72, "min_ratio": 1, "either": True}})
        self.assertEqual(p.capabilities, {"feed"})
        self.assertEqual(p.seed_rule.as_lifecycle(), {"private": True, "min_seed_hours": 72, "min_ratio": 1.0, "either": True})
        self.assertEqual(FeedOnly("b", {"private": False}).seed_rule.min_ratio, 0)
        self.assertEqual(FeedOnly("c", {"feed": False}).capabilities, set())     # turned off in the config


class TestTorznab(unittest.TestCase):
    def test_parse(self):
        a, b = parse_feed(TORZNAB, [2000], [5000])
        self.assertEqual((a.kind, a.seeds, a.size, a.freeleech, a.ids), ("movie", 42, 4000000000, 100, {"imdb": "tt0063350"}))
        self.assertEqual(a.infohash, "abcdef0123456789abcdef0123456789abcdef01")
        self.assertEqual((b.kind, b.torrent_url[:8], b.ids, b.freeleech, b.size), ("series", "magnet:?", {"tvdb": "79349"}, None, 30000000000))

    def test_error_answer(self):
        with self.assertRaises(ProviderError):
            parse_feed(b'<error code="100" description="Invalid API Key"/>')

    def provider(self, **opts):
        return Torznab("idx", {"url": "http://prowlarr:9696/1/api", "private": True, **opts}, {"api_key": "K"})

    def test_movie_search_by_imdb_then_text(self):
        p = self.provider()
        calls = []

        def fake_get(url, params=None, **kw):
            calls.append(params)
            return TORZNAB if params.get("t") == "search" else b"<rss><channel></channel></rss>"
        with mock.patch.object(p.http, "get", side_effect=fake_get):
            got = p.search_movie(MovieQuery(imdb="tt0063350", title="A Noite", original_title="Night of the Living Dead", year=1968))
        self.assertEqual([c["t"] for c in calls], ["movie", "search"])
        self.assertEqual(calls[0]["imdbid"], "0063350")
        self.assertEqual(calls[1]["q"], "Night of the Living Dead 1968")
        self.assertEqual({(i.provider, i.private) for i in got}, {("idx", True)})

    def test_series_search_by_tvdb_per_season(self):
        p = self.provider()
        calls = []
        with mock.patch.object(p.http, "get", side_effect=lambda url, params=None, **kw: calls.append(params) or TORZNAB):
            got = p.search_series(SeriesQuery(name="Dexter", tvdb="79349", seasons=[1, 2]))
        self.assertEqual([(c["t"], c.get("season")) for c in calls], [("tvsearch", 1), ("tvsearch", 2)])
        self.assertEqual(len(got), 2)                                   # the same items are not repeated

    def test_needs_url_and_key(self):
        with self.assertRaises(SystemExit):
            Torznab("idx", {"url": "http://x"}, {})


class TestUnit3D(unittest.TestCase):
    RAW = {"data": [{"id": 9, "attributes": {
        "name": "Dexter S01 1080p BluRay x265-V", "info_hash": "AB" * 20, "download_link": "https://t.example/torrent/download/9.KEY",
        "size": 30000000000, "seeders": 3, "freeleech": "100%", "created_at": "2026-10-19T10:00:00.000000Z",
        "tvdb_id": 79349, "imdb_id": 773262, "tmdb_id": 1405, "category": "TV",
        "files": [{"name": "Dexter.S01E01.mkv"}, {"name": "Dexter.S01E02.mkv"}]}}]}

    def provider(self):
        return Unit3D("u3d", {"url": "https://t.example", "series_categories": [2], "movie_categories": [1]}, {"api_token": "T"})

    def test_series_search(self):
        p = self.provider()
        with mock.patch.object(p.http, "get_json", return_value=self.RAW) as g:
            got = p.search_series(SeriesQuery(name="Dexter", tvdb="79349"))
        self.assertEqual(g.call_args[0][1]["tvdbId"], "79349")
        i = got[0]
        self.assertEqual((i.kind, i.freeleech, i.ids, i.files, i.private), ("series", 100,
                         {"imdb": "tt0773262", "tmdb": "1405", "tvdb": "79349"}, ["Dexter.S01E01.mkv", "Dexter.S01E02.mkv"], True))
        self.assertNotIn("T", str(p.http.headers.get("User-Agent")))

    def test_a_broken_page_is_read_item_by_item(self):
        p = self.provider()
        one = {"data": [{"id": 1, "attributes": self.RAW["data"][0]["attributes"]}]}

        def fake(url, params, **kw):
            if params["perPage"] == 25 and params["page"] == 1:
                raise ProviderError("HTTP 500")
            if params["perPage"] == 1:
                if params["page"] == 2:
                    raise ProviderError("HTTP 500")         # the torrent that breaks the listing
                return one if params["page"] <= 3 else {"data": []}
            return {"data": []}
        with mock.patch.object(p.http, "get_json", side_effect=fake):
            self.assertEqual(len(p.category(1)), 2)


class FakeQBit:
    def rss_items(self):
        art = lambda i, t: {"id": i, "title": t, "torrentURL": f"https://tl.example/rss/{i}/KEY", "date": "Mon, 19 Oct 2026 10:00:00 GMT",
                            "description": "Category: Episodes HD - Seeders: 12 - Leechers: 1"}
        return {"ondemand": {"My Tracker": {"articles": [art("1", "Phony S01E01 1080p WEB H264-B")]},
                             "My Tracker 24h": {"articles": [art("1", "Phony S01E01 1080p WEB H264-B"), art("2", "Other 2020 1080p")]},
                             "Unrelated": {"articles": [art("3", "Nope 2020 1080p")]}}}


class TestQBitRSS(unittest.TestCase):
    def test_reads_matching_feeds_without_repeats(self):
        p = QBitRSS("rss", {"folder": "ondemand", "feeds": ["My Tracker"]}, {}, {"qbit": FakeQBit()})
        got = p.feed()
        self.assertEqual([(i.id, i.seeds, i.category) for i in got], [("1", 12, "Episodes HD"), ("2", 12, "Episodes HD")])
        self.assertEqual(p.capabilities, {"feed"})


class TestArchiveOrg(unittest.TestCase):
    def test_items_are_web_seeded_sd_movies(self):
        p = ArchiveOrg("ia", {})
        docs = {"response": {"docs": [{"identifier": "night_of_the_living_dead", "title": "Night of the Living Dead",
                                       "year": "1968", "item_size": "4000"}, {"identifier": "x", "title": "No year"}]}}
        with mock.patch.object(p.http, "get_json", return_value=docs):
            got = p.feed()
        self.assertEqual(len(got), 1)
        i = got[0]
        self.assertEqual(i.torrent_url, "https://archive.org/download/night_of_the_living_dead/night_of_the_living_dead_archive.torrent")
        self.assertEqual((i.private, i.resolutions, i.kind), (False, ["SD", "720p", "1080p"], "movie"))
        from ondemand.release_name import parse_movie
        self.assertEqual(parse_movie(i.title)[0]["raw_name"], "Night of the Living Dead")


class Boom(Provider):
    type = "boom"

    def feed(self):
        raise ProviderError("HTTP 502")

    def search_series(self, q):
        raise RuntimeError("bug")


class FeedOnly(Provider):
    type = "feedonly"

    def feed(self):
        return [self.stamp(TorrentItem(title="Phony S01E01 1080p WEB H264-B", torrent_url="https://x/1", seeds=4, id="1")),
                self.stamp(TorrentItem(title="Night of the Living Dead 1968 1080p BluRay x264-G", torrent_url="https://x/2", id="2"))]


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_broken_provider_becomes_a_warning_and_feed_only_items_are_archived(self):
        reg = Registry([Boom("boom", {}), FeedOnly("rss", {"private": True})], self.db)
        items, warnings = reg.feed()
        self.assertEqual(len(items), 2)
        self.assertEqual(warnings, ["boom: ProviderError"])
        got, warnings = reg.search_series(SeriesQuery(name="Phony"))
        self.assertEqual([i.title for i in got], ["Phony S01E01 1080p WEB H264-B"])
        self.assertEqual(warnings, ["boom: RuntimeError"])
        got, _ = reg.search_movie(MovieQuery(title="A Noite", original_title="Night of the Living Dead", year=1968))
        self.assertEqual([(i.provider, i.private, i.kind) for i in got], [("rss", True, "movie")])
        self.assertEqual(reg.feed()[0][0].provider, "rss")
        self.assertEqual(self.db.one("SELECT count(*) n FROM archive")["n"], 2)        # seen again: not duplicated

    def test_seed_rules(self):
        reg = Registry([FeedOnly("rss", {"private": True, "seed": {"min_ratio": 2}})])
        self.assertEqual(reg.seed_rule("rss")["min_ratio"], 2)
        self.assertFalse(reg.seed_rule("manual")["private"])
        self.assertIsNone(reg.seed_rule("gone"))

    def test_build_from_config_with_a_plugin(self):
        plugins = os.path.join(self.tmp.name, "plugins")
        os.makedirs(plugins)
        with open(os.path.join(plugins, "my_provider.py"), "w") as f:
            f.write("from ondemand.providers import Provider\n"
                    "class Mine(Provider):\n    type = 'mine'\n    def feed(self):\n        return []\n")
        cfg = {"general": {"plugins_dir": plugins},
               "secrets": {"providers": {"ia": {"unused": "x"}}},
               "providers": [{"name": "ia", "type": "archive_org"},
                             {"name": "mine", "type": "python", "class": "my_provider:Mine"},
                             {"name": "off", "type": "archive_org", "enabled": False}]}
        reg = build(cfg, self.db)
        self.assertEqual([p.name for p in reg], ["ia", "mine"])
        self.assertEqual(reg.by_name["ia"].secrets, {"unused": "x"})
        with self.assertRaises(SystemExit):
            build({"providers": [{"name": "x", "type": "nope"}]})
        with self.assertRaises(SystemExit):
            build({"providers": [{"name": "x", "type": "archive_org"}, {"name": "x", "type": "archive_org"}]})


if __name__ == "__main__":
    unittest.main()
