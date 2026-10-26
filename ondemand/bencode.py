"""Bencode (BEP 3) and what to extract from a .torrent.

For the video file, the gateway needs to know at which byte of the torrent's
"continuous stream" it starts: that is what says which piece holds which range.
Pad files (BEP 47) count towards the offsets, but qBittorrent hides them in its
API — so matching against its file list is done by path, not by position.
"""

import hashlib
from dataclasses import dataclass


def _dec(d, i):
    c = d[i:i + 1]
    if c == b"i":
        end = d.index(b"e", i)
        return int(d[i + 1:end]), end + 1
    if c == b"l":
        i, items = i + 1, []
        while d[i:i + 1] != b"e":
            v, i = _dec(d, i)
            items.append(v)
        return items, i + 1
    if c == b"d":
        i, dct = i + 1, {}
        while d[i:i + 1] != b"e":
            k, i = _dec(d, i)
            v, i = _dec(d, i)
            dct[k] = v
        return dct, i + 1
    if c.isdigit():
        colon = d.index(b":", i)
        start = colon + 1
        n = int(d[i:colon])
        if start + n > len(d):
            raise ValueError("bencode string runs past the end")
        return d[start:start + n], start + n
    raise ValueError(f"invalid bencode at position {i}")


def decode(data):
    v, end = _dec(data, 0)
    if end != len(data):
        raise ValueError("trailing data after bencode")
    return v


def _raw_info(data):
    """Exact bytes of the 'info' dictionary (the infohash is their SHA-1)."""
    if data[:1] != b"d":
        raise ValueError(".torrent does not start with a dictionary")
    i = 1
    while data[i:i + 1] != b"e":
        k, i = _dec(data, i)
        start = i
        _, i = _dec(data, i)
        if k == b"info":
            return data[start:i]
    raise ValueError(".torrent without 'info'")


def _txt(b):
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b)


@dataclass
class TorrentFile:
    path: str         # relative, including the torrent's root folder ("Original" layout)
    size: int
    offset: int       # first byte in the torrent's continuous stream
    pad: bool = False


@dataclass
class TorrentMeta:
    infohash: str
    name: str
    piece_size: int
    num_pieces: int
    files: list
    private: bool

    @property
    def real_files(self):
        """Actual files (no pad), in order — the order of qBittorrent's file indexes."""
        return [f for f in self.files if not f.pad]


def read_torrent(data):
    meta = decode(data)
    info = meta[b"info"]
    if b"pieces" not in info:
        raise ValueError("v2-only torrent (no 'pieces') is not supported")
    name = _txt(info.get(b"name.utf-8", info[b"name"]))
    files, off = [], 0
    if b"files" in info:
        for f in info[b"files"]:
            parts = [_txt(p) for p in f.get(b"path.utf-8", f[b"path"])]
            pad = b"p" in f.get(b"attr", b"") or (parts and parts[0] == ".pad")
            files.append(TorrentFile(name + "/" + "/".join(parts), f[b"length"], off, pad))
            off += f[b"length"]
    else:
        files.append(TorrentFile(name, info[b"length"], 0))
    return TorrentMeta(
        infohash=hashlib.sha1(_raw_info(data)).hexdigest(),
        name=name,
        piece_size=info[b"piece length"],
        num_pieces=len(info[b"pieces"]) // 20,
        files=files,
        private=info.get(b"private") == 1,
    )


def encode(v):
    """Inverse of decode (used by the tests to build synthetic .torrent files)."""
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(encode(x) for x in v) + b"e"
    if isinstance(v, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, x) for k, x in v.items())
        return b"d" + b"".join(encode(k) + encode(x) for k, x in items) + b"e"
    raise TypeError(type(v))
