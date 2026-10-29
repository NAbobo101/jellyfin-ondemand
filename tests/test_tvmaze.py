import json
import os
import tempfile
import unittest
from unittest import mock

from ondemand.tvmaze import VALIDITY, WHEN, TVmaze, TVmazeError

EP = {"id": 1, "season": 1, "number": 2, "airdate": "2020-01-08", "name": "Pilot",
      "summary": "<p>long</p>" * 50, "image": {"medium": "x"}, "_links": {"self": {"href": "y"}}}
SPECIAL = dict(EP, number=None)


class TestSlimCache(unittest.TestCase):
    def test_old_cache_is_slimmed_on_open_without_changing_episodes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cache.json")
            show = {"id": 10, "name": "X", "summary": "stays"}
            with open(path, "w") as f:
                json.dump({"shows/10/episodes?": [EP, SPECIAL], "lookup/shows?imdb=tt1": show}, f)
            before = os.path.getsize(path)
            tv = TVmaze(cache_file=path, pause=0)
            self.assertEqual(tv.cache["shows/10/episodes?"][0], {"season": 1, "number": 2, "airdate": "2020-01-08"})
            self.assertEqual(tv.cache["lookup/shows?imdb=tt1"], show)      # whole show: summary() reads several fields
            self.assertEqual(tv.by_imdb("tt1"), show)
            self.assertEqual(tv.requests, 0)
            with mock.patch.object(tv, "_fetch", side_effect=TVmazeError("down")):
                self.assertEqual(tv.episodes(10), [(1, 2, "2020-01-08")])    # the old list expired; down = the old one
            tv.save()
            self.assertLess(os.path.getsize(path), before / 4)               # (the request times go along)

    def test_new_answer_comes_in_slim(self):
        tv = TVmaze(pause=0)

        class Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s, *a): return json.dumps([EP]).encode()
        with mock.patch("ondemand.tvmaze.urllib.request.urlopen", lambda req, timeout=None: Resp()):
            self.assertEqual(tv.episodes(7), [(1, 2, "2020-01-08")])
        self.assertEqual(tv.cache["shows/7/episodes?"], [{"season": 1, "number": 2, "airdate": "2020-01-08"}])


class Answer:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return json.dumps(self.data).encode()


class TestValidity(unittest.TestCase):
    """A cache that never expires freezes the episode list: a new season never gets past the filter."""

    def test_an_expired_list_is_requested_again_and_the_times_go_to_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cache.json")
            tv = TVmaze(cache_file=path, pause=0)
            answers = [[EP], [EP, dict(EP, season=2, number=1, airdate="2026-10-20")]]
            with mock.patch("ondemand.tvmaze.urllib.request.urlopen", side_effect=lambda req, timeout=None: Answer(answers.pop(0))):
                self.assertEqual(len(tv.episodes(7)), 1)
                self.assertEqual(len(tv.episodes(7)), 1)                 # still valid: cache
                tv.when["shows/7/episodes?"] -= VALIDITY["episodes"] + 1
                self.assertEqual(tv.episodes(7)[-1], (2, 1, "2026-10-20"))
            self.assertEqual(tv.requests, 2)
            tv.save()
            with open(path) as f:
                self.assertIn("shows/7/episodes?", json.load(f)[WHEN])
            self.assertEqual(TVmaze(cache_file=path).episodes(7)[-1], (2, 1, "2026-10-20"))

    def test_an_empty_answer_expires_sooner_and_an_unused_one_leaves_on_save(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cache.json")
            tv = TVmaze(cache_file=path, pause=0)
            tv.cache = {"search/shows?q=a": [], "search/shows?q=b": [{"show": {}}], "search/shows?q=old": [{"show": {}}]}
            now = time.time()
            tv.when = {"search/shows?q=a": now - VALIDITY["empty"] - 1, "search/shows?q=b": now - VALIDITY["empty"] - 1,
                       "search/shows?q=old": now - 91 * 86400}
            self.assertTrue(tv._expired("search/shows?q=a"))
            self.assertFalse(tv._expired("search/shows?q=b"))
            tv.save()
            self.assertEqual(sorted(TVmaze(cache_file=path).cache), ["search/shows?q=a", "search/shows?q=b"])


if __name__ == "__main__":
    unittest.main()
