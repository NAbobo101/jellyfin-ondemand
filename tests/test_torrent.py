import re
import unittest
from unittest import mock

from ondemand import torrent
from ondemand.bencode import TorrentFile, TorrentMeta
from ondemand.sources import Unavailable
from ondemand.torrent import check_safety, choose_video, to_local

PATH_MAP = {"/data/ondemand": "/downloads", "/data/temp/": "/incomplete/", "/data": "/data"}


def make_meta(*files):
    off, items = 0, []
    for path, size in files:
        items.append(TorrentFile(path, size, off))
        off += size
    return TorrentMeta("0" * 40, "x", 1 << 20, off // (1 << 20) + 1, items, True)


class TestChoice(unittest.TestCase):
    def test_largest_video_ignores_sample_and_subtitle(self):
        m = make_meta(("F/sample.mkv", 900_000_000), ("F/movie.mkv", 4_000_000_000),
                      ("F/movie.por.srt", 80_000), ("F/extras.mp4", 1_000_000))
        self.assertEqual(choose_video(m).path, "F/movie.mkv")

    def test_episode_in_pack(self):
        m = make_meta(("S/Show.S01E01.1080p.mkv", 2_000_000_000), ("S/Show.S01E10.1080p.mkv", 2_100_000_000),
                      ("S/Show.S01E02.1080p.mkv", 1_900_000_000))
        self.assertEqual(choose_video(m, (1, 1)).path, "S/Show.S01E01.1080p.mkv")   # not E10
        self.assertEqual(choose_video(m, (1, 10)).path, "S/Show.S01E10.1080p.mkv")
        with self.assertRaises(Unavailable):
            choose_video(m, (1, 5))

    def test_1x02_format(self):
        m = make_meta(("S/show.1x02.hdtv.mkv", 500_000_000))
        self.assertEqual(choose_video(m, (1, 2)).path, "S/show.1x02.hdtv.mkv")


class TestSafety(unittest.TestCase):
    def test_refuses_executable_and_fake(self):
        with self.assertRaises(Unavailable):
            check_safety(make_meta(("F/movie.mkv", 4_000_000_000), ("F/codec.exe", 2_000_000)), 300)
        with self.assertRaises(Unavailable):
            check_safety(make_meta(("F/movie.mkv", 20_000_000)), 300)
        check_safety(make_meta(("F/movie.mkv", 4_000_000_000), ("F/movie.srt", 50_000)), 300)


class TestPaths(unittest.TestCase):
    def test_qbit_to_local(self):
        self.assertEqual(to_local("/data/ondemand/X/x.mkv", PATH_MAP), "/downloads/X/x.mkv")
        self.assertEqual(to_local("/data/temp/a.mkv", PATH_MAP), "/incomplete/a.mkv")
        self.assertEqual(to_local("/data/movies/b.mkv", PATH_MAP), "/data/movies/b.mkv")   # longest prefix wins
        with self.assertRaises(Unavailable):
            to_local("/elsewhere/c.mkv", PATH_MAP)
        with self.assertRaises(Unavailable):
            to_local("/database/c.mkv", PATH_MAP)          # a prefix only matches whole path components


CFG = {"save_path": "/data/ondemand", "path_map": PATH_MAP, "torrent_wait_s": 1, "min_video_mb": 1,
       "subtitle_pattern": re.compile(r"pt[-_. ]?br", re.I)}
VIDEO, SUB, EXTRA = "F/Movie.mkv", "F/Movie.pt-BR.srt", "F/extras.mkv"


def meta():
    files = [TorrentFile(VIDEO, 2_000_000_000, 0), TorrentFile(SUB, 80_000, 2_000_000_000),
             TorrentFile(EXTRA, 300_000_000, 2_000_080_000)]
    return TorrentMeta("a" * 40, "F", 1 << 22, 600, files, False)


class FakeQBit:
    """Records every call; deliberately without `resume` (it would turn force start off)."""

    def __init__(self, torrents=None, accepts_add=True):
        self.torrents, self.accepts_add, self.calls = dict(torrents or {}), accepts_add, []
        self.prios = {VIDEO: 1, SUB: 1, EXTRA: 1}

    def _t(self, h, tags, state="pausedDL", progress=0.0):
        return {"hash": h, "tags": tags, "state": state, "progress": progress, "seq_dl": True,
                "f_l_piece_prio": True, "save_path": "/data/ondemand", "download_path": ""}

    def info(self, tag=None, hashes=None, **kw):
        return [t for t in self.torrents.values()
                if (tag and tag in t["tags"].split(",")) or (hashes and t["hash"] == hashes)]

    def one(self, h):
        return self.torrents.get(h)

    def add(self, url=None, **op):
        self.calls.append(("add", op.get("paused"), op.get("sequentialDownload"), op.get("firstLastPiecePrio")))
        if self.accepts_add:
            self.torrents["a" * 40] = self._t("a" * 40, op["tags"])

    def export(self, h):
        return b"x"

    def files(self, h):
        names = [VIDEO, SUB, EXTRA]
        sizes = [2_000_000_000, 80_000, 300_000_000]
        return [{"name": n, "index": i, "priority": self.prios[n], "size": s} for i, (n, s) in enumerate(zip(names, sizes))]

    def pause(self, hashes):
        self.calls.append(("pause",))
        for h in hashes:
            self.torrents[h]["state"] = "pausedDL"

    def set_file_priority(self, h, indexes, prio):
        if indexes:
            state = self.torrents[h]["state"]
            self.calls.append(("prio", tuple(indexes), prio, state))

    def set_sequential(self, h, on, t=None):
        self.calls.append(("sequential", t is not None))

    def set_first_last(self, h, on, t=None):
        self.calls.append(("first_last", t is not None))

    def force_start(self, hashes, value=True):
        self.calls.append(("force",))


class TestStart(unittest.TestCase):
    def start(self, qb, **kw):
        with mock.patch("ondemand.torrent.read_torrent", return_value=meta()):
            return torrent.start(qb, "rid1", "https://tracker.example/t", CFG, infohash="A" * 40, **kw)

    def prios(self, qb):
        return [c for c in qb.calls if c[0] == "prio"]

    def test_new_paused_video_at_1_subtitle_at_7_rest_at_0(self):
        qb = FakeQBit()
        info = self.start(qb)
        self.assertEqual(qb.calls[0], ("add", True, True, True))
        self.assertIn(("prio", (0,), 1, "pausedDL"), qb.calls)          # video: 1, never 7
        self.assertIn(("prio", (1,), 7, "pausedDL"), qb.calls)          # the torrent's subtitle in the wanted language
        self.assertIn(("prio", (2,), 0, "pausedDL"), qb.calls)          # extras out
        self.assertTrue(all(c[3] == "pausedDL" for c in self.prios(qb)))   # priorities only while paused
        self.assertNotIn(("prio", (0,), 7, "pausedDL"), qb.calls)
        self.assertEqual(qb.calls[-1], ("force",))                     # and nothing after (no resume)
        self.assertFalse(any(c[0] in ("sequential", "first_last") for c in qb.calls))   # the add turns them on
        self.assertEqual((info["foreign"], info["path"]), (False, "/downloads/F/Movie.mkv"))

    def test_reentry_neither_pauses_nor_redoes_priorities(self):
        qb = FakeQBit()
        qb.torrents["a" * 40] = qb._t("a" * 40, "ondemand,od-rid1", state="downloading", progress=0.4)
        qb.prios[EXTRA] = 1                                                 # private: the lifecycle already released it
        self.start(qb)
        self.assertNotIn(("pause",), qb.calls)
        self.assertEqual(self.prios(qb), [])
        self.assertEqual(qb.calls, [("sequential", True), ("first_last", True), ("force",)])

    def test_other_version_of_the_same_torrent_raises_only_its_own_video(self):
        qb = FakeQBit()
        qb.torrents["a" * 40] = qb._t("a" * 40, "ondemand,od-other", state="downloading", progress=0.4)
        qb.prios[VIDEO] = 0                                                 # the other release uses the extras file
        info = self.start(qb)
        self.assertFalse(info["foreign"])                                   # has the ondemand tag: it is ours
        self.assertIn(("pause",), qb.calls)
        self.assertIn(("prio", (0,), 1, "pausedDL"), qb.calls)
        self.assertFalse(any(c[2] == 0 for c in self.prios(qb)))           # does not zero the other's video

    def test_users_own_torrent_is_reused_untouched(self):
        qb = FakeQBit()
        qb.torrents["a" * 40] = qb._t("a" * 40, "movies", state="uploading", progress=1.0)
        info = self.start(qb)
        self.assertTrue(info["foreign"])
        self.assertEqual(qb.calls, [])

    def test_without_add_nothing_is_fetched_from_here(self):
        qb = FakeQBit(accepts_add=False)
        with mock.patch("urllib.request.urlopen") as net, self.assertRaises(Unavailable):
            self.start(qb)
        net.assert_not_called()

    def test_path_uses_the_incomplete_folder_and_the_save_path(self):
        qb = FakeQBit()
        qb.torrents["a" * 40] = qb._t("a" * 40, "ondemand,od-rid1", state="downloading", progress=0.4)
        qb.torrents["a" * 40]["download_path"] = "/data/temp"
        info = self.start(qb)
        self.assertEqual(info["path"], "/incomplete/F/Movie.mkv")
        self.assertEqual(info["alternatives"], ["/downloads/F/Movie.mkv"])


if __name__ == "__main__":
    unittest.main()
