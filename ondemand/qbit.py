"""Client for the qBittorrent 4.6 WebUI API v2 (names from that version: `paused`,
`pause`/`resume`, sequential and first/last-piece toggles).

Authentication: the preferred path is the WebUI subnet whitelist with the
container's IP — no stored password. Without a whitelist and with a
username/password, it logs in ONCE and reuses the SID cookie; repeated failed
logins make qBittorrent ban the IP.
"""

import http.cookiejar
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

log = logging.getLogger("qbit")


class QBitError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code          # HTTP status, when qBittorrent answered (404 = torrent gone)


class QBit:
    def __init__(self, base, username=None, password=None, timeout=20):
        self.base = base.rstrip("/")
        self.username, self.password, self.timeout = username, password, timeout
        self._cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._cookies))
        self._login_lock = threading.Lock()

    # ------------------------------------------------------------ transport
    def _req(self, method, path, data=None, body=None, ctype=None, raw=False, tried_login=False):
        url = f"{self.base}/api/v2/{path}"
        headers = {"Referer": self.base}
        if data is not None and body is None:
            body = urllib.parse.urlencode(data).encode()
            ctype = "application/x-www-form-urlencoded"
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                content = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 403 and not tried_login and self.username:
                self._login()
                return self._req(method, path, data, body, ctype, raw, tried_login=True)
            raise QBitError(f"{method} {path}: HTTP {e.code}", e.code) from None
        except (urllib.error.URLError, OSError) as e:
            raise QBitError(f"{method} {path}: {e}") from None
        if raw:
            return content
        if not content:
            return None
        try:
            return json.loads(content)
        except ValueError:
            return content.decode("utf-8", "replace")

    def _login(self):
        with self._login_lock:
            body = urllib.parse.urlencode({"username": self.username, "password": self.password}).encode()
            req = urllib.request.Request(f"{self.base}/api/v2/auth/login", data=body, method="POST",
                                         headers={"Referer": self.base,
                                                  "Content-Type": "application/x-www-form-urlencoded"})
            with self._opener.open(req, timeout=self.timeout) as r:
                if r.read().strip() != b"Ok.":
                    raise QBitError("login refused (wrong password or banned IP)")
            log.info("logged in to the qBittorrent WebUI")

    def _get(self, path, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self._req("GET", path + q)

    def _post(self, path, **data):
        return self._req("POST", path, data=data)

    # ------------------------------------------------------------ reading
    def version(self):
        return self._req("GET", "app/version")

    def info(self, **filters):
        """torrents/info: filters hashes=, tag=, category=, filter=."""
        return self._get("torrents/info", **filters) or []

    def one(self, infohash):
        r = self.info(hashes=infohash)
        return r[0] if r else None

    def files(self, infohash):
        return self._get("torrents/files", hash=infohash) or []

    def piece_states(self, infohash):
        return self._get("torrents/pieceStates", hash=infohash) or []

    def export(self, infohash):
        return self._req("GET", "torrents/export?" + urllib.parse.urlencode({"hash": infohash}), raw=True)

    def categories(self):
        return self._get("torrents/categories") or {}

    def rss_items(self):
        """rss/items with article data: {name: feed | {sub-folder...}}."""
        return self._get("rss/items", withData="true") or {}

    # ------------------------------------------------------------ writing
    def add(self, url=None, torrent=None, **options):
        """torrents/add by URL (qBittorrent itself fetches the .torrent — through its own
        network path, e.g. a VPN) or by the .torrent bytes (multipart). It does not return
        the hash: use a tag."""
        fields = {k: ("true" if v is True else "false" if v is False else str(v)) for k, v in options.items()}
        if url:
            fields["urls"] = url
        if torrent is None:
            r = self._req("POST", "torrents/add", data=fields)
        else:
            boundary = uuid.uuid4().hex
            parts = []
            for k, v in fields.items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; '
                         f'filename="ondemand.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n'.encode()
                         + torrent + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            r = self._req("POST", "torrents/add", body=b"".join(parts),
                          ctype=f"multipart/form-data; boundary={boundary}")
        if isinstance(r, str) and r.strip() == "Fails.":
            raise QBitError("torrents/add: Fails. (duplicate or invalid torrent)")
        return r

    def set_file_priority(self, infohash, indexes, priority):
        """0 = do not download, 1 = normal, 6 = high, 7 = maximum."""
        if indexes:
            self._post("torrents/filePrio", hash=infohash, id="|".join(str(i) for i in indexes),
                       priority=priority)

    def force_start(self, hashes, value=True):
        self._post("torrents/setForceStart", hashes="|".join(hashes), value="true" if value else "false")

    def pause(self, hashes):
        self._post("torrents/pause", hashes="|".join(hashes))

    def set_sequential(self, infohash, on, t=None):
        """The API only has a toggle: check the state (the `t` already read, or read now) before
        toggling. (There is deliberately no `resume` here: after setForceStart it turns force off.)"""
        t = t or self.one(infohash)
        if t is not None and bool(t.get("seq_dl")) != on:
            self._post("torrents/toggleSequentialDownload", hashes=infohash)

    def set_first_last(self, infohash, on, t=None):
        t = t or self.one(infohash)
        if t is not None and bool(t.get("f_l_piece_prio")) != on:
            self._post("torrents/toggleFirstLastPiecePrio", hashes=infohash)

    def limit_download(self, hashes, bytes_per_s):
        """0 = unlimited."""
        self._post("torrents/setDownloadLimit", hashes="|".join(hashes), limit=int(bytes_per_s))

    def delete(self, hashes, with_files=True):
        self._post("torrents/delete", hashes="|".join(hashes),
                   deleteFiles="true" if with_files else "false")

    def ensure_category(self, name, path):
        cats = self.categories()
        if name not in cats:
            self._post("torrents/createCategory", category=name, savePath=path)
        elif cats[name].get("savePath", "").rstrip("/") != path.rstrip("/"):
            self._post("torrents/editCategory", category=name, savePath=path)
