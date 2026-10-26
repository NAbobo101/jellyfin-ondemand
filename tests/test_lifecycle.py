import os
import tempfile
import time
import unittest

from ondemand import config
from ondemand.db import Database
from ondemand.jellyfin import strm_item_id
from ondemand.lifecycle import Lifecycle

DAY = 86400
RULES = {
    "public": {"private": False, "min_seed_hours": 0, "min_ratio": 0},
    "manual": {"private": False, "min_seed_hours": 0, "min_ratio": 0},
    "ratio-tracker": {"private": True, "min_seed_hours": 0, "min_ratio": 1.0},
    # "72 h OR ratio 1.0" (75 h leaves room for the announce)
    "either-tracker": {"private": True, "min_seed_hours": 75, "min_ratio": 1.0, "either": True},
}


class FakeQBit:
    def __init__(self):
        self.torrents, self.others, self.deleted, self.forced, self.limits, self.prios = {}, [], [], [], [], []
        self.files_of = {}

    def info(self, category=None, filter=None, **kw):
        if filter == "downloading":
            return self.others
        return [] if getattr(self, "empty_list", False) else list(self.torrents.values())

    def one(self, h):
        return self.torrents.get(h)

    def delete(self, hashes, with_files=True):
        self.deleted += hashes
        for h in hashes:
            self.torrents.pop(h, None)

    def force_start(self, hashes, value=True):
        self.forced += hashes

    def files(self, h):
        return self.files_of.get(h, [])

    def set_file_priority(self, h, indexes, prio):
        self.prios.append((h, sorted(indexes), prio))

    def limit_download(self, hashes, limit):
        if getattr(self, "fail_limit", None) in hashes:
            raise RuntimeError("qBittorrent down")
        self.limits.append((tuple(hashes), limit))


class FakeJellyfin:
    # deliberately no _req: who is playing comes from the .strm's computed id
    def __init__(self):
        self.sessions = []
        self.saved = {}              # item_id -> ticks ("continue watching")
        self.down = False
        self.queries = 0

    def playing(self):
        return self.sessions

    def positions(self, ids):
        self.queries += 1
        if self.down:
            raise RuntimeError("Jellyfin down")
        return {i: self.saved[i] for i in ids if i in self.saved}


class FakeService:
    def __init__(self):
        self.sources, self.forgotten = {}, []

    def forget(self, rid):
        self.forgotten.append(rid)


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        self.qb, self.jf, self.sv = FakeQBit(), FakeJellyfin(), FakeService()
        self.lc = Lifecycle(config.DEFAULTS, self.db, self.qb, self.jf, self.sv, rules=RULES.get)
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def torrent(self, provider="public", state="trial", progress=0.3, ratio=0.0, seeding=0, last_play=None,
                completed_at=None, age=0, foreign=False, private=None):
        self.n += 1
        tid = self.db.save_title({"key": f"imdb:tt{self.n}", "imdb": f"tt{self.n}", "name": f"F{self.n}", "origin": provider})
        rid = self.db.save_release(tid, {"label": "1080p", "torrent_url": "u", "provider": provider,
                                         "private": provider.endswith("tracker") if private is None else private})
        strm = f"/catalog/Movies/F{self.n}.strm"
        self.db.execute("UPDATE releases SET strm=? WHERE id=?", strm, rid)
        self.db.execute("UPDATE titles SET last_play=? WHERE id=?", last_play, tid)
        h = f"{self.n:040d}"
        now = time.time()
        self.db.execute("INSERT INTO torrents(infohash,release_id,state,foreign_,file,started_at,last_access,completed_at) "
                        "VALUES(?,?,?,?,?,?,?,?)", h, rid, "foreign" if foreign else state, int(foreign), "F/f.mkv",
                        now - age, now - age, completed_at)
        self.qb.torrents[h] = {"hash": h, "progress": progress, "ratio": ratio, "seeding_time": seeding,
                               "force_start": True, "category": "ondemand"}
        return h, rid, strm

    def state(self, h):
        return self.db.one("SELECT state, reason FROM torrents WHERE infohash=?", h)

    def stopped_at(self, strm, minutes):
        self.jf.saved[strm_item_id(strm)] = int(minutes * 60 * 1e7)

    def watching(self, strm, minutes):
        self.jf.sessions.append({"NowPlayingItem": {"Id": strm_item_id(strm)},
                                 "PlayState": {"PositionTicks": int(minutes * 60 * 1e7)}})

    def test_public_stopped_early_is_abandoned(self):
        h, rid, _ = self.torrent(age=11 * 60)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "removed")
        self.assertIn("abandoned", self.state(h)["reason"])
        self.assertEqual((self.qb.deleted, self.sv.forgotten), ([h], [rid]))

    def test_complete_during_trial_goes_to_seeding_without_abandon(self):
        h, _, _ = self.torrent(age=60 * 60, progress=1)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "seeding")
        self.assertEqual(self.qb.deleted, [])

    def test_recent_public_waits(self):
        h, _, _ = self.torrent(age=3 * 60)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "trial")

    def test_public_watched_15_min_commits(self):
        h, _, strm = self.torrent(age=30 * 60)
        self.watching(strm, 16)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "committed")
        self.assertEqual(self.qb.deleted, [])

    def test_private_goes_to_the_end_and_releases_the_rest_only_with_the_video_complete(self):
        h, _, _ = self.torrent(provider="ratio-tracker", age=60 * 60)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "committed")        # no abandon
        self.qb.files_of[h] = [{"name": "F/f.mkv", "index": 0, "priority": 1, "progress": 0.5},
                               {"name": "F/sample.mkv", "index": 1, "priority": 0, "progress": 0}]
        self.lc.step()
        self.assertEqual(self.qb.prios, [])                           # video incomplete: hands off
        self.qb.files_of[h][0]["progress"] = 1
        self.lc.step()
        self.assertEqual(self.qb.prios, [(h, [1], 1)])

    def test_complete_becomes_seeding_and_goes_after_days_unwatched(self):
        h, _, _ = self.torrent(state="committed", progress=1)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "seeding")
        self.db.execute("UPDATE torrents SET completed_at=? WHERE infohash=?", time.time() - 3 * DAY, h)
        self.lc.step()
        self.assertEqual(self.state(h)["reason"], "seed rule met and unwatched")

    def test_seed_watched_recently_stays(self):
        h, _, _ = self.torrent(state="seeding", progress=1, last_play=time.time() - DAY, completed_at=time.time() - 5 * DAY)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "seeding")

    def test_private_only_leaves_with_ratio(self):
        old = time.time() - 5 * DAY
        h, _, _ = self.torrent(provider="ratio-tracker", state="seeding", progress=1, ratio=0.6, completed_at=old)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "seeding")
        self.qb.torrents[h]["ratio"] = 1.02
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "removed")

    def test_either_rule_leaves_with_hours_or_ratio(self):
        old = time.time() - 5 * DAY
        h1, _, _ = self.torrent(provider="either-tracker", state="seeding", progress=1, seeding=70 * 3600, completed_at=old)
        h2, _, _ = self.torrent(provider="either-tracker", state="seeding", progress=1, seeding=76 * 3600, completed_at=old)
        h3, _, _ = self.torrent(provider="either-tracker", state="seeding", progress=1, seeding=3600, ratio=1.0, completed_at=old)
        self.lc.step()
        self.assertEqual([self.state(h)["state"] for h in (h1, h2, h3)], ["seeding", "removed", "removed"])

    def test_private_is_never_abandoned(self):
        h, _, _ = self.torrent(provider="either-tracker", age=3600)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "committed")

    def test_unknown_provider_is_treated_as_private(self):
        h, _, _ = self.torrent(provider="who-knows", age=3600, private=False)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "committed")

    def test_vanished_from_qbittorrent(self):
        h, rid, _ = self.torrent()
        del self.qb.torrents[h]
        self.lc.step()
        self.assertEqual(self.state(h)["reason"], "vanished from qBittorrent")
        self.assertEqual(self.sv.forgotten, [rid])

    def test_empty_qbittorrent_list_deletes_nothing(self):
        h, _, _ = self.torrent(age=60)
        self.qb.empty_list = True                      # qBittorrent restarting: info() returns []
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "trial")
        self.assertEqual(self.sv.forgotten, [])

    def test_private_manual_is_not_abandoned(self):
        h, _, _ = self.torrent(provider="manual", private=True, age=60 * 60)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "committed")
        self.assertEqual(self.qb.deleted, [])

    def test_private_complete_during_trial_releases_the_rest_before_seeding(self):
        h, _, _ = self.torrent(provider="ratio-tracker", age=60, progress=1)
        self.qb.files_of[h] = [{"name": "F/f.mkv", "index": 0, "priority": 1, "progress": 1},
                               {"name": "F/extras.mkv", "index": 1, "priority": 0, "progress": 0}]
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "committed")
        self.lc.step()
        self.assertEqual(self.qb.prios, [(h, [1], 1)])
        self.assertEqual(self.state(h)["state"], "committed")      # the rest still has to download
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "seeding")

    def test_a_limit_that_does_not_restore_is_retried(self):
        h, _, strm = self.torrent(age=60, progress=0.2)
        self.qb.others = [{"hash": "x" * 40, "category": "", "dl_limit": 500}, {"hash": "y" * 40, "category": "", "dl_limit": 0}]
        self.watching(strm, 2)
        self.lc.step()
        self.jf.sessions = []
        self.qb.fail_limit = "x" * 40
        self.lc.step()
        self.assertEqual([r["infohash"] for r in self.db.all("SELECT * FROM saved_limits")], ["x" * 40])
        self.qb.fail_limit = None
        self.lc.step()
        self.assertEqual(self.db.all("SELECT * FROM saved_limits"), [])
        self.assertIn((("x" * 40,), 500), self.qb.limits)

    def test_foreign_is_never_touched(self):
        h, _, _ = self.torrent(foreign=True, state="seeding", progress=1, completed_at=time.time() - 30 * DAY)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "foreign")
        self.assertEqual(self.qb.deleted, [])

    def test_playback_first_throttles_and_restores(self):
        h, _, strm = self.torrent(age=60, progress=0.2)
        self.qb.others = [{"hash": "x" * 40, "category": "", "dl_limit": -1},
                          {"hash": h, "category": "ondemand", "dl_limit": 0}]
        self.watching(strm, 2)
        self.lc.step()
        self.assertEqual(self.qb.limits, [(("x" * 40,), 300 * 1024)])   # only the one outside the catalog
        self.lc.step()
        self.assertEqual(len(self.qb.limits), 1)                        # does not throttle again
        self.jf.sessions = []
        self.lc.step()
        self.assertEqual(self.qb.limits[-1], (("x" * 40,), 0))          # -1 (unlimited) comes back as 0
        self.assertEqual(self.db.all("SELECT * FROM saved_limits"), [])

    # ------------------------------------------------------------ "continue watching"
    def idle_seed(self, days, **kw):
        return self.torrent(state="seeding", progress=1, completed_at=time.time() - days * DAY, **kw)

    def test_seed_with_saved_position_stays(self):
        h, _, strm = self.idle_seed(5)
        self.stopped_at(strm, 40)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "seeding")
        self.assertEqual(self.qb.deleted, [])

    def test_seed_with_position_goes_after_the_deadline(self):
        h, _, strm = self.idle_seed(15)
        self.stopped_at(strm, 40)
        self.lc.step()
        self.assertEqual(self.state(h)["reason"], "seed rule met and unwatched")

    def test_seed_without_position_goes_as_before(self):
        h, _, _ = self.idle_seed(3)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "removed")

    def test_kept_seed_does_not_ask_jellyfin_every_step(self):
        h, _, strm = self.idle_seed(5)
        self.stopped_at(strm, 40)
        for _ in range(3):
            self.lc.step()
        self.assertEqual(self.jf.queries, 1)
        self.assertEqual(self.state(h)["state"], "seeding")

    def test_pack_with_position_on_another_episode_stays(self):
        h, rid, _ = self.idle_seed(5)
        tid = self.db.one("SELECT title_id FROM releases WHERE id=?", rid)["title_id"]
        sibling = self.db.save_release(tid, {"label": "S01E02", "torrent_url": "u", "provider": "public", "private": False})
        strm2 = "/catalog/Series/S (2020) [tvdbid-1]/Season 01/S S01E02.strm"
        self.db.execute("UPDATE releases SET torrent_tag='pack', strm=? WHERE id=?", strm2, sibling)
        self.db.execute("UPDATE releases SET torrent_tag='pack' WHERE id=?", rid)
        self.stopped_at(strm2, 12)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "seeding")

    def test_jellyfin_down_deletes_nothing(self):
        h, _, strm = self.idle_seed(5)
        self.jf.down = True
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "seeding")
        h2, _, _ = self.torrent(age=11 * 60)
        self.lc.step()
        self.assertEqual(self.state(h2)["state"], "trial")
        self.assertEqual(self.qb.deleted, [])

    def test_public_stopped_midway_commits_instead_of_abandoning(self):
        h, _, strm = self.torrent(age=11 * 60)
        self.stopped_at(strm, 8)
        self.lc.step()
        self.assertEqual(self.state(h)["state"], "committed")
        self.assertEqual(self.qb.deleted, [])


class TestLifecycleWithPack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        tid = self.db.save_title({"key": "tvdb:79349", "tvdb": "79349", "name": "X", "kind": "series", "origin": "series"})
        self.rids = []
        for e in (1, 2):
            rid = self.db.save_release(tid, {"label": f"S01E0{e}", "season": 1, "episode": e, "provider": "public",
                                             "torrent_url": "u", "torrent_tag": "pack", "seeds": 1})
            strm = f"/catalog/Series/X/Season 01/X S01E0{e}.strm"
            self.db.execute("UPDATE releases SET strm=? WHERE id=?", strm, rid)
            self.rids.append((rid, strm))
        now = time.time()
        self.db.execute("INSERT INTO torrents(infohash,release_id,state,file,started_at,last_access,completed_at,rest_released) "
                        "VALUES('h1',?, 'seeding', 'P/a.mkv', ?, ?, ?, 1)", self.rids[0][0], now - 9e5, now - 9e5, now - 9e5)
        self.qb = FakeQBit()
        self.qb.torrents = {"h1": {"hash": "h1", "progress": 1, "ratio": 9, "seeding_time": 9e6, "force_start": True}}
        self.jf, self.sv = FakeJellyfin(), FakeService()
        self.lc = Lifecycle(config.DEFAULTS, self.db, self.qb, self.jf, self.sv, rules=RULES.get)

    def tearDown(self):
        self.tmp.cleanup()

    def test_watching_the_other_episode_holds_the_pack(self):
        self.jf.sessions.append({"NowPlayingItem": {"Id": strm_item_id(self.rids[1][1])},
                                 "PlayState": {"PositionTicks": 60 * 10**7}})
        self.lc.step()
        self.assertEqual(self.qb.deleted, [])

    def test_deleting_the_pack_forgets_every_episode(self):
        self.lc.step()
        self.assertEqual(self.qb.deleted, ["h1"])
        self.assertEqual(sorted(self.sv.forgotten), sorted(r for r, _ in self.rids))


if __name__ == "__main__":
    unittest.main()
