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

    def test_a_refused_new_torrent_is_deleted_from_qbittorrent(self):
        qb = FakeQBit()
        qb.deleted = []
        qb.delete = lambda hashes, with_files=True: qb.deleted.extend(hashes)
        bad = TorrentMeta("a" * 40, "F", 1 << 22, 600, [TorrentFile(VIDEO, 2_000_000_000, 0),
                                                          TorrentFile("F/setup.exe", 1_000, 2_000_000_000)], False)
        with mock.patch("ondemand.torrent.read_torrent", return_value=bad), self.assertRaises(Unavailable):
            torrent.start(qb, "rid1", "https://tracker.example/t", CFG, infohash="A" * 40, public=True)
        self.assertEqual(qb.deleted, ["a" * 40])
        qb.deleted = []
        qb.torrents["a" * 40] = qb._t("a" * 40, "ondemand,od-rid1")      # already ours before: never deleted here
        with mock.patch("ondemand.torrent.read_torrent", return_value=bad), self.assertRaises(Unavailable):
            torrent.start(qb, "rid1", "https://tracker.example/t", CFG, infohash="A" * 40, public=True)
        self.assertEqual(qb.deleted, [])

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


# ---------------------------------------------------------------- season pack in torrent.start
PACK = [("P/Show.S01E01.mkv", 1_000_000_000), ("P/Show.S01E02.mkv", 1_000_000_000), ("P/Show.S01E03.mkv", 1_000_000_000)]


def pack_meta():
    off, files = 0, []
    for name, size in PACK:
        files.append(TorrentFile(name, size, off))
        off += size
    return TorrentMeta("b" * 40, "P", 1 << 22, off // (1 << 22) + 1, files, False)


class PackQBit:
    def __init__(self, prios, progress, state="downloading"):
        self.t = {"hash": "b" * 40, "tags": "ondemand,od-pack", "state": state, "progress": 0.3, "seq_dl": True,
                  "f_l_piece_prio": True, "save_path": "/data/ondemand", "download_path": ""}
        self.prios, self.progress, self.calls = list(prios), list(progress), []

    def info(self, tag=None, hashes=None, **kw):
        return [self.t] if tag == "od-pack" else []

    def one(self, h):
        return self.t

    def export(self, h):
        return b"x"

    def files(self, h):
        return [{"name": n, "index": i, "priority": self.prios[i], "size": s, "progress": self.progress[i]}
                for i, (n, s) in enumerate(PACK)]

    def pause(self, hashes):
        self.calls.append(("pause",))
        self.t["state"] = "pausedDL"

    def set_file_priority(self, h, indexes, prio):
        if indexes:
            self.calls.append(("prio", tuple(sorted(indexes)), prio))

    def set_sequential(self, *a, **k):
        pass

    def set_first_last(self, *a, **k):
        pass

    def force_start(self, hashes, value=True):
        self.calls.append(("force",))


class TestPack(unittest.TestCase):
    CFG = {"save_path": "/data/ondemand", "path_map": PATH_MAP, "torrent_wait_s": 1, "min_video_mb": 1}

    def start(self, qb, episode, **kw):
        with mock.patch("ondemand.torrent.read_torrent", return_value=pack_meta()):
            return torrent.start(qb, "rid2", "u", self.CFG, episode=episode, tag="od-pack", **kw)

    def test_requested_episode_jumps_the_pack_queue(self):
        qb = PackQBit(prios=[1, 1, 1], progress=[0.5, 0, 0])          # E01 downloading, rest released
        info = self.start(qb, (1, 3))
        self.assertIn(("pause",), qb.calls)
        self.assertIn(("prio", (2,), 1), qb.calls)
        self.assertIn(("prio", (0, 1), 0), qb.calls)                   # E01 and E02 step aside
        self.assertTrue(info["switched_target"])
        self.assertEqual(qb.calls[-1], ("force",))

    def test_does_not_zero_a_file_someone_else_is_reading(self):
        qb = PackQBit(prios=[1, 1, 0], progress=[0.5, 0, 0])
        self.start(qb, (1, 3), keep={"P/Show.S01E01.mkv"})
        self.assertIn(("prio", (1,), 0), qb.calls)
        self.assertNotIn(("prio", (0, 1), 0), qb.calls)

    def test_episode_already_the_target_or_complete_changes_nothing(self):
        qb = PackQBit(prios=[1, 0, 0], progress=[0.5, 0, 0])
        info = self.start(qb, (1, 1))                                   # already the target, nobody else active
        self.assertEqual(qb.calls, [("force",)])
        self.assertFalse(info["switched_target"])
        qb = PackQBit(prios=[1, 1, 1], progress=[0.5, 0, 1])
        self.assertFalse(self.start(qb, (1, 3))["switched_target"])     # E03 already downloaded


class MagnetQBit(PackQBit):
    """Magnet: the add does NOT pause (paused=false + stopCondition) and export answers 409 until the metadata arrives."""

    def __init__(self, export_failures=2):
        super().__init__(prios=[1, 1, 1], progress=[0, 0, 0], state="metaDL")
        self.t["tags"], self.failures, self.added = "", export_failures, None

    def add(self, url=None, **op):
        self.added = (url, op)
        self.t["tags"] = op["tags"]

    def export(self, h):
        from ondemand.qbit import QBitError
        if self.failures > 0:
            self.failures -= 1
            raise QBitError("GET torrents/export: HTTP 409", 409)
        self.t["state"] = "pausedDL"                       # stopCondition stopped the torrent
        return b"x"

    def info(self, tag=None, hashes=None, **kw):
        return [self.t] if self.t["tags"] and tag and tag in self.t["tags"].split(",") else []

    def one(self, h):
        return self.t if self.added else None                     # only exists after the add


class TestMagnet(unittest.TestCase):
    CFG = {"save_path": "/data/ondemand", "path_map": PATH_MAP, "torrent_wait_s": 2, "metadata_wait_s": 5, "min_video_mb": 1}

    def test_add_unpaused_with_stop_condition_and_waits_for_export(self):
        qb = MagnetQBit(export_failures=2)
        with mock.patch("ondemand.torrent.read_torrent", return_value=pack_meta()):
            info = torrent.start(qb, "rid2", "magnet:?xt=urn:btih:" + "b" * 40, self.CFG, episode=(1, 2), tag="od-pack",
                                 infohash="b" * 40, public=True, min_video_mb=100)
        url, op = qb.added
        self.assertTrue(url.startswith("magnet:"))
        self.assertIs(op["paused"], False)
        self.assertEqual(op["stopCondition"], "MetadataReceived")
        self.assertEqual(qb.failures, 0)                                   # retried on 409
        self.assertIn(("prio", (1,), 1), qb.calls)                       # only the requested episode downloads
        self.assertIn(("prio", (0, 2), 0), qb.calls)
        self.assertEqual(info["file"].path, "P/Show.S01E02.mkv")

    def test_metadata_that_never_arrives_becomes_unavailable(self):
        qb = MagnetQBit(export_failures=10 ** 6)
        cfg = {**self.CFG, "metadata_wait_s": 1}
        with mock.patch("ondemand.torrent.read_torrent", return_value=pack_meta()), self.assertRaises(Unavailable):
            torrent.start(qb, "rid2", "magnet:?xt=urn:btih:" + "b" * 40, cfg, episode=(1, 2), tag="od-pack")

    def test_torrent_file_does_not_wait_for_metadata_nor_change_the_add(self):
        qb = MagnetQBit(export_failures=0)
        qb.t["state"] = "pausedDL"
        with mock.patch("ondemand.torrent.read_torrent", return_value=pack_meta()):
            torrent.start(qb, "rid2", "https://t.example/x.torrent", self.CFG, episode=(1, 2), tag="od-pack")
        self.assertIs(qb.added[1]["paused"], True)
        self.assertNotIn("stopCondition", qb.added[1])


if __name__ == "__main__":
    unittest.main()
