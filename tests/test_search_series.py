import copy
import http.client
import json
import os
import tempfile
import threading
import time
import unittest

from ondemand import config
from ondemand.catalog import MARKER, Catalog
from ondemand.db import Database
from ondemand.gateway import Metrics
from ondemand.jellyfin import TYPE_SERIES, item_id
from ondemand.providers import TorrentItem
from ondemand.search import Search, SearchAPI, SearchError
from ondemand.search_series import SeriesError, SeriesSearch
from tests.fakes import FakeTV

DEXTER = {"Imdb": "tt0773262", "Tvdb": "79349", "Tmdb": "1405"}
DEFAULT_TITLES = [f"Dexter S01E{n:02d} 1080p BluRay x264-G" for n in range(1, 4)] + ["Dexter New Blood S01E01 1080p WEB x264-X"]


class FakeRegistry:
    def __init__(self, titles=DEFAULT_TITLES, warnings=()):
        self.titles, self.warnings, self.calls = list(titles), list(warnings), 0

    def search_series(self, q, progress=None):
        self.calls += 1
        if progress:
            progress(1, 1, "public-index")
        return [TorrentItem(title=t, infohash=f"{i + 1:040x}", seeds=30 + i, provider="public-index", private=False)
                for i, t in enumerate(self.titles)], self.warnings

    def search_movie(self, q):
        return [], []


class FakeJellyfin:
    def __init__(self):
        self.remote = [{"Name": "Dexter", "PremiereDate": "2006-10-01T00:00:00Z", "ImageUrl": "https://image.tmdb.org/t/p/original/x.jpg",
                        "ProviderIds": {"Tmdb": "1405"}},
                       {"Name": "Dexter: New Blood", "PremiereDate": "2021-11-07T00:00:00Z", "ProviderIds": {"Tmdb": "2"}},
                       {"Name": "No ids", "ProviderIds": {"Tmdb": "3"}}]
        self.ids = {"1405": DEXTER, "2": {"Imdb": "tt14164730", "Tvdb": "412366"}, "3": {}}
        self.have = set()                  # ids Jellyfin already has ("tvdb:79349"…)
        self.items_by_id = {}              # item(id) -> dict (Type, Path, ProviderIds)
        self.created = False
        self.count, self.after = 0, 0      # items Jellyfin counts in the series: now and after the refresh
        self.calls = []
        self.series_id = None

    def remote_search_movie(self, term, year=None, limit=8, language=None):   # movies: nothing (series are the focus)
        return []

    def local_movies(self, catalog_prefix):
        return {}

    def remote_search_series(self, term, limit=8):
        return self.remote[:limit]

    def series_ids_from_tmdb(self, tmdb):
        return self.ids.get(tmdb, {})

    def series_by_imdb(self, imdb):
        return {"Name": "Dexter", "Overview": "A cop.", "PremiereDate": "2006-10-01T00:00:00Z",
                "ImageUrl": "https://image.tmdb.org/t/p/original/x.jpg", "ProviderIds": {"Tmdb": "1405"}}

    def all_series_ids(self):
        return set(self.have)

    def item(self, item_id_, fields=""):
        return self.items_by_id.get(item_id_)

    def idle(self):
        return True

    def local_episodes(self, catalog_prefix):
        return {}

    def _items(self, **params):
        if self.created and params.get("Ids") == self.series_id:
            return [{"Id": self.series_id, "ChildCount": 1, "RecursiveItemCount": self.count, "Path": "x"}]
        return []

    def refresh_folder(self, path):
        self.calls.append(("folder", path))
        self.created, self.count = True, self.after
        return True

    def refresh_item(self, item_id_, recursive=False):
        self.calls.append(("item", item_id_, recursive))
        self.count = self.after

    def notify_paths(self, paths, kind="Created"):
        self.calls.append(("notice", tuple(paths)))


class Base(unittest.TestCase):
    jellyfin_root = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        open(os.path.join(root, MARKER), "w").close()
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg["secrets"] = {}
        self.cfg["search"]["item_wait_s"] = 2          # fail fast instead of waiting 5 min
        self.db = Database(os.path.join(root, "t.db"))
        self.jf = FakeJellyfin()
        self.reg = FakeRegistry()
        self.cat = Catalog(self.db, self.jf, root, "http://gw:8097", movies="Movies", series="Series",
                           jellyfin_root=self.jellyfin_root)
        self.ss = SeriesSearch(self.db, self.jf, self.cat, self.cfg, FakeTV(), self.reg, Metrics(), background=False)
        self.folder_jf = os.path.join(self.cat.to_jellyfin(self.cat.series), "Dexter (2006) [tvdbid-79349]")
        self.jf.series_id = item_id(TYPE_SERIES, self.folder_jf)

    def tearDown(self):
        self.tmp.cleanup()


class TestCards(Base):
    def test_only_what_jellyfin_does_not_have_and_with_complete_ids(self):
        c = self.ss.cards("dexter")
        self.assertEqual([x["imdb"] for x in c], ["tt0773262", "tt14164730"])     # "No ids" is left out
        self.assertEqual((c[0]["kind"], c[0]["name"], c[0]["year"]), ("series", "Dexter", 2006))
        self.assertTrue(c[0]["poster"].startswith("https://image.tmdb.org/t/p/w342/"))
        self.jf.have = {"tvdb:79349"}
        self.ss._lib = (float("-inf"), set())
        self.assertEqual([x["imdb"] for x in self.ss.cards("dexter")], ["tt14164730"])

    def test_search_cache(self):
        self.ss.cards("dexter")
        self.jf.remote = []
        self.assertEqual(len(self.ss.cards("dexter")), 2)           # still from the cache


class TestDetails(Base):
    def test_overview_from_tmdb_and_numbers_from_tvmaze(self):
        d = self.ss.details("tt0773262")
        self.assertEqual((d["kind"], d["name"], d["overview"], d["seasons"], d["episodes"]), ("series", "Dexter", "A cop.", 2, 24))
        with self.assertRaises(SeriesError):
            self.ss.details("xx")


class TestRequest(Base):
    def test_research_registers_writes_and_returns_the_series(self):
        self.ss.cards("dexter")                                       # the card is where the ids come from
        self.jf.after = 4
        item, text = self.ss.prepare_request("tt0773262", lambda t: None)
        self.assertEqual(item, self.jf.series_id)
        self.assertEqual(text, "3 of 24 episodes")
        season = os.path.join(self.cat.series, "Dexter (2006) [tvdbid-79349]", "Season 01")
        self.assertEqual(sorted(os.listdir(season)), [f"Dexter S01E0{n}.strm" for n in (1, 2, 3)])
        self.assertEqual(self.db.one("SELECT count(*) n FROM releases WHERE strm IS NOT NULL AND season=1")["n"], 3)
        self.assertEqual(self.db.one("SELECT kind FROM titles WHERE key='tvdb:79349'")["kind"], "series")
        self.assertIn(("folder", self.cat.to_jellyfin(self.cat.series)), self.jf.calls)    # new series: refresh of the root
        self.assertNotIn("Dexter New Blood", " ".join(r["name"] for r in self.db.all("SELECT name FROM releases")))

    def test_no_torrent_at_all_is_a_clear_error(self):
        self.ss.registry = FakeRegistry(titles=["Dexter New Blood S01E01 1080p WEB x264-X"])
        with self.assertRaises(SeriesError) as e:
            self.ss.prepare_request("tt0773262", lambda t: None)
        self.assertEqual(e.exception.code, 404)

    def test_end_to_end_request_with_progress(self):
        s = Search(self.db, self.jf, self.cat, self.reg, self.cfg, Metrics(), background=False, series=self.ss)
        self.ss.cards("dexter")
        self.jf.after = 4
        user = {"Id": "u1", "Name": "ana", "Policy": {"IsAdministrator": False}}
        st = s.request("tt0773262", user, "series")
        self.assertEqual((st["state"], st["item_id"], st["detail"]), ("ready", self.jf.series_id, "3 of 24 episodes"))
        self.assertEqual(self.db.one("SELECT kind FROM requests WHERE imdb='tt0773262'")["kind"], "series")
        with self.assertRaises(SearchError):
            s.request("tt0773262", user, "soap-opera")


class TestRequestWithSeparateJellyfinRoot(TestRequest):
    jellyfin_root = "/media/catalog"


class TestComplete(Base):
    def catalog_item(self, **kw):
        base = {"Id": "a" * 32, "Type": "Series", "Name": "Dexter", "Path": self.folder_jf,
                "ProviderIds": {"Tvdb": "79349", "Imdb": "tt0773262"}}
        self.jf.items_by_id["a" * 32] = {**base, **kw}

    def test_only_catalog_series(self):
        self.catalog_item(Path="/media/tv/Dexter")
        with self.assertRaises(SeriesError) as e:
            self.ss.complete("a" * 32)
        self.assertEqual(e.exception.code, 422)
        self.catalog_item(Type="Movie")
        with self.assertRaises(SeriesError):
            self.ss.complete("a" * 32)
        with self.assertRaises(SeriesError) as e:
            self.ss.complete("not-an-id")
        self.assertEqual(e.exception.code, 400)

    def test_completes_once_a_week(self):
        self.catalog_item()
        self.jf.created, self.jf.count, self.jf.after = True, 1, 4
        st = self.ss.complete("a" * 32)
        self.assertEqual((st["state"], st["detail"], st["found"]), ("ready", "3 of 24 episodes", 3))     # 3 written now
        self.assertIn(("item", self.jf.series_id, True), self.jf.calls)         # the series existed: recursive refresh
        calls = self.reg.calls
        st2 = self.ss.complete("a" * 32)
        self.assertTrue(st2.get("recent"))
        self.assertEqual(self.reg.calls, calls)                                   # did not even search
        self.ss.complete("a" * 32, force=True)
        self.assertEqual(self.reg.calls, calls + 1)
        self.db.execute("UPDATE series_searches SET last_at=last_at-?", 8 * 86400)
        self.ss.complete("a" * 32)
        self.assertEqual(self.reg.calls, calls + 2)

    def test_an_error_is_retried_after_an_hour(self):
        self.catalog_item()
        self.ss.registry = FakeRegistry(titles=[])
        st = self.ss.complete("a" * 32)
        self.assertEqual(st["state"], "ready")                                    # no torrent: nothing new, but no error
        self.db.execute("UPDATE series_searches SET state='error', last_at=?", time.time() - 7200)
        self.ss.registry = reg = FakeRegistry()
        self.ss.complete("a" * 32)
        self.assertEqual(reg.calls, 1)


class TestHTTPRoutes(Base):
    """The series routes behind the search server (with and without the /ondemand prefix)."""

    def setUp(self):
        super().setUp()
        self.jf.user_from_token = lambda t: {"Id": "u1", "Name": "ana", "Policy": {"IsAdministrator": t == "admin"}} if t in ("admin", "user") else None
        self.s = Search(self.db, self.jf, self.cat, self.reg, self.cfg, Metrics(), background=False, series=self.ss)
        self.srv = SearchAPI(("127.0.0.1", 0), self.s)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.jf.items_by_id["a" * 32] = {"Id": "a" * 32, "Type": "Series", "Name": "Dexter", "Path": self.folder_jf,
                                         "ProviderIds": {"Tvdb": "79349", "Imdb": "tt0773262"}}

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def call(self, method, path, token="user", body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=20)
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"X-Emby-Token": token} if token else {})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"null")

    def test_search_returns_series_cards_and_details_by_kind(self):
        st, d = self.call("GET", "/ondemand/api/search?q=dexter")
        self.assertEqual(st, 200)
        self.assertEqual([x["imdb"] for x in d["results"] if x["kind"] == "series"], ["tt0773262", "tt14164730"])
        st, d = self.call("GET", "/api/details/tt0773262?kind=series")
        self.assertEqual((st, d["kind"], d["seasons"]), (200, "series", 2))

    def test_request_a_series_and_follow_it(self):
        self.ss.cards("dexter")
        self.jf.after = 4
        st, d = self.call("POST", "/ondemand/api/request", body={"imdb": "tt0773262", "kind": "series"})
        self.assertEqual(st, 202)
        st, d = self.call("GET", "/api/request/tt0773262")
        self.assertEqual((st, d["state"], d["detail"]), (200, "ready", "3 of 24 episodes"))
        st, d = self.call("POST", "/api/request", body={"imdb": "tt0773262", "kind": "soap-opera"})
        self.assertEqual(st, 400)

    def test_complete_needs_a_valid_session_and_admin_forces(self):
        self.jf.created, self.jf.count, self.jf.after = True, 1, 4
        self.assertEqual(self.call("POST", "/api/complete", token=None, body={"item_id": "a" * 32})[0], 401)
        st, d = self.call("POST", "/api/complete", body={"item_id": "a" * 32})
        self.assertEqual((st, d["state"]), (202, "ready"))
        st, d = self.call("GET", "/ondemand/api/complete/79349")
        self.assertEqual((st, d["state"], d["found"]), (200, "ready", 3))
        st, d = self.call("POST", "/api/complete", body={"item_id": "a" * 32, "force": True})      # a normal user cannot force
        self.assertTrue(d.get("recent"))
        st, d = self.call("POST", "/api/complete", token="admin", body={"item_id": "a" * 32, "force": True})
        self.assertNotIn("recent", d)
        self.assertEqual(self.call("GET", "/api/complete/1")[0], 404)
        self.jf.items_by_id["b" * 32] = {"Type": "Series", "Path": "/media/tv/The Boys", "ProviderIds": {"Tvdb": "1"}}
        st, d = self.call("POST", "/api/complete", body={"item_id": "b" * 32})
        self.assertEqual(st, 422)


if __name__ == "__main__":
    unittest.main()
