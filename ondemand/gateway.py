"""HTTP server that hands the media to Jellyfin while the torrent downloads.

Each title's .strm points to <gateway public_url>/v/<id>. The reader is
Jellyfin's ffprobe/ffmpeg (ForceRemoteSourceTranscoding stops the device from
opening the URL directly). Rules that come from Jellyfin 10.11's behaviour:

- The PlaybackInfo ffprobe has no timeout, and the Android TV app gives up after
  30 s: the first byte has `first_byte_wait` seconds to arrive; otherwise 503.
- 503 only BEFORE the headers. Once the body has started, closing the
  connection becomes an I/O error in ffmpeg (it does not reconnect) and the
  video drops. In the middle of the body we wait for the piece (up to
  `stall_wait`) and only then give up.
- HTTP/1.0 + Connection: close: ffmpeg reopens the connection on every seek.
- A client that goes away while we wait for a piece is detected (otherwise the
  threads pile up on every seek).
"""

import http.server
import ipaddress
import logging
import re
import select
import socket
import threading
import time
from socketserver import ThreadingMixIn

from .http_range import UNSATISFIABLE, parse_range
from .sources import Unavailable

log = logging.getLogger("gateway")

CHUNK = 1 << 20          # bytes per sendfile call between two piece checks
VIDEO_ROUTE = re.compile(r"^/v/([A-Za-z0-9_-]{1,64})$")


class NotFound(Exception):
    pass


class AccessList:
    """Who may call a route: IP addresses, CIDR networks or host names (a compose service
    name such as "jellyfin" is resolved again every `ttl` seconds — container IPs change)."""

    def __init__(self, entries, ttl=30.0):
        self.nets, self.names, self.ttl = [], [], ttl
        for e in entries or ():
            try:
                self.nets.append(ipaddress.ip_network(str(e), strict=False))
            except ValueError:
                self.names.append(str(e))
        # None, not 0.0: time.monotonic() counts from boot, so on a machine up for less than `ttl`
        # seconds (a fresh CI runner) "now - 0.0 > ttl" was false and the names were never resolved.
        self._resolved, self._at = set(), None
        self._lock = threading.Lock()

    def _names(self):
        with self._lock:
            if self.names and (self._at is None or time.monotonic() - self._at > self.ttl):
                found = set()
                for n in self.names:
                    try:
                        found.update(socket.gethostbyname_ex(n)[2])
                    except OSError:
                        pass
                self._resolved, self._at = found, time.monotonic()
            return self._resolved

    def __contains__(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if any(addr in n for n in self.nets):
            return True
        return str(addr) in self._names()


class Metrics:
    """Counters and gauges in the Prometheus text format.

    collectors: functions called on every /metrics to refresh gauges (current state);
    files: .prom texts from outside the process (tools/transcode_watchdog.py runs from cron)."""

    PREFIX = "ondemand_"

    def __init__(self):
        self._lock = threading.Lock()
        self.counters = {}
        self.gauges = {}
        self.collectors = []
        self.files = []

    def inc(self, name, v=1, **labels):
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + v

    def zero(self, name, **labels):
        """The counter exists from the start at 0: an increase() alert needs no `or vector(0)`."""
        self.inc(name, 0, **labels)

    def set(self, name, v, **labels):
        with self._lock:
            self.gauges[(name, tuple(sorted(labels.items())))] = v

    def clear(self, name):
        """Drop the `name` gauges (a label that no longer exists does not keep a stale value)."""
        with self._lock:
            self.gauges = {k: v for k, v in self.gauges.items() if k[0] != name}

    def text(self):
        for collect in self.collectors:
            try:
                collect(self)
            except Exception as e:
                log.warning("metrics collector failed: %s", e)
        lines = []
        with self._lock:
            items = list(self.counters.items()) + list(self.gauges.items())
        for (name, labels), v in sorted(items):
            r = ",".join(f'{k}="{val}"' for k, val in labels)
            lines.append(f"{self.PREFIX}{name}{{{r}}} {v}" if r else f"{self.PREFIX}{name} {v}")
        for path in self.files:
            try:
                with open(path, encoding="utf-8") as f:
                    lines += [l.rstrip("\n") for l in f if l.startswith(self.PREFIX)]
            except OSError:
                pass
        return "\n".join(lines) + "\n"


class Gateway(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, resolver, video_clients, admin_clients, first_byte_wait=25.0,
                 stall_wait=900.0, metrics=None, extras=None, on_failure=None):
        """resolver(id, request) -> Source (may block while starting the torrent);
        raises NotFound (404) or Unavailable (503).
        video_clients / admin_clients: AccessList entries (IP, CIDR or host name).
        extras: {path: function(handler)} for admin routes.
        on_failure(id): called when a request ends in 503 (no data in time)."""
        super().__init__(address, Handler)
        self.resolver = resolver
        self.on_failure = on_failure or (lambda ident: None)
        self.video_clients = video_clients if isinstance(video_clients, AccessList) else AccessList(video_clients)
        self.admin_clients = admin_clients if isinstance(admin_clients, AccessList) else AccessList(admin_clients)
        self.first_byte_wait = first_byte_wait
        self.stall_wait = stall_wait
        self.metrics = metrics or Metrics()
        self.extras = extras or {}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "jellyfin-ondemand"
    sys_version = ""
    timeout = 30      # only for reading the request line; sending runs without a timeout

    def log_message(self, fmt, *args):   # silence the default log (we write our own)
        pass

    def do_HEAD(self):
        self._route(head=True)

    def do_GET(self):
        self._route(head=False)

    # ---------------------------------------------------------------- routing
    def _route(self, head):
        ip = self.client_address[0]
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._text(200, "ok\n")
        if path == "/metrics":
            if ip not in self.server.admin_clients:
                return self._text(403, "forbidden\n")
            return self._text(200, self.server.metrics.text(), "text/plain; version=0.0.4")
        if path in self.server.extras:
            if ip not in self.server.admin_clients:
                return self._text(403, "forbidden\n")
            return self.server.extras[path](self)
        m = VIDEO_ROUTE.match(path)
        if not m:
            return self._text(404, "nothing here\n")
        if ip not in self.server.video_clients:
            log.warning("request from %s refused (not in video_clients)", ip)
            return self._text(403, "forbidden\n")
        self._video(m.group(1), head)

    def _text(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ---------------------------------------------------------------- video
    def _client_gone(self):
        """True if the other side closed the connection (readable EOF on the socket)."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if ready:
                return self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True
        return False

    def _video(self, ident, head):
        srv = self.server
        t0 = time.monotonic()
        rng_txt = self.headers.get("Range", "")
        ua = self.headers.get("User-Agent", "")
        result, source, fh = "?", None, None
        self._sent = 0
        self.wait = None          # the resolver may adjust it (a probe waits little, a play waits long)
        self.started = t0         # the resolver subtracts the start-up time from the first-byte wait
        log.info("-> %s %s range=%s ua=%r", self.command, ident, rng_txt or "-", ua[:40])
        try:
            try:
                source = srv.resolver(ident, self)
            except NotFound:
                result = "404"
                return self._text(404, "unknown title\n")
            except Unavailable as e:
                result = "503"
                log.warning("%s unavailable: %s", ident, e)
                if not head:            # HEAD is not a probe: does not block the release
                    srv.on_failure(ident)
                return self._text(503, f"unavailable: {e}\n")

            size = source.size
            rng = parse_range(rng_txt, size)
            if rng is UNSATISFIABLE:
                result = "416"
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            start, end = rng if rng else (0, size - 1)

            # First byte BEFORE the headers: a 503 is still possible.
            wait = self.wait if self.wait is not None else srv.first_byte_wait
            if not head and not source.wait_for(start, wait, self._client_gone):
                if self._client_gone():
                    result = "client-gone"
                    return
                result = "503-wait"
                srv.metrics.inc("first_byte_timeouts_total")
                srv.on_failure(ident)
                return self._text(503, "the torrent did not deliver in time\n")

            if not head:
                # Open BEFORE the headers: a missing file becomes a 503; after the 206 we could only
                # cut the body, and ffmpeg does not reconnect (I/O error in the player).
                try:
                    fh = source.open()
                except Unavailable as e:
                    result = "503-file"
                    log.warning("%s: %s", ident, e)
                    srv.on_failure(ident)
                    return self._text(503, f"unavailable: {e}\n")

            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", source.mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Connection", "close")
            self.end_headers()
            if head:
                result = "head"
                return

            self.connection.settimeout(None)   # blocking send (real sendfile)
            source.reader_started()
            try:
                result = self._send(source, fh, start, end)
            finally:
                source.reader_finished()
        except (BrokenPipeError, ConnectionResetError):
            result = "client-gone"
        finally:
            if fh is not None:
                fh.close()
            dur = time.monotonic() - t0
            srv.metrics.inc("bytes_sent_total", self._sent)
            srv.metrics.inc("requests_total", result=result.split("-")[0])
            log.info("%s %s %s range=%s ua=%r -> %s, %.1f MB in %.1fs",
                     self.client_address[0], self.command, ident, rng_txt or "-", ua[:60],
                     result, self._sent / 1e6, dur)

    def _send(self, source, fh, start, end):
        """Send [start, end] as the pieces arrive. self._sent tracks the progress so the
        log is right even if the client closes midway."""
        srv = self.server
        pos = start
        short = 0
        while pos <= end:
            until = min(source.available_until(pos), end + 1)
            if until <= pos:
                srv.metrics.inc("piece_waits_total")
                if not source.wait_for(pos, srv.stall_wait, self._client_gone):
                    return "client-gone" if self._client_gone() else "stalled"
                continue
            while pos < until:
                n = self.connection.sendfile(fh, pos, min(CHUNK, until - pos))
                if n == 0:
                    # sendfile 0 = end of the file ON DISK (piece marked but not written yet, a
                    # preallocated file shorter than expected): wait and retry, like a missing piece.
                    if self._client_gone():
                        return "client-gone"
                    short += 1
                    if short * 0.5 > srv.stall_wait:
                        return "short-file"
                    time.sleep(0.5)
                    continue
                short = 0
                pos += n
                self._sent = pos - start
        return "ok"
