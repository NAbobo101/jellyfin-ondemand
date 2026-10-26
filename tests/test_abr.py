import http.client
import json
import os
import threading
import unittest

from ondemand.search import SearchAPI
from tests.test_search import SearchBase

ITEM = "aaa21d2f6870ec2f501b705d0a510e50"


class TestABR(SearchBase):
    def setUp(self):
        super().setUp()
        self.abr_root = os.path.join(self.tmp.name, "abr")
        d = os.path.join(self.abr_root, ITEM, "v0")
        os.makedirs(d)
        with open(os.path.join(self.abr_root, ITEM, "master.m3u8"), "w") as f:
            f.write("#EXTM3U\nv0/index.m3u8\n")
        with open(os.path.join(d, "init_0.mp4"), "wb") as f:      # the name ffmpeg gives with var_stream_map
            f.write(b"\x00" * 1000)
        with open(os.path.join(d, "seg00000.m4s"), "wb") as f:
            f.write(b"\x00" * (600 * 1024))          # larger than one read chunk
        self.srv = SearchAPI(("127.0.0.1", 0), self.s, abr_root=self.abr_root)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def get(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        return r.status, r.getheader("Content-Type"), r.read()

    def test_available_needs_a_token_header(self):
        self.assertEqual(self.get(f"/ondemand/api/abr/{ITEM}")[0], 401)
        st, _, body = self.get(f"/ondemand/api/abr/{ITEM}", {"X-Emby-Token": "tok-ana"})
        self.assertEqual((st, json.loads(body)), (200, {"available": True}))
        st, _, body = self.get("/api/abr/" + "0" * 32, {"X-Emby-Token": "tok-ana"})
        self.assertEqual(json.loads(body), {"available": False})

    def test_lists_only_what_is_ready(self):
        os.makedirs(os.path.join(self.abr_root, ITEM + ".tmp"))          # a generation in progress
        os.makedirs(os.path.join(self.abr_root, "b" * 32))               # no master.m3u8
        self.assertEqual(self.get("/ondemand/api/abr")[0], 401)
        st, _, body = self.get("/ondemand/api/abr", {"X-Emby-Token": "tok-ana"})
        self.assertEqual((st, json.loads(body)), (200, {"items": [ITEM]}))

    def test_files_with_the_token_in_the_path(self):
        st, ctype, body = self.get(f"/ondemand/abr/t/tok-ana/{ITEM}/master.m3u8")
        self.assertEqual((st, ctype, body[:7]), (200, "application/vnd.apple.mpegurl", b"#EXTM3U"))
        st, ctype, body = self.get(f"/abr/t/tok-ana/{ITEM}/v0/seg00000.m4s")   # without the prefix too
        self.assertEqual((st, ctype, len(body)), (200, "video/iso.segment", 600 * 1024))
        st, ctype, body = self.get(f"/ondemand/abr/t/tok-ana/{ITEM}/v0/init_0.mp4")
        self.assertEqual((st, ctype, len(body)), (200, "video/mp4", 1000))

    def test_invalid_token_or_odd_file(self):
        self.assertEqual(self.get(f"/ondemand/abr/t/fake/{ITEM}/master.m3u8")[0], 401)
        self.assertEqual(self.get(f"/ondemand/abr/t/tok-ana/{ITEM}/v0/seg00001.m4s")[0], 404)
        # nothing outside the route is served: without a header it is 401, with a token 404
        self.assertEqual(self.get(f"/ondemand/abr/t/tok-ana/{ITEM}/../../t.db")[0], 401)
        self.assertEqual(self.get(f"/ondemand/abr/t/tok-ana/{ITEM}/../../t.db", {"X-Emby-Token": "tok-ana"})[0], 404)
        self.assertEqual(self.get(f"/ondemand/abr/t/tok-ana/{ITEM}/v9/index.m3u8")[0], 401)

    def test_abr_off_serves_nothing(self):
        self.srv.abr_root = ""
        st, _, body = self.get("/ondemand/api/abr", {"X-Emby-Token": "tok-ana"})
        self.assertEqual(json.loads(body), {"items": []})
        self.assertEqual(self.get(f"/ondemand/abr/t/tok-ana/{ITEM}/master.m3u8")[0], 404)


if __name__ == "__main__":
    unittest.main()
