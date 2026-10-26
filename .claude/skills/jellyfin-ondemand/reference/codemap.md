# Code map

Python ≥ 3.11, standard library only. Tests: `python3 -I -m unittest discover -s tests -t .` (~40 s).
Web: ES5 only; `node --check web/*.js`; `node web/tests/search.test.js` needs `npm i --no-save jsdom@24`.

| Module (`ondemand/`) | Responsibility | Tests (`tests/`) |
|---|---|---|
| `service.py` | wiring (`build`), `Service.resolver` (first request starts the torrent), scheduler loops, import job | `test_service.py` |
| `gateway.py` | HTTP server for `/v/<id>`, Range, waits for pieces, `AccessList`, `Metrics` | `test_gateway.py` |
| `torrent.py` | add/prioritise/force-start in qBittorrent, `choose_video`, `check_safety`, `to_local` | `test_torrent.py` |
| `qbit.py` · `pieces.py` · `bencode.py` · `sources.py` · `http_range.py` | qBittorrent API · piece watcher · .torrent parsing · byte sources · Range header | `test_pieces.py`, `test_bencode.py`, `test_http_range.py` |
| `lifecycle.py` | trial → committed → seeding → removed, throttling | `test_lifecycle.py` |
| `catalog.py` | folders/.strm/placeholder subtitles, LockData | `test_catalog.py` |
| `db.py` | SQLite schema, `save_title`, `save_release` (provider competition) | via others |
| `release_name.py` | parse release names (movie/series) | `test_series_filter.py`, `test_movie_filter.py` |
| `movie_filter.py` | movie candidates → versions; `MovieResolver` (TMDb via Jellyfin) | `test_movie_filter.py` |
| `series_filter.py` · `tvmaze.py` | series candidates → one winner per episode; TVmaze client | `test_series_filter.py`, `test_tvmaze.py` |
| `series_ondemand.py` | one whole series across providers | `test_series_ondemand.py` |
| `search.py` · `search_series.py` | search API (`/ondemand/api/*`), requests, "complete" | `test_search.py`, `test_search_series.py` |
| `abr.py` | ABR ladder routes | `test_abr.py` |
| `providers/base.py` · `registry.py` | `Provider`, `TorrentItem`, `Http`; loading `[[providers]]`, isolation, archive | `test_providers.py` |
| `providers/torznab.py` · `unit3d.py` · `qbit_rss.py` · `archive_org.py` | built-in adapters | `test_providers.py` |
| `archive.py` | items of feed-only providers, searched by name | `test_series_ondemand.py` |
| `subtitles/` | SAMI/SRT formats, SubDL, on-play fetcher | `test_subtitles.py` |
| `jellyfin.py` | Jellyfin API, computed item ids | via others |
| `config.py` · `languages.py` · `doctor.py` · `cli.py` | defaults · subtitle languages · setup checks · CLI | `test_doctor.py` |

Other: `web/` (search.js, abr.js, hls-ts.js, extras/zapping.js, install-script.sh, tests/), `tools/`
(generate-abr.sh, transcode_watchdog.py), `docker/`, `examples/`.

Rules for changes: a test with every behaviour change; comments say *why* (most odd code is a measured
Jellyfin/qBittorrent/libtorrent behaviour — check `docs/architecture.md` before "fixing" it); never log
secrets or torrent URLs; new config keys go in `config.DEFAULTS` and `config.example.toml`
(doctor warns on unknown keys).
