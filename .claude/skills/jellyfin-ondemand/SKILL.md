---
name: jellyfin-ondemand
description: Set up, configure, troubleshoot or change jellyfin-ondemand (Jellyfin catalog of torrent releases that downloads on play). Use for installing it, connecting Jellyfin/qBittorrent, adding or writing providers, subtitles, the search page, ABR, errors on play, or editing this repository's code.
---

# jellyfin-ondemand

Answer in the user's language. Work from what the tools print, not from reading files.

## The system in one table

| Piece | Where | Notes |
|---|---|---|
| ondemand service | `ondemand/`, container `ondemand` | gateway `:8097` (`/v/<id>`, only Jellyfin reads it), search API `:8098` |
| config | `config/config.toml` (all keys + defaults: `config.example.toml`) | secrets in `config/secrets.toml`, chmod 600; read once at start → restart after edits |
| catalog | `[catalog] root` (`/catalog`), needs `.ondemand-marker` | `Movies`, `Movies 4K`, `Series` → Jellyfin libraries |
| Jellyfin 10.11 | `[jellyfin] url` + API key | users need "Force remote media transcoding" |
| qBittorrent 4.6 | `[qbittorrent] url`, `save_path`, `path_map` | 5.x unsupported |
| providers | `[[providers]]` in config | torznab, unit3d, qbit_rss, archive_org, python |
| web scripts | `web/*.js` via JavaScript Injector plugin | `web/install-script.sh`; search needs `/ondemand/*` proxied on Jellyfin's origin |
| compose files | `docker/compose.example.yml` (full stack) · `docker/compose.ondemand-only.yml` (existing Jellyfin/qBittorrent) | |

`C` = `docker compose -f <the compose file in use>`. Use `C run --rm ondemand python3 -m ondemand.cli …`
while the service is not running, `C exec ondemand python3 -m ondemand.cli …` once it is.

## Workflow

1. **Fresh clone** (no `config/` yet): do "First steps" in `reference/setup.md` first.
2. **Diagnose:** `cli doctor` (`--network` also reads every provider feed). One line per check, e.g.
   `FAIL  users  "Force remote media transcoding" off for: kid` + a `→ fix` line. Act on FAIL, then WARN.
3. **Open only the reference file(s) the task needs** (a mixed request may need two). Each names any
   extra file, and which part, worth opening:
   - installing / connecting / config keys → `reference/setup.md`
   - adding, choosing or writing a provider → `reference/providers.md`
   - something does not work (play, catalog, subtitles, search) → `reference/troubleshooting.md`
   - changing code or tests → `reference/codemap.md`
4. Change config → `C restart ondemand` → `cli doctor` again (and `cli preview` after provider changes).

## Cheap commands (prefer these to reading files)

| Need | `cli …` |
|---|---|
| health of the whole setup | `doctor [--network]` |
| providers and capabilities | `providers` |
| what feeds would add, and why the rest was dropped | `preview [--provider NAME] [--title TEXT]` |
| one movie / series across providers | `search-movie "Name"` / `search-series "Name"` |
| counts per state | `status` |
| import now / write now | `import` / `write --force`, `write-series --force` |
| after changing `[gateway] public_url` | `rewrite-strm` |
| logs | `C logs --tail 50 ondemand` |

## Token rules

- Do not read `docs/*.md`, `tests/`, `LICENSE` or whole modules for setup questions.
- In `config.example.toml`, grep the section you need (`grep -n -A12 '^\[qbittorrent\]'`), never read it all.
- Read code only through `reference/codemap.md`; grep one symbol instead of reading a file.
- Show the user only the doctor lines that matter.

## Safety rules

- Secrets: ask the user to put keys in `config/secrets.toml` themselves. Never ask them to paste a key
  in the chat; never print `secrets.toml`, API keys, passkeys or torrent URLs (they carry passkeys).
- Ask before changing the user's Jellyfin or qBittorrent through their APIs — this includes running
  `web/install-script.sh`, creating libraries and changing users.
- Never start `docker/compose.example.yml` where the user already runs Jellyfin/qBittorrent (port clash).
- Never commit `config/`, `data/`, `catalog/`, `plugins/*`, `docker/.env` (all git-ignored).
- Built-in providers stay generic (a protocol or software); a specific site goes in the user's `plugins/`.
