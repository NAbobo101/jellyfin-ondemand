/*
 * jellyfin-ondemand — adaptive bitrate (ABR) for items with a pre-generated HLS ladder.
 * Injected by the "JavaScript Injector" plugin (RequiresAuthentication = true).
 * Install/update: JELLYFIN_URL=… JELLYFIN_API_KEY=… bash web/install-script.sh web/abr.js "On demand: adaptive bitrate"
 *
 * Why it exists: over a slow link a 4K title always transcodes, at a single quality; bandwidth
 * that drops = a stall, and every seek past what was already generated restarts ffmpeg. For the
 * items that have a pre-generated ladder (tools/generate-abr.sh: 1080p at 6 and 3.5 Mbit/s, 720p
 * at 2, 480p at 0.9, identical cuts), this script swaps the transcode URL for the ladder's
 * master.m3u8, served by jellyfin-ondemand at /ondemand/abr/ (same origin). jellyfin-web's hls.js
 * measures the bandwidth on every segment and switches quality without pausing; seeking is just
 * fetching another ready segment.
 *
 * How: jellyfin-web asks POST /Items/<id>/PlaybackInfo through its SDK (axios over XHR). When the
 * answer arrives and Jellyfin chose to TRANSCODE an item that has a ladder, the answer is
 * rewritten: TranscodingUrl → the ladder, TranscodingSubProtocol = hls. Direct Play/Direct Stream
 * stay as they are. Position, external subtitles and progress reports still go to Jellyfin.
 * The list of items with a ladder comes from /ondemand/api/abr and is refreshed on screen changes.
 *
 * ES5 on purpose: TV browsers are often old Chromium.
 */
(function () {
    'use strict';
    if (window.__ondemandAbr) {
        return;
    }
    window.__ondemandAbr = true;

    var PREFIX = '/ondemand';
    var RE_PLAYBACKINFO = /\/Items\/([0-9a-f]{32})\/PlaybackInfo/i;
    var LIST_TTL_MS = 60000;
    var withLadder = {};
    var listAt = 0;

    var originalOpen = XMLHttpRequest.prototype.open;
    var originalSend = XMLHttpRequest.prototype.send;

    function client() {
        return window.ApiClient;
    }

    function token() {
        var a = client();
        return a && a.accessToken ? a.accessToken() : null;
    }

    function refreshList() {
        var a = client();
        var tk = token();
        if (!a || !tk || !a.serverAddress) {
            return;
        }
        listAt = Date.now();
        var x = new XMLHttpRequest();
        x.open('GET', a.serverAddress() + PREFIX + '/api/abr');
        x.setRequestHeader('X-Emby-Token', tk);
        x.onload = function () {
            if (x.status !== 200) {
                return;
            }
            try {
                var fresh = {};
                var items = JSON.parse(x.responseText).items || [];
                for (var i = 0; i < items.length; i++) {
                    fresh[items[i]] = true;
                }
                withLadder = fresh;
            } catch (e) {
                // odd answer: keep the previous list
            }
        };
        x.send();
    }

    function ladderUrl(itemId, playSessionId) {
        return PREFIX + '/abr/t/' + encodeURIComponent(token()) + '/' + itemId + '/master.m3u8' +
            (playSessionId ? '?PlaySessionId=' + encodeURIComponent(playSessionId) : '');
    }

    // Rewrites the PlaybackInfo answer; returns the new text, or null if nothing changed.
    function rewrite(itemId, text) {
        if (!withLadder[itemId] || !token()) {
            return null;
        }
        var d;
        try {
            d = JSON.parse(text);
        } catch (e) {
            return null;
        }
        var sources = (d && d.MediaSources) || [];
        var changed = false;
        for (var i = 0; i < sources.length; i++) {
            var f = sources[i];
            // only where Jellyfin was going to transcode, and only the item's own version (the ladder is of it)
            if (!f.TranscodingUrl || f.SupportsDirectPlay || f.SupportsDirectStream ||
                String(f.Id || '').toLowerCase() !== itemId) {
                continue;
            }
            f.TranscodingUrl = ladderUrl(itemId, d.PlaySessionId);
            f.TranscodingSubProtocol = 'hls';
            f.TranscodingContainer = 'mp4';
            var streams = f.MediaStreams || [];
            for (var j = 0; j < streams.length; j++) {
                var s = streams[j];
                // a text subtitle that would come inside Jellyfin's HLS or burned into the video: the ladder
                // does not have it, so it goes as an external file (jellyfin-web draws it)
                if (s.Type === 'Subtitle' && s.IsTextSubtitleStream &&
                    (s.DeliveryMethod === 'Hls' || s.DeliveryMethod === 'Encode')) {
                    s.DeliveryMethod = 'External';
                    s.DeliveryUrl = '/Videos/' + itemId + '/' + f.Id + '/Subtitles/' + s.Index +
                        '/0/Stream.vtt?ApiKey=' + encodeURIComponent(token());
                }
            }
            changed = true;
        }
        return changed ? JSON.stringify(d) : null;
    }

    XMLHttpRequest.prototype.open = function (method, url) {
        var m = RE_PLAYBACKINFO.exec(String(url));
        this.__abrItem = m && String(method).toUpperCase() === 'POST' ? m[1].toLowerCase() : null;
        return originalOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function () {
        var x = this;
        if (x.__abrItem) {
            if (Date.now() - listAt > LIST_TTL_MS) {
                refreshList();
            }
            // registered before send: runs before axios' onloadend/onreadystatechange
            x.addEventListener('readystatechange', function () {
                if (x.readyState !== 4 || x.__abrDone || x.status !== 200) {
                    return;
                }
                x.__abrDone = true;
                var fresh = rewrite(x.__abrItem, x.responseText);
                if (fresh === null) {
                    return;
                }
                Object.defineProperty(x, 'responseText', { configurable: true, get: function () { return fresh; } });
                Object.defineProperty(x, 'response', {
                    configurable: true,
                    get: function () { return x.responseType === 'json' ? JSON.parse(fresh) : fresh; }
                });
                if (window.console && console.info) {
                    console.info('[abr] adaptive ladder instead of the transcode:', x.__abrItem);
                }
            });
        }
        return originalSend.apply(this, arguments);
    };

    // list refreshed on every screen change (an episode's page shows up before the play)
    document.addEventListener('viewshow', function () {
        if (Date.now() - listAt > LIST_TTL_MS) {
            refreshList();
        }
    });
    var attempts = 0;
    var timer = setInterval(function () {
        if (token()) {
            clearInterval(timer);
            refreshList();
        } else if (++attempts > 60) {
            clearInterval(timer);
        }
    }, 1000);

    window.__ondemandAbrRewrite = rewrite;    // for tests and the console
    window.__ondemandAbrSet = function (ids) { withLadder = {}; for (var i = 0; i < ids.length; i++) { withLadder[ids[i]] = true; } };
})();
