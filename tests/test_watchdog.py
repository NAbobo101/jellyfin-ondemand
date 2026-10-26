import os
import tempfile
import unittest
from unittest import mock

from tools.transcode_watchdog import Watchdog


class TestWatchdog(unittest.TestCase):
    def test_kills_only_software_transcodes_of_the_gateway(self):
        with tempfile.TemporaryDirectory() as d:
            w = Watchdog("http://ondemand:8097", d)
            cmds = {1: b"ffmpeg\0-i\0http://ondemand:8097/v/abc\0-c:v\0libx264\0out",
                    2: b"ffmpeg\0-i\0http://ondemand:8097/v/def\0-c:v\0h264_vaapi\0out",
                    3: b"ffmpeg\0-i\0/media/movie.mkv\0-c:v\0libx264\0out"}
            real_open = open

            def fake_open(path, *a, **kw):
                if str(path).startswith("/proc/") and str(path).endswith("/cmdline"):
                    import io
                    return io.BytesIO(cmds[int(str(path).split("/")[2])])
                return real_open(path, *a, **kw)
            with mock.patch.object(Watchdog, "ffmpegs", staticmethod(lambda: [1, 2, 3])), \
                    mock.patch("builtins.open", fake_open), mock.patch("os.kill") as kill:
                self.assertEqual(w.check(), 1)
            kill.assert_called_once()
            self.assertEqual(kill.call_args[0][0], 1)
            w.write_prom(3)
            self.assertEqual(w.read_kills(), 3)
            with open(os.path.join(d, "transcode-watchdog.log")) as f:
                self.assertIn("release abc", f.read())


if __name__ == "__main__":
    unittest.main()
