<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg">
    <img alt="jellyfin-ondemand — a Jellyfin catalog that downloads only when you press play" src="docs/assets/banner-light.svg" width="100%">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/NAbobo101/jellyfin-ondemand/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/NAbobo101/jellyfin-ondemand/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: GPL-3.0" src="https://img.shields.io/badge/license-GPL--3.0-blue"></a>
  <img alt="Python 3.11+, no dependencies" src="https://img.shields.io/badge/python-3.11%2B%20%C2%B7%20zero%20deps-3776AB?logo=python&logoColor=white">
  <img alt="Jellyfin 10.11" src="https://img.shields.io/badge/Jellyfin-10.11-AA5CC3?logo=jellyfin&logoColor=white">
  <img alt="qBittorrent 4.6" src="https://img.shields.io/badge/qBittorrent-4.6-2F67BA?logo=qbittorrent&logoColor=white">
</p>

<p align="center">
  <b>English</b> · <a href="README.pt-BR.md">Português (Brasil)</a>
</p>

---

**Your library looks full. Your disk stays empty.** jellyfin-ondemand fills Jellyfin with movies and
series from the torrent sources *you* choose — real items, with posters, metadata, search and
"continue watching" — and downloads nothing until someone presses play. Then the torrent starts and
the video streams **while it downloads**. When the source's seeding rule is met and nobody is watching
any more, the torrent goes away and the title stays in the catalog, ready for next time.

## How it works

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/how-it-works-dark.svg">
    <img alt="Providers → filters → catalog → press play; after seeding, the torrent is deleted" src="docs/assets/how-it-works-light.svg" width="100%">
  </picture>
</p>

1. **Providers** bring releases: an indexer through Prowlarr, a private tracker's API, an RSS feed — or a plugin you write.
2. **Filters** pick one release per movie version or per episode, and check every identity on TMDb and TVmaze.
3. **The catalog** is a folder of tiny `.strm` files inside your Jellyfin libraries. They take no space.
4. **Press play**: qBittorrent starts the torrent (sequential, first and last pieces first) and a small gateway hands Jellyfin only the pieces that have arrived.

The details, and why each piece works the way it does: [docs/architecture.md](docs/architecture.md).

## Highlights

<table>
<tr>
<td width="50%" valign="top">

### 🔌 Bring your own providers
Generic adapters for **Torznab** (Prowlarr/Jackett: hundreds of indexers, no code), **UNIT3D**
trackers, **RSS feeds in qBittorrent** and the **Internet Archive**. Anything else is a Python plugin of
about 40 lines.

</td>
<td width="50%" valign="top">

### 🔎 Search outside the library
A section on Jellyfin's own search page shows what you do *not* have yet. One tap adds a movie and
plays it; for a series, one tap brings in every season available.

</td>
</tr>
<tr>
<td valign="top">

### 📺 Series done right
One release per episode chosen across all sources: season coherence, source quality, PROPER, pack vs
single episode, seeds. It only fills the gaps of the series you already have.

</td>
<td valign="top">

### 🤝 Friendly to private trackers
A private torrent is never abandoned halfway. Each provider has its own seeding rule
("72 h OR ratio 1.0") and freeleech filter. Passkeys never reach a log, and only qBittorrent fetches
`.torrent` files.

</td>
</tr>
<tr>
<td valign="top">

### ▶️ Playback comes first
Catalog writes and heavy work wait while someone is watching. Other downloads are throttled while a
catalog title is playing and still downloading.

</td>
<td valign="top">

### 💬 Subtitles in your language
A subtitle in your language is fetched on play and swapped in without restarting the video. The search
page speaks English and Portuguese, and adding a language is one table.

</td>
</tr>
<tr>
<td valign="top">

### ✨ Extras for jellyfin-web
Adaptive bitrate from pre-generated HLS ladders, exact seeks away from home (TS segments), and channel
zapping for Live TV. All optional.

</td>
<td valign="top">

### 🪶 Runs on a Celeron
It uses only Python's standard library: no dependencies, about 20 MB of RAM, and ~250 tests. It grew
out of a setup that runs every day on a small home server.

</td>
</tr>
</table>

## Why not just Radarr/Sonarr?

They are great at building a library you *keep*. jellyfin-ondemand is for everything you *might* watch:

| | Download first (*arr) | Download on play (ondemand) |
|---|---|---|
| Disk space | every title you add | only what is being watched or seeded |
| Catalog size | limited by your disks | limited by your sources |
| Seeding | rules you set and watch | per-provider rules, then automatic cleanup |
| First play | instant | a few seconds while the torrent starts |

They also work well together. Torrents that already exist in your qBittorrent are reused and never
deleted, and catalog episodes only fill the gaps of the series you keep locally.

## Quick start (Docker)

You need Docker with Compose. The example stack brings Jellyfin 10.11, qBittorrent 4.6 and a Caddy
reverse proxy.

```bash
git clone https://github.com/NAbobo101/jellyfin-ondemand && cd jellyfin-ondemand
mkdir -p config data catalog downloads && touch catalog/.ondemand-marker
cp config.example.toml config/config.toml                 # providers, subtitle language…
cp secrets.example.toml config/secrets.toml && chmod 600 config/secrets.toml
docker compose -f docker/compose.example.yml up -d --build
```

<details>
<summary><b>Then, once, in Jellyfin (5 steps)</b></summary>

1. Create an API key (Dashboard → API Keys) and put it in `config/secrets.toml`. Let qBittorrent's
   WebUI accept the ondemand container: whitelist the subnet, or put a username and password in the
   secrets.
2. Add `/catalog/Movies` to a Movies library, `/catalog/Movies 4K` to a **separate** one and
   `/catalog/Series` to your Shows library. Turn **real-time monitoring off** for those libraries.
3. For every user, turn on **Force remote media transcoding** (`.strm` items need it).
4. Install the [JavaScript Injector](https://github.com/n00bcodr/Jellyfin-JavaScript-Injector) plugin, then:
   ```bash
   JELLYFIN_URL=http://localhost:8096 JELLYFIN_API_KEY=… bash web/install-script.sh web/search.js "On demand: search"
   ```
5. See what your providers would add, then import:
   ```bash
   docker compose -f docker/compose.example.yml exec ondemand python3 -m ondemand.cli preview
   docker compose -f docker/compose.example.yml exec ondemand python3 -m ondemand.cli import
   ```

Already running Jellyfin and qBittorrent? Keep only the `ondemand` service: [docs/jellyfin-setup.md](docs/jellyfin-setup.md).

</details>

The example config ships with one provider, the Internet Archive's public-domain films, so you can
watch the whole flow work before adding your own.

## Bring your own providers

```toml
[[providers]]
name = "my-indexer"
type = "torznab"                    # torznab | unit3d | qbit_rss | archive_org | python
url = "http://prowlarr:9696/1/api"
private = true
seed = { min_hours = 0, min_ratio = 1.0 }
# secrets.toml → [providers.my-indexer] api_key = "…"
```

| Type | For | Feed | Movie search | Series search |
|---|---|:-:|:-:|:-:|
| `torznab` | anything in Prowlarr or Jackett | ✓ | ✓ | ✓ |
| `unit3d` | private trackers running UNIT3D | ✓ | ✓ | ✓ |
| `qbit_rss` | RSS feeds read by qBittorrent (the passkey stays there) | ✓ | archive | archive |
| `archive_org` | Internet Archive public-domain films | ✓ | ✓ | — |
| `python` | your own plugin | you choose | you choose | you choose |

"Archive": a feed without search has its items kept on every import and looked up by name later.

Writing a provider takes a class with up to three methods:

```python
from ondemand.providers import Provider, TorrentItem
from ondemand.providers.base import Http

class MySite(Provider):
    http = Http(pause=1.0)                # paced requests; errors never carry the URL

    def feed(self):                       # latest uploads, for the periodic import
        return [self.stamp(TorrentItem(title=r["name"], torrent_url=r["magnet"], seeds=r["seeders"]))
                for r in self.http.get_json(self.option("url") + "/latest")["results"]]

    def search_movie(self, query): ...    # optional: query.imdb, query.title, query.year
    def search_series(self, query): ...   # optional: query.name, query.tvdb, query.seasons
```

Parsing, identification, filtering, playback and seeding are shared by every provider. The full guide,
with a tested example: [docs/writing-a-provider.md](docs/writing-a-provider.md).

## Documentation

| | |
|---|---|
| [Architecture](docs/architecture.md) | the pieces, and the Jellyfin/qBittorrent behaviours they rely on |
| [Writing a provider](docs/writing-a-provider.md) | the provider interface, step by step |
| [Jellyfin setup](docs/jellyfin-setup.md) | paths, network, libraries, users, reverse proxy |
| [ABR and TS](docs/abr.md) | optional: adaptive bitrate ladders, exact seeks away from home |
| [FAQ](docs/faq.md) | troubleshooting |
| [Live TV zapping](web/extras/README.md) | optional: channel switching for IPTV |

## Roadmap

- qBittorrent 5 (its WebUI API renamed pause/resume)
- More languages for the web texts and subtitles
- Removing never-played catalog titles after a configurable time
- More generic built-in providers (protocols and software, never specific sites)

Ideas and pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md).

> [!IMPORTANT]
> **This project ships no content sources.** You choose which providers to configure, and you are
> responsible for following the law and each source's rules. See [DISCLAIMER.md](DISCLAIMER.md).

## License

[GPL-3.0](LICENSE). Found a security issue? See [SECURITY.md](SECURITY.md).
