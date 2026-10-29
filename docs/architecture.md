# Architecture

One Python process (standard library only) with four jobs:

| Piece | Module | What it does |
|---|---|---|
| Gateway | `gateway.py`, `service.py` | serves `/v/<release id>` to Jellyfin; on the first request starts the torrent and waits for the first bytes |
| Torrent bridge | `torrent.py`, `qbit.py`, `pieces.py`, `bencode.py` | adds the torrent to qBittorrent the safe way, picks the video file, maps byte ranges to pieces |
| Catalog | `catalog.py`, `movie_filter.py`, `series_filter.py`, `providers/` | turns provider items into one `.strm` per movie version / per episode |
| Lifecycle | `lifecycle.py` | trial → committed → seeding → removed; throttles other downloads during playback |
| Search API | `search.py`, `search_series.py`, `series_ondemand.py` | "search outside the library" for `web/search.js`; "complete" a catalog series |
| Subtitles | `subtitles/` | placeholder at write time, real subtitle fetched on play |

State lives in one SQLite database (`db.py`). A `.strm` contains only `<public_url>/v/<id>`; the id is
random and stable, so the release behind it can change (a better torrent) without touching Jellyfin.

## Playing a title

1. Jellyfin's `PlaybackInfo` probes the `.strm` URL with ffprobe. That probe **must** succeed: if it
   fails Jellyfin plays the item as a live stream without knowing its codec and transcodes everything
   on the CPU. So the first request starts the torrent and waits up to `first_byte_wait_s` (60 s; the
   Android TV app gives up at 30 s, web clients wait).
2. `torrent.start`: add **paused** (sequential + first/last piece, no ratio limit, tagged `od-<id>`),
   wait until it appears under its tag, export the `.torrent` to know the real file offsets (pad files
   included), set file priorities **while still paused** (changing them on a running torrent stalls
   libtorrent for 10–60 s), then **force start** — never `resume`, which turns force start off.
   - Never priority 7 on the video: in sequential mode libtorrent ignores priorities except 7, which
     turns the whole file into "rarest first" and the beginning never arrives.
   - Magnet links: a paused torrent never fetches metadata in qBittorrent 4.6, so magnets are added
     unpaused with `stopCondition=MetadataReceived`.
   - A torrent that already exists in qBittorrent (same infohash, not ours) is reused and marked
     *foreign*: never paused, never deleted.
3. The gateway only sends bytes covered by **complete pieces** (`pieceStates`). A sparse file returns
   zeros for missing ranges, so it is never read blindly. `503` is only possible before the headers;
   mid-body it waits for the piece (`stall_wait_s`), because ffmpeg does not reconnect.
4. A failed probe blocks that release for `block_s`: the next request would be a "blind" transcode
   that Jellyfin re-creates on every player retry.

Why **one version per item**: PlaybackInfo probes *every* version of an item on each play; with a
version picker, one play started three torrents. So the 4K release is a separate item in its own
library.

## The catalog

Providers return `TorrentItem`s (see [writing-a-provider.md](writing-a-provider.md)). Every import:

- **Movies** (`movie_filter.py`): parse the release name → drop by seeds/freeleech/language/resolution/
  size → identity (IMDb id from the provider, or name + year through Jellyfin's TMDb RemoteSearch;
  unsure = quarantine) → one winner per version (SDR over HDR, source, PROPER, private over public with
  at least half the seeds, seeds, x264, smaller).
- **Series** (`series_filter.py`): parse → drop → identity on TVmaze (exact original name, premiere year,
  country, item date vs air date to tell namesakes apart) → the episode must exist in TVmaze's list →
  one winner per episode (season's dominant resolution, source, PROPER, pack vs single, seeds) → only
  the episodes missing from your local series are written.

Folders are written in small batches **only while nobody is watching** (each new folder is a metadata
lookup in Jellyfin's single queue), and only if `<root>/.ondemand-marker` exists — a missing marker
means the disk is not mounted and nothing is written under the empty mount point. New folders are
announced with `/Library/Media/Updated` (no full scan); a search request uses a priority refresh of the
library folder (~4 s instead of ~77 s).

Different providers compete for the same version: the one with more seeds wins, but a release that has
already played keeps its torrent (the real subtitle and a running download point at that encode).

### The archive: nothing a provider showed is lost

Feeds only show their latest items and any source may take a torrent down, so every item a provider
returns (every feed, and the results of a series search) is kept in the `archive` table (`archive.py`):
the address, what the source said (ids, files, seeds of the last sighting) and, for series, what the
filter decided — `winner`, `fallback` (lost to another torrent for the same episode), `quarantine` (a
discard that may change on its own: unresolved name, a season TVmaze does not list yet), `discarded`,
or `dead` (playback proved it useless).

- **Every import** filters what the feeds show now **plus** the archive's due items: new ones and
  quarantine whose turn came (back-off 1, 2, 4… days up to 30), together with every live item of the
  same series, `reevaluate_names` series per round (catalog series first). A season announced before
  TVmaze listed it comes in once TVmaze does, months after it left the feed.
- **Feed-only providers** are also read every `collect_min` just to archive (no filters, no TVmaze),
  so a service that is down for a day loses no item.
- **TVmaze answers expire** (`tvmaze.py`): an episode list is good for 2 days — a cache that never
  expires freezes the list and a new season never gets past the filter. Expired + TVmaze down = the
  old answer.
- **Fallback** (`fallback.py`): when an episode's torrent does not play, the archived item counts a
  failure — at once if it is fake or lacks the episode's video, otherwise on the 2nd failure at least
  30 min apart (cold magnet, tracker down, empty swarm). Once dead, the filter runs on the archive alone
  and every episode of that torrent switches to its new winner, **keeping its id and `.strm`** (the
  subtitle goes back to the placeholder). The next click plays the fallback.
- Items of a series in the catalog are kept for good; the rest leave after 365 days unseen.
  `cli archive` shows it (never the URL); `cli fallback RELEASE` switches by hand.

## Lifecycle

```
trial ──(public: 15 min watched | private: at once)──> committed ──(100 %)──> seeding
  └──(public: stopped early + 10 min idle)──> removed
seeding ──(provider's seed rule + 2 days unwatched + nobody reading)──> removed
```

- A saved position ("continue watching") holds a torrent for `days_with_position` and counts as a
  commitment: without the torrent, resuming at minute 40 would mean downloading from the start again.
- Private torrents go to 100 % (the video first; the other files are released afterwards, since seeding
  only counts with the whole torrent).
- An empty answer from qBittorrent (restarting) never marks torrents as removed: each one is confirmed.

## Jellyfin facts this code depends on (10.11)

- `/Items` with only an API key and no `userId` **omits items created after the server started**.
  Every listing sends the admin's `userId`; lookups by `Ids` are preferred.
- Item ids are computable: `Guid(MD5(UTF-16LE(type + path)))` — the `.strm` path gives the movie or
  episode id without listing anything.
- An external subtitle added later to a `.strm` item disappears on the next play (bug #15882): every
  `.strm` is born with a placeholder `<name>.<iso>.smi`, overwritten in place. SAMI because remote `.srt`
  reaches some clients as a local path; UTF-8 **with BOM** because short files are read as Mac Roman.
- `ForceRemoteSourceTranscoding` must be on for every user: clients must not try to open the gateway URL
  directly.
- `LockData` after identification stops every play from re-running the metadata providers.
- `RecursiveItemCount`/`ChildCount` only come back with `EnableUserData=true`.
