# Extras

Optional web scripts that are not part of the on-demand feature itself.

## zapping.js — Live TV channel zapping

Adds TV-style channel switching to the Jellyfin 10.11 web player:

- **CH+ / CH−** (PageUp/PageDown, remote channel keys, media next/previous, or the two buttons
  added next to Pause) step to the neighbouring channel. The name shows on screen right away; the
  stream only opens once you stop pressing.
- **Channel number**: type the digits and wait 1.5 s.
- **Groups menu**: header button, player button or the remote's green key, when a groups list is
  provided (see below).
- **Single tuner**: the web client opens the new stream before closing the old one, which a tuner
  with `TunerCount` 1 refuses. Switches are done as Stop → wait for the session to empty → PlayNow.

The UI is in English or Brazilian Portuguese, picked from the page language.

### Requirements

- Jellyfin Live TV with an M3U tuner (IPTV).
- The [JavaScript Injector](https://github.com/n00bcodr/Jellyfin-JavaScript-Injector) plugin.

### Install

```sh
JELLYFIN_URL=http://localhost:8096 JELLYFIN_API_KEY=… \
  bash web/install-script.sh web/extras/zapping.js "On demand: channel zapping"
```

Reload the Jellyfin page in the browser; no restart needed. Remove with
`bash web/install-script.sh --remove "On demand: channel zapping"`.

### Channel order (SortName)

Neighbours are found by the channels' `SortName`, which is how Jellyfin sorts channels. If the
M3U gives each channel a `tvg-chno` and a SortName like `%05d.0-Name` (e.g. `00042.0-News`), the
order is numeric and typing a number finds the channel by that prefix. Otherwise the order is
alphabetical by name.

### Channel groups

Jellyfin 10.11 drops the M3U `group-title`, so the menu needs a list of number ranges. Install a
second tiny script **before** zapping.js that sets the list:

```js
window.__ondemandChannelGroups = [
    {c: 'News', g: 'National', i: 1, f: 99},
    {c: 'Sports', g: 'Football', i: 100, f: 149}
];
```

Each item is `c` = category (a heading), `g` = group name, `i` = first channel number, `f` = last
channel number, in numbering order. Groups with no channels in their range are hidden. Without a
list the groups button and menu stay hidden.

```sh
JELLYFIN_URL=… JELLYFIN_API_KEY=… bash web/install-script.sh my-groups.js "On demand: channel groups"
JELLYFIN_URL=… JELLYFIN_API_KEY=… bash web/install-script.sh web/extras/zapping.js "On demand: channel zapping"
```

The installer appends an updated entry at the end of the list, so after updating the groups script,
reinstall zapping.js too so that it still comes after the list.

### Diagnostics

`var DIAGNOSTICS = false;` near the end of the file. When set to `true`, the script keeps the last
`<video>` events, HLS requests and key presses, and when a channel stops on its own it posts them
to Jellyfin's `/ClientLog/Document` (they show up as `upload_*.log` in the server's log directory).
