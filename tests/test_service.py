import os
import tempfile
import time
import unittest
from unittest import mock

from ondemand import config
from ondemand.bencode import TorrentFile, TorrentMeta
from ondemand.db import Database
from ondemand.gateway import Metrics
from ondemand.service import Service, retry_subtitles
from ondemand.sources import Unavailable
from ondemand.subtitles.base import ProviderLimit


class Request:
    headers = {"User-Agent": "Lavf/61.7.103"}   # probe and play arrive alike


class FakeSource:
    def __init__(self, start):
        self.start = start

    def available_until(self, pos):
        return 1000 if self.start else 0


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        tid = self.db.save_title({"key": "imdb:tt1", "imdb": "tt1", "name": "X", "origin": "test"})
        self.rid = self.db.save_release(tid, {"label": "1080p", "torrent_url": "u"})
        self.s = Service(config.DEFAULTS, self.db, qbit=None, jellyfin=None, metrics=Metrics())
        self.started = []
        self.s._ensure = lambda rid: self.started.append(rid) or "source"
        self.g = config.DEFAULTS["gateway"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_request_starts_and_waits_a_little(self):
        r = Request()
        self.assertEqual(self.s.resolver(self.rid, r), "source")
        self.assertAlmostEqual(r.wait, self.g["first_byte_wait_s"], delta=1)
        self.assertEqual(self.started, [self.rid])

    def test_first_byte_wait_discounts_the_start_up(self):
        r = Request()
        r.started = time.monotonic() - 20          # starting the torrent took 20 s
        self.s.resolver(self.rid, r)
        self.assertAlmostEqual(r.wait, self.g["first_byte_wait_s"] - 20, delta=1)

    def test_head_does_not_start_a_torrent(self):
        r = Request()
        r.command = "HEAD"
        with self.assertRaises(Unavailable):
            self.s.resolver(self.rid, r)
        self.assertEqual(self.started, [])

    def test_replay_after_deletion_returns_to_the_lifecycle(self):
        h = "a" * 40
        self.db.execute("INSERT INTO torrents(infohash,release_id,state,removed_at,reason,rest_released) "
                        "VALUES(?,?,'removed',1,'abandoned',1)", h, self.rid)
        f = TorrentFile("F/f.mkv", 1 << 20, 0)
        info = {"hash": h, "meta": TorrentMeta(h, "F", 1 << 18, 4, [f], False), "file": f,
                "path": "/downloads/F/f.mkv", "alternatives": [], "foreign": False}

        class PieceQBit:
            def piece_states(self, h):
                return [0, 0, 0, 0]
        self.s.qbit = PieceQBit()
        with mock.patch("ondemand.service.torrent.start", return_value=info):
            source = self.s._start(self.rid)
        source.watcher.stop()
        row = self.db.one("SELECT state, removed_at, reason, rest_released FROM torrents WHERE infohash=?", h)
        self.assertEqual(tuple(row), ("trial", None, None, 0))

    def test_trusted_provider_skips_the_public_file_check(self):
        class Reg:
            def trusted(self, name):
                return name == "archive"
        self.db.execute("UPDATE releases SET provider='archive', private=0 WHERE id=?", self.rid)
        self.s.registry = Reg()
        with mock.patch("ondemand.service.torrent.start", side_effect=Unavailable("stop here")) as start:
            with self.assertRaises(Unavailable):
                self.s._start(self.rid)
        self.assertFalse(start.call_args.kwargs["public"])
        self.db.execute("UPDATE releases SET provider='other' WHERE id=?", self.rid)
        with mock.patch("ondemand.service.torrent.start", side_effect=Unavailable("stop here")) as start:
            with self.assertRaises(Unavailable):
                self.s._start(self.rid)
        self.assertTrue(start.call_args.kwargs["public"])

    def test_active_torrent_waits_for_the_piece(self):
        self.s.sources[self.rid] = FakeSource(start=True)
        r = Request()
        self.assertIs(self.s.resolver(self.rid, r), self.s.sources[self.rid])
        self.assertEqual(r.wait, self.g["play_wait_s"])
        self.assertEqual(self.started, [])

    def test_active_torrent_without_its_start_is_still_a_probe(self):
        self.s.sources[self.rid] = FakeSource(start=False)
        r = Request()
        self.s.resolver(self.rid, r)
        self.assertAlmostEqual(r.wait, self.g["first_byte_wait_s"], delta=1)

    def test_a_vanished_torrent_is_added_again(self):
        class GoneWatcher:
            gone, hash = True, "e" * 40
        s = FakeSource(start=True)
        s.watcher = GoneWatcher()
        self.s.sources[self.rid] = s
        self.assertEqual(self.s.resolver(self.rid, Request()), "source")   # went through _ensure
        self.assertEqual(self.started, [self.rid])
        self.assertNotIn(self.rid, self.s.sources)

    def test_a_failed_probe_blocks_the_blind_transcode(self):
        self.s.on_failure(self.rid)
        with self.assertRaises(Unavailable):
            self.s.resolver(self.rid, Request())
        self.assertEqual(self.started, [])

    def test_block_does_not_stretch_and_expires(self):
        self.s.cfg = {**config.DEFAULTS, "gateway": {**self.g, "block_s": 0.3}}
        self.s.on_failure(self.rid)
        first = self.s.failures[self.rid]
        time.sleep(0.1)
        self.s.on_failure(self.rid)                  # refused during the block
        self.assertEqual(self.s.failures[self.rid], first)
        time.sleep(0.3)
        self.assertEqual(self.s.resolver(self.rid, Request()), "source")   # expired: tries again


class TestRetrySubtitles(unittest.TestCase):
    def test_queued_retry_stops_at_the_provider_limit(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.db"))
            for rid in ("a", "b"):
                db.execute("INSERT INTO subtitles(release_id,state,attempts,updated_at) VALUES(?, 'not_found', 1, 0)", rid)

            class Sub:
                calls = []

                def fetch_now(self, rid):
                    self.calls.append(rid)
                    return "limit"
            s = Sub()
            retry_subtitles(config.DEFAULTS, db, s, pause_s=0)
            self.assertEqual(len(s.calls), 1)
            self.assertTrue(ProviderLimit)


if __name__ == "__main__":
    unittest.main()
