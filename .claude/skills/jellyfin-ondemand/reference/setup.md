# Setup

## First steps (any install)

```bash
mkdir -p config data catalog && touch catalog/.ondemand-marker
cp config.example.toml config/config.toml
cp secrets.example.toml config/secrets.toml && chmod 600 config/secrets.toml
```
Ask the user for: a Jellyfin API key (Dashboard → API Keys → `+`), to be written by them into
`secrets.toml [jellyfin] api_key`. Then follow A or B, and finish with `cli doctor`.

## A. Nothing installed yet → full example stack

`mkdir -p downloads && docker compose -f docker/compose.example.yml up -d --build`. Jellyfin is at
`http://<host>:8096` (through Caddy); finish its first-run wizard, then:

1. qBittorrent: `C logs qbittorrent | grep -i password` gives the temporary WebUI password. Whitelist the
   Docker subnet (WebUI options → bypass authentication for clients in whitelisted subnets,
   `172.16.0.0/12`) or put `username`/`password` in `secrets.toml [qbittorrent]`.
2. Libraries (ask first): `/catalog/Movies` → a Movies library; `/catalog/Movies 4K` → a separate Movies
   library; `/catalog/Series` → Shows library. Real-time monitoring off on them.
3. Every user: "Force remote media transcoding" on.
4. Plugins → Catalog → JavaScript Injector, restart Jellyfin, then (ask first)
   `JELLYFIN_URL=http://<host>:8096 JELLYFIN_API_KEY=<key> bash web/install-script.sh web/search.js "On demand: search"`.
5. `C restart ondemand` → `cli doctor` → `cli preview` → `cli import`.

## B. Jellyfin and qBittorrent already running → ondemand alone

Use `docker/compose.ondemand-only.yml` (never the full example: port clash). It publishes 8097/8098,
mounts `./config` and `./data`, and reads the rest from `docker/.env` (git-ignored):
```
DOWNLOADS_DIR=/data/torrents   # qBittorrent's download folder ON THE HOST (mounted read-only at /downloads)
CATALOG_DIR=/abs/path/catalog  # optional; default ./catalog. Elsewhere → create .ondemand-marker THERE
PUID=1000                      # owner of catalog/ and data/; must be able to read the downloads
PGID=1000
```
Ask the user (one message):
- Jellyfin version (10.11 needed) and whether it runs in Docker — it must mount the catalog folder (a new
  volume, e.g. `<catalog>:/media/ondemand`, then restart Jellyfin);
- is qBittorrent in Docker? Its `save_path`/`path_map` use **its own** paths, `DOWNLOADS_DIR` the
  **host** path; incomplete folder in use? version 4.6?
- qBittorrent access: whitelist the Docker subnet `172.16.0.0/12` in its WebUI, or user/password in
  `secrets.toml [qbittorrent]` (`username`, `password`);
- for a Prowlarr/Jackett provider: its address (ondemand AND qBittorrent must reach it) → `reference/providers.md`;
- whether a reverse proxy fronts Jellyfin (needed only for the search page, see below).

Config keys (`grep -n -A12 '^\[gateway\]' config.example.toml` etc. for the exact syntax):

| Key | Value |
|---|---|
| `[jellyfin] url` | Jellyfin as seen from the ondemand container, e.g. `http://<server-ip>:8096` |
| `[gateway] public_url` | ondemand as seen from Jellyfin: `http://<this host>:8097` (written in every `.strm`; change later → `cli rewrite-strm`) |
| `[gateway] video_clients` | Jellyfin's source IP: its LAN IP, or for a Jellyfin on the same host the Docker bridge range `["172.16.0.0/12"]`. A refused request logs `request from <ip> refused` → add that IP |
| `[qbittorrent] url` | WebUI from ondemand, e.g. `http://<server-ip>:8080` |
| `[qbittorrent] save_path` | qBittorrent's own path for catalog torrents, e.g. `/data/torrents/ondemand` |
| `[qbittorrent] path_map` | `{ "<qBittorrent path prefix>" = "/downloads" }` (+ incomplete folder) |
| `[catalog] jellyfin_root` | the catalog's path inside Jellyfin, e.g. `/media/ondemand` |

Same host with a firewall (ufw etc.): allow the Docker range to reach the host's 8097 (Jellyfin → gateway)
and qBittorrent's port (ondemand → WebUI).

Then the Jellyfin steps of A (2–4, ask first), `C up -d --build`, `cli doctor`.

Without a reverse proxy: catalog and playback work; only the search page needs `/ondemand/*` on
Jellyfin's address. Smallest fix: put Caddy in front (copy `docker/Caddyfile`, set the two upstreams to
`<jellyfin host>:8096` and `<this host>:8098`) and open Jellyfin through it.

## C. Keys people usually change

- `[subtitles] language = "pt-BR"` (BCP 47; list in `ondemand/languages.py`) + `secrets.toml [subdl] api_key`.
- `[movies] resolutions` (add `"SD"`), `allowed_language_tags` (e.g. `["SPANISH","LATINO"]`), size caps.
- `[movies]`/`[series] freeleech_min` (100 = private trackers only freeleech; 0 = everything).
- `[series] enabled`, `resolutions`, `pack_max_gb`; `[catalog] movies_4k = ""` to skip 4K; `import_interval_h`.
- `[lifecycle] days_unwatched`, `commit_minutes`; `[search] daily_limit`, `check_availability`.

## D. Optional

- ABR ladders: `docs/abr.md` (VAAPI GPU; `tools/generate-abr.sh`, `[abr] root`, `web/abr.js`).
- TS away from home: `web/hls-ts.js`. Live TV zapping: `web/extras/README.md`.
- Software-transcode watchdog (weak CPUs): host cron, `tools/transcode_watchdog.py --gateway <host>:8097 --data <data dir>`.
- Without Docker: Python ≥ 3.11, `ONDEMAND_CONFIG=… ONDEMAND_SECRETS=… python3 -m ondemand.service`.
