"""What a subtitle provider looks like, and how a candidate is chosen and unpacked.

A subtitle provider has two methods:
    search(imdb, kind="movie"|"tv", season=None, episode=None) -> [candidate]
    download(url) -> bytes of a .zip (or of a bare .srt)
where a candidate is {"releases": [release names], "name", "url", "hi", "comment"}.
Raise ProviderLimit on quota/refusal (429/403): the fetch is retried later.
"""

import io
import os
import re
import zipfile

from .formats import decode


class ProviderLimit(Exception):
    """429/403: daily quota or key refused — try again later."""


def _tokens(text):
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1}


_GROUP = re.compile(r"-([A-Za-z0-9]+)$")


def score(sub, release_name):
    """How well the subtitle matches our release (a scene-style name, Title.Year.1080p.BluRay.x264-GROUP)."""
    texts = " ".join(sub["releases"] + [sub["name"], sub["comment"]]).lower()
    target = release_name.lower()
    s = 0
    g = _GROUP.search(release_name.strip())
    if g and len(g.group(1)) > 1 and re.search(rf"(?<![a-z0-9]){re.escape(g.group(1).lower())}(?![a-z0-9])", texts):
        s += 40                       # made for the same encode (release group): timing will be right
    for mark in ("bluray", "brrip", "bdrip", "web-dl", "webrip", "web"):
        if mark in target and mark in texts:
            s += 15
            break
    for res in ("2160p", "1080p", "720p"):
        if res in target and res in texts:
            s += 10
    s += 2 * len(_tokens(target) & _tokens(texts))
    if sub["hi"]:
        s -= 5                        # prefer without sound descriptions
    return s


_EP_NAME = re.compile(r"(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,3})(?!\d)|(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?!\d)", re.I)


def _episode_of(name):
    m = _EP_NAME.search(name)
    if not m:
        return None
    return (int(m.group(1) or m.group(3)), int(m.group(2) or m.group(4)))


def extract_srt(data, target="", episode=None):
    """The .srt in the zip that best matches the release (or the largest). `episode` = (season, number): a
    season's zip carries one .srt per episode — only that episode's counts (or, if no file carries a number,
    the zip is a single episode's); a subtitle of ANOTHER episode is out of sync and worse than none.
    A bare .srt (not a zip) is accepted as is."""
    if not data.startswith(b"PK"):
        return decode(data), "subtitle.srt"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [i for i in z.infolist() if i.filename.lower().endswith(".srt")
                 and not i.filename.startswith("__MACOSX") and i.file_size < (5 << 20)]
        if not names:
            raise ValueError("zip without .srt")
        if episode:
            of_ep = [i for i in names if _episode_of(i.filename) == tuple(episode)]
            no_number = [i for i in names if _episode_of(i.filename) is None]
            if of_ep:
                names = of_ep
            elif len(no_number) == len(names) and len(names) == 1:
                pass                                   # a single file without a number in its name: trust it
            else:
                raise ValueError(f"the zip does not have episode S{episode[0]:02d}E{episode[1]:02d}")
        target_t = _tokens(target)
        best = max(names, key=lambda i: (len(_tokens(i.filename) & target_t), i.file_size))
        return decode(z.read(best)), os.path.basename(best.filename)
