# Setting up Jellyfin (and an existing install)

Tested with Jellyfin **10.11** and qBittorrent **4.6** (qBittorrent 5 renamed parts of its WebUI API).

Already running Jellyfin and qBittorrent? Use `docker/compose.ondemand-only.yml` (ondemand alone,
ports 8097/8098 published; paths in `docker/.env`). Do not start the full example stack next to them:
its Jellyfin, qBittorrent and Caddy would take the same ports.

## 1. Paths: who sees what

| Folder | jellyfin-ondemand | Jellyfin | qBittorrent |
|---|---|---|---|
| catalog (`.strm` + subtitles) | read-write (`[catalog] root`) | read-only, any path (`[catalog] jellyfin_root`) | — |
| downloads | read-only (`[qbittorrent] path_map`) | not needed (it reads through the gateway) | read-write (`save_path`) |

- Create the marker once: `touch <catalog>/.ondemand-marker`. Without it nothing is written (protects
  against an unmounted disk).
- `path_map` maps qBittorrent's paths to this container's: e.g. qBittorrent saves to `/data/torrents`
  and the same host folder is `/downloads` here → `path_map = { "/data/torrents" = "/downloads" }`.
  Include the incomplete folder if qBittorrent uses one.
- If Jellyfin mounts the catalog somewhere else (e.g. `/media/ondemand`), set
  `jellyfin_root = "/media/ondemand"`.

## 2. Network

- Jellyfin must reach `[gateway] public_url` (default `http://ondemand:8097`, the compose service name).
  `video_clients` must allow Jellyfin's address (a service name, an IP or a CIDR).
- jellyfin-ondemand must reach Jellyfin (`[jellyfin] url`) and qBittorrent's WebUI (`[qbittorrent] url`).
- qBittorrent WebUI: either whitelist the ondemand container's subnet (Options → Web UI → *Bypass
  authentication for clients in whitelisted IP subnets*) or set `username`/`password` in `secrets.toml`.
  Repeated failed logins get an IP banned by qBittorrent.
- The search API (port 8098) must be published **on the same origin as Jellyfin under `/ondemand/`**,
  because the browser scripts call `<jellyfin address>/ondemand/api/…`. The example stack does it with
  Caddy (`docker/Caddyfile`). Traefik, nginx or any proxy works: route `/ondemand/*` to `ondemand:8098`
  (with or without stripping the prefix), everything else to Jellyfin.

## 3. Jellyfin settings

1. **API key**: Dashboard → API Keys → `+` → `secrets.toml` `[jellyfin] api_key`.
2. **Libraries**:
   - add `<catalog>/Movies` as an extra folder of your Movies library (catalog and local movies mix;
     duplicates of local movies are never written);
   - add `<catalog>/Movies 4K` to a **separate** Movies library (one version per item: see
     [architecture.md](architecture.md));
   - add `<catalog>/Series` to your Shows library, with automatic series grouping on.
   - Turn **real-time monitoring off** on those libraries: the catalog announces its own folders, and
     the monitor would rescan on every write. Local media added later then needs a library scan (or
     keep monitoring on in a library that has no catalog folder).
   - The TMDb plugin is used for search and identification (it ships with Jellyfin).
3. **Users**: for every user, *Force remote media transcoding* (`ForceRemoteSourceTranscoding`) **on**.
   Without it some clients try to open the gateway URL directly.
4. Recommended: hardware transcoding on. A blind software transcode of a 4K stream can take a small
   server down; `tools/transcode_watchdog.py` is a safety net for that.
5. Optional: if you use the Intro Skipper plugin, exclude the catalog path (it would read every file).

## 4. The search page

Install the [JavaScript Injector](https://github.com/n00bcodr/Jellyfin-JavaScript-Injector) plugin, then:

```bash
export JELLYFIN_URL=http://localhost:8096 JELLYFIN_API_KEY=…
bash web/install-script.sh web/search.js "On demand: search"
```

Reload the browser. Type at least 3 letters on the search page: a section "Outside the library"
appears after Jellyfin's own results. The texts follow Jellyfin's display language (English and
Portuguese included; add yours to `TEXTS` in `web/search.js`). Apps that do not run jellyfin-web
scripts (TV apps) still play everything already in the catalog.

## 5. Subtitles

Set `[subtitles] language` (e.g. `"pt-BR"`, `"es"`) and a SubDL key. Each new `.strm` gets a placeholder
subtitle; the real one is fetched when the torrent starts and swapped in without restarting the video.
Set Jellyfin's subtitle preferences per user as usual.

## 6. Check it

`python3 -m ondemand.cli doctor` checks every item of this page and prints how to fix what fails.

## 7. Monitoring (optional)

`GET :8097/metrics` (from `admin_clients`) exposes Prometheus metrics: starts, blocks, first-byte
timeouts, piece waits, active streams, torrents per state, pending catalog titles, search requests.
