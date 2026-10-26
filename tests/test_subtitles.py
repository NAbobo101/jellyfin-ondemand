import io
import os
import tempfile
import unittest
import zipfile

from ondemand import languages
from ondemand.db import Database
from ondemand.jellyfin import strm_item_id
from ondemand.subtitles.base import ProviderLimit, extract_srt, score
from ondemand.subtitles.formats import (BOM, decode, parse_srt, placeholder_sami, subtitle_path, to_sami,
                                        write_atomic)
from ondemand.subtitles.subtitler import Subtitler

PT = languages.get("pt-BR")
SRT = ("1\r\n00:00:01,000 --> 00:00:04,500\r\n<i>Ação</i> & coração\r\nsegunda linha\r\n\r\n"
       "2\r\n00:00:04,500 --> 00:00:06,000\r\nemendada\r\n\r\n"
       "3\r\n00:01:00.250 --> 00:01:02,000\r\nfim\r\n")
TARGET = "Night of the Living Dead.1968.1080p.BluRay.x264-GRP"


def srt(n):
    return "\n\n".join(f"{i}\n00:{i // 60:02d}:{i % 60:02d},000 --> 00:{i // 60:02d}:{i % 60:02d},800\nLine {i} ação"
                       for i in range(1, n + 1))


def zip_with(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, text in files.items():
            z.writestr(name, text.encode("cp1252"))
    return b.getvalue()


class TestLanguages(unittest.TestCase):
    def test_lookup(self):
        self.assertEqual((PT.iso, PT.subdl), ("por", "BR_PT"))
        self.assertEqual(languages.get("es-MX").iso, "spa")         # falls back to the base language
        self.assertIsNone(languages.get(""))
        with self.assertRaises(SystemExit):
            languages.get("xx")
        self.assertTrue(PT.regex.search("Movie.pt-BR.srt"))
        self.assertFalse(PT.regex.search("Movie.eng.srt"))


class TestSRT(unittest.TestCase):
    def test_parse(self):
        b = parse_srt(SRT)
        self.assertEqual([x[:2] for x in b], [(1000, 4500), (4500, 6000), (60250, 62000)])
        self.assertEqual(b[0][2], ["<i>Ação</i> & coração", "segunda linha"])

    def test_garbage_and_block_without_text(self):
        self.assertEqual(parse_srt("garbage\n\n1\n00:00:01,000 --> 00:00:02,000\n\n"), [])

    def test_decode_cp1252(self):
        self.assertEqual(decode("Ação".encode("cp1252")), "Ação")
        self.assertEqual(decode((BOM + "Ação").encode("utf-8")), "Ação")


class TestSAMI(unittest.TestCase):
    def test_converts_with_bom_and_tags(self):
        s = to_sami(parse_srt(SRT), "pt-BR", "Portuguese (Brazil)")
        self.assertTrue(s.startswith(BOM + "<SAMI>"))
        self.assertIn("lang: pt-BR", s)
        self.assertIn("<SYNC Start=1000><P Class=SUBS><i>Ação</i> &amp; coração<br>segunda linha", s)
        # block 1 ends when block 2 starts: no &nbsp; between them
        self.assertNotIn("<SYNC Start=4500><P Class=SUBS>&nbsp;", s)
        self.assertIn("<SYNC Start=6000><P Class=SUBS>&nbsp;", s)

    def test_placeholder(self):
        s = placeholder_sami(PT)
        self.assertIn(PT.placeholder, s)
        self.assertTrue(s.startswith(BOM))
        self.assertIn("Downloading subtitles", placeholder_sami(languages.get("en")))


class TestFiles(unittest.TestCase):
    def test_atomic_write_and_name(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "Movie (2020) [imdbid-tt1].por.smi")
            write_atomic(target, placeholder_sami(PT))
            write_atomic(target, "new")
            with open(target, encoding="utf-8") as f:
                self.assertEqual(f.read(), "new")
            self.assertEqual(os.listdir(d), [os.path.basename(target)])      # no temp file left over
        self.assertEqual(subtitle_path("/x/Movie - 1080p.strm", "por"), "/x/Movie - 1080p.por.smi")
        self.assertEqual(subtitle_path("/x/Movie.strm", "spa"), "/x/Movie.spa.smi")


class TestEpisodeInZip(unittest.TestCase):
    def zip_of(self, names):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in names:
                z.writestr(n, "1\n00:00:01,000 --> 00:00:02,000\n" + n + "\n")
        return buf.getvalue()

    def test_season_zip_gives_the_episode_file(self):
        z = self.zip_of([f"Show.S01E{n:02d}.HDTV.XviD-GRP.srt" for n in range(1, 13)])
        text, name = extract_srt(z, "Show S01 1080p BluRay x265", (1, 1))
        self.assertEqual(name, "Show.S01E01.HDTV.XviD-GRP.srt")
        self.assertEqual(extract_srt(z, "x", (1, 12))[1], "Show.S01E12.HDTV.XviD-GRP.srt")

    def test_1x05_format_and_missing_episode(self):
        z = self.zip_of(["Show 1x05 - Title.srt", "Show 1x06 - Title.srt"])
        self.assertEqual(extract_srt(z, "", (1, 5))[1], "Show 1x05 - Title.srt")
        with self.assertRaises(ValueError):
            extract_srt(z, "", (1, 9))                       # never another episode's subtitle

    def test_single_file_zip_without_number_counts_and_movie_unchanged(self):
        self.assertEqual(extract_srt(self.zip_of(["subtitle.srt"]), "", (2, 3))[1], "subtitle.srt")
        z = self.zip_of(["Movie.2020.1080p.srt", "Movie.2020.720p.srt"])
        self.assertIn("Movie.2020", extract_srt(z, "Movie.2020.1080p")[1])

    def test_bare_srt(self):
        self.assertIn("Line 3", extract_srt(srt(3).encode(), "x")[0])


class TestScore(unittest.TestCase):
    def sub(self, releases, hi=False):
        return {"releases": releases, "name": "", "comment": "", "hi": hi}

    def test_same_group_and_same_source_win(self):
        group = score(self.sub(["Night.Of.The.Living.Dead.1968.1080p.BluRay.x264-GRP"]), TARGET)
        web = score(self.sub(["Night.of.the.Living.Dead.1968.WEBRip.720p"]), TARGET)
        empty = score(self.sub([]), TARGET)
        self.assertGreater(group, web)
        self.assertGreater(web, empty)
        self.assertGreaterEqual(group - score(self.sub(["Night.Of.The.Living.Dead.1968.1080p.BluRay.x264-OTHER"]), TARGET), 40)

    def test_hi_loses_on_a_tie(self):
        self.assertGreater(score(self.sub(["x.BluRay"]), TARGET), score(self.sub(["x.BluRay"], hi=True), TARGET))

    def test_extract_picks_the_matching_one(self):
        z = zip_with({"other.release.srt": srt(30), "Night.Of.The.Living.Dead.1968.1080p.BluRay.srt": srt(25),
                      "readme.txt": "x"})
        text, name = extract_srt(z, TARGET)
        self.assertEqual(name, "Night.Of.The.Living.Dead.1968.1080p.BluRay.srt")
        self.assertIn("ação", text)                                   # cp1252 decoded

    def test_zip_without_srt(self):
        with self.assertRaises(ValueError):
            extract_srt(zip_with({"a.txt": "x"}))


class FakeProvider:
    name = "fake"

    def __init__(self, candidates=None, zip_bytes=None, error=None):
        self.candidates, self.zip, self.error, self.searches = candidates or [], zip_bytes, error, 0

    def search(self, imdb, *a, **kw):
        self.searches += 1
        if self.error:
            raise self.error
        return self.candidates

    def download(self, url):
        return self.zip


class FakeJellyfin:
    def __init__(self, sessions=()):
        self.sessions, self.toggled, self.reloaded, self.messages = list(sessions), [], [], []
        self.items_asked = []

    def sessions_of_item(self, item_id):
        # deliberately no lookup by path: the id comes from the path, no library listing
        self.items_asked.append(item_id)
        return self.sessions

    def toggle_subtitle(self, sid, index):
        self.toggled.append((sid, index))

    def reload(self, sid, item, pos, ms, audio, sub):
        self.reloaded.append((sid, pos, sub))

    def message(self, sid, text, ms=6000):
        self.messages.append(text)


def cand(url="/s.zip", releases=()):
    return {"releases": list(releases), "name": "", "comment": "", "hi": False, "url": url}


class TestSubtitler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        tid = self.db.save_title({"key": "imdb:tt0063350", "imdb": "tt0063350", "name": "Night", "origin": "test"})
        self.rid = self.db.save_release(tid, {"label": "1080p", "name": TARGET, "torrent_url": "u"})
        self.strm = os.path.join(self.tmp.name, "Night (1968) [imdbid-tt0063350].strm")
        open(self.strm, "w").close()
        write_atomic(subtitle_path(self.strm, PT.iso), placeholder_sami(PT))
        self.db.execute("UPDATE releases SET strm=? WHERE id=?", self.strm, self.rid)

    def tearDown(self):
        self.tmp.cleanup()

    def sub(self, jf, provider):
        return Subtitler(self.db, jf, provider, PT)

    def state(self):
        return self.db.one("SELECT * FROM subtitles WHERE release_id=?", self.rid)

    def read_sub(self):
        with open(subtitle_path(self.strm, PT.iso), encoding="utf-8") as f:
            return f.read()

    def test_downloads_overwrites_the_placeholder_and_toggles_whoever_is_watching(self):
        session = {"Id": "s1", "DeviceName": "Chrome", "SupportedCommands": ["SetSubtitleStreamIndex"],
                   "PlayState": {"SubtitleStreamIndex": 0, "PositionTicks": 10}}
        jf = FakeJellyfin([session])
        prov = FakeProvider([cand(releases=["x.BluRay.GRP"])], zip_with({"Night.srt": srt(40)}))
        self.assertTrue(self.sub(jf, prov).fetch(self.rid))
        content = self.read_sub()
        self.assertIn("Line 40", content)
        self.assertNotIn(PT.placeholder, content)
        self.assertTrue(content.startswith(BOM + "<SAMI>"))
        self.assertEqual(self.state()["state"], "real")
        self.assertEqual(jf.toggled, [("s1", 0)])
        self.assertEqual(jf.messages, [PT.loaded])
        self.assertEqual(jf.items_asked, [strm_item_id(self.strm)])
        self.assertTrue(self.sub(jf, prov).fetch(self.rid))   # already real: does not search again
        self.assertEqual(prov.searches, 1)

    def test_subtitle_off_does_not_touch_the_player(self):
        jf = FakeJellyfin([{"Id": "s1", "SupportedCommands": [], "PlayState": {"SubtitleStreamIndex": -1}}])
        self.sub(jf, FakeProvider([cand()], zip_with({"a.srt": srt(40)}))).fetch(self.rid)
        self.assertEqual((jf.toggled, jf.reloaded), ([], []))

    def test_client_without_the_command_reloads_at_the_same_position(self):
        jf = FakeJellyfin([{"Id": "s1", "SupportedCommands": [], "PlayState": {"SubtitleStreamIndex": 0, "PositionTicks": 600}}])
        self.sub(jf, FakeProvider([cand()], zip_with({"a.srt": srt(40)}))).fetch(self.rid)
        self.assertEqual(jf.reloaded, [("s1", 600, 0)])

    def test_not_found_and_quota(self):
        self.assertFalse(self.sub(FakeJellyfin(), FakeProvider([])).fetch(self.rid))
        self.assertEqual(self.state()["state"], "not_found")
        self.assertFalse(self.sub(FakeJellyfin(), FakeProvider(error=ProviderLimit("429"))).fetch(self.rid))
        self.assertEqual(self.state()["attempts"], 2)
        self.assertIn(PT.placeholder, self.read_sub())         # placeholder intact

    def test_bad_subtitle_tries_the_next_candidate(self):
        class TwoSubs(FakeProvider):
            def download(self, url):
                return zip_with({"trailer.srt": srt(5)}) if url == "/bad.zip" else zip_with({"Night.srt": srt(40)})
        prov = TwoSubs([cand("/bad.zip", ["Night.1968.1080p.BluRay.x264-GRP"]), cand("/good.zip")], b"")
        self.assertTrue(self.sub(FakeJellyfin(), prov).fetch(self.rid))   # the best scored one was garbage
        self.assertIn("Line 40", self.read_sub())

    def test_garbage_subtitle_is_refused(self):
        prov = FakeProvider([cand()], zip_with({"trailer.srt": srt(5)}))
        with self.assertRaises(ValueError):
            self.sub(FakeJellyfin(), prov).fetch(self.rid)
        self.assertIn(PT.placeholder, self.read_sub())

    def test_writes_through_the_path_converter(self):
        jf_path = "/jellyfin/view/" + os.path.basename(self.strm)
        self.db.execute("UPDATE releases SET strm=? WHERE id=?", jf_path, self.rid)
        prov = FakeProvider([cand()], zip_with({"a.srt": srt(40)}))
        s = Subtitler(self.db, FakeJellyfin(), prov, PT,
                      to_local=lambda p: p.replace("/jellyfin/view", self.tmp.name, 1))
        self.assertTrue(s.fetch(self.rid))
        self.assertIn("Line 40", self.read_sub())


if __name__ == "__main__":
    unittest.main()
