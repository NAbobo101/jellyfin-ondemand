#!/usr/bin/env bash
# generate-abr.sh — an adaptive "permanent cache" for one video: an HLS ladder of 4 qualities with
# identical cuts (4 s), so the player switches quality with the bandwidth WITHOUT pausing.
#   v0 1080p 6 Mbit/s · v1 1080p 3.5 · v2 720p 2 · v3 480p 0.9 — all with stereo AAC 128k
# Measured on an RX 580 (VAAPI): 1.84× real time, ~6 GB per hour of video. SDR sources only:
# an HDR source comes out washed out without tone mapping.
# web/abr.js (JavaScript Injector) hands the ladder to the player; jellyfin-ondemand serves it
# from [abr] root.
#
#   ABR_ROOT_HOST=/srv/abr ABR_ROOT=/abr JELLYFIN_API_KEY=… bash tools/generate-abr.sh <video> <itemId>
#
#   <video>   the file's path AS THE JELLYFIN CONTAINER SEES IT (ffmpeg runs inside it: it has
#             jellyfin-ffmpeg and the GPU)
#   <itemId>  Jellyfin's item id (32 hex digits) — the ladder is keyed by it
#   output:   $ABR_ROOT_HOST/<itemId>/master.m3u8 (+ v0..v3/); born as <itemId>.tmp and renamed only
#             if the 4 playlists end with the original's duration (±0.5 s).
#
# Environment:
#   ABR_ROOT_HOST       the ladders' folder on the host (mounted into jellyfin-ondemand as [abr] root)
#   ABR_ROOT            the same folder inside the Jellyfin container (must be mounted read-write there)
#   JELLYFIN_URL        default http://localhost:8096 (to check whether Jellyfin is transcoding)
#   JELLYFIN_API_KEY    required
#   JELLYFIN_CONTAINER  default jellyfin
#   VAAPI_DRIVER        kernel driver of the GPU to use (amdgpu, i915, radeon…); default: the first
#                       render node with one of those. The render node NUMBER can change between
#                       boots, so the GPU is picked by its driver in sysfs, never by renderD12N.
#
# Playback first: the encode only runs with the GPU free — SIGSTOP on ffmpeg while Jellyfin is
# transcoding for someone, SIGCONT when it stops. The source file is only read (it may be a
# torrent's payload: never rewrite it).
set -uo pipefail

SRC="${1:?usage: generate-abr.sh <video path inside the Jellyfin container> <itemId>}"
ID="${2:?missing the Jellyfin itemId}"
[[ "$ID" =~ ^[0-9a-f]{32}$ ]] || { echo "itemId must be 32 hex digits" >&2; exit 1; }
: "${ABR_ROOT_HOST:?set ABR_ROOT_HOST}"; : "${ABR_ROOT:?set ABR_ROOT}"; : "${JELLYFIN_API_KEY:?set JELLYFIN_API_KEY}"
JELLYFIN_URL="${JELLYFIN_URL:-http://localhost:8096}"
CT="${JELLYFIN_CONTAINER:-jellyfin}"
FF=/usr/lib/jellyfin-ffmpeg/ffmpeg
FP=/usr/lib/jellyfin-ffmpeg/ffprobe
LOG="$ABR_ROOT_HOST/generate-abr.log"

log() { echo "$(date '+%F %T') [abr $ID] $*" | tee -a "$LOG"; }
node_driver() { basename "$(readlink -f "/sys/class/drm/$1/device/driver" 2>/dev/null)" 2>/dev/null; }
pick_node() {
  local n b d
  for n in /dev/dri/renderD*; do
    [ -e "$n" ] || continue
    b=$(basename "$n"); d=$(node_driver "$b")
    if [ -n "${VAAPI_DRIVER:-}" ]; then
      [ "$d" = "$VAAPI_DRIVER" ] && { echo "$b"; return 0; }
    else
      case "$d" in amdgpu|radeon|i915|xe) echo "$b"; return 0 ;; esac
    fi
  done
  return 1
}
# "idle" = no Jellyfin transcode in progress (a session with TranscodingInfo and a job not finished)
idle() { curl -s "$JELLYFIN_URL/Sessions" -H "Authorization: MediaBrowser Token=$JELLYFIN_API_KEY" | python3 -c "
import sys,json
s=json.load(sys.stdin)
sys.exit(1 if any(x.get('NowPlayingItem') and x.get('TranscodingInfo') and (x['TranscodingInfo'].get('CompletionPercentage') or 0)<100 for x in s) else 0)"; }

mkdir -p "$ABR_ROOT_HOST"
[ -f "$ABR_ROOT_HOST/$ID/master.m3u8" ] && { log "already there, nothing to do"; exit 0; }
NODE=$(pick_node) || { log "ERROR: no VAAPI render node${VAAPI_DRIVER:+ with driver $VAAPI_DRIVER}"; exit 1; }
DUR=$(docker exec "$CT" "$FP" -v error -show_entries format=duration -of csv=p=0 "$SRC") \
  || { log "ERROR: ffprobe could not read $SRC inside $CT"; exit 1; }
TMP="$ABR_ROOT_HOST/$ID.tmp"; rm -rf "$TMP"; mkdir -p "$TMP"

until idle; do sleep 30; done
log "start: $(basename "$SRC") (GPU $NODE, $DUR s)"
t0=$(date +%s)
docker exec -w "$ABR_ROOT/$ID.tmp" "$CT" nice -n 10 "$FF" -hide_banner -loglevel error -nostdin -y \
  -init_hw_device vaapi=va:/dev/dri/$NODE -hwaccel vaapi -hwaccel_output_format vaapi -hwaccel_device va \
  -i "$SRC" \
  -filter_complex "[0:v:0]split=3[a][c][d];[a]scale_vaapi=w=1920:h=-2:format=nv12,split=2[v0][v1];[c]scale_vaapi=w=1280:h=-2:format=nv12[v2];[d]scale_vaapi=w=854:h=-2:format=nv12[v3]" \
  -map "[v0]" -map "[v1]" -map "[v2]" -map "[v3]" -map 0:a:0 -map 0:a:0 -map 0:a:0 -map 0:a:0 \
  -c:v h264_vaapi -profile:v high -rc_mode VBR \
  -b:v:0 6000k -maxrate:v:0 7500k -bufsize:v:0 12000k \
  -b:v:1 3500k -maxrate:v:1 4500k -bufsize:v:1 7000k \
  -b:v:2 2000k -maxrate:v:2 2600k -bufsize:v:2 4000k \
  -b:v:3 900k -maxrate:v:3 1200k -bufsize:v:3 1800k \
  -force_key_frames "expr:gte(t,n_forced*2)" -g 48 \
  -c:a aac -ac 2 -b:a 128k -sn -dn \
  -f hls -hls_time 4 -hls_playlist_type vod -hls_segment_type fmp4 -hls_flags independent_segments \
  -master_pl_name master.m3u8 -hls_segment_filename "v%v/seg%05d.m4s" -hls_fmp4_init_filename init.mp4 \
  -var_stream_map "v:0,a:0 v:1,a:1 v:2,a:2 v:3,a:3" "v%v/index.m3u8" >>"$LOG" 2>&1 &
pid_exec=$!

paused=0
while kill -0 "$pid_exec" 2>/dev/null; do
  sleep 5
  # the host's "docker exec" also has the path on its command line: find the ffmpeg by its working folder
  pid_ff=""
  for p in $(pgrep -x ffmpeg); do
    [ "$(readlink "/proc/$p/cwd" 2>/dev/null)" = "$ABR_ROOT/$ID.tmp" ] && { pid_ff=$p; break; }
  done
  [ -n "$pid_ff" ] || continue
  if idle; then
    [ $paused = 1 ] && { kill -CONT "$pid_ff"; paused=0; log "resumed (GPU free)"; }
  else
    [ $paused = 0 ] && { kill -STOP "$pid_ff"; paused=1; log "paused (Jellyfin is transcoding)"; }
  fi
done
wait "$pid_exec"; rc=$?

ok=1
for v in 0 1 2 3; do
  pl="$TMP/v$v/index.m3u8"
  grep -q '#EXT-X-ENDLIST' "$pl" 2>/dev/null || { ok=0; continue; }
  total=$(grep -o 'EXTINF:[0-9.]*' "$pl" | cut -d: -f2 | python3 -c "import sys;print(sum(float(x) for x in sys.stdin))")
  python3 -c "import sys;sys.exit(0 if abs($total-$DUR)<=0.5 else 1)" || { ok=0; log "v$v: total $total vs $DUR"; }
done
if [ $rc -eq 0 ] && [ $ok = 1 ]; then
  mv "$TMP" "$ABR_ROOT_HOST/$ID"
  log "ok in $(( $(date +%s) - t0 )) s, $(du -sh "$ABR_ROOT_HOST/$ID" | cut -f1)"
else
  log "FAILED (rc=$rc) — $TMP kept for inspection"
  exit 1
fi
