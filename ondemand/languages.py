"""Languages for subtitles and release filtering.

`[subtitles] language` takes a BCP 47 code. Each entry gives:
- iso: the ISO 639-2 code Jellyfin reads from the file name (`Movie.por.smi`);
- subdl: the SubDL language code;
- pattern: how a subtitle file for this language is usually named inside a torrent
  (those files get top priority, since they arrive in seconds);
- placeholder / loaded: the texts shown before and after the real subtitle arrives.

Add a language by adding an entry; anything missing falls back to English text.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str
    name: str
    iso: str
    subdl: str
    pattern: str
    placeholder: str = "Downloading subtitles..."
    loaded: str = "Subtitles loaded"

    @property
    def regex(self):
        return re.compile(self.pattern, re.I)


LANGUAGES = {
    "pt-BR": Language("pt-BR", "Portuguese (Brazil)", "por", "BR_PT",
                      r"(pt[-_. ]?br|portugu|brazil|brasil|\bpob\b|\bpor\b|\.br\.)",
                      "Baixando a legenda em português...", "Legenda em português carregada"),
    "pt": Language("pt", "Portuguese", "por", "PT", r"(pt[-_. ]?pt|portugu|\bpor\b|\.pt\.)",
                   "A descarregar as legendas...", "Legendas carregadas"),
    "en": Language("en", "English", "eng", "EN", r"(\beng?\b|english|\.en\.)"),
    "es": Language("es", "Spanish", "spa", "ES", r"(\bspa\b|spanish|espa[nñ]ol|castellano|latino|\.es\.)",
                   "Descargando los subtítulos...", "Subtítulos cargados"),
    "fr": Language("fr", "French", "fre", "FR", r"(\bfre\b|\bfra\b|french|fran[cç]ais|\.fr\.)",
                   "Téléchargement des sous-titres...", "Sous-titres chargés"),
    "de": Language("de", "German", "ger", "DE", r"(\bger\b|\bdeu\b|german|deutsch|\.de\.)",
                   "Untertitel werden geladen...", "Untertitel geladen"),
    "it": Language("it", "Italian", "ita", "IT", r"(\bita\b|italian|italiano|\.it\.)",
                   "Download dei sottotitoli...", "Sottotitoli caricati"),
    "nl": Language("nl", "Dutch", "dut", "NL", r"(\bdut\b|\bnld\b|dutch|nederlands|\.nl\.)",
                   "Ondertitels downloaden...", "Ondertitels geladen"),
    "pl": Language("pl", "Polish", "pol", "PL", r"(\bpol\b|polish|polski|\.pl\.)",
                   "Pobieranie napisów...", "Napisy załadowane"),
    "ro": Language("ro", "Romanian", "rum", "RO", r"(\brum\b|\bron\b|romanian|rom[aâ]n|\.ro\.)",
                   "Se descarcă subtitrarea...", "Subtitrare încărcată"),
    "tr": Language("tr", "Turkish", "tur", "TR", r"(\btur\b|turkish|t[uü]rk[cç]e|\.tr\.)",
                   "Altyazı indiriliyor...", "Altyazı yüklendi"),
    "sv": Language("sv", "Swedish", "swe", "SV", r"(\bswe\b|swedish|svenska|\.sv\.)",
                   "Laddar ner undertexter...", "Undertexter laddade"),
    "ru": Language("ru", "Russian", "rus", "RU", r"(\brus\b|russian|\.ru\.)",
                   "Загрузка субтитров...", "Субтитры загружены"),
    "ar": Language("ar", "Arabic", "ara", "AR", r"(\bara\b|arabic|\.ar\.)"),
    "he": Language("he", "Hebrew", "heb", "HE", r"(\bheb\b|hebrew|\.he\.)"),
    "el": Language("el", "Greek", "gre", "EL", r"(\bgre\b|\bell\b|greek|\.el\.)"),
}


def get(code):
    """The Language for a BCP 47 code ("pt-BR", "es"...), None for "" (subtitles off).
    An unknown code raises: better to fail at start-up than to fetch the wrong subtitles."""
    if not code:
        return None
    if code in LANGUAGES:
        return LANGUAGES[code]
    base = code.split("-")[0].lower()
    if base in LANGUAGES:
        return LANGUAGES[base]
    raise SystemExit(f"[subtitles] language = {code!r} is not in ondemand/languages.py — add an entry")
