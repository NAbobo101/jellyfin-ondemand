import os
import shutil
import tempfile
import unittest
from unittest import mock

from ondemand.movie_filter import build_movies
from ondemand.providers import MovieQuery
from ondemand.providers.registry import build

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSWER = {"results": [
    {"name": "Night of the Living Dead 1968 1080p BluRay x264-GRP", "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
     "seeders": 42, "size": 4000000000, "imdb": "tt0063350", "type": "movie", "added": 1760000000},
    {"name": "Some Show S01E01 1080p WEB h264-GRP", "magnet": "magnet:?xt=urn:btih:" + "b" * 40, "seeders": 9, "type": "tv"},
]}


class TestTheDocumentedExample(unittest.TestCase):
    """docs/writing-a-provider.md walks through examples/json_api_provider.py: it must keep working."""

    def test_loads_as_a_plugin_and_feeds_the_movie_filter(self):
        with tempfile.TemporaryDirectory() as plugins:
            shutil.copy(os.path.join(ROOT, "examples", "json_api_provider.py"), plugins)
            reg = build({"general": {"plugins_dir": plugins},
                         "providers": [{"name": "example-site", "type": "python", "private": False,
                                        "class": "json_api_provider:JsonApiProvider", "url": "https://torrents.example"}]})
            p = reg.by_name["example-site"]
            self.assertEqual(p.capabilities, {"feed", "movie"})
            with mock.patch.object(p.http, "get_json", return_value=ANSWER) as get:
                items, warnings = reg.feed()
                found, _ = reg.search_movie(MovieQuery(imdb="tt0063350"))
            self.assertEqual(warnings, [])
            self.assertEqual(get.call_args_list[1][0][1], {"imdb": "tt0063350"})
            self.assertEqual([(i.kind, i.provider, i.private) for i in items], [("movie", "example-site", False), ("series", "example-site", False)])
            r = build_movies([i.candidate() for i in items], None, {"seeds_min": 1, "public_seeds_min": 2, "freeleech_min": 100,
                                                                  "resolutions": ["1080p"], "max_gb": 40})
            self.assertEqual(list(r["movies"]), ["tt0063350"])
            self.assertEqual(len(found), 2)


if __name__ == "__main__":
    unittest.main()
