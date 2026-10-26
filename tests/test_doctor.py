import os
import tempfile
import unittest

from ondemand import doctor
from ondemand.catalog import MARKER
from ondemand.jellyfin import JellyfinError
from ondemand.qbit import QBitError


class FakeJellyfin:
    def __init__(self):
        self.answers = {
            "/System/Info": {"Version": "10.11.11"},
            "/Library/VirtualFolders": [],
            "/Users": [{"Name": "admin", "Policy": {"ForceRemoteSourceTranscoding": True}}],
            "/Plugins": [{"Id": "f5a34f7b-2e8a-4e6a-a722-3a216a81b374", "Name": "JavaScript Injector"}],
            f"/Plugins/{doctor.INJECTOR_ID}/Configuration": {"CustomJavaScripts": [
                {"Name": "On demand: search", "Script": "window.__ondemandSearch = true", "Enabled": True}]},
        }
        self.error = None

    def _req(self, method, path, *a, **kw):
        if self.error:
            raise self.error
        return self.answers[path]


class FakeQBit:
    def __init__(self, version="v4.6.7", error=None):
        self.v, self.error = version, error

    def version(self):
        if self.error:
            raise self.error
        return self.v


class DoctorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        for d in ("data", "catalog", "downloads/ondemand"):
            os.makedirs(os.path.join(t, d))
        open(os.path.join(t, "catalog", MARKER), "w").close()
        self.config = os.path.join(t, "config.toml")
        self.secrets = os.path.join(t, "secrets.toml")
        self.write_config()
        self.write_secrets('[jellyfin]\napi_key = "k"\n')
        self.jf, self.qb = FakeJellyfin(), FakeQBit()
        self.jf.answers["/Library/VirtualFolders"] = [
            {"Name": "Movies", "Locations": [f"{t}/catalog/Movies"], "LibraryOptions": {"EnableRealtimeMonitor": False}}]

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, extra=""):
        t = self.tmp.name
        with open(self.config, "w") as f:
            f.write(f'''[general]
database = "{t}/data/ondemand.db"
plugins_dir = ""
[catalog]
root = "{t}/catalog"
movies_4k = ""
series = ""
[qbittorrent]
save_path = "/downloads/ondemand"
path_map = {{ "/downloads" = "{t}/downloads" }}
[[providers]]
name = "ia"
type = "archive_org"
''' + extra)

    def write_secrets(self, text, mode=0o600):
        with open(self.secrets, "w") as f:
            f.write(text)
        os.chmod(self.secrets, mode)

    def run_doctor(self):
        return doctor.run(self.config, self.secrets, jf=self.jf, qb=self.qb)

    def lines(self, r, level=None, area=None):
        return [m for lv, a, m, _ in r.lines if (level is None or lv == level) and (area is None or a == area)]


class TestDoctor(DoctorBase):
    def test_a_good_setup_has_no_failure(self):
        r = self.run_doctor()
        self.assertFalse(r.failed, r.text())
        self.assertEqual(self.lines(r, "WARN"), [])
        self.assertIn("0 failure(s)", r.text())

    def test_missing_files(self):
        os.unlink(self.secrets)
        r = self.run_doctor()
        self.assertTrue(self.lines(r, "FAIL", "secrets"))
        self.assertTrue(self.lines(r, "FAIL", "jellyfin"))           # no key, no Jellyfin check
        r = doctor.run(self.config + ".nope", self.secrets)
        self.assertEqual([a for _, a, *_ in r.lines], ["config"])

    def test_secrets_readable_by_others_and_unknown_keys(self):
        self.write_secrets('[jellyfin]\napi_key = "k"\n', 0o644)
        self.write_config("[catalog]\nmovis = 'x'\n".replace("[catalog]", "[search]"))
        r = self.run_doctor()
        self.assertTrue(any("chmod 600" in (fix or "") for *_, fix in r.lines))
        self.assertTrue(any("movis" in m for m in self.lines(r, "WARN", "config")))

    def test_marker_missing(self):
        os.unlink(os.path.join(self.tmp.name, "catalog", MARKER))
        self.assertTrue(any(MARKER in m for m in self.lines(self.run_doctor(), "FAIL", "catalog")))

    def test_jellyfin_key_refused_and_unreachable(self):
        self.jf.error = JellyfinError("GET /System/Info: HTTP 401", 401)
        self.assertTrue(any("refused" in m for m in self.lines(self.run_doctor(), "FAIL", "jellyfin")))
        self.jf.error = JellyfinError("GET /System/Info: timed out")
        self.assertTrue(any("not reachable" in m for m in self.lines(self.run_doctor(), "FAIL", "jellyfin")))

    def test_library_user_and_web_checks(self):
        self.jf.answers["/Library/VirtualFolders"] = []
        self.jf.answers["/Users"].append({"Name": "kid", "Policy": {}})
        self.jf.answers[f"/Plugins/{doctor.INJECTOR_ID}/Configuration"] = {"CustomJavaScripts": []}
        r = self.run_doctor()
        self.assertTrue(any("in no library" in m for m in self.lines(r, "FAIL", "libraries")))
        self.assertTrue(any("kid" in m for m in self.lines(r, "FAIL", "users")))
        self.assertTrue(self.lines(r, "WARN", "web"))

    def test_realtime_monitor_is_a_warning(self):
        self.jf.answers["/Library/VirtualFolders"][0]["LibraryOptions"]["EnableRealtimeMonitor"] = True
        self.assertTrue(any("monitoring ON" in m for m in self.lines(self.run_doctor(), "WARN", "libraries")))

    def test_qbittorrent_version_refusal_and_path_map(self):
        self.qb = FakeQBit("v5.0.1")
        self.assertTrue(any("only 4.x" in m for m in self.lines(self.run_doctor(), "FAIL", "qbittorrent")))
        self.qb = FakeQBit(error=QBitError("GET app/version: HTTP 403", 403))
        self.assertTrue(any("refused" in m for m in self.lines(self.run_doctor(), "FAIL", "qbittorrent")))
        self.qb = FakeQBit()
        # a save_path outside the map
        with open(self.config) as f:
            text = f.read().replace('save_path = "/downloads/ondemand"', 'save_path = "/elsewhere"')
        with open(self.config, "w") as f:
            f.write(text)
        self.assertTrue(any("not covered" in m for m in self.lines(self.run_doctor(), "FAIL", "qbittorrent")))

    def test_bad_provider_and_subtitles_without_key(self):
        self.write_config('[subtitles]\nlanguage = "pt-BR"\n[[providers]]\nname = "x"\ntype = "nope"\n')
        r = self.run_doctor()
        self.assertTrue(any("unknown type" in m for m in self.lines(r, "FAIL", "providers")))
        self.assertTrue(self.lines(r, "WARN", "subtitles"))

    def test_gateway_on_localhost_is_a_warning(self):
        self.write_config('[gateway]\npublic_url = "http://localhost:8097"\n')
        self.assertTrue(self.lines(self.run_doctor(), "WARN", "gateway"))

    def test_nothing_secret_is_printed(self):
        self.write_secrets('[jellyfin]\napi_key = "SUPERSECRET123"\n[qbittorrent]\npassword = "PW-SECRET-9"\n')
        self.jf.error = JellyfinError("GET /System/Info: HTTP 401", 401)
        text = self.run_doctor().text()
        self.assertNotIn("SUPERSECRET123", text)
        self.assertNotIn("PW-SECRET-9", text)


if __name__ == "__main__":
    unittest.main()
