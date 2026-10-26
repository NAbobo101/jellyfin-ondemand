import os
import tempfile
import time
import unittest

from ondemand import languages
from ondemand.catalog import (MARKER, Catalog, clean_name, episode_path, movie_folder, series_folder, strm_path)
from ondemand.db import Database
from ondemand.jellyfin import TYPE_EPISODE, item_id, strm_item_id

PT = languages.get("pt-BR")
TITLE = {"key": "imdb:tt4154756", "imdb": "tt4154756", "name": "Avengers: Infinity War", "year": 2018,
         "rating": 8.4, "origin": "test"}
VERSIONS = [
    {"label": "1080p", "quality": "1080p", "source_type": "BluRay", "codec": "x264", "size": 3, "seeds": 100,
     "infohash": "866b", "torrent_url": "https://tracker.example/866B", "provider": "test",
     "name": "Avengers Infinity War 2018 1080p BluRay x264-GRP"},
    {"label": "4K", "quality": "2160p", "source_type": "BluRay", "codec": "x265", "size": 7, "seeds": 100,
     "infohash": "f990", "torrent_url": "https://tracker.example/F990", "provider": "test"},
]
SERIES = {"key": "tvdb:79349", "tvdb": "79349", "imdb": "tt0773262", "tmdb": "1405", "name": "Dexter: Original Sin", "year": 2024}


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def ep(t, e, tag="aaa", **kw):
    return {"label": f"S{t:02d}E{e:02d}", "season": t, "episode": e, "name": "Dexter S01 1080p", "provider": "id-tracker",
            "private": True, "torrent_url": "u", "torrent_tag": tag, "seeds": 1, **kw}


class FakeJellyfin:
    def __init__(self, idle=True):
        self._idle, self.notified, self.by_path, self.locked, self.queries = idle, [], {}, [], 0

    def idle(self):
        return self._idle

    def notify_paths(self, paths, kind="Created"):
        self.notified += paths

    def items(self, ids):
        # self.by_path: {.strm path: item}; the real query is by computed id
        self.queries += 1
        by_id = {strm_item_id(c): {**i, "Id": strm_item_id(c)} for c, i in self.by_path.items()}
        return [by_id[i] for i in ids if i in by_id]

    def lock(self, item_id):
        self.locked.append(item_id)


class TestNames(unittest.TestCase):
    def test_clean_name(self):
        self.assertEqual(clean_name('Avengers: Infinity War'), "Avengers - Infinity War")
        self.assertEqual(clean_name('A/B "C"?  D.'), "AB C D")

    def test_folder_and_strm(self):
        p = movie_folder("/cat", "Avengers: Infinity War", 2018, "tt4154756")
        self.assertEqual(p, "/cat/Avengers - Infinity War (2018) [imdbid-tt4154756]")
        self.assertEqual(strm_path(p), p + "/Avengers - Infinity War (2018) [imdbid-tt4154756].strm")

    def test_series_names(self):
        self.assertEqual(series_folder("/c/Series", SERIES), "/c/Series/Dexter - Original Sin (2024) [tvdbid-79349]")
        self.assertEqual(episode_path("/p", "Dexter: Original Sin", 1, 2), "/p/Season 01/Dexter - Original Sin S01E02.strm")

    def test_episode_id_uses_the_episode_type(self):
        c = "/media/catalog/Series/X (2020) [tvdbid-1]/Season 01/X S01E01.strm"
        self.assertEqual(strm_item_id(c), item_id(TYPE_EPISODE, c))
        self.assertNotEqual(strm_item_id(c), strm_item_id("/media/catalog/Movies/X/X.strm"))


class CatalogBase(unittest.TestCase):
    jellyfin_root = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        open(os.path.join(self.root, MARKER), "w").close()
        self.db = Database(os.path.join(self.root, "t.db"))
        self.jf = FakeJellyfin()
        self.cat = Catalog(self.db, self.jf, self.root, "http://gw:8097", movies="Movies", movies_4k="Movies 4K",
                           series="Series", jellyfin_root=self.jellyfin_root, lang=PT)
        self.movies, self.movies_4k = self.cat.movies, self.cat.movies_4k

    def tearDown(self):
        self.tmp.cleanup()

    def _rid(self, strm):
        return read(strm).strip().rsplit("/", 1)[1]


class TestCatalog(CatalogBase):
    def test_main_in_movies_and_4k_in_its_own_library(self):
        self.cat.register(TITLE, VERSIONS, local_ids=set())
        self.assertEqual(self.cat.write_pending(batch=10), 1)
        p1 = movie_folder(self.movies, TITLE["name"], 2018, TITLE["imdb"])
        p4 = movie_folder(self.movies_4k, TITLE["name"], 2018, TITLE["imdb"])
        for folder, label in ((p1, "1080p"), (p4, "4K")):
            self.assertEqual(len(os.listdir(folder)), 2)                  # one .strm + the placeholder
            rid = self._rid(strm_path(folder))
            self.assertEqual(self.db.one("SELECT label FROM releases WHERE id=?", rid)["label"], label)
            self.assertTrue(read(strm_path(folder)[:-5] + ".por.smi").startswith("﻿<SAMI>"))
            self.assertEqual(self.db.one("SELECT state FROM subtitles WHERE release_id=?", rid)["state"], "placeholder")
        self.assertEqual(sorted(self.jf.notified), sorted([p1, p4]))
        self.assertEqual(self.cat.write_pending(batch=10), 0)        # nothing pending

    def test_without_a_4k_root_only_the_main_is_written(self):
        cat = Catalog(self.db, self.jf, self.root, "http://gw:8097", movies="Movies", lang=PT)
        cat.register(TITLE, VERSIONS, set())
        cat.write_pending()
        self.assertFalse(os.path.exists(os.path.join(self.root, "Movies 4K")))

    def test_without_language_there_is_no_placeholder(self):
        cat = Catalog(self.db, self.jf, self.root, "http://gw:8097", movies="Movies")
        cat.register(TITLE, VERSIONS[:1], set())
        cat.write_pending()
        folder = movie_folder(cat.movies, TITLE["name"], 2018, TITLE["imdb"])
        self.assertEqual(os.listdir(folder), [os.path.basename(strm_path(folder))])

    def test_does_not_duplicate_a_local_movie(self):
        self.cat.register(TITLE, VERSIONS, local_ids={"imdb:tt4154756"})
        self.assertEqual(self.cat.write_pending(), 0)
        self.assertEqual(self.db.one("SELECT state FROM titles")["state"], "duplicate_local")

    def test_without_the_marker_nothing_is_written(self):
        os.unlink(os.path.join(self.root, MARKER))
        self.cat.register(TITLE, VERSIONS, set())
        self.assertEqual(self.cat.write_pending(), 0)
        self.assertFalse(os.path.exists(self.movies))

    def test_waits_while_someone_is_watching(self):
        self.jf._idle = False
        self.cat.register(TITLE, VERSIONS, set())
        self.assertEqual(self.cat.write_pending(), 0)

    def test_minimal_manual_title(self):
        t = {"key": "imdb:tt2285752", "imdb": "tt2285752", "name": "Tears of Steel", "year": 2012, "origin": "manual"}
        self.cat.register(t, [{"label": "1080p", "torrent_url": "http://x/t.torrent", "file": "a.mkv"}], set())
        self.assertEqual(self.cat.write_pending(require_idle=False), 1)
        self.assertEqual(self.db.one("SELECT file FROM releases")["file"], "a.mkv")

    def test_locks_only_with_every_item_identified_and_in_one_query(self):
        self.cat.register(TITLE, VERSIONS, set())
        self.cat.write_pending()
        s1080 = strm_path(movie_folder(self.movies, TITLE["name"], 2018, TITLE["imdb"]))
        s4k = strm_path(movie_folder(self.movies_4k, TITLE["name"], 2018, TITLE["imdb"]))
        ready = {"ProviderIds": {"Imdb": "tt4154756"}, "ImageTags": {"Primary": "x"}}
        self.jf.by_path = {s1080: dict(ready), s4k: {"ProviderIds": {}, "ImageTags": {}}}
        self.assertEqual(self.cat.lock_identified(), 0)                  # just written: not checked yet
        self.assertEqual(self.jf.queries, 0)
        self.assertEqual(self.cat.lock_identified(min_age_s=0), 0)       # the 4K still without metadata
        self.jf.by_path[s4k] = dict(ready)
        self.assertEqual(self.cat.lock_identified(min_age_s=0), 1)
        self.assertEqual(sorted(self.jf.locked), sorted([strm_item_id(s1080), strm_item_id(s4k)]))
        self.assertEqual(self.jf.queries, 2)                             # one query per round
        self.assertEqual(self.cat.lock_identified(min_age_s=0), 0)       # nothing pending: not even a query
        self.assertEqual(self.jf.queries, 2)

    def test_locks_only_with_nobody_watching(self):
        self.cat.register(TITLE, VERSIONS, set())
        self.cat.write_pending()
        self.jf._idle = False
        self.assertEqual(self.cat.lock_identified(min_age_s=0), 0)
        self.assertEqual(self.jf.queries, 0)

    def test_without_poster_after_a_day_locks_with_imdb_or_leaves_the_queue(self):
        self.cat.register(TITLE, VERSIONS, set())
        self.cat.write_pending()
        self.db.execute("UPDATE titles SET written_at=?", time.time() - 25 * 3600)
        s1080 = strm_path(movie_folder(self.movies, TITLE["name"], 2018, TITLE["imdb"]))
        s4k = strm_path(movie_folder(self.movies_4k, TITLE["name"], 2018, TITLE["imdb"]))
        no_imdb = {"ProviderIds": {}, "ImageTags": {}}
        self.jf.by_path = {s1080: dict(no_imdb), s4k: dict(no_imdb)}
        self.assertEqual(self.cat.lock_identified(), 0)
        self.assertEqual(self.db.one("SELECT locked FROM titles")["locked"], -1)   # left the queue
        self.db.execute("UPDATE titles SET locked=0")
        imdb_only = {"ProviderIds": {"Imdb": "tt4154756"}, "ImageTags": {}}
        self.jf.by_path = {s1080: dict(imdb_only), s4k: dict(imdb_only)}
        self.assertEqual(self.cat.lock_identified(), 1)                  # locked with just the IMDb id
        self.assertEqual(len(self.jf.locked), 2)

    def test_release_keeps_its_id_on_update(self):
        self.cat.register(TITLE, VERSIONS, set())
        before = {r["label"]: r["id"] for r in self.db.all("SELECT label,id FROM releases")}
        self.cat.register(TITLE, VERSIONS, set())
        after = {r["label"]: r["id"] for r in self.db.all("SELECT label,id FROM releases")}
        self.assertEqual(before, after)          # the .strm points to the id: it must not change

    def test_competing_providers_keep_the_most_seeded_until_it_plays(self):
        tid = self.cat.register(TITLE, VERSIONS[:1], set())
        rid = self.db.one("SELECT id FROM releases")["id"]
        other = {**VERSIONS[0], "provider": "other", "seeds": 150, "infohash": "aaaa"}
        self.db.save_release(tid, other)
        r = self.db.one("SELECT * FROM releases")
        self.assertEqual((r["id"], r["provider"], r["infohash"]), (rid, "other", "aaaa"))
        self.db.save_release(tid, {**VERSIONS[0], "seeds": 120})       # fewer seeds: stays
        self.assertEqual(self.db.one("SELECT provider FROM releases")["provider"], "other")


class TestSeparateJellyfinRoot(CatalogBase):
    jellyfin_root = "/media/catalog"

    def test_paths_in_the_database_and_notices_are_jellyfins(self):
        self.cat.register(TITLE, VERSIONS[:1], set())
        self.cat.write_pending()
        local = strm_path(movie_folder(self.movies, TITLE["name"], 2018, TITLE["imdb"]))
        self.assertTrue(os.path.exists(local))
        strm = self.db.one("SELECT strm FROM releases")["strm"]
        self.assertTrue(strm.startswith("/media/catalog/Movies/"), strm)
        self.assertEqual(self.cat.to_local(strm), local)
        self.assertTrue(all(p.startswith("/media/catalog/") for p in self.jf.notified))


class TestSeriesWriting(CatalogBase):
    def test_writes_strm_and_placeholder_per_episode_and_only_what_is_missing(self):
        self.cat.register_series(SERIES, [ep(1, 1), ep(1, 2)])
        self.assertEqual(self.cat.write_pending_series(), 1)
        folder = series_folder(self.cat.series, SERIES)
        strm = os.path.join(folder, "Season 01", "Dexter - Original Sin S01E01.strm")
        self.assertTrue(os.path.exists(strm) and os.path.exists(strm[:-5] + ".por.smi"))
        rid = self.db.one("SELECT id FROM releases WHERE label='S01E01'")["id"]
        self.assertEqual(read(strm), f"http://gw:8097/v/{rid}\n")
        self.assertEqual(self.db.one("SELECT state FROM titles")["state"], "written")
        self.assertEqual(self.jf.notified, [folder])
        # nothing pending -> nothing written; a new episode later -> only that one
        self.assertEqual(self.cat.write_pending_series(), 0)
        self.cat.register_series(SERIES, [ep(1, 1), ep(1, 3)])
        self.assertEqual(self.cat.write_pending_series(), 1)
        self.assertIn("Dexter - Original Sin S01E03.strm", os.listdir(os.path.join(folder, "Season 01")))
        self.assertEqual(self.db.one("SELECT count(*) n FROM releases WHERE strm IS NOT NULL")["n"], 3)

    def test_batch_limits_series_and_series_do_not_touch_movies(self):
        for i in range(3):
            self.cat.register_series({**SERIES, "key": f"tvdb:{i}", "tvdb": str(i), "name": f"S{i}"}, [ep(1, 1)])
        self.assertEqual(self.cat.write_pending_series(batch=2), 2)
        self.assertEqual(self.cat.write_pending(), 0)               # the movie writer ignores series


class TestSeriesTorrentSwitch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        self.tid = self.db.save_title({**SERIES, "kind": "series", "origin": "series"})

    def tearDown(self):
        self.tmp.cleanup()

    def rid(self, **kw):
        return self.db.save_release(self.tid, ep(1, 1, **kw))

    def test_without_strm_follows_the_filter(self):
        r1 = self.rid(tag="aaa")
        self.assertEqual(self.rid(tag="bbb", provider="feed-tracker"), r1)
        self.assertEqual(self.db.one("SELECT torrent_tag FROM releases WHERE id=?", r1)["torrent_tag"], "bbb")

    def test_with_strm_only_switches_if_it_never_played(self):
        r1 = self.rid(tag="aaa")
        self.db.execute("UPDATE releases SET strm='/x.strm' WHERE id=?", r1)
        self.db.execute("INSERT INTO subtitles(release_id,state) VALUES(?, 'placeholder')", r1)
        self.assertEqual(self.rid(tag="bbb"), r1)
        self.assertEqual(self.db.one("SELECT torrent_tag FROM releases WHERE id=?", r1)["torrent_tag"], "bbb")
        self.db.execute("INSERT INTO torrents(infohash,release_id,state) VALUES('h',?, 'seeding')", r1)   # it played
        self.rid(tag="ccc")
        self.assertEqual(self.db.one("SELECT torrent_tag FROM releases WHERE id=?", r1)["torrent_tag"], "bbb")


if __name__ == "__main__":
    unittest.main()
