/*
 * jellyfin-ondemand — HLS in MPEG-TS segments away from home.
 * Injected by the "JavaScript Injector" plugin (RequiresAuthentication = true).
 * Install/update: JELLYFIN_URL=… JELLYFIN_API_KEY=… bash web/install-script.sh web/hls-ts.js "On demand: HLS in TS away from home"
 *
 * Why it exists: jellyfin-web turns on "Prefer fMP4-HLS media container" by default in Chrome,
 * Firefox and Safari. In an fMP4 transcode the server uses `-ss … -noaccurate_seek`: after every
 * seek/resume the video starts at the previous keyframe and subtitles run 0–10 s early (measured:
 * the 600.0 s segment started at 593.93 s). With TS segments the seek is exact.
 *
 * Only acts away from home (where large titles always transcode), and only if the user never
 * chose the option in this browser (an empty localStorage key) — whoever sets it by hand still
 * decides. The key is jellyfin-web's userSettings one: "<userId>-preferFmp4HlsContainer".
 *
 * ES5 on purpose: TV browsers are often old Chromium.
 */
(function () {
    'use strict';
    if (window.__ondemandHlsTs) {
        return;
    }
    window.__ondemandHlsTs = true;

    // At home = Jellyfin opened by a private address; anything else counts as "away".
    var LOCAL = /^(localhost|127\.|192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.|\[?::1\]?$|[^.]+$)/;
    if (LOCAL.test(location.hostname)) {
        return;
    }

    function apply() {
        var api = window.ApiClient;
        var uid = api && api.getCurrentUserId && api.getCurrentUserId();
        if (!uid) {
            return false;
        }
        try {
            var key = uid + '-preferFmp4HlsContainer';
            if (localStorage.getItem(key) === null) {
                localStorage.setItem(key, 'false');
            }
        } catch (e) {
            // localStorage blocked: jellyfin-web's default stays
        }
        return true;
    }

    // the user may switch accounts without reloading: check on every screen change
    document.addEventListener('viewshow', apply);
    var attempts = 0;
    var timer = setInterval(function () {
        if (apply() || ++attempts > 60) {
            clearInterval(timer);
        }
    }, 1000);
})();
