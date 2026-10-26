# Optional: adaptive bitrate and TS away from home

## Adaptive bitrate (ABR)

Over a slow link a large title always transcodes at a single quality: a bandwidth drop is a stall, and
a seek past what was generated restarts ffmpeg. For titles you watch remotely, a **pre-generated HLS
ladder** fixes both: the player picks the quality segment by segment and seeking is instant.

1. Generate a ladder (needs a VAAPI GPU inside the Jellyfin container, SDR sources):
   ```bash
   ABR_ROOT_HOST=/srv/abr ABR_ROOT=/abr JELLYFIN_API_KEY=… \
     bash tools/generate-abr.sh "/media/movies/Some Movie (2020)/movie.mkv" <jellyfin item id>
   ```
   Mount `/srv/abr` read-write in Jellyfin at `/abr`, and read-only in jellyfin-ondemand; set
   `[abr] root = "/abr"`. About 6 GB per hour of video, ~1.8× real time on a mid-range GPU. The encode
   pauses itself whenever Jellyfin is transcoding for someone.
2. Install the script: `bash web/install-script.sh web/abr.js "On demand: adaptive bitrate"`.

When Jellyfin decides to transcode an item that has a ladder, `abr.js` rewrites the PlaybackInfo answer
to the ladder. Direct play is untouched; position, subtitles and progress still go through Jellyfin.

## HLS in TS segments away from home

jellyfin-web prefers fMP4 HLS by default; in an fMP4 transcode Jellyfin seeks with `-noaccurate_seek`,
so after a seek the video starts at the previous keyframe and subtitles run 0–10 s early. With TS
segments the seek is exact. `web/hls-ts.js` sets the user's "prefer fMP4" option to off **only** when
Jellyfin is opened from a non-private address and the user never chose it:

```bash
bash web/install-script.sh web/hls-ts.js "On demand: HLS in TS away from home"
```
