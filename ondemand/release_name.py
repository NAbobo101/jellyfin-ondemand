"""Scene-style release names: what a torrent title says about itself.

    "Franklin 2024 S01E03 1080p WEB h264-ETHEL"        -> series, season 1, episode 3
    "Night of the Living Dead 1968 1080p BluRay x264-GRP" -> movie, 1968

Pure functions over strings, shared by the series and movie filters.
"""

import re
import unicodedata

_EP = re.compile(r"\bS(\d{1,2}) ?E(\d{1,3})((?:[ -]?E\d{1,3})*)\b", re.I)
_EP_X = re.compile(r"\b(\d{1,2})x(\d{2,3})\b", re.I)
_PACK = re.compile(r"\bS(\d{1,2})\b(?! ?E\d)", re.I)
_MULTI_SEASON = re.compile(r"\bS\d{1,2} ?(?:-|to) ?S?\d{1,2}\b|\bseasons? \d+ ?(?:-|to) ?\d+|complete (?:series|collection)", re.I)
_DATE = re.compile(r"\b(?:19|20)\d\d[ -]\d\d[ -]\d\d\b")
_YEAR_END = re.compile(r"[ (\[]((?:19|20)\d\d)[)\]]?$")
_YEAR = re.compile(r"^[(\[]?((?:19|20)\d\d)[)\]]?$")
_COUNTRY = re.compile(r"[ (]((?:US|UK|AU|CA|NZ|IE|DE|FR))\)?$")
_RESOLUTION = re.compile(r"\b(2160p|1080p|720p|576p|480p|4K|UHD)\b", re.I)
_CODEC = re.compile(r"\b(?:x|h) ?26([45])\b|\b(hevc)\b|\b(avc)\b", re.I)
_HDR = re.compile(r"\b(HDR10\+?|HDR|DV|DoVi|Dolby Vision|HLG)\b", re.I)
_PROPER = re.compile(r"\b(PROPER|REPACK|RERIP)\b", re.I)
_MULTI_LANG = re.compile(r"\b(DUAL|MULTI)\b|\bGERMAN DL\b", re.I)
_GROUP = re.compile(r"-\s*([A-Za-z0-9&]+)\s*$")
# A token that starts the technical part of a movie name ("Title 2019 <here> 1080p ...").
_TECH = re.compile(r"^(2160p|1080p|720p|576p|480p|4K|UHD|BluRay|BDRip|BRRip|REMUX|WEB(-?DL|Rip)?|HDTV|DVDRip|"
                   r"x26[45]|h26[45]|HEVC|AVC|HDR10\+?|HDR|DV|IMAX|PROPER|REPACK|EXTENDED|UNRATED|REMASTERED|"
                   r"DIRECTORS|LIMITED|MULTI|DUAL|AMZN|NF|DSNP|HMAX|ATVP)$", re.I)
FOREIGN = ("GERMAN", "FRENCH", "SPANISH", "ITALIAN", "NORDIC", "SWEDISH", "DANISH", "NORWEGIAN", "FINNISH",
           "DUTCH", "FLEMISH", "RUSSIAN", "POLISH", "TURKISH", "CZECH", "HUNGARIAN", "KOREAN", "JAPANESE",
           "HINDI", "ARABIC", "LATINO", "GREEK", "HEBREW", "THAI", "ICELANDIC", "ROMANIAN", "BULGARIAN",
           "CHINESE", "MANDARIN", "CANTONESE", "TAMIL", "TELUGU", "PORTUGUESE", "BRAZILIAN")
_FOREIGN = re.compile(r"\b(" + "|".join(FOREIGN) + r")\b", re.I)
COUNTRIES = {"UK": "GB"}
SOURCE_RANK = {"BluRay": 3, "WEB-DL": 3, "WEBRip": 2, "HDTV": 1, None: 1}


def normalize(text):
    """A comparable name: no accents or apostrophes, lower case, '&' = 'and', only letters and digits."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = t.lower().replace("&", " and ").replace("'", "")       # "It's" and "Its" (scene drops the apostrophe) are the same name
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def source_of(after):
    if re.search(r"\b(REMUX|BluRay|BDRip|BRRip|BD)\b", after, re.I):
        return "BluRay"
    if re.search(r"\bWEB[ -]?Rip\b", after, re.I):
        return "WEBRip"
    if re.search(r"\bWEB([ -]?DL)?\b", after, re.I):
        return "WEB-DL"
    if re.search(r"\b(HDTV|PDTV|DSR)\b", after, re.I):
        return "HDTV"
    return None


def _clean(title):
    return " ".join(re.sub(r"[._]", " ", title).split())


def _technical(after, original):
    """Quality fields from the part after the name."""
    res = _RESOLUTION.search(after)
    resolution = None
    if res:
        resolution = "2160p" if res.group(1).upper() in ("4K", "UHD") else res.group(1).lower()
    cod = _CODEC.search(after)
    codec = None
    if cod:
        codec = f"x26{cod.group(1)}" if cod.group(1) else ("x265" if cod.group(2) else "x264")
    g = _GROUP.search(original.strip())
    return {"resolution": resolution, "source": source_of(after), "codec": codec,
            "hdr": bool(_HDR.search(after)), "proper": bool(_PROPER.search(after)),
            "foreign": sorted({x.upper() for x in _FOREIGN.findall(after)}),
            "multi": bool(_MULTI_LANG.search(after)),
            "group": g.group(1) if g and g.group(1).upper() not in ("DL", "RIP") else None}


def parse_series(title):
    """(data, None) or (None, reason). `data`: raw_name, year, country, season, episodes (list),
    pack, resolution, source, codec, hdr, foreign (list), multi, proper, group."""
    t = _clean(title)
    if _MULTI_SEASON.search(t):
        return None, "multi-season pack (not supported)"
    m = _EP.search(t)
    pack = False
    if m:
        season, eps = int(m.group(1)), [int(m.group(2))]
        extra = [int(x) for x in re.findall(r"E(\d{1,3})", m.group(3) or "", re.I)]
        if extra and (m.group(3) or "").lstrip().startswith("-"):
            eps = list(range(eps[0], extra[-1] + 1))
        else:
            eps += extra
    elif (m := _EP_X.search(t)):
        season, eps = int(m.group(1)), [int(m.group(2))]
    elif (m := _PACK.search(t)):
        season, eps, pack = int(m.group(1)), [], True
    else:
        return None, "daily show (by date)" if _DATE.search(t) else "not a series (no season/episode)"
    before, after = t[:m.start()].strip(" -"), t[m.end():]
    year = country = None
    for _ in range(2):                              # "Show 2024 US" and "Show US 2024"
        if (a := _YEAR_END.search(before)) and year is None:
            year, before = int(a.group(1)), before[:a.start()].strip(" -")
        if (p := _COUNTRY.search(before)) and country is None:
            country, before = COUNTRIES.get(p.group(1).upper(), p.group(1).upper()), before[:p.start()].strip(" -")
    if not before:
        return None, "no series name"
    return {"raw_name": before, "year": year, "country": country, "season": season, "episodes": eps, "pack": pack,
            **_technical(after, title)}, None


def parse_movie(title):
    """(data, None) or (None, reason). `data`: raw_name, year, resolution, source, codec, hdr, foreign,
    multi, proper, group. The year is the last year-like token before the technical part (so
    "2001 A Space Odyssey 1968 1080p" is "2001 A Space Odyssey", 1968); a movie name needs one."""
    t = _clean(title)
    if _EP.search(t) or _EP_X.search(t) or _PACK.search(t) or _MULTI_SEASON.search(t):
        return None, "looks like a series"
    tokens = t.split(" ")
    tech = next((i for i, tok in enumerate(tokens) if i > 0 and _TECH.match(tok.strip("[]()"))), len(tokens))
    year_at = next((i for i in range(tech - 1, 0, -1) if _YEAR.match(tokens[i])), None)
    if year_at is None:
        return None, "no year in the name"
    name = " ".join(tokens[:year_at]).strip(" -([")
    if not name:
        return None, "no movie name"
    after = " ".join(tokens[year_at + 1:])
    return {"raw_name": name, "year": int(_YEAR.match(tokens[year_at]).group(1)), **_technical(after, title)}, None


def kind_of(title):
    """"series", "movie" or None (neither parses)."""
    if parse_series(title)[0] is not None:
        return "series"
    if parse_movie(title)[0] is not None:
        return "movie"
    return None
