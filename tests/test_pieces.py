import threading
import time
import unittest

from ondemand.pieces import PieceMap, Watcher
from ondemand.qbit import QBitError


class TestPieceMap(unittest.TestCase):
    def test_aligned(self):
        m = PieceMap(4096, 4096, 10000)          # pieces 1..3
        st = [2, 2, 2, 0]
        self.assertEqual((m.first, m.last), (1, 3))
        self.assertEqual(m.available_until(0, st), 8192)
        self.assertEqual(m.available_until(9000, st), 9000)   # piece 3 missing
        self.assertEqual(m.available_until(0, [0, 2, 2, 2]), 10000)

    def test_unaligned(self):
        m = PieceMap(4096, 100, 10000)           # pieces 0..2, shares piece 0 with another file
        self.assertEqual(m.available_until(0, [2, 0, 2]), 3996)
        self.assertEqual(m.available_until(3996, [2, 0, 2]), 3996)
        self.assertEqual(m.available_until(9000, [2, 0, 2]), 10000)  # end of file in the last piece

    def test_no_states_and_out_of_range(self):
        m = PieceMap(4096, 0, 10000)
        self.assertEqual(m.available_until(5, []), 5)
        self.assertEqual(m.available_until(10000, [2, 2, 2]), 10000)
        self.assertAlmostEqual(m.fraction_complete([2, 0, 2]), 2 / 3)


class FakeQBit:
    def __init__(self, n):
        self.states = [0] * n
        self.calls = 0

    def piece_states(self, h):
        self.calls += 1
        return list(self.states)


class TestWatcher(unittest.TestCase):
    def test_wakes_when_the_piece_arrives(self):
        q = FakeQBit(4)
        w = Watcher(q, "a" * 40, interval=0.05)
        m = PieceMap(4096, 0, 4 * 4096)

        def arrives():
            time.sleep(0.3)
            q.states[0] = 2
        threading.Thread(target=arrives, daemon=True).start()
        t = time.monotonic()
        self.assertTrue(w.wait(lambda: m.available_until(0, w.states) > 0, timeout=3))
        self.assertLess(time.monotonic() - t, 2)
        w.stop()

    def test_timeout_and_cancel(self):
        w = Watcher(FakeQBit(2), "b" * 40, interval=0.05)
        self.assertFalse(w.wait(lambda: False, timeout=0.3))
        self.assertFalse(w.wait(lambda: False, timeout=5, cancelled=lambda: True))
        w.stop()

    def test_torrent_that_vanishes_ends_the_watcher(self):
        class GoneQBit:
            def piece_states(self, h):
                raise QBitError("GET torrents/pieceStates?hash=x: HTTP 404", 404)
        w = Watcher(GoneQBit(), "d" * 40, interval=0.05)
        t = time.monotonic()
        self.assertFalse(w.wait(lambda: False, timeout=5))       # does not wait the whole timeout
        self.assertLess(time.monotonic() - t, 2)
        self.assertTrue(w.gone)

    def test_stops_when_complete(self):
        q = FakeQBit(3)
        q.states = [2, 2, 2]
        w = Watcher(q, "c" * 40, interval=0.05)
        time.sleep(0.3)
        self.assertTrue(w.complete)
        n = q.calls
        time.sleep(0.3)
        self.assertEqual(q.calls, n)      # does not keep polling a complete torrent

    def test_complete_by_the_pieces_of_the_served_file(self):
        q = FakeQBit(6)
        q.states = [2, 2, 2, 0, 0, 0]       # video in pieces 0–2; the rest at priority 0
        w = Watcher(q, "e" * 40, interval=0.05, idle=0.05)
        w.interest(0, 2)
        time.sleep(0.3)
        self.assertTrue(w.complete)
        self.assertEqual(w.states, [])      # map released: complete answers for everything
        n = q.calls
        time.sleep(0.3)
        self.assertEqual(q.calls, n)

    def test_stop_releases_waiters(self):
        w = Watcher(FakeQBit(2), "f" * 40, interval=0.05)
        threading.Timer(0.2, w.stop).start()
        t = time.monotonic()
        self.assertFalse(w.wait(lambda: False, timeout=10))
        self.assertLess(time.monotonic() - t, 3)

    def test_reads_slowly_with_nobody_waiting(self):
        q = FakeQBit(2)
        w = Watcher(q, "g" * 40, interval=0.05, idle=30)
        time.sleep(0.4)
        self.assertEqual(q.calls, 1)      # the first read, then only in 30 s
        self.assertFalse(w.wait(lambda: False, timeout=0.4))
        self.assertGreater(q.calls, 2)    # someone waiting: reads every `interval`
        w.stop()


if __name__ == "__main__":
    unittest.main()
