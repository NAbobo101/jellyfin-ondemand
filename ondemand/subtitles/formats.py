"""Catalog subtitles: format and writing.

Measured facts (Jellyfin 10.11):
- The subtitle must exist with its FINAL name before the first play (Jellyfin bug
  #15882: an external subtitle added later to a .strm item disappears on the next
  play): every .strm is born with a placeholder <base>.<iso>.smi, overwritten in place.
- SAMI (.smi) because an external .srt of a remote source reaches Android TV as a
  local path (a 10.11 bug); SAMI is always converted and served by the server.
- UTF-8 WITH BOM: a short file without BOM is read by Jellyfin as Mac Roman.
- Atomic write (temp file in the same folder + os.replace): the server re-reads the
  file on every request and must never get a half-written file.
"""

import html
import os
import re
import tempfile

BOM = "﻿"
_TIME = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_OK_TAGS = re.compile(r"&lt;(/?)(i|b|u)&gt;", re.I)


def decode(data):
    """Subtitles come in UTF-8 or a Windows code page; latin-1 never fails."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue


def _ms(m):
    h, mi, s, frac = m.groups()
    return ((int(h) * 60 + int(mi)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0")[:3])


def parse_srt(text):
    """SRT -> [(start_ms, end_ms, [lines])], tolerant of \\r\\n, a missing index and garbage."""
    blocks = []
    for block in re.split(r"\r?\n\s*\r?\n", text.replace(BOM, "").strip()):
        lines = block.splitlines()
        for i, l in enumerate(lines):
            if "-->" in l:
                a, b = l.split("-->", 1)
                ma, mb = _TIME.search(a), _TIME.search(b)
                if ma and mb:
                    text_lines = [x.strip() for x in lines[i + 1:] if x.strip()]
                    if text_lines:
                        blocks.append((_ms(ma), _ms(mb), text_lines))
                break
    blocks.sort(key=lambda x: x[0])
    return blocks


def _sami_line(lines):
    t = "<br>".join(html.escape(l, quote=False) for l in lines)
    return _OK_TAGS.sub(lambda m: f"<{m.group(1)}{m.group(2).lower()}>", t)   # keeps italic/bold


def to_sami(blocks, lang_code="en", lang_name="Subtitles"):
    parts = [BOM, "<SAMI>\n<HEAD>\n<TITLE>jellyfin-ondemand</TITLE>\n<STYLE TYPE=\"text/css\">\n<!--\n"
             "P { font-family: Arial; text-align: center; }\n"
             f".SUBS {{ Name: {lang_name}; lang: {lang_code}; SAMIType: CC; }}\n-->\n</STYLE>\n</HEAD>\n<BODY>\n"]
    for n, (start, end, lines) in enumerate(blocks):
        parts.append(f"<SYNC Start={start}><P Class=SUBS>{_sami_line(lines)}\n")
        nxt = blocks[n + 1][0] if n + 1 < len(blocks) else None
        if nxt is None or nxt > end:
            parts.append(f"<SYNC Start={end}><P Class=SUBS>&nbsp;\n")
    parts.append("</BODY>\n</SAMI>\n")
    return "".join(parts)


def placeholder_sami(lang):
    return to_sami([(500, 5000, [lang.placeholder])], lang.code, lang.name)


def write_atomic(path, text):
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".od-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def subtitle_path(strm, iso):
    """The subtitle that goes with a .strm: same base name + .<iso>.smi."""
    base = strm[:-len(".strm")] if strm.endswith(".strm") else strm
    return f"{base}.{iso}.smi"
