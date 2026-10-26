import unittest

from ondemand.http_range import UNSATISFIABLE, parse_range


class TestHttpRange(unittest.TestCase):
    T = 1000

    def test_no_range(self):
        self.assertIsNone(parse_range(None, self.T))
        self.assertIsNone(parse_range("", self.T))

    def test_open_ended(self):
        self.assertEqual(parse_range("bytes=0-", self.T), (0, 999))
        self.assertEqual(parse_range("bytes=500-", self.T), (500, 999))

    def test_closed_and_clamped_at_end(self):
        self.assertEqual(parse_range("bytes=10-19", self.T), (10, 19))
        self.assertEqual(parse_range("bytes=990-99999", self.T), (990, 999))

    def test_suffix(self):
        self.assertEqual(parse_range("bytes=-100", self.T), (900, 999))
        self.assertEqual(parse_range("bytes=-5000", self.T), (0, 999))
        self.assertIs(parse_range("bytes=-0", self.T), UNSATISFIABLE)

    def test_unsatisfiable(self):
        self.assertIs(parse_range("bytes=1000-", self.T), UNSATISFIABLE)
        self.assertIs(parse_range("bytes=5000-6000", self.T), UNSATISFIABLE)

    def test_bad_syntax_is_ignored(self):
        for bad in ("bytes=5-2", "items=0-1", "bytes=abc-", "bytes", "bytes=1"):
            self.assertIsNone(parse_range(bad, self.T), bad)

    def test_multiple_ranges_serves_first(self):
        self.assertEqual(parse_range("bytes=0-9, 20-29", self.T), (0, 9))


if __name__ == "__main__":
    unittest.main()
