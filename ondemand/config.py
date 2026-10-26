"""Configuration: config.toml (no secrets, safe to share) + secrets.toml (chmod 600).

Defaults below; the file only needs what changes. See config.example.toml for every key
with an explanation, and docs/writing-a-provider.md for the [[providers]] entries.
"""

import copy
import os
import tomllib

DEFAULT_CONFIG = os.environ.get("ONDEMAND_CONFIG", "/config/config.toml")
DEFAULT_SECRETS = os.environ.get("ONDEMAND_SECRETS", "/config/secrets.toml")

DEFAULTS = {
    "general": {
        "database": "/data/ondemand.db",
        "log_level": "INFO",
        "plugins_dir": "/plugins",    # your own providers (see docs/writing-a-provider.md)
    },
    "gateway": {
        "port": 8097,
        # What goes inside every .strm: this service's address AS JELLYFIN REACHES IT. Changing it later
        # means rewriting every .strm (cli rewrite-strm).
        "public_url": "http://ondemand:8097",
        # Who may read /v/<id> (IP, CIDR or host name — a compose service name is resolved again every 30 s)
        "video_clients": ["jellyfin"],
        "admin_clients": ["127.0.0.1"],      # /metrics and admin routes
        # First request = PlaybackInfo's probe. Jellyfin Web clients wait; a cold 4K takes ~25 s because
        # qBittorrent fetches 1% of each end first. (The Android TV app would give up after 30 s.)
        "first_byte_wait_s": 60,
        "play_wait_s": 90,            # torrent already active: wait for the piece
        "block_s": 120,               # probe failed: refuse the release for this long (see Service)
        "stall_wait_s": 900,          # in the middle of the video: wait for the piece up to 15 min
    },
    "jellyfin": {"url": "http://jellyfin:8096", "message_header": "On demand"},
    "qbittorrent": {
        "url": "http://qbittorrent:8080",
        "save_path": "/downloads/ondemand",   # as qBittorrent sees it
        # qBittorrent's paths -> this container's paths (longest prefix wins). Every folder where a
        # catalog torrent may live (save path, incomplete folder, your own reused torrents) must be here.
        "path_map": {"/downloads": "/downloads"},
        "category": "ondemand",
        "torrent_wait_s": 20,
        "metadata_wait_s": 40,
        "min_video_mb": 300,
    },
    "catalog": {
        "root": "/catalog",            # where THIS service writes; must contain the marker file
        "jellyfin_root": "",          # the same folder as Jellyfin sees it ("" = same path)
        "movies": "Movies",           # sub-folders (add each one to a Jellyfin library); "" disables
        "movies_4k": "Movies 4K",
        "series": "Series",
        "batch": 10, "series_batch": 5, "interval_min": 2,   # writes only with nobody watching
        "import_interval_h": 6,       # how often every provider's feed() is read
    },
    "movies": {
        "enabled": True,
        "resolutions": ["2160p", "1080p", "720p"],      # 2160p goes to the 4K library (labelled "4K")
        "seeds_min": 1, "public_seeds_min": 2, "freeleech_min": 100,
        "max_gb": 40, "max_gb_4k": 80,
        "allowed_language_tags": [],   # e.g. ["SPANISH", "LATINO"]: release tags that are NOT foreign to you
        "prefer_sdr": True,            # an HDR/DV release only wins when there is no SDR one
    },
    "series": {
        "enabled": True, "tvmaze_cache": "/data/tvmaze-cache.json",
        "resolutions": ["1080p", "720p"],
        "seeds_min": 1, "public_seeds_min": 2, "freeleech_min": 100,
        "pack_max_gb": 60, "single_max_gb": 8,
        "allowed_language_tags": [],
        # 100 MB: a 20-minute anime episode fits. A public torrent is abandoned/deleted by the lifecycle like any other.
        "min_video_mb": 100, "complete_days": 7,
    },
    "lifecycle": {
        "interval_s": 30,
        "commit_minutes": 15,         # public: watched this much -> download to the end
        "abandon_minutes": 10,        # public: stopped earlier and nobody came back in this time -> delete
        "days_unwatched": 2,          # seed rule met + this long unwatched -> delete
        "days_with_position": 14,     # someone stopped midway ("continue watching"): keep up to this long
        "throttle_others_kbs": 300,   # other downloads while a catalog title plays and downloads
    },
    "subtitles": {
        "language": "",               # BCP 47 code (e.g. "pt-BR", "es", "fr"); "" = no subtitle fetching
        "provider": "subdl",          # "subdl" (api_key in secrets.toml [subdl]) or "module:Class" (a plugin)
        "interval_h": 6, "max_attempts": 56,    # 56 x 6 h = 14 days trying
    },
    # Search outside the library (search.py + web/search.js): API only for logged-in Jellyfin users.
    "search": {
        "enabled": True,
        "port": 8098,
        "daily_limit": 10,            # requests per user in 24 h (administrators have no limit)
        "cache_min": 60,              # the same term does not go back to TMDb/providers for this long
        "library_cache_min": 30,      # IMDb ids of LOCAL movies (the catalog comes from the database)
        "tmdb_max": 5,                # TMDb results turned into IMDb ids per search
        "max_results": 24,
        "check_availability": True,   # only show movies some provider has (one provider search per card)
        "item_wait_s": 300,           # folder written -> item with metadata
        "metadata_wait_s": 45,        # item born without a poster: play anyway after this
    },
    "abr": {"root": ""},              # pre-generated HLS ladders (tools/generate-abr.sh); "" = off
    "providers": [],
}


def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path=None, secrets_path=None):
    path = path or DEFAULT_CONFIG
    secrets_path = secrets_path if secrets_path is not None else DEFAULT_SECRETS
    cfg = copy.deepcopy(DEFAULTS)
    with open(path, "rb") as f:
        _merge(cfg, tomllib.load(f))
    if secrets_path and os.path.exists(secrets_path):
        if os.stat(secrets_path).st_mode & 0o077:
            raise SystemExit(f"{secrets_path} must be chmod 600")
        with open(secrets_path, "rb") as f:
            cfg["secrets"] = tomllib.load(f)
    else:
        cfg["secrets"] = {}
    return cfg


def catalog_paths(cfg):
    """{"movies"|"movies_4k"|"series": (local path, path as Jellyfin sees it) or None}."""
    c = cfg["catalog"]
    root = c["root"].rstrip("/")
    jroot = (c.get("jellyfin_root") or root).rstrip("/")
    out = {}
    for k in ("movies", "movies_4k", "series"):
        sub = (c.get(k) or "").strip("/")
        out[k] = (f"{root}/{sub}", f"{jroot}/{sub}") if sub else None
    return out
