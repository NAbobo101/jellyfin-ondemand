"""The archive's states (archive.py) and the fallback (fallback.py): nothing a provider showed is lost,
quarantine comes back on its own and a dead torrent gives way to the next candidate."""

import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ondemand import archive, languages, series_ondemand, service
from ondemand.catalog import Catalog
from ondemand.db import Database
from ondemand.fallback import Fallback
from ondemand.providers import TorrentItem, magnet
from ondemand.series_filter import Resolver, build_catalog, build_for_series
from ondemand.sources import BadTorrent, Unavailable
from ondemand.subtitles.formats import placeholder_sami, subtitle_path
from tests.fakes import NOW, FakeTV
from tests.test_series_filter import CFG, TODAY

SERIES = {"key": "tvdb:79349", "tvdb": "79349", "imdb": "tt0773262", "tmdb": "1405", "name": "Dexter", "year": 2006,
          "country": "US", "tvmaze": 10}
CFG_S = {**CFG, "public_seeds_min": 2, "reevaluate_names": 60, "min_video_mb": 100}
DAY = 86400
LANG = languages.get("en")


def feed(title, seeds=5, ident=None, provider="feed-tracker"):
    return TorrentItem(title=title, torrent_url=f"https://tracker.example/rss/download/{abs(hash(title))}/KEY/x.torrent",
                       seeds=seeds, id=ident or title, provider=provider, private=True, published=NOW)


def with_ids(title, files, seeds=1):
    return TorrentItem(title=title, torrent_url="https://ids.example/torrent/download/7.KEY", seeds=seeds, id=title,
                       provider="id-tracker", private=True, freeleech=100, size=30_000_000_000, files=files,
                       ids={"tvdb": "79349", "imdb": "tt0773262", "tmdb": "1405"}, published=NOW)


def public(title, seeds=50):
    h = (format(abs(hash(title)), "x") * 3)[:40].ljust(40, "0")
    return TorrentItem(title=title, infohash=h, seeds=seeds, id=h, provider="public-index", private=False,
                       size=2_000_000_000, published=NOW)


def cands(items):
    return [i.candidate() for i in items]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, title):
        return self.db.one("SELECT * FROM archive WHERE title=?", title)

    def catalog(self, lang=None):
        root = os.path.join(self.tmp.name, "catalog")
        os.makedirs(root, exist_ok=True)
        return Catalog(self.db, mock.Mock(), root, "http://gw", series="Series", lang=lang)


class TestRecord(Base):
    def test_every_source_with_ids_and_files_fresh_seeds_and_search_by_name_or_key(self):
        items = [feed("Dexter S01E01 1080p BluRay x264-RSS"), public("Dexter S01E02 1080p WEB x264-PUB"),
                 with_ids("Dexter S01 1080p BluRay x264-IDS", ["Dexter.S01E01.mkv"])]
        self.assertEqual(archive.record(self.db, items), 3)
        items[0].seeds = 99
        self.assertEqual(archive.record(self.db, items), 0)
        self.assertEqual(self.row("Dexter S01E01 1080p BluRay x264-RSS")["seeds"], 99)
        found = {i.provider: i for i in archive.find_series(self.db, "Dexter")}
        self.assertEqual(sorted(found), ["feed-tracker", "id-tracker", "public-index"])
        self.assertEqual(found["id-tracker"].files, ["Dexter.S01E01.mkv"])
        self.assertFalse(found["public-index"].private)
        c = found["feed-tracker"].candidate()
        tag = series_ondemand.episode_release({**c, "resolution": "1080p", "source": "BluRay", "codec": "x264"}, 1, 1)["torrent_tag"]
        self.assertEqual(self.row("Dexter S01E01 1080p BluRay x264-RSS")["tag"], tag)
        self.db.execute("UPDATE archive SET norm_name='renamed', series_key='tvdb:79349' WHERE provider='public-index'")
        self.assertEqual([i.provider for i in archive.find_series(self.db, "Dexter", ["tvdb:79349"])
                          if i.provider == "public-index"], ["public-index"])

    def test_retention_spares_a_catalog_series(self):
        archive.record(self.db, [feed("Dexter S01E01 1080p BluRay x264-A"), feed("Something Else S01E01 1080p WEB x264-B")])
        self.db.execute("UPDATE archive SET seen_at=1")
        self.db.execute("UPDATE archive SET series_key='tvdb:79349' WHERE norm_name='dexter'")
        self.db.save_title({"key": "tvdb:79349", "kind": "series", "imdb": None, "name": "Dexter", "origin": "series"})
        archive.record(self.db, [])
        self.assertEqual([r["norm_name"] for r in self.db.all("SELECT norm_name FROM archive")], ["dexter"])


class TestStates(Base):
    def test_winner_fallback_quarantine_and_discarded(self):
        items = [feed("Dexter S01E01 1080p BluRay x264-AAA", seeds=40), feed("Dexter S01E01 1080p WEBRip x264-BBB", seeds=90),
                 feed("Dexter S09E01 1080p WEB x264-NEW"),                      # a season TVmaze does not list
                 feed("Dexter S01E02 1080p BluRay x264-ZERO", seeds=0),
                 feed("A Series That Does Not Exist S01E01 1080p WEB x264-X")]
        archive.record(self.db, items)
        archive.record_result(self.db, build_catalog(cands(items), Resolver(FakeTV(), today=TODAY), CFG_S))
        st = lambda t: (self.row(t)["state"], self.row(t)["series_key"])
        self.assertEqual(st("Dexter S01E01 1080p BluRay x264-AAA"), ("winner", "tvdb:79349"))
        self.assertEqual(st("Dexter S01E01 1080p WEBRip x264-BBB"), ("fallback", "tvdb:79349"))
        self.assertEqual(st("Dexter S09E01 1080p WEB x264-NEW")[0], "quarantine")
        self.assertEqual(st("Dexter S01E02 1080p BluRay x264-ZERO")[0], "discarded")
        self.assertEqual(st("A Series That Does Not Exist S01E01 1080p WEB x264-X")[0], "quarantine")

    def test_dead_stays_dead(self):
        it = feed("Dexter S01E01 1080p BluRay x264-AAA")
        archive.record(self.db, [it])
        self.db.execute("UPDATE archive SET state='dead'")
        archive.record_result(self.db, build_catalog(cands([it]), Resolver(FakeTV(), today=TODAY), CFG_S))
        self.assertEqual(self.row(it.title)["state"], "dead")
        self.assertEqual(archive.find_series(self.db, "Dexter"), [])


class TestPending(Base):
    def test_backoff_limit_priority_and_the_whole_season_together(self):
        now = time.time()
        archive.record(self.db, [feed("Dexter S09E01 1080p WEB x264-A"), feed("Dexter S01E01 1080p WEB x264-B"),
                                 feed("Dexter S01E02 1080p WEB x264-C"), feed("Phony S01E01 1080p WEB x264-D")], now)
        self.db.execute("UPDATE archive SET state='winner' WHERE title LIKE '%-B'")
        self.db.execute("UPDATE archive SET state='discarded' WHERE title LIKE '%-C'")
        one = archive.pending(self.db, 1, now, priority={"dexter"})
        self.assertEqual(sorted(c["title"] for c in one),                  # the winner comes along; discarded does not
                         ["Dexter S01E01 1080p WEB x264-B", "Dexter S09E01 1080p WEB x264-A"])
        self.assertEqual(self.row("Dexter S09E01 1080p WEB x264-A")["retry_at"], now + DAY)
        self.assertEqual([c["title"] for c in archive.pending(self.db, None, now)], ["Phony S01E01 1080p WEB x264-D"])
        self.assertEqual(archive.pending(self.db, None, now + DAY / 2), [])
        archive.pending(self.db, None, now + DAY)                                  # 2nd attempt: in 2 days
        self.assertEqual(self.row("Dexter S09E01 1080p WEB x264-A")["retry_at"], now + 3 * DAY)

    def test_movies_are_not_pending(self):
        archive.record(self.db, [feed("Night of the Living Dead 1968 1080p BluRay x264-G")])
        self.assertEqual(archive.pending(self.db), [])


class TestQuarantineComesBack(Base):
    """A season announced before TVmaze lists it leaves the feed and still gets in once TVmaze learns it."""

    def run_import(self, tv, live):
        archive.record(self.db, live)
        app = SimpleNamespace(cfg={"series": CFG_S}, db=self.db, jf=mock.Mock(), cat=self.catalog(), tv=tv)
        app.jf.local_episodes.return_value = {}
        return service.import_series(app, cands(live))

    def test_a_new_season_gets_in_after_tvmaze_learns_it(self):
        tv = FakeTV()
        new = feed("Dexter S03E01 1080p WEB x264-S3")
        self.assertEqual(self.run_import(tv, [feed("Dexter S01E01 1080p WEB x264-S1"), new]), 1)
        self.assertEqual(self.row(new.title)["state"], "quarantine")
        tv.eps[10] = tv.eps[10] + [(3, 1, "2008-09-28")]
        self.run_import(tv, [])                                       # left the feed; not its turn yet
        self.assertEqual(self.row(new.title)["state"], "quarantine")
        self.db.execute("UPDATE archive SET retry_at=0")
        self.run_import(tv, [])
        self.assertEqual(self.row(new.title)["state"], "winner")
        self.assertIsNotNone(self.db.one("SELECT 1 FROM releases WHERE label='S03E01'"))


class TestMigration(Base):
    def test_releases_of_episodes_go_in_as_winners(self):
        db = self.db
        archive.record(db, [feed("Dexter S02E01 1080p WEB x264-A", ident="55")])
        db.execute("UPDATE archive SET tag=NULL")
        tid = db.save_title({"key": "tvdb:79349", "kind": "series", "tvdb": "79349", "imdb": "tt0773262", "name": "Dexter",
                             "origin": "series"})
        h = "d" * 40
        db.save_release(tid, {"label": "S01E01", "season": 1, "episode": 1, "name": "Dexter S01 1080p WEB x264-PUB",
                              "provider": "public-index", "torrent_url": magnet(h), "infohash": h,
                              "torrent_tag": archive.tag_of("public-index", h)})
        db.save_release(tid, {"label": "S01E03", "season": 1, "episode": 3, "name": "Dexter S01E03 1080p WEB x264-Z",
                              "provider": "x", "torrent_url": "https://x/9", "torrent_tag": "nomatch00"})
        db.execute("DELETE FROM kv WHERE key='archive_states_migrated'")
        archive.migrate(db)
        archive.migrate(db)                                                # idempotent
        rows = {r["id"]: r for r in db.all("SELECT * FROM archive")}
        self.assertEqual(sorted(rows), ["55", h])                          # a tag that does not match stays out
        self.assertEqual(rows["55"]["tag"], archive.tag_of("feed-tracker", "55"))
        self.assertEqual((rows[h]["state"], rows[h]["series_key"]), ("winner", "tvdb:79349"))
        self.assertIn('"tvdb": "79349"', rows[h]["item"])


class TestFailure(Base):
    def setUp(self):
        super().setUp()
        archive.record(self.db, [feed("Dexter S01E01 1080p WEB x264-A", ident="1")])
        self.tag = archive.tag_of("feed-tracker", "1")

    def test_severe_kills_at_once(self):
        self.assertEqual(archive.failure(self.db, self.tag, "fake", True), "dead")
        self.assertIn("fake", self.row("Dexter S01E01 1080p WEB x264-A")["reason"])

    def test_transient_only_on_the_second_one_apart(self):
        t = 1_000_000.0
        self.assertEqual(archive.failure(self.db, self.tag, "magnet", False, t), "new")
        self.assertEqual(archive.failure(self.db, self.tag, "magnet", False, t + 600), "new")       # the same play
        self.assertEqual(archive.failure(self.db, self.tag, "magnet", False, t + 1800), "dead")

    def test_an_old_failure_does_not_count(self):
        t = 1_000_000.0
        archive.failure(self.db, self.tag, "magnet", False, t)
        self.assertEqual(archive.failure(self.db, self.tag, "magnet", False, t + 8 * DAY), "new")

    def test_unknown_torrent(self):
        self.assertIsNone(archive.failure(self.db, "nothing", "x", True))


class FakeQBit:
    def __init__(self):
        self.deleted = []

    def info(self, tag=None, **kw):
        return [{"hash": "e" * 40}] if tag else []

    def delete(self, hashes, with_files=True):
        self.deleted += hashes


class TestFallback(Base):
    def setUp(self):
        super().setUp()
        self.cat = self.catalog(LANG)
        self.tv = FakeTV()
        # the id-tracker pack wins; a single episode from the feed and a public pack stay as fallbacks
        self.items = [with_ids("Dexter S01 1080p BluRay x264-IDS", [f"Dexter.S01E{n:02d}.mkv" for n in (1, 2)], seeds=30),
                      feed("Dexter S01E01 1080p BluRay x264-FEED", seeds=10, ident="70"),
                      public("Dexter S01 1080p BluRay x264-PUB", seeds=5)]
        archive.record(self.db, self.items)
        r = build_for_series(cands(self.items), SERIES, Resolver(self.tv, today=TODAY), CFG_S)
        archive.record_result(self.db, r)
        series_ondemand.register(self.cat, r, SERIES)
        self.e1 = self.db.one("SELECT * FROM releases WHERE label='S01E01'")
        self.e2 = self.db.one("SELECT * FROM releases WHERE label='S01E02'")
        self.assertEqual((self.e1["provider"], self.e2["provider"]), ("id-tracker", "id-tracker"))
        self.strm = os.path.join(self.cat.root, "Dexter S01E01.strm")
        self.sub = subtitle_path(self.strm, LANG.iso)
        with open(self.sub, "w") as f:
            f.write("real subtitle")
        self.db.execute("UPDATE releases SET strm=? WHERE id=?", self.strm, self.e1["id"])
        self.db.execute("INSERT INTO subtitles(release_id,state,attempts) VALUES(?, 'real', 3)", self.e1["id"])
        self.db.execute("INSERT INTO torrents(infohash,release_id,state) VALUES(?,?,'trial')", "e" * 40, self.e1["id"])
        self.qb = FakeQBit()
        self.metrics = mock.Mock()
        self.fb = Fallback({"series": CFG_S}, self.db, self.qb, self.tv, self.cat, self.metrics)

    def test_a_refused_torrent_goes_to_the_fallback_keeping_id_and_strm(self):
        self.assertEqual(self.fb.failed(self.e1["id"], "torrent refused", True, background=False), "dead")
        e1 = self.db.one("SELECT * FROM releases WHERE id=?", self.e1["id"])
        e2 = self.db.one("SELECT * FROM releases WHERE id=?", self.e2["id"])
        self.assertEqual((e1["provider"], e1["strm"]), ("feed-tracker", self.strm))    # private single: more seeds than the public pack
        self.assertEqual(e2["provider"], "public-index")                                # E02 only had the public pack
        self.assertEqual(self.qb.deleted, ["e" * 40])
        self.assertEqual(self.db.one("SELECT state FROM torrents")["state"], "removed")
        sub = self.db.one("SELECT state, attempts FROM subtitles WHERE release_id=?", self.e1["id"])
        self.assertEqual((sub["state"], sub["attempts"]), ("placeholder", 0))
        with open(self.sub) as f:
            self.assertEqual(f.read(), placeholder_sami(LANG))
        self.metrics.inc.assert_called_with("fallbacks_used_total", 2)
        self.assertEqual(self.row("Dexter S01 1080p BluRay x264-IDS")["state"], "dead")

    def test_without_a_fallback_it_stays_as_it_was(self):
        self.db.execute("UPDATE archive SET state='dead' WHERE provider<>'id-tracker'")
        self.fb.failed(self.e1["id"], "fake", True, background=False)
        self.assertEqual(self.db.one("SELECT provider FROM releases WHERE id=?", self.e1["id"])["provider"], "id-tracker")

    def test_a_torrent_being_read_is_not_switched(self):
        self.fb.service = mock.Mock()
        self.fb.service.sources = {self.e2["id"]: mock.Mock(readers=1)}
        self.fb.failed(self.e1["id"], "fake", True, background=False)
        self.assertEqual(self.db.one("SELECT provider FROM releases WHERE id=?", self.e1["id"])["provider"], "id-tracker")
        self.assertEqual(self.qb.deleted, [])

    def test_a_single_transient_failure_does_not_switch(self):
        self.assertEqual(self.fb.failed(self.e1["id"], "magnet", False, background=False), "winner")
        self.assertEqual(self.db.one("SELECT provider FROM releases WHERE id=?", self.e1["id"])["provider"], "id-tracker")


class TestServiceCounts(Base):
    def test_a_bad_torrent_on_play_goes_to_the_archive_and_qbittorrent_down_does_not(self):
        cfg = {"qbittorrent": {}, "series": {"min_video_mb": 100}, "gateway": {"block_s": 120}}
        tid = self.db.save_title({"key": "tvdb:1", "kind": "series", "imdb": None, "name": "X", "origin": "series"})
        rid = self.db.save_release(tid, {"label": "S01E01", "season": 1, "episode": 1, "provider": "feed-tracker",
                                         "torrent_url": "u", "torrent_tag": "t1"})
        s = service.Service(cfg, self.db, mock.Mock(), mock.Mock(), mock.Mock())
        s.fallback = mock.Mock()
        with mock.patch("ondemand.service.torrent.start", side_effect=BadTorrent("fake", severe=True)), \
                self.assertRaises(Unavailable):
            s._start(rid)
        s.fallback.failed.assert_called_once_with(rid, "fake", True)
        s.fallback.reset_mock()
        with mock.patch("ondemand.service.torrent.start", side_effect=OSError("qBittorrent down")), self.assertRaises(Unavailable):
            s._start(rid)
        s.fallback.failed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
