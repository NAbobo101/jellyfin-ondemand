# Providers

Pick the first that fits; only write code for the last case. After any change: `C restart ondemand`.

| Source | Type | Config (besides `name`, `type`, `private`, `seed`) | Secret `[providers.<name>]` |
|---|---|---|---|
| anything Prowlarr/Jackett supports | `torznab` | `url` (Prowlarr: `http://<prowlarr>:9696/<indexer id>/api`); optional `movie_categories` (default `[2000]`), `series_categories` (default `[5000]`), `id_search` (default `true`: IMDb/TVDB queries; `false` if the indexer only does text) | `api_key` (Prowlarr's) |
| UNIT3D private tracker | `unit3d` | `url` (site base); `movie_categories`, `series_categories` (the site's category ids, required); `feed_pages` | `api_token` |
| RSS feed with passkey | `qbit_rss` | add the feed in qBittorrent's RSS reader (no auto-download) inside `folder` (default `ondemand`); `feeds = ["Feed name"]` | — |
| public-domain films | `archive_org` | `collection`, `rows` (already `trusted`) | — |
| anything else | `python` | `class = "module:Class"`, file in `plugins/` (mounted at `/plugins`) | free |

- `name`: unique, letters/digits/`-`/`_` (it is the `[providers.<name>]` table and is stored with releases — do not rename later).
- `private = true` → never abandoned halfway, deleted only after `seed` is met.
- `seed = { min_hours = 72, min_ratio = 1.0, either = true }` → "72 h OR ratio 1.0"; `either` defaults to
  `false` (both needed). Defaults when `seed` is omitted: private → ratio 1.0, public → nothing.
- **Private trackers + freeleech:** by default only freeleech releases of private providers are kept
  (`[movies]`/`[series] freeleech_min = 100`, global, not per provider). If the tracker has little
  freeleech, tell the user and offer `freeleech_min = 0` (downloads then count against their ratio).
- `resolutions = [...]` overrides the accepted resolutions for this provider; `enabled = false` turns it off;
  `trusted = true` skips the public-torrent file check (known-good sources only).
- Torznab through Prowlarr: download links are proxied by Prowlarr → qBittorrent must reach Prowlarr too.

## Verify

`cli providers` (loaded? capabilities?) → `cli doctor --network` (feed answers?) →
`cli preview --provider NAME` (what enters; discard reasons) → `cli search-movie "X"` /
`search-series "X"` → `cli import`. Some "no year in the name" / "series not resolved" discards are normal.

## Writing one

Copy `examples/json_api_provider.py` to `plugins/` and adapt `_items()`. Contract:

- subclass `ondemand.providers.Provider`; implement any of `feed()`, `search_movie(q)` (`q.imdb, q.tmdb,
  q.title, q.original_title, q.year`), `search_series(q)` (`q.name, q.year, q.tvdb, q.imdb, q.seasons,
  q.queries()`); each returns a list of `TorrentItem`.
- `TorrentItem(title=<release name>, torrent_url=<.torrent url or magnet>, infohash, seeds, size,
  published, ids={"imdb","tmdb","tvdb"} of the movie/series, freeleech, kind, files)`; wrap each in `self.stamp(...)`.
- HTTP through `ondemand.providers.base.Http` (paced; errors are `ProviderError` without the URL);
  `self.option(k)`, `self.secret(k)`, `self.require(...)`.
- Never download the `.torrent` or log a URL. Test with fake HTTP like `tests/test_example_provider.py`.
Full guide (only if needed): `docs/writing-a-provider.md`.
