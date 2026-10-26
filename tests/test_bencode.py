import hashlib
import unittest

from ondemand.bencode import decode, encode, read_torrent


def multi_file_torrent(private=True):
    info = {
        "name": "Movie.2026.1080p.WEB-DL",
        "piece length": 4096,
        "pieces": b"\x00" * 20 * 4,
        "files": [
            {"length": 100, "path": ["sample.mkv"]},
            {"length": 3996, "path": [".pad", "3996"], "attr": "p"},
            {"length": 10000, "path": ["Movie.2026.1080p.WEB-DL.mkv"]},
            {"length": 500, "path": ["Subs", "pt-BR.srt"]},
        ],
    }
    if private:
        info["private"] = 1
    return encode({"announce": "https://tracker.example/announce/PASSKEY", "info": info}), info


class TestBencode(unittest.TestCase):
    def test_round_trip(self):
        v = {"a": 1, "b": [b"x", 2, {"c": b""}], "n": -5}
        self.assertEqual(decode(encode(v)), {b"a": 1, b"b": [b"x", 2, {b"c": b""}], b"n": -5})

    def test_garbage_and_truncated(self):
        with self.assertRaises(ValueError):
            decode(b"i1eXYZ")
        with self.assertRaises(ValueError):
            decode(b"10:abc")

    def test_multi_with_pad(self):
        data, info = multi_file_torrent()
        m = read_torrent(data)
        self.assertEqual(m.infohash, hashlib.sha1(encode(info)).hexdigest())
        self.assertEqual((m.piece_size, m.num_pieces, m.private), (4096, 4, True))
        self.assertEqual([(f.path, f.offset, f.pad) for f in m.files], [
            ("Movie.2026.1080p.WEB-DL/sample.mkv", 0, False),
            ("Movie.2026.1080p.WEB-DL/.pad/3996", 100, True),
            ("Movie.2026.1080p.WEB-DL/Movie.2026.1080p.WEB-DL.mkv", 4096, False),
            ("Movie.2026.1080p.WEB-DL/Subs/pt-BR.srt", 14096, False),
        ])
        self.assertEqual(len(m.real_files), 3)

    def test_single_file_public(self):
        info = {"name": "movie.mkv", "piece length": 1 << 20, "pieces": b"\x01" * 40, "length": 1500000}
        m = read_torrent(encode({"info": info}))
        self.assertFalse(m.private)
        self.assertEqual([(f.path, f.size, f.offset) for f in m.files], [("movie.mkv", 1500000, 0)])


if __name__ == "__main__":
    unittest.main()
