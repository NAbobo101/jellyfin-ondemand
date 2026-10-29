# Writing a provider

A provider turns a source of torrents into `TorrentItem`s. Everything else — parsing release names,
identifying movies and series, filtering, picking winners, writing the catalog, downloading on play,
seeding and cleaning up — is shared.

**First check whether you need code at all:** if the source is supported by
[Prowlarr](https://prowlarr.com/) or Jackett, use the built-in `torznab` type. RSS feeds can go through
qBittorrent's RSS reader with `qbit_rss`. UNIT3D trackers have `unit3d`.

## The interface

```python
from ondemand.providers import Provider, TorrentItem

class MyProvider(Provider):
    type = "my_site"

    def feed(self):                 # optional: items for the periodic import (latest uploads…)
        ...
    def search_movie(self, query):  # optional: MovieQuery(imdb, tmdb, title, original_title, year)
        ...
    def search_series(self, query): # optional: SeriesQuery(name, year, tvdb, imdb, tmdb, seasons)
        ...
```

Implement only what the source can do. `capabilities` is derived from the methods you override. Every
item a provider returns is archived; a provider with a feed but no search is searched there by name
later (that is how RSS-only trackers take part in "Add series"), and series items stay available as
fallbacks even after the source drops them.

### TorrentItem

| field | |
|---|---|
| `title` | the release name as the source shows it — **the most important field**: quality, season, episode and language come from it |
| `torrent_url` | an http(s) link to the `.torrent` or a magnet. qBittorrent fetches it, not this service — it may carry your passkey |
| `infohash` | if known (lets an existing torrent in qBittorrent be reused); with only an infohash a magnet is built |
| `seeds`, `size`, `published` | integers / bytes / unix time |
| `ids` | `{"imdb", "tmdb", "tvdb"}` of the **movie** or of the **series** (not of the episode). With ids no name matching is needed |
| `freeleech` | percent; `None` = unknown. Private items below `freeleech_min` are dropped |
| `kind` | `"movie"`, `"series"` or `None` (guessed from the title) |
| `files` | file names inside the torrent, if the source lists them — lets a season pack say exactly which episodes it has |
| `id` | a stable id within the provider (default: infohash, then URL, then title) |

Always pass items through `self.stamp(item)`: it fills in the provider name, the private flag and the
per-provider resolution override.

### Helpers on `Provider`

- `self.option(key, default)` — the `[[providers]]` table; `self.secret(key)` — `secrets.toml`
  `[providers.<name>]` (falls back to an option of the same name); `self.require("url", "api_key")`
  stops at start-up with a clear message.
- `self.private`, `self.seed_rule` — from `private = …` and `seed = {min_hours, min_ratio, either}`.
- `self.trusted` — from `trusted = true`: public torrents of this provider skip the file check (no
  executables/archives, plausible video size). Leave it off unless the source's torrents are known good.
- `ondemand.providers.base.Http` — a paced HTTP client (`get`, `get_json`, `post_json`) whose errors are
  `ProviderError` **without the URL** (it may contain a key). Raise `ProviderError` for any source
  failure: the registry turns it into a warning and the other providers carry on.
- `parse_size("1.4 GB")`, `clean_ids({...})`, `magnet(infohash, name)`.

## Step by step

1. Copy [`examples/json_api_provider.py`](../examples/json_api_provider.py) into your `plugins/` folder
   (mounted at `/plugins`) and adapt `_items` to your source's answer.
2. Add it to `config.toml`:
   ```toml
   [[providers]]
   name = "example-site"
   type = "python"
   class = "json_api_provider:JsonApiProvider"
   url = "https://torrents.example"
   private = false
   ```
3. `python3 -m ondemand.cli providers` — is it loaded, with the capabilities you expect?
4. `python3 -m ondemand.cli preview --provider example-site` — what would enter the catalog, and why
   everything else was discarded (by reason). Fix the parsing until the discards make sense.
5. `python3 -m ondemand.cli search-movie "Some Movie"` / `search-series "Some Show"` for the search paths.
6. Write a test with a fake HTTP answer — see `tests/test_example_provider.py`.

## Rules of thumb

- **Never log or print a torrent URL**; never download the `.torrent` yourself.
- Keep requests polite: one at a time, a pause between them (`Http(pause=…)`), no hammering in `feed()`.
- Prefer ids over names. If the source knows the IMDb/TVDB id, put it in `ids`.
- Mark private trackers `private = true` and give their real seeding rule: a private torrent is never
  abandoned halfway and is only deleted after the rule is met.
- Built-in providers in this repository must be generic (a protocol or software). Providers for specific
  sites belong in your plugins folder or in your own repository.
