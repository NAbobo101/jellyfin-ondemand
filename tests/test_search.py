import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from ondemand import search as search_mod
from ondemand.catalog import MARKER, Catalog, movie_folder, strm_path
from ondemand.config import DEFAULTS
from ondemand.db import Database
from ondemand.gateway import Metrics
from ondemand.jellyfin import item_id, strm_item_id
from ondemand.providers import TorrentItem
from ondemand.search import Search, SearchAPI, SearchError

GODFATHER = {"Name": "O Poderoso Chefão", "ProductionYear": 1972, "Overview": "Uma família mafiosa.",
             "ProviderIds": {"Tmdb": "238", "Imdb": "tt0068646"}, "ImageUrl": "https://image.tmdb.org/t/p/original/x.jpg"}
GODFATHER_2 = {"Name": "O Poderoso Chefão: Parte II", "ProductionYear": 1974, "ProviderIds": {"Tmdb": "240"}}
NO_TORRENT = {"Name": "Sem Torrent", "ProductionYear": 2010, "ProviderIds": {"Tmdb": "999", "Imdb": "tt0000999"}}
ENGLISH = {"238": "The Godfather", "240": "The Godfather Part II", "999": "No Torrent"}


class FakeRegistry:
    """Two releases per known movie: one by IMDb id, one only by name (a text-search source)."""

    def __init__(self):
        self.queries = []

    def search_movie(self, q):
        self.queries.append(q)
        out = []
        if q.imdb == "tt0068646":
            out += [TorrentItem(title="The Godfather 1972 1080p BluRay x264-A", torrent_url="https://t.example/a",
                                seeds=50, ids={"imdb": "tt0068646"}, provider="idx", private=False),
                    TorrentItem(title="The.Godfather.1972.2160p.BluRay.x265-B", torrent_url="https://t.example/b",
                                seeds=20, provider="idx", private=False),
                    TorrentItem(title="The Godfather Part III 1990 1080p BluRay x264-C", torrent_url="https://t.example/c",
                                seeds=99, provider="idx", private=False)]
        if q.imdb == "tt0071562":
            out.append(TorrentItem(title="The Godfather Part II 1974 1080p BluRay x264-D", torrent_url="https://t.example/d",
                                   seeds=30, provider="idx", private=False))
        return out, []


class FakeJellyfin:
    def __init__(self):
        self.library, self.remote, self.tmdb, self.notified, self.new = [], {}, {"240": "tt0071562"}, [], []
        self.with_poster, self.refreshed, self.folders_refreshed, self.ids_ok = True, [], [], True
        self.by_imdb = {"tt0068646": GODFATHER, "tt0071562": {**GODFATHER_2, "ProviderIds": {"Tmdb": "240", "Imdb": "tt0071562"}},
                        "tt0000999": NO_TORRENT}
        self.users = {"tok-ana": {"Id": "u-ana", "Name": "ana", "Policy": {"IsAdministrator": False}},
                      "tok-admin": {"Id": "u-admin", "Name": "admin", "Policy": {"IsAdministrator": True}}}
        self.listings = self.imdb_lookups = 0

    def local_movies(self, catalog_prefix):
        self.listings += 1
        return {i["ProviderIds"]["Imdb"]: i for i in self.library if not i["Path"].startswith(catalog_prefix)}

    def items(self, ids):
        # an item just created comes even without metadata (like 10.11, through the query by Ids)
        ids = list(ids)
        return [i for i in self.new if i["Id"] in ids]

    def remote_search_movie(self, term, year=None, limit=8, language=None):
        return self.remote.get(term.lower(), [])[:limit]

    def imdb_from_tmdb(self, tmdb):
        return self.tmdb.get(tmdb)

    def movie_by_imdb(self, imdb):
        self.imdb_lookups += 1
        return self.by_imdb.get(imdb)

    def movie_by_tmdb(self, tmdb, language=None):
        return {"Name": ENGLISH.get(str(tmdb))} if language == "en" else None

    def user_from_token(self, token):
        return self.users.get(token)

    def _create(self, folder):
        # Jellyfin creates the .strm's item (with a poster, or without, if the queue is slow)
        self.new.append({"Id": strm_item_id(strm_path(folder)), "Name": os.path.basename(folder),
                         "Path": strm_path(folder), "ImageTags": {"Primary": "x"} if self.with_poster else {}})

    def notify_paths(self, paths, kind="Created"):
        self.notified += paths
        for p in paths:
            self._create(p)

    def refresh_folder(self, root):
        if not self.ids_ok:
            return False
        self.folders_refreshed.append(root)
        existing = {i["Path"] for i in self.new}
        for d in sorted(os.listdir(root)):
            if strm_path(os.path.join(root, d)) not in existing:
                self._create(os.path.join(root, d))
        return True

    def refresh_item(self, item_id_):
        self.refreshed.append(item_id_)
        for i in self.new:
            if i["Id"] == item_id_:
                i["ImageTags"] = {"Primary": "x"}


class SearchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        open(os.path.join(root, MARKER), "w").close()
        self.db = Database(os.path.join(root, "t.db"))
        self.jf = FakeJellyfin()
        self.reg = FakeRegistry()
        self.cat = Catalog(self.db, self.jf, root, "http://gw:8097", movies="Movies", movies_4k="Movies 4K")
        self.movies, self.movies_4k = self.cat.movies, self.cat.movies_4k
        self.cfg = {**DEFAULTS, "search": {**DEFAULTS["search"], "daily_limit": 2}}
        self.s = Search(self.db, self.jf, self.cat, self.reg, self.cfg, Metrics(), background=False)
        self.ana = self.jf.users["tok-ana"]
        self.admin = self.jf.users["tok-admin"]
        self.jf.remote["o poderoso chefão"] = [GODFATHER]
        self.jf.remote["godfather"] = [GODFATHER, GODFATHER_2, NO_TORRENT]

    def tearDown(self):
        self.tmp.cleanup()


class TestSearch(SearchBase):
    def test_tmdb_result_becomes_a_card_with_the_providers_qualities(self):
        r = self.s.search("  O Poderoso   Chefão ")
        self.assertEqual([c["imdb"] for c in r], ["tt0068646"])
        c = r[0]
        self.assertEqual((c["kind"], c["name"], c["original_name"], c["year"]), ("movie", "O Poderoso Chefão", "The Godfather", 1972))
        self.assertEqual(c["poster"], "https://image.tmdb.org/t/p/w342/x.jpg")
        self.assertEqual((c["qualities"], c["seeds"], c["request"]), (["1080p", "4K"], 50, None))
        q = self.reg.queries[0]
        self.assertEqual((q.imdb, q.tmdb, q.title, q.original_title, q.year), ("tt0068646", "238", "O Poderoso Chefão", "The Godfather", 1972))

    def test_tmdb_order_and_movies_without_a_torrent_are_left_out(self):
        self.assertEqual([c["imdb"] for c in self.s.search("godfather")], ["tt0068646", "tt0071562"])

    def test_leaves_out_what_jellyfin_already_has(self):
        self.jf.library = [{"Id": "x", "ProviderIds": {"Imdb": "tt0068646"}, "Path": "/media/movies/a.mkv"}]
        self.assertEqual([c["imdb"] for c in self.s.search("godfather")], ["tt0071562"])

    def test_leaves_out_what_is_written_in_the_catalog_without_listing_again(self):
        self.s.search("godfather")
        self.s.request("tt0071562", self.admin)                       # written to the catalog
        self.assertEqual([c["imdb"] for c in self.s.search("godfather")], ["tt0068646"])
        self.assertEqual(self.jf.listings, 1)                         # locals cached; catalog from the database

    def test_short_term_does_not_search(self):
        self.assertEqual(self.s.search("a"), [])
        self.assertEqual(self.reg.queries, [])

    def test_same_term_comes_from_the_cache(self):
        self.s.search("godfather")
        n = len(self.reg.queries)
        self.s.search("GODFATHER")
        self.assertEqual(len(self.reg.queries), n)

    def test_an_imdb_id_searches_that_movie(self):
        self.assertEqual([c["imdb"] for c in self.s.search("tt0068646")], ["tt0068646"])

    def test_without_availability_check_every_tmdb_result_is_a_card(self):
        self.s.s = {**self.s.s, "check_availability": False}
        self.assertEqual(len(self.s.search("godfather")), 3)
        self.assertEqual(self.reg.queries, [])


class TestRequest(SearchBase):
    def test_request_writes_refreshes_the_folder_and_gets_ready(self):
        st = self.s.request("tt0068646", self.ana)
        self.assertEqual(st["state"], "ready")
        p1 = movie_folder(self.movies, "The Godfather", 1972, "tt0068646")
        p4 = movie_folder(self.movies_4k, "The Godfather", 1972, "tt0068646")
        self.assertTrue(os.path.exists(strm_path(p1)) and os.path.exists(strm_path(p4)))
        self.assertEqual(self.jf.folders_refreshed, [self.movies, self.movies_4k])   # Movies first
        self.assertEqual(self.jf.notified, [])
        item = next(i for i in self.jf.new if i["Id"] == st["item_id"])
        self.assertEqual(item["Path"], strm_path(p1))                # plays the main version
        self.assertEqual(self.db.one("SELECT state FROM titles")["state"], "written")
        names = {r["label"]: r["name"] for r in self.db.all("SELECT label, name FROM releases")}
        self.assertEqual(names, {"1080p": "The Godfather 1972 1080p BluRay x264-A", "4K": "The.Godfather.1972.2160p.BluRay.x265-B"})

    def test_without_the_folder_id_falls_back_to_the_notice(self):
        self.jf.ids_ok = False
        self.assertEqual(self.s.request("tt0068646", self.ana)["state"], "ready")
        self.assertEqual(sorted(self.jf.notified), sorted([movie_folder(r, "The Godfather", 1972, "tt0068646")
                                                           for r in (self.movies, self.movies_4k)]))

    def test_item_without_metadata_gets_a_refresh_and_waits_for_the_poster(self):
        self.jf.with_poster = False
        with mock.patch.object(search_mod.time, "sleep", lambda s: None):    # no 3 s between queries
            st = self.s.request("tt0068646", self.ana)
        self.assertEqual(st["state"], "ready")
        self.assertEqual(self.jf.refreshed, [st["item_id"]])          # one refresh, on the main item

    def test_local_movie_is_ready_without_writing(self):
        self.jf.library = [{"Id": "f", "ProviderIds": {"Imdb": "tt0068646"}, "Path": "/media/movies/a.mkv"}]
        st = self.s.request("tt0068646", self.ana)
        self.assertEqual((st["state"], st["item_id"]), ("ready", "f"))
        self.assertFalse(os.path.exists(self.movies))

    def test_already_in_the_catalog_is_ready_by_id_without_writing(self):
        title, versions = self.s.versions("tt0068646")
        tid = self.cat.register(title, versions, set())
        self.cat.write_pending(require_idle=False)                # normal batch: notifies Jellyfin
        notified = list(self.jf.notified)
        st = self.s.request("tt0068646", self.ana)
        self.assertEqual((st["state"], st["item_id"]), ("ready", strm_item_id(self.cat.main_strm(tid))))
        self.assertEqual((self.jf.notified, self.jf.folders_refreshed), (notified, []))

    def test_repeated_request_does_not_write_again(self):
        st = self.s.request("tt0068646", self.ana)
        self.assertEqual(self.s.request("tt0068646", self.ana), st)
        self.assertEqual(len(self.jf.folders_refreshed), 2)

    def test_invalid_imdb(self):
        for bad in ("", "0068646", "tt12", "tt0068646; rm", "../tt0068646"):
            with self.assertRaises(SearchError) as e:
                self.s.request(bad, self.ana)
            self.assertEqual(e.exception.code, 400)

    def test_no_torrent_becomes_an_error(self):
        st = self.s.request("tt0000999", self.ana)
        self.assertEqual(st["state"], "error")
        self.assertIn("no provider", st["detail"])

    def test_daily_limit_only_for_non_admins(self):
        self.s.request("tt0068646", self.ana)
        self.s.request("tt0071562", self.ana)
        with self.assertRaises(SearchError) as e:
            self.s.request("tt0000999", self.ana)
        self.assertEqual(e.exception.code, 429)
        self.assertEqual(self.s.request("tt0000999", self.admin)["state"], "error")   # admin passes (no torrent)

    def test_restart_marks_a_pending_request_as_error(self):
        self.db.execute("INSERT INTO requests(imdb,user_id,requested_at,state) VALUES('tt0068646','u',0,'preparing')")
        Search(self.db, self.jf, self.cat, self.reg, self.cfg, Metrics())
        self.assertEqual(self.s.status("tt0068646")["state"], "error")

    def test_search_shows_a_request_in_progress(self):
        self.db.execute("INSERT INTO requests(imdb,user_id,requested_at,state) VALUES('tt0071562','u',0,'preparing')")
        r = {c["imdb"]: c["request"] for c in self.s.search("godfather")}
        self.assertEqual(r, {"tt0071562": "preparing", "tt0068646": None})


class TestDetails(SearchBase):
    def test_tmdb_plus_the_providers_qualities(self):
        d = self.s.details("tt0068646")
        self.assertEqual((d["name"], d["original_name"], d["overview"], d["year"]),
                         ("O Poderoso Chefão", "The Godfather", "Uma família mafiosa.", 1972))
        self.assertEqual((d["poster"], d["qualities"], d["request"]), ("https://image.tmdb.org/t/p/w500/x.jpg", ["1080p", "4K"], None))
        self.assertEqual(self.db.one("SELECT count(*) n FROM titles")["n"], 0)   # opening writes nothing

    def test_unknown_and_invalid_imdb(self):
        with self.assertRaises(SearchError) as e:
            self.s.details("tt9999999")
        self.assertEqual(e.exception.code, 404)
        with self.assertRaises(SearchError) as e:
            self.s.details("../x")
        self.assertEqual(e.exception.code, 400)

    def test_cache_and_live_request_state(self):
        self.s.details("tt0068646")
        self.s.request("tt0068646", self.ana)
        d = self.s.details("tt0068646")
        self.assertEqual(d["request"], "ready")
        self.assertEqual(len(self.reg.queries), 1)                    # versions cached between details and request


class TestItemId(unittest.TestCase):
    def test_the_id_recipe_matches_jellyfin(self):
        # real ids of physical folders in Jellyfin 10.11.11
        folder = "MediaBrowser.Controller.Entities.Folder"
        self.assertEqual(item_id(folder, "/media/Catalogo/Filmes"), "125c55f2b9353ded0aaa44a3493bdefb")  # scan-ok
        self.assertEqual(item_id(folder, "/media/Catalogo/Filmes4K"), "8b0af7b7048b4c6a86289eee7dba2916")  # scan-ok


class TestAPI(SearchBase):
    def setUp(self):
        super().setUp()
        self.srv = SearchAPI(("127.0.0.1", 0), self.s)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def call(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"null")

    def test_health_without_token_and_the_rest_needs_a_token(self):
        self.assertEqual(self.call("GET", "/ondemand/api/health")[0], 200)
        self.assertEqual(self.call("GET", "/ondemand/api/search?q=godfather")[0], 401)
        self.assertEqual(self.call("GET", "/api/search?q=godfather", {"X-Emby-Token": "fake"})[0], 401)

    def test_with_and_without_prefix_and_token_in_authorization(self):
        st, d = self.call("GET", "/ondemand/api/search?q=godfather", {"X-Emby-Token": "tok-ana"})
        self.assertEqual((st, len(d["results"])), (200, 2))
        h = {"Authorization": 'MediaBrowser Client="Jellyfin Web", Device="Chrome", Token="tok-ana"'}
        self.assertEqual(self.call("GET", "/api/search?q=godfather", h)[0], 200)

    def test_request_and_status(self):
        h = {"X-Emby-Token": "tok-ana", "Content-Type": "application/json"}
        st, d = self.call("POST", "/ondemand/api/request", h, {"imdb": "tt0068646"})
        self.assertEqual((st, d["state"]), (202, "ready"))
        st, d2 = self.call("GET", "/ondemand/api/request/tt0068646", h)
        self.assertEqual((st, d2["item_id"]), (200, d["item_id"]))
        self.assertEqual(self.call("GET", "/ondemand/api/request/tt0000001", h)[0], 404)
        self.assertEqual(self.call("POST", "/ondemand/api/request", h, ["tt0068646"])[0], 400)
        self.assertEqual(self.call("POST", "/ondemand/api/request", h, {"imdb": "x"})[0], 400)

    def test_details(self):
        self.assertEqual(self.call("GET", "/ondemand/api/details/tt0068646")[0], 401)
        st, d = self.call("GET", "/ondemand/api/details/tt0068646", {"X-Emby-Token": "tok-ana"})
        self.assertEqual((st, d["original_name"]), (200, "The Godfather"))
        self.assertEqual(self.call("GET", "/api/details/abc", {"X-Emby-Token": "tok-ana"})[0], 400)


if __name__ == "__main__":
    unittest.main()
