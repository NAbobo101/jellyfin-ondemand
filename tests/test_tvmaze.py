import json
import os
import tempfile
import unittest
from unittest import mock

from ondemand.tvmaze import TVmaze

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
            self.assertEqual(tv.episodes(10), [(1, 2, "2020-01-08")])
            self.assertEqual(tv.requests, 0)
            tv.save()
            self.assertLess(os.path.getsize(path), before / 5)

    def test_new_answer_comes_in_slim(self):
        tv = TVmaze(pause=0)

        class Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s, *a): return json.dumps([EP]).encode()
        with mock.patch("ondemand.tvmaze.urllib.request.urlopen", lambda req, timeout=None: Resp()):
            self.assertEqual(tv.episodes(7), [(1, 2, "2020-01-08")])
        self.assertEqual(tv.cache["shows/7/episodes?"], [{"season": 1, "number": 2, "airdate": "2020-01-08"}])


if __name__ == "__main__":
    unittest.main()
