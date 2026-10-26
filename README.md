# jellyfin-ondemand

**A Jellyfin catalog that downloads only when you press play.**

[Português (Brasil)](README.pt-BR.md)

jellyfin-ondemand puts movies and series from torrent sources *you* configure into your Jellyfin
libraries as lightweight `.strm` items — posters, metadata, search, "continue watching", all native.
Nothing is downloaded until someone presses play. Then the torrent starts in qBittorrent (sequential,
first and last pieces first) and a small gateway streams it to Jellyfin **while it downloads**. When
the seeding rule of the source is met and nobody is watching, the torrent is deleted and the title goes
back to being just a catalog entry.

- **Bring your own providers.** Generic adapters for **Torznab** (Prowlarr/Jackett — hundreds of
  indexers without code), **UNIT3D** trackers, **RSS feeds in qBittorrent**, and the **Internet Archive**
  (public-domain films, for a first test). Or write a provider in ~40 lines of Python.
- **Search outside the library.** A section on Jellyfin's own search page shows movies and series you
  do not have yet; one tap adds the movie and plays it, or adds a whole series (every season available).
- **Series done right.** One release per episode chosen across all sources (season coherence, source
  quality, PROPER, pack vs single episode, seeds), identities checked against TVmaze's episode lists,
  only the gaps of your local series filled.
- **Plays nicely with private trackers.** Private torrents are never abandoned halfway, seeding rules
  per provider ("72 h OR ratio 1.0"), freeleech filters, passkeys never logged, `.torrent` files only
  ever fetched by qBittorrent.
- **Playback first.** Catalog writes and heavy work wait while someone is watching; other downloads are
  throttled while a catalog title plays and still downloads.
- **Subtitles in your language** fetched on play (SubDL), shown without restarting the video.
- **Optional extras for jellyfin-web:** adaptive bitrate from pre-generated HLS ladders, TS segments
  away from home (exact seeks), and channel zapping for Live TV.

Pure Python standard library — no dependencies, ~20 MB of RAM, runs on a Celeron.

> **Legal.** This project ships no content sources. You decide which providers to configure and you
> are responsible for complying with the law and with each source's rules. See [DISCLAIMER.md](DISCLAIMER.md).

## How it works

```
 Jellyfin library ── .strm ──> http://ondemand:8097/v/<id> ──> gateway ──> qBittorrent (on play)
       ▲                                                        │  serves only complete pieces
       │ folders written while nobody watches                   ▼
   catalog  <── movie/series filters <── providers (Torznab, UNIT3D, RSS, your own…)
```

More in [docs/architecture.md](docs/architecture.md).

## Quick start (Docker)

Requirements: Docker with Compose, and a Jellyfin **10.11** server (the example stack brings one).

```bash
git clone https://github.com/NAbobo101/jellyfin-ondemand && cd jellyfin-ondemand
mkdir -p config data catalog downloads && touch catalog/.ondemand-marker
cp config.example.toml config/config.toml                 # edit: providers, subtitle language…
cp secrets.example.toml config/secrets.toml && chmod 600 config/secrets.toml
docker compose -f docker/compose.example.yml up -d --build
```

Then, once (details in [docs/jellyfin-setup.md](docs/jellyfin-setup.md)):

1. Create a Jellyfin API key and put it in `config/secrets.toml`; let qBittorrent's WebUI accept the
   ondemand container (whitelist the subnet or put a username/password in the secrets).
2. Add `/catalog/Movies` to a Movies library, `/catalog/Movies 4K` to a separate one, `/catalog/Series`
   to your Shows library. Turn **real-time monitoring off** for those paths' libraries.
3. Users → each user → **"Force remote media transcoding"** on (required by `.strm` items).
4. Install the [JavaScript Injector](https://github.com/n00bcodr/Jellyfin-JavaScript-Injector) plugin and run
   `JELLYFIN_URL=http://localhost:8096 JELLYFIN_API_KEY=… bash web/install-script.sh web/search.js "On demand: search"`.
5. `docker compose -f docker/compose.example.yml exec ondemand python3 -m ondemand.cli preview` — see
   what your providers would add, then `… cli import` (or wait for the scheduler).

## Adding providers

```toml
[[providers]]
name = "my-indexer"
type = "torznab"                  # torznab | unit3d | qbit_rss | archive_org | python
url = "http://prowlarr:9696/1/api"
private = true
seed = { min_hours = 0, min_ratio = 1.0 }
```

Secrets go in `secrets.toml` under `[providers.my-indexer]`. Writing your own provider:
[docs/writing-a-provider.md](docs/writing-a-provider.md).

## Documentation

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | the pieces and why they are the way they are |
| [docs/writing-a-provider.md](docs/writing-a-provider.md) | the provider interface, step by step |
| [docs/jellyfin-setup.md](docs/jellyfin-setup.md) | libraries, users, reverse proxy, existing installs |
| [docs/abr.md](docs/abr.md) | optional: adaptive bitrate ladders, TS away from home |
| [docs/faq.md](docs/faq.md) | troubleshooting |
| [web/extras/README.md](web/extras/README.md) | optional: Live TV channel zapping |

## Status

Used daily on a small home server (Jellyfin 10.11, qBittorrent 4.6). qBittorrent 5.x renamed parts of
its API and is not supported yet. Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[GPL-3.0](LICENSE).
