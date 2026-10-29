import http.client
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from ondemand.gateway import AccessList, Gateway, NotFound
from ondemand.sources import FileSource, SimulatedSource, Unavailable

SIZE = 3_000_000


class GatewayBase(unittest.TestCase):
    video_clients = ("127.0.0.1",)

    @classmethod
    def setUpClass(cls):
        fd, cls.file = tempfile.mkstemp(suffix=".mkv")
        cls.data = os.urandom(SIZE)
        with os.fdopen(fd, "wb") as f:
            f.write(cls.data)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.file)

    def setUp(self):
        self.source = FileSource(self.file)
        self.srv = Gateway(("127.0.0.1", 0), self.resolver, self.video_clients, ("127.0.0.1",),
                           first_byte_wait=1.0, stall_wait=1.0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def resolver(self, ident, request):
        if ident == "gone":
            raise NotFound()
        if ident == "dead":
            raise Unavailable("no seeders")
        return self.source

    def get(self, path, rng=None, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request(method, path, headers={"Range": rng} if rng else {})
        r = c.getresponse()
        return r, r.read() if method == "GET" else b""


class TestGateway(GatewayBase):
    def test_whole_file(self):
        r, body = self.get("/v/abc")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Accept-Ranges"), "bytes")
        self.assertEqual(r.getheader("Content-Type"), "video/x-matroska")
        self.assertEqual(body, self.data)

    def test_range(self):
        r, body = self.get("/v/abc", "bytes=1000-1999")
        self.assertEqual(r.status, 206)
        self.assertEqual(r.getheader("Content-Range"), f"bytes 1000-1999/{SIZE}")
        self.assertEqual(body, self.data[1000:2000])

    def test_suffix_and_head(self):
        r, body = self.get("/v/abc", "bytes=-64")
        self.assertEqual(body, self.data[-64:])
        r, _ = self.get("/v/abc", method="HEAD")
        self.assertEqual(r.getheader("Content-Length"), str(SIZE))

    def test_416(self):
        r, _ = self.get("/v/abc", f"bytes={SIZE}-")
        self.assertEqual(r.status, 416)
        self.assertEqual(r.getheader("Content-Range"), f"bytes */{SIZE}")

    def test_404_503_and_unknown_route(self):
        self.assertEqual(self.get("/v/gone")[0].status, 404)
        self.assertEqual(self.get("/v/dead")[0].status, 503)
        self.assertEqual(self.get("/other/thing")[0].status, 404)

    def test_missing_file_becomes_503_before_the_headers(self):
        class NoFile(FileSource):
            def open(self):
                raise Unavailable("the file does not exist on disk yet")
        self.source = NoFile(self.file)
        failures = []
        self.srv.on_failure = failures.append
        r, body = self.get("/v/abc", "bytes=0-99")
        self.assertEqual(r.status, 503)
        self.assertEqual(failures, ["abc"])

    def test_unavailable_head_does_not_block_the_release(self):
        failures = []
        self.srv.on_failure = failures.append
        self.assertEqual(self.get("/v/dead", method="HEAD")[0].status, 503)
        self.assertEqual(failures, [])

    def test_metrics_and_health(self):
        self.get("/v/abc", "bytes=0-9")
        r, body = self.get("/metrics")
        self.assertEqual(r.status, 200)
        self.assertIn(b"ondemand_bytes_sent_total", body)
        self.assertEqual(self.get("/health")[1], b"ok\n")


class TestWaiting(GatewayBase):
    def test_waits_for_the_first_byte(self):
        self.source = SimulatedSource(self.file, delay_s=0.6, rate=50_000_000, edge=0)
        t = time.monotonic()
        r, body = self.get("/v/abc", "bytes=0-99")
        self.assertGreaterEqual(time.monotonic() - t, 0.5)
        self.assertEqual((r.status, body), (206, self.data[:100]))

    def test_503_if_it_does_not_arrive_in_time(self):
        self.source = SimulatedSource(self.file, delay_s=30, edge=0)
        self.assertEqual(self.get("/v/abc", "bytes=0-99")[0].status, 503)

    def test_last_slice_early(self):
        self.source = SimulatedSource(self.file, delay_s=0, rate=1, edge=1000)
        r, body = self.get("/v/abc", f"bytes={SIZE - 500}-")
        self.assertEqual(body, self.data[-500:])

    def test_hole_in_the_middle_cuts_after_the_timeout(self):
        # The body has started: no 503 any more; wait for the piece and give up at the deadline.
        self.source = SimulatedSource(self.file, delay_s=0, rate=10**9, edge=0,
                                      holes=[(2_000_000, 2_100_000)])
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("GET", "/v/abc", headers={"Range": "bytes=1000000-2999999"})
        r = c.getresponse()
        self.assertEqual(r.status, 206)
        with self.assertRaises(http.client.IncompleteRead) as e:
            r.read()
        self.assertEqual(len(e.exception.partial), 1_000_000)

    def test_client_that_leaves_frees_the_thread(self):
        self.srv.stall_wait = 30
        self.source = SimulatedSource(self.file, delay_s=0, rate=10**9, edge=0, holes=[(1000, SIZE)])
        s = socket.create_connection(("127.0.0.1", self.srv.server_address[1]))
        s.sendall(b"GET /v/abc HTTP/1.0\r\nRange: bytes=0-\r\n\r\n")
        s.recv(65536)
        s.close()
        for _ in range(40):           # the wait checks for disconnection every 0.5 s
            if self.source.readers == 0:
                break
            time.sleep(0.1)
        time.sleep(1.0)
        self.assertEqual(self.source.readers, 0)


class TestAccess(GatewayBase):
    video_clients = ("10.99.0.6",)

    def test_ip_not_in_the_list(self):
        self.assertEqual(self.get("/v/abc")[0].status, 403)


class TestAccessList(unittest.TestCase):
    def test_ip_cidr_and_name(self):
        # the name is resolved through a fake DNS: a real lookup of "localhost" is not reliable on CI runners
        dns = {"jellyfin": ("jellyfin", [], ["10.9.0.6"])}
        acl = AccessList(["10.0.0.5", "172.20.0.0/16", "jellyfin"])
        with mock.patch("ondemand.gateway.socket.gethostbyname_ex", side_effect=lambda n: dns[n]):
            self.assertIn("10.0.0.5", acl)
            self.assertIn("172.20.3.4", acl)
            self.assertIn("10.9.0.6", acl)                   # resolved from the host name
            self.assertIn("::ffff:10.0.0.5", acl)            # IPv4-mapped IPv6
            self.assertNotIn("10.0.0.6", acl)
            self.assertNotIn("not-an-ip", acl)

    def test_names_resolve_right_after_boot(self):
        dns = {"jellyfin": ("jellyfin", [], ["10.9.0.6"])}
        acl = AccessList(["jellyfin"])
        with mock.patch("ondemand.gateway.socket.gethostbyname_ex", side_effect=lambda n: dns[n]), \
                mock.patch("ondemand.gateway.time.monotonic", return_value=5.0):       # machine up for 5 s
            self.assertIn("10.9.0.6", acl)

    def test_a_name_that_does_not_resolve_lets_nobody_in(self):
        acl = AccessList(["jellyfin"])
        with mock.patch("ondemand.gateway.socket.gethostbyname_ex", side_effect=OSError("no such host")):
            self.assertNotIn("10.9.0.6", acl)


if __name__ == "__main__":
    unittest.main()
