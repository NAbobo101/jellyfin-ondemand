# FAQ and troubleshooting

**First, always:** `python3 -m ondemand.cli doctor` (in the container; `docker compose … run --rm ondemand
python3 -m ondemand.cli doctor` if the service does not start). It checks config, secrets, the catalog
marker, Jellyfin (key, libraries, users, scripts), qBittorrent (version, access, path mapping) and the
providers, and prints how to fix each failure. `--network` also reads every provider's feed.

**Nothing is written to the catalog.** Check `<root>/.ondemand-marker` exists, then
`cli providers` and `cli preview`: the discards are counted by reason. Writes also wait while someone is
watching. `docker compose logs ondemand` shows each import.

**Play fails right away (error in the player).** The gateway answered 503: the torrent did not deliver
its first bytes within `first_byte_wait_s` (no seeders, tracker down, qBittorrent unreachable). The
release is blocked for `block_s` (120 s) to avoid a blind transcode — try again after that. The log line
`-> GET <id>` followed by `unavailable:` says why.

**The first play is slow.** A cold torrent needs peers and the first/last pieces: 10–60 s is normal,
private trackers through a VPN can take longer. The second play is instant.

**"path outside the configured [qbittorrent] path_map".** qBittorrent saved the torrent somewhere this
container cannot see: add that prefix to `path_map` (and mount it).

**Jellyfin shows the folder name instead of the movie.** Metadata is fetched in Jellyfin's queue; a
batch of new folders can take minutes. Items are locked (`LockData`) once identified.

**A series shows only some seasons.** `cli search-series "Name"` lists what each provider has and what
is missing; TVmaze's episode list is the reference. A season TVmaze does not list yet waits in the
archive's quarantine and gets in by itself once TVmaze does (`cli archive --series "Name"`).

**An episode does not play.** If the torrent is fake, lacks the episode or has no seeders twice in a row,
the episode switches to the next torrent kept in the archive (same item, same `.strm`): try again. By
hand: `cli fallback RELEASE` (the id at the end of the `.strm` URL).

**Subtitles never arrive.** `[subtitles] language` set? SubDL key in `secrets.toml`? SubDL has a daily
quota: retries happen hourly, up to `max_attempts`.

**Can I use it without Docker?** Yes: Python ≥ 3.11, `ONDEMAND_CONFIG=… ONDEMAND_SECRETS=… python3 -m
ondemand.service`. Paths in the config are then host paths.

**qBittorrent 5?** Not yet: its WebUI API renamed pause/resume to stop/start. Pin 4.6.x for now.
