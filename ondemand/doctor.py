"""`cli doctor`: checks a setup end to end and says, line by line, what to fix.

Runs without the service (it works even when the service cannot start, e.g. with no API key):

    docker compose -f docker/compose.example.yml run --rm ondemand python3 -m ondemand.cli doctor [--network]

Every check prints one line: OK, WARN (works, but probably not what you want), FAIL (will not
work) or INFO (cannot be checked from here: a command to run by hand). Nothing secret is printed.
The exit code is 1 when anything FAILs.
"""

import os
import tomllib
import urllib.parse

from . import config, languages
from .catalog import MARKER
from .jellyfin import Jellyfin, JellyfinError
from .qbit import QBit, QBitError
from .sources import Unavailable
from .torrent import to_local

INJECTOR_ID = "f5a34f7b2e8a4e6aa7223a216a81b374"
LEVELS = ("OK", "INFO", "WARN", "FAIL")


class Report:
    def __init__(self):
        self.lines = []

    def add(self, level, area, message, fix=None):
        self.lines.append((level, area, message, fix))

    @property
    def failed(self):
        return any(level == "FAIL" for level, *_ in self.lines)

    def text(self):
        out = []
        for level, area, message, fix in self.lines:
            out.append(f"{level:4}  {area:11} {message}" + (f"\n{'':17}→ {fix}" if fix else ""))
        n = {lv: sum(1 for l, *_ in self.lines if l == lv) for lv in LEVELS}
        out.append(f"\n{n['OK']} ok · {n['WARN']} warning(s) · {n['FAIL']} failure(s) · {n['INFO']} to check by hand")
        return "\n".join(out)


# ---------------------------------------------------------------- files
def load(r, config_path=None, secrets_path=None):
    """The configuration (with secrets), or None if it cannot be read at all."""
    config_path = config_path or config.DEFAULT_CONFIG
    secrets_path = secrets_path if secrets_path is not None else config.DEFAULT_SECRETS
    try:
        cfg = config.load(config_path, secrets_path="")
    except FileNotFoundError:
        r.add("FAIL", "config", f"{config_path} not found", "cp config.example.toml config/config.toml")
        return None
    except tomllib.TOMLDecodeError as e:
        r.add("FAIL", "config", f"{config_path} is not valid TOML: {e}")
        return None
    r.add("OK", "config", f"{config_path} read")
    for section, keys in config.DEFAULTS.items():
        if isinstance(keys, dict):
            unknown = sorted(set(cfg.get(section) or {}) - set(keys))
            if unknown:
                r.add("WARN", "config", f"[{section}] unknown key(s): {', '.join(unknown)} (ignored)",
                      "check the spelling against config.example.toml")
    cfg["secrets"] = {}
    if not os.path.exists(secrets_path):
        r.add("FAIL", "secrets", f"{secrets_path} not found",
              "cp secrets.example.toml config/secrets.toml && chmod 600 config/secrets.toml")
        return cfg
    if os.stat(secrets_path).st_mode & 0o077:
        r.add("FAIL", "secrets", f"{secrets_path} is readable by others (the service refuses to start)",
              f"chmod 600 {secrets_path}")
    try:
        with open(secrets_path, "rb") as f:
            cfg["secrets"] = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        r.add("FAIL", "secrets", f"{secrets_path} cannot be read: {type(e).__name__}")
        return cfg
    r.add("OK", "secrets", f"{secrets_path} read")
    return cfg


def check_files(cfg, r):
    db_dir = os.path.dirname(cfg["general"]["database"]) or "."
    if os.path.isdir(db_dir) and os.access(db_dir, os.W_OK):
        r.add("OK", "data", f"{db_dir} is writable")
    else:
        r.add("FAIL", "data", f"{db_dir} is missing or not writable", "mount a writable folder there (compose: ../data:/data)")
    root = cfg["catalog"]["root"]
    if not os.path.isdir(root):
        r.add("FAIL", "catalog", f"{root} does not exist", "mount the catalog folder (compose: ../catalog:/catalog)")
        return
    if not os.path.exists(os.path.join(root, MARKER)):
        r.add("FAIL", "catalog", f"no {MARKER} in {root}: nothing will be written (protects an unmounted disk)",
              f"touch <catalog folder on the host>/{MARKER}")
    elif not os.access(root, os.W_OK):
        r.add("FAIL", "catalog", f"{root} is not writable by this user", "fix the owner (PUID/PGID) of the catalog folder")
    else:
        r.add("OK", "catalog", f"{root} is ready (marker present, writable)")
    if not any(config.catalog_paths(cfg).values()):
        r.add("FAIL", "catalog", "movies, movies_4k and series are all off: nothing to write")


def check_subtitles(cfg, r):
    code = cfg["subtitles"]["language"]
    try:
        lang = languages.get(code)
    except SystemExit as e:
        r.add("FAIL", "subtitles", str(e))
        return
    if lang is None:
        r.add("INFO", "subtitles", "off ([subtitles] language = \"\")")
        return
    if cfg["subtitles"].get("provider", "subdl") == "subdl" and not (cfg["secrets"].get("subdl") or {}).get("api_key"):
        r.add("WARN", "subtitles", f"language {code} but no SubDL key: only the placeholder subtitle will exist",
              "secrets.toml [subdl] api_key (subdl.com → account → API)")
    else:
        r.add("OK", "subtitles", f"{lang.name} ({lang.iso})")


# ---------------------------------------------------------------- Jellyfin
def check_jellyfin(cfg, r, jf=None):
    key = (cfg["secrets"].get("jellyfin") or {}).get("api_key")
    url = cfg["jellyfin"]["url"]
    if not key:
        r.add("FAIL", "jellyfin", "no API key", "Jellyfin Dashboard → API Keys → + ; put it in secrets.toml [jellyfin] api_key")
        return
    jf = jf or Jellyfin(url, key, timeout=10)
    try:
        info = jf._req("GET", "/System/Info")
    except JellyfinError as e:
        if e.code in (401, 403):
            r.add("FAIL", "jellyfin", f"{url} refused the API key", "create a new key in Dashboard → API Keys")
        else:
            r.add("FAIL", "jellyfin", f"{url} not reachable ({e})", "[jellyfin] url must be Jellyfin's address as seen from this container")
        return
    version = str(info.get("Version") or "?")
    r.add("OK" if version.startswith("10.11") else "WARN", "jellyfin", f"version {version}"
          + ("" if version.startswith("10.11") else " (tested with 10.11)"))

    jroot = (cfg["catalog"].get("jellyfin_root") or cfg["catalog"]["root"]).rstrip("/")
    try:
        folders = jf._req("GET", "/Library/VirtualFolders") or []
    except JellyfinError as e:
        r.add("FAIL", "jellyfin", f"could not list libraries ({e})")
        folders = []
    for kind, paths in config.catalog_paths(cfg).items():
        if not paths:
            continue
        target = paths[1]
        lib = next((f for f in folders if any(loc.rstrip("/") == target for loc in f.get("Locations") or [])), None)
        if lib is None:
            hint = "a separate Movies library" if kind == "movies_4k" else ("your Shows library" if kind == "series" else "a Movies library")
            r.add("FAIL", "libraries", f"{target} is in no library", f"add it to {hint} (Dashboard → Libraries)")
            continue
        monitor = (lib.get("LibraryOptions") or {}).get("EnableRealtimeMonitor")
        r.add("WARN" if monitor else "OK", "libraries", f"{target} → \"{lib.get('Name')}\""
              + (" with real-time monitoring ON" if monitor else ""),
              "turn real-time monitoring off in that library (the catalog announces its own folders)" if monitor else None)
    if folders and not any(loc.startswith(jroot + "/") or loc == jroot for f in folders for loc in f.get("Locations") or []):
        r.add("WARN", "libraries", f"no library path is under {jroot}",
              "is [catalog] jellyfin_root the path Jellyfin uses for the catalog?")

    try:
        users = jf._req("GET", "/Users") or []
    except JellyfinError:
        users = []
    missing = [u.get("Name") for u in users if not (u.get("Policy") or {}).get("ForceRemoteSourceTranscoding")]
    if missing:
        r.add("FAIL", "users", f"\"Force remote media transcoding\" off for: {', '.join(missing)}",
              "Dashboard → Users → <user> → turn it on (catalog .strm items need it)")
    elif users:
        r.add("OK", "users", f"{len(users)} user(s) with forced remote transcoding")

    try:
        plugins = jf._req("GET", "/Plugins") or []
    except JellyfinError:
        plugins = []
    if not any(str(p.get("Id", "")).replace("-", "") == INJECTOR_ID for p in plugins):
        r.add("WARN", "web", "JavaScript Injector plugin not installed: no \"search outside the library\"",
              "install it (Dashboard → Plugins → Catalog), restart Jellyfin, then web/install-script.sh web/search.js")
        return
    try:
        scripts = (jf._req("GET", f"/Plugins/{INJECTOR_ID}/Configuration") or {}).get("CustomJavaScripts") or []
    except JellyfinError:
        scripts = []
    installed = [s.get("Name") for s in scripts if "__ondemand" in (s.get("Script") or "") and s.get("Enabled", True)]
    if any("__ondemandSearch" in (s.get("Script") or "") for s in scripts):
        r.add("OK", "web", f"scripts installed: {', '.join(installed)}")
    else:
        r.add("WARN", "web", "search.js is not installed in the JavaScript Injector",
              "JELLYFIN_URL=… JELLYFIN_API_KEY=… bash web/install-script.sh web/search.js \"On demand: search\"")


# ---------------------------------------------------------------- qBittorrent
def check_qbittorrent(cfg, r, qb=None):
    q = cfg["qbittorrent"]
    sec = cfg["secrets"].get("qbittorrent") or {}
    qb = qb or QBit(q["url"], sec.get("username") or None, sec.get("password") or None, timeout=10)
    try:
        version = str(qb.version()).strip()
    except QBitError as e:
        if e.code == 403:
            r.add("FAIL", "qbittorrent", f"{q['url']} refused this container",
                  "whitelist the Docker subnet in the WebUI options, or set username/password in secrets.toml")
        else:
            r.add("FAIL", "qbittorrent", f"{q['url']} not reachable ({e})", "[qbittorrent] url as seen from this container")
        return
    major = version.lstrip("v").split(".")[0]
    if major == "4":
        r.add("OK", "qbittorrent", f"version {version}")
    else:
        r.add("FAIL", "qbittorrent", f"version {version}: only 4.x is supported (5.x renamed pause/resume)",
              "use a 4.6.x image, e.g. lscr.io/linuxserver/qbittorrent:4.6.7")
    save = q["save_path"].rstrip("/")
    try:
        local = to_local(save + "/x", q.get("path_map") or {})[:-2]
    except Unavailable:
        r.add("FAIL", "qbittorrent", f"save_path {save} is not covered by [qbittorrent] path_map",
              f"add {{ \"{save}\" = \"<where that folder is mounted here>\" }} to path_map")
        return
    parent = local if os.path.isdir(local) else os.path.dirname(local)
    if os.path.isdir(parent):
        r.add("OK", "qbittorrent", f"save_path {save} → {local} here")
    else:
        r.add("FAIL", "qbittorrent", f"save_path {save} maps to {local}, which is not mounted here",
              "mount qBittorrent's download folder in this container (read-only is enough)")


# ---------------------------------------------------------------- providers and gateway
def check_providers(cfg, r, qb=None, network=False):
    from .providers import registry
    try:
        reg = registry.build(cfg, None, {"qbit": qb})
    except SystemExit as e:
        r.add("FAIL", "providers", str(e))
        return
    except Exception as e:
        r.add("FAIL", "providers", f"could not load: {type(e).__name__}: {e}")
        return
    if not len(reg):
        r.add("WARN", "providers", "none configured: the catalog stays empty",
              "add a [[providers]] entry (config.example.toml has examples)")
    for p in reg:
        caps = ", ".join(sorted(p.capabilities)) or "nothing"
        if not network or "feed" not in p.capabilities:
            r.add("OK", "providers", f"{p.name} ({p.type}, {'private' if p.private else 'public'}): {caps}")
            continue
        try:
            n = len(list(p.feed() or []))
            r.add("OK" if n else "WARN", "providers", f"{p.name}: feed returned {n} item(s)",
                  None if n else "check the provider's url/categories; `cli preview --provider NAME` shows the details")
        except Exception as e:
            r.add("FAIL", "providers", f"{p.name}: feed failed ({type(e).__name__}: {e})")
    if reg and not network:
        r.add("INFO", "providers", "feeds not contacted; run `doctor --network` to try them")


def check_gateway(cfg, r):
    url = cfg["gateway"]["public_url"]
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        r.add("FAIL", "gateway", f"public_url {url!r} is not a URL", "e.g. http://ondemand:8097")
    elif u.hostname in ("localhost", "127.0.0.1", "::1"):
        r.add("WARN", "gateway", f"public_url {url} points to localhost: Jellyfin in another container cannot reach it",
              "use this service's name or IP as Jellyfin sees it, e.g. http://ondemand:8097")
    else:
        r.add("OK", "gateway", f".strm files will point to {url}/v/<id>")
    r.add("INFO", "gateway", f"from the Jellyfin container: curl -s {url.rstrip('/')}/health  → ok")
    r.add("INFO", "search", "from a browser: <your Jellyfin address>/ondemand/api/health → {\"ok\": true} (reverse proxy route)")


def run(config_path=None, secrets_path=None, network=False, jf=None, qb=None):
    r = Report()
    cfg = load(r, config_path, secrets_path)
    if cfg is None:
        return r
    check_files(cfg, r)
    check_subtitles(cfg, r)
    check_jellyfin(cfg, r, jf)
    if qb is None:
        sec = cfg["secrets"].get("qbittorrent") or {}
        qb = QBit(cfg["qbittorrent"]["url"], sec.get("username") or None, sec.get("password") or None, timeout=10)
    check_qbittorrent(cfg, r, qb)
    check_providers(cfg, r, qb, network)
    check_gateway(cfg, r)
    return r
