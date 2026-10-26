# Troubleshooting

Start with `cli doctor`; most problems are one of its FAIL/WARN lines. Then match the symptom.

| Symptom | Look at | Usual cause → fix |
|---|---|---|
| service restarts in a loop | `C logs --tail 20 ondemand` | missing `[jellyfin] api_key`, or `secrets.toml` not chmod 600 |
| catalog stays empty | `cli preview`, logs `movies:` / `series:` lines | no provider / feed empty / everything discarded (read the reasons); marker `.ondemand-marker` missing; someone watching (writes wait) |
| private tracker adds almost nothing | `cli preview --provider NAME` reasons | "private tracker without freeleech": `[movies]`/`[series] freeleech_min = 0` (ratio then counts) |
| folder shows raw name in Jellyfin | wait; `cli status` | Jellyfin's metadata queue; items get locked after identification |
| play fails at once | logs `-> GET <id>` then `unavailable:` | no seeders, tracker down, qBittorrent unreachable; release then blocked `block_s` (120 s) |
| play fails with "path outside … path_map" | doctor qbittorrent lines | add qBittorrent's prefix to `path_map` and mount it |
| "torrent refused: suspicious file / video too small" | logs | public torrent failed the file check (fake?); `trusted = true` only for known-good providers |
| 403 on `/v/<id>` | logs `refused (not in video_clients)` | add Jellyfin's address/name/CIDR to `[gateway] video_clients` |
| first play slow | — | normal: 10–60 s for a cold torrent (private + VPN can be more) |
| plays but transcodes on CPU / server overloaded | Jellyfin dashboard | a probe failed ("blind" transcode); enable hardware transcoding; optional `tools/transcode_watchdog.py` |
| search section missing on the search page | doctor `web` line; browser console | JavaScript Injector or `search.js` not installed; reload page; `/ondemand/api/health` must answer on Jellyfin's address (proxy route) |
| search shows nothing for a known movie | `cli search-movie "Name"` | no provider has a usable release (`check_availability`), or the user hit `daily_limit` |
| series incomplete | `cli search-series "Name"` | missing episodes listed; TVmaze does not know a new season yet |
| no subtitles | `cli status` (subtitles states) | language unset, no SubDL key, SubDL quota (retried hourly up to `max_attempts`) |
| qBittorrent 5 errors (pause/resume) | doctor | unsupported: use a 4.6.x image |
| catalog `.strm` point to an old address | — | fix `[gateway] public_url`, then `cli rewrite-strm` |

Deeper explanations (only if needed): `docs/faq.md`, `docs/architecture.md` ("Playing a title", "Lifecycle").
