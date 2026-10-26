/*
 * Channel zapping (Live TV) — CH+/CH− and channel-number entry in the Jellyfin web player.
 *
 * Optional extra, only useful with IPTV (Live TV fed by an M3U tuner). Injected by the
 * "JavaScript Injector" plugin. Install/update:
 *   JELLYFIN_URL=… JELLYFIN_API_KEY=… bash web/install-script.sh web/extras/zapping.js "On demand: channel zapping"
 * No Jellyfin restart needed — reload the page in the browser. See web/extras/README.md.
 *
 * Why it exists: in jellyfin-web 10.11 "channel +/−" is nextTrack() of the play queue, and a
 * live channel always plays alone in the queue — there is nowhere to go. Here the neighbour is
 * found by channel order with ONE query to /Items, without downloading the whole channel list
 * (which can be tens of thousands of entries).
 *
 * Channel order comes from the channels' SortName: Jellyfin sorts channels by it. An M3U whose
 * channels carry tvg-chno and a SortName like "%05d.0-Name" (e.g. "00042.0-News") gives numeric
 * order, and typing a number finds the channel by that prefix; otherwise the order is
 * alphabetical by name.
 *
 * Single-tuner setups: the web client opens the new media BEFORE closing the old one, and a
 * tuner with TunerCount 1 would refuse it. So a switch is: Stop -> wait for the session to
 * empty -> PlayNow.
 *
 * Groups menu: Jellyfin 10.11 drops the group-title of M3U playlists, so Live TV cannot browse
 * by group. If your playlist numbers each group in its own range of channel numbers, publish
 * those ranges as window.__ondemandChannelGroups from a small script injected BEFORE this one
 * (see the README). Each item is {c: category, g: group name, i: first number, f: last
 * number}, in numbering order; categories are headings, groups are rows under them. With no
 * list the groups button and menu stay hidden. The menu opens from the header button, the
 * player button or the remote's green key.
 *
 * ES5 on purpose (no async/await, arrow functions, template strings, let/const): TV browsers
 * are often old Chromium.
 */
(function () {
    'use strict';
    if (window.__ondemandZapping) {
        return;
    }
    window.__ondemandZapping = true;

    // ---------------------------------------------------------------- UI strings

    var STRINGS = {
        'en': {
            noSignal: 'no signal — CH+ / CH− to move on',
            tuning: 'tuning…',
            switchFailed: 'switch failed ({0})',
            channelEntry: 'Channel {0}_',
            channel: 'Channel {0}',
            noSuchChannel: 'does not exist',
            groupsTitle: 'Channel groups',
            groupsHint: '↑↓ choose · OK open · Back closes',
            channelsHint: '↑↓ choose · OK watch · Back to groups',
            loading: 'loading…',
            groupCount: '{0} · {1} channels',
            noChannels: 'no channels in Live TV',
            noChannelsInGroup: 'no channels in this group',
            noGroupList: 'no group list — see web/extras/README.md',
            prevChannel: 'Previous channel (CH−)',
            nextChannel: 'Next channel (CH+)'
        },
        'pt-BR': {
            noSignal: 'sem sinal — CH+ / CH− para seguir',
            tuning: 'sintonizando…',
            switchFailed: 'falhou ao trocar ({0})',
            channelEntry: 'Canal {0}_',
            channel: 'Canal {0}',
            noSuchChannel: 'não existe',
            groupsTitle: 'Grupos de canais',
            groupsHint: '↑↓ escolher · OK abrir · Voltar fecha',
            channelsHint: '↑↓ escolher · OK assistir · Voltar grupos',
            loading: 'carregando…',
            groupCount: '{0} · {1} canais',
            noChannels: 'nenhum canal no Live TV',
            noChannelsInGroup: 'nenhum canal neste grupo',
            noGroupList: 'lista de grupos ausente — veja o web/extras/README.md',
            prevChannel: 'Canal anterior (CH−)',
            nextChannel: 'Próximo canal (CH+)'
        }
    };

    // Exact match first, then the base language ("pt-PT" -> "pt-BR", "en-GB" -> "en"), else en.
    // Resolved on every call: jellyfin-web sets <html lang> only after the user's culture loads.
    function strings() {
        var lang = String(document.documentElement.getAttribute('lang') || navigator.language || '');
        var k;
        for (k in STRINGS) {
            if (k.toLowerCase() === lang.toLowerCase()) { return STRINGS[k]; }
        }
        var base = lang.split('-')[0].toLowerCase();
        for (k in STRINGS) {
            if (k.split('-')[0].toLowerCase() === base) { return STRINGS[k]; }
        }
        return STRINGS.en;
    }

    function t(key) {
        var s = strings()[key];
        if (s === undefined) { s = STRINGS.en[key] || key; }
        var args = arguments;
        return s.replace(/\{(\d+)\}/g, function (m, i) {
            var v = args[parseInt(i, 10) + 1];
            return v === undefined ? m : String(v);
        });
    }

    // ---------------------------------------------------------------- settings

    var QUERY_FIELDS = {
        IncludeItemTypes: 'TvChannel', Recursive: true, SortBy: 'SortName',
        Fields: 'SortName', EnableImages: false, EnableUserData: false
    };
    var ZAPPING_MODE_MS = 30000; // after a switch, the keys work even outside the player
    var SWITCH_DELAY_MS = 800;   // pressing CH+ several times in a row only opens the last channel
    var DIGIT_DELAY_MS = 1500;
    var SIGNAL_CHECK_MS = 15000;

    var NEXT_KEYS = { PageUp: 1, ChannelUp: 1, MediaTrackNext: 1 };
    var PREV_KEYS = { PageDown: 1, ChannelDown: 1, MediaTrackPrevious: 1 };
    var NEXT_CODES = { 33: 1, 427: 1, 176: 1 };  // PageUp, CH+ (Tizen/webOS), media ⏭
    var PREV_CODES = { 34: 1, 428: 1, 177: 1 };

    // [{c: category, g: group, i: first number, f: last number}], in numbering order
    var GROUPS = window.__ondemandChannelGroups || [];
    var GROUPS_KEYS = { ColorF1Green: 1 };
    var GROUPS_CODES = { 404: 1 };                    // green key (webOS/Tizen)
    var BACK_KEYS = { Escape: 1, Backspace: 1, BrowserBack: 1, GoBack: 1 };
    var BACK_CODES = { 27: 1, 8: 1, 461: 1, 10009: 1 }; // 461 = webOS, 10009 = Tizen
    var COUNT_TTL_MS = 600000;
    var SELECTION_COLOR = 'rgba(0,164,220,.9)';       // Jellyfin blue

    // ---------------------------------------------------------------- state

    var current = null;      // channel playing, with SortName
    var pending = null;      // channel picked with CH± but not tuned yet
    var sessionId = null;
    var zappingUntil = 0;
    var queue = Promise.resolve();
    var switchTimer = null;
    var digits = '';
    var digitTimer = null;
    var ticks = 0;
    var menu = null;         // open groups menu
    var groupCounts = null;  // Promise of the per-group counts
    var groupCountsAt = 0;
    var lastGroup = 0;

    function api() { return window.ApiClient; }
    function uid() { return api() && api().getCurrentUserId(); }
    function inPlayer() { return location.hash.indexOf('#/video') === 0; }
    function inZapping() { return Date.now() < zappingUntil; }
    function isActive() { return !!current && (inPlayer() || inZapping()); }
    function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
    function label(c) { return c.ChannelNumber + ' · ' + c.Name; }

    function queryRaw(extra) {
        var q = { UserId: uid() };
        var k;
        for (k in QUERY_FIELDS) { q[k] = QUERY_FIELDS[k]; }
        for (k in extra) { q[k] = extra[k]; }
        return api().getJSON(api().getUrl('Items', q));
    }

    function query(extra) {
        return queryRaw(extra).then(function (r) {
            return (r && r.Items) || [];
        });
    }

    function pad5(n) {
        var p = String(n);
        while (p.length < 5) { p = '0' + p; }
        return p;
    }

    function mySession() {
        return api().getSessions({ DeviceId: api().deviceId() }).then(function (list) {
            var u = uid();
            var s = (list || []).filter(function (x) { return x.UserId === u; })[0] || null;
            if (s) { sessionId = s.Id; }
            return s;
        });
    }

    // ---------------------------------------------------------------- on-screen notice

    function notify(title, detail, sticky) {
        var el = document.getElementById('zapNotice');
        if (!el) {
            el = document.createElement('div');
            el.id = 'zapNotice';
            el.style.cssText = 'position:fixed;top:5vh;left:4vw;z-index:2147483647;' +
                'padding:.5em .9em;border-radius:.35em;background:rgba(0,0,0,.78);color:#fff;' +
                'font-size:2.1em;font-weight:600;line-height:1.25;max-width:80vw;' +
                'pointer-events:none;transition:opacity .3s;opacity:0';
            el.appendChild(document.createElement('div'));
            el.appendChild(document.createElement('div'));
            el.lastChild.style.cssText = 'font-size:.55em;font-weight:400;opacity:.85';
            document.body.appendChild(el);
        }
        el.firstChild.textContent = title;   // textContent: channel names come from the provider
        el.lastChild.textContent = detail || '';
        el.style.opacity = '1';
        clearTimeout(el._t);
        if (!sticky) {
            el._t = setTimeout(function () { el.style.opacity = '0'; }, 3500);
        }
    }

    // ---------------------------------------------------------------- channels

    function withSortName(id) {
        return query({ Ids: id }).then(function (items) { return items[0] || null; });
    }

    // Next/previous by channel order. NameLessThan also returns the channel itself (measured),
    // so always take the first one with a different Id. At either end, wrap around.
    function neighbour(base, dir) {
        var q = { Limit: 2 };
        if (dir > 0) {
            q.NameStartsWithOrGreater = base.SortName;
        } else {
            q.NameLessThan = base.SortName;
            q.SortOrder = 'Descending';
        }
        return query(q).then(function (items) {
            var c = items.filter(function (i) { return i.Id !== base.Id; })[0];
            if (c) { return c; }
            return query(dir > 0 ? { Limit: 1 } : { Limit: 1, SortOrder: 'Descending' })
                .then(function (end) { return end[0] || null; });
        });
    }

    function byNumber(n) {
        return query({ Limit: 1, NameStartsWithOrGreater: pad5(n) }).then(function (items) {
            return items[0] || null;
        });
    }

    // ---------------------------------------------------------------- tuning

    function waitForEmptySession(attempts) {
        return wait(200).then(mySession).then(function (s) {
            if (!s || !s.NowPlayingItem || attempts <= 0) { return; }
            return waitForEmptySession(attempts - 1);
        });
    }

    function checkSignal(channel) {
        setTimeout(function () {
            if (!current || current.Id !== channel.Id || pending) { return; } // already zapped elsewhere
            mySession().then(function (s) {
                var np = s && s.NowPlayingItem;
                if (!np || np.Id !== channel.Id) {
                    zappingUntil = Date.now() + ZAPPING_MODE_MS;
                    notify(label(channel), t('noSignal'));
                }
            });
        }, SIGNAL_CHECK_MS);
    }

    function tune(channel) {
        if (!channel) { return Promise.resolve(); }
        current = channel;
        zappingUntil = Date.now() + ZAPPING_MODE_MS;
        intentionalStopUntil = Date.now() + 8000;
        record('zapping', 'switch to ' + label(channel));
        notify(label(channel), t('tuning'), true);
        return mySession().then(function (s) {
            if (s && s.NowPlayingItem) {
                return api().sendPlayStateCommand(sessionId, 'Stop')
                    .then(function () { return waitForEmptySession(20); })   // up to ~4 s
                    .then(function () { return wait(300); });                // slack for the stream to close
            }
        }).then(function () {
            return api().sendPlayCommand(sessionId, { PlayCommand: 'PlayNow', ItemIds: channel.Id });
        }).then(function () {
            notify(label(channel), '');
            checkSignal(channel);
        }).catch(function (e) {
            notify(label(channel), t('switchFailed', (e && e.status) || e));
        });
    }

    function enqueue(fn) {
        queue = queue.then(fn).catch(function (e) { console.warn('zapping:', e); });
    }

    // CH+/CH−: walks the channel order showing the name; only opens the stream once the user stops.
    function zap(dir) {
        enqueue(function () {
            var base = pending || current;
            if (!base) { return; }
            return neighbour(base, dir).then(function (c) {
                if (!c) { return; }
                pending = c;
                notify(label(c), '');
                clearTimeout(switchTimer);
                switchTimer = setTimeout(function () {
                    var chosen = pending;
                    pending = null;
                    enqueue(function () { return tune(chosen); });
                }, SWITCH_DELAY_MS);
            });
        });
    }

    function addDigit(d) {
        digits = (digits + d).slice(-5);
        notify(t('channelEntry', digits), '', true);
        clearTimeout(digitTimer);
        digitTimer = setTimeout(function () {
            var n = parseInt(digits, 10);
            digits = '';
            enqueue(function () {
                return byNumber(n).then(function (c) {
                    if (!c) { notify(t('channel', n), t('noSuchChannel')); return; }
                    return tune(c);
                });
            });
        }, DIGIT_DELAY_MS);
    }

    // ---------------------------------------------------------------- groups menu

    function rangeQuery(g, extra) {
        var q = { NameStartsWithOrGreater: pad5(g.i), NameLessThan: pad5(g.f + 1) };
        for (var k in extra) { q[k] = extra[k]; }
        return q;
    }

    // How many channels Jellyfin has in each range: hides empty groups (a range can stay
    // reserved for a source that is hidden or gone) and shows the real count.
    function countGroups() {
        if (!groupCounts || Date.now() - groupCountsAt > COUNT_TTL_MS) {
            groupCountsAt = Date.now();
            groupCounts = Promise.all(GROUPS.map(function (g) {
                return queryRaw(rangeQuery(g, { Limit: 0 })).then(function (r) {
                    return (r && r.TotalRecordCount) || 0;
                }, function () { return -1; });
            }));
        }
        return groupCounts;
    }

    function currentNumber() {
        return current ? parseFloat(current.ChannelNumber) : NaN;
    }

    function createMenu() {
        var el = document.createElement('div');
        el.id = 'zapGroups';
        el.style.cssText = 'position:fixed;top:0;left:0;bottom:0;width:62vw;max-width:60em;' +
            'z-index:2147483646;background:rgba(12,12,12,.95);color:#fff;font-size:1.5em;' +
            'display:flex;flex-direction:column;box-shadow:0 0 2em #000';
        var title = document.createElement('div');
        title.style.cssText = 'padding:.8em 1.2em .2em;font-size:1.25em;font-weight:600';
        var hint = document.createElement('div');
        hint.style.cssText = 'padding:0 1.5em .7em;font-size:.65em;opacity:.7';
        var list = document.createElement('div');
        list.style.cssText = 'flex:1;overflow-y:auto;padding:0 .6em 1.5em';
        el.appendChild(title);
        el.appendChild(hint);
        el.appendChild(list);
        document.body.appendChild(el);
        return { el: el, title: title, hint: hint, list: list, rows: [], sel: -1,
                 level: '', generation: 0 };
    }

    function setHeader(title, hint) {
        menu.title.textContent = title;
        menu.hint.textContent = hint;
        menu.list.innerHTML = '';
        menu.rows = [];
        menu.sel = -1;
        menu.generation++;   // discards responses for a screen that has already been replaced
        return menu.generation;
    }

    // always textContent: group, channel and programme names come from the provider
    function addRow(main, side, action) {
        var r = document.createElement('div');
        if (!action) {
            r.style.cssText = 'padding:1em .8em .25em;font-size:.7em;font-weight:700;' +
                'letter-spacing:.08em;text-transform:uppercase;opacity:.55';
            r.textContent = main;
            menu.list.appendChild(r);
            return;
        }
        r.style.cssText = 'display:flex;align-items:baseline;padding:.35em .8em;' +
            'border-radius:.3em;cursor:pointer';
        var a = document.createElement('span');
        a.style.cssText = 'flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
        a.textContent = main;
        r.appendChild(a);
        if (side) {
            var b = document.createElement('span');
            b.style.cssText = 'margin-left:1em;max-width:45%;white-space:nowrap;overflow:hidden;' +
                'text-overflow:ellipsis;font-size:.75em;opacity:.7';
            b.textContent = side;
            r.appendChild(b);
        }
        var idx = menu.rows.length;
        menu.rows.push({ el: r, action: action });
        r.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            select(idx);
            action();
        });
        menu.list.appendChild(r);
    }

    function select(i) {
        if (!menu || !menu.rows.length) { return; }
        i = Math.max(0, Math.min(menu.rows.length - 1, i));
        if (menu.sel >= 0 && menu.rows[menu.sel]) {
            menu.rows[menu.sel].el.style.background = '';
        }
        menu.sel = i;
        var el = menu.rows[i].el;
        el.style.background = SELECTION_COLOR;
        // scrollIntoView with options does not exist in old Chromium: scroll by hand
        var l = menu.list;
        if (el.offsetTop < l.scrollTop + 40) {
            l.scrollTop = Math.max(0, el.offsetTop - 40);
        } else if (el.offsetTop + el.offsetHeight > l.scrollTop + l.clientHeight - 20) {
            l.scrollTop = el.offsetTop + el.offsetHeight - l.clientHeight + 20;
        }
    }

    function showGroups() {
        var gen = setHeader(t('groupsTitle'), t('groupsHint'));
        menu.level = 'groups';
        addRow(t('loading'));
        return countGroups().then(function (counts) {
            if (!menu || menu.generation !== gen) { return; }
            menu.list.innerHTML = '';
            var n = currentNumber();
            var cat = null;
            var initial = -1;
            GROUPS.forEach(function (g, gi) {
                if (counts[gi] === 0) { return; }
                if (g.c !== cat) {
                    cat = g.c;
                    addRow(cat);
                }
                if (n >= g.i && n <= g.f) { initial = menu.rows.length; }
                if (gi === lastGroup && initial < 0) { initial = menu.rows.length; }
                addRow(g.g, counts[gi] > 0 ? t('groupCount', g.i, counts[gi]) : String(g.i),
                    function () { lastGroup = gi; showChannels(g); });
            });
            if (!menu.rows.length) { addRow(t('noChannels')); }
            select(Math.max(0, initial));
        });
    }

    function showChannels(g) {
        var gen = setHeader(g.g, t('channelsHint'));
        menu.level = 'channels';
        addRow(t('loading'));
        return query(rangeQuery(g, { Limit: 1000 })).then(function (channels) {
            if (!menu || menu.generation !== gen) { return; }
            menu.list.innerHTML = '';
            var initial = 0;
            channels.forEach(function (c) {
                if (current && c.Id === current.Id) { initial = menu.rows.length; }
                var prog = c.CurrentProgram && c.CurrentProgram.Name;
                addRow(c.ChannelNumber + '   ' + c.Name, prog || '', function () {
                    closeMenu();
                    enqueue(function () { return tune(c); });
                });
            });
            if (!channels.length) { addRow(t('noChannelsInGroup')); }
            select(initial);
        });
    }

    function openMenu() {
        if (!api() || !uid()) { return; }
        if (!GROUPS.length) {
            notify(t('groupsTitle'), t('noGroupList'));
            return;
        }
        if (!menu) { menu = createMenu(); }
        showGroups().catch(function (e) { console.warn('zapping:', e); });
    }

    function closeMenu() {
        if (menu) {
            menu.generation++;
            if (menu.el.parentNode) { menu.el.parentNode.removeChild(menu.el); }
            menu = null;
        }
    }

    function menuBack() {
        if (menu && menu.level === 'channels') {
            showGroups().catch(function (e) { console.warn('zapping:', e); });
        } else {
            closeMenu();
        }
    }

    // While the menu is open it takes ALL the keys it handles: without
    // stopImmediatePropagation jellyfin-web would also move the focus underneath.
    function menuKey(e) {
        var k = e.key;
        var c = e.keyCode;
        if (k === 'ArrowDown' || c === 40) {
            select(menu.sel + 1);
        } else if (k === 'ArrowUp' || c === 38) {
            select(menu.sel - 1);
        } else if (NEXT_KEYS[k] || NEXT_CODES[c]) {
            select(menu.sel + 10);
        } else if (PREV_KEYS[k] || PREV_CODES[c]) {
            select(menu.sel - 10);
        } else if (k === 'Enter' || c === 13 ||
                   (menu.level === 'groups' && (k === 'ArrowRight' || c === 39))) {
            var l = menu.rows[menu.sel];
            if (l) { l.action(); }
        } else if (BACK_KEYS[k] || BACK_CODES[c] || k === 'ArrowLeft' || c === 37) {
            menuBack();
        } else if (GROUPS_KEYS[k] || GROUPS_CODES[c]) {
            closeMenu();
        } else {
            return;
        }
        e.preventDefault();
        e.stopImmediatePropagation();
    }

    // ---------------------------------------------------------------- buttons

    function createButton(cls, icon, title, action, large) {
        // no 2nd argument to createElement: old Chromium treats it as a string
        var b = document.createElement('button');
        b.setAttribute('is', 'paper-icon-button-light');
        b.className = 'paper-icon-button-light ' + cls;
        b.title = title;
        b.setAttribute('aria-label', title);
        var s = document.createElement('span');
        s.className = (large ? 'xlargePaperIconButton ' : '') + 'material-icons ' + icon;
        s.setAttribute('aria-hidden', 'true');
        b.appendChild(s);
        b.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            action();
        });
        return b;
    }

    // "Groups" button in the header of every page (the player hides the header)
    function headerButton() {
        var right = document.querySelector('.skinHeader .headerRight');
        if (!right || right.querySelector('.btnZapGroups') || !GROUPS.length) { return; }
        right.insertBefore(createButton('headerButton headerButtonRight btnZapGroups',
            'view_list', t('groupsTitle'), openMenu), right.firstChild);
    }

    function osdButtons() {
        var pause = document.querySelector('.videoOsdBottom .btnPause');
        if (!pause) { return; }
        var bar = pause.parentNode;
        if (!bar.querySelector('.btnZapNext')) {
            bar.insertBefore(createButton('autoSize btnZapPrev', 'skip_previous',
                t('prevChannel'), function () { zap(-1); }, true), pause);
            var next = createButton('autoSize btnZapNext', 'skip_next',
                t('nextChannel'), function () { zap(1); }, true);
            bar.insertBefore(next, pause.nextSibling);
            if (GROUPS.length) {   // no group list: no groups button
                bar.insertBefore(createButton('autoSize btnZapGroupsOsd', 'view_list',
                    t('groupsTitle'), openMenu, true), next.nextSibling);
            }
        }
        var show = inPlayer() && !!current;
        var bs = bar.querySelectorAll('.btnZapPrev, .btnZapNext, .btnZapGroupsOsd');
        for (var i = 0; i < bs.length; i++) {
            bs[i].classList.toggle('hide', !show);
        }
    }

    // ---------------------------------------------------------------- diagnostics (off by default)
    //
    // Written to chase live channels stopping on their own in some browsers. The server log only
    // shows that the PLAYER gave up (DELETE ActiveEncodings -> "q" to ffmpeg), not why. When on,
    // this keeps the last events of the <video> element, of the HLS requests and of the keys;
    // when a channel stops, they are sent with POST /ClientLog/Document and land in Jellyfin's
    // log directory as upload_*.log. Turn on: DIAGNOSTICS = true.
    var DIAGNOSTICS = false;
    var RE_WATCHED = /\/videos\/|\.m3u8|\.ts(\?|$)|activeencodings|playbackinfo|\/sessions\/playing|livestreams/i;
    var diagLog = [];
    var diagChannel = null;        // name of the channel seen playing
    var intentionalStopUntil = 0;  // switch made by the zapping: not the drop being chased
    var sendTimer = null;
    var originalFetch = window.fetch;

    function record(kind, detail) {
        if (!DIAGNOSTICS) { return; }
        diagLog.push(new Date().toISOString().slice(11, 23) + ' ' + kind + ' ' + (detail || ''));
        if (diagLog.length > 200) { diagLog.shift(); }
    }

    // path only: the query string carries api_key and session ids
    function pathOnly(url) {
        return String(url || '').replace(/^https?:\/\/[^\/]+/, '').split('?')[0];
    }

    function videoState() {
        var v = document.querySelector('video');
        if (!v) { return 'no <video>'; }
        var b = v.buffered && v.buffered.length ? v.buffered.end(v.buffered.length - 1).toFixed(1) : '-';
        return 't=' + v.currentTime.toFixed(1) + ' buf=' + b + ' ready=' + v.readyState +
            ' net=' + v.networkState + ' paused=' + v.paused +
            (v.error ? ' ERROR=' + v.error.code + ':' + (v.error.message || '') : '');
    }

    function onChannelStopped(reason) {
        if (!DIAGNOSTICS || !diagChannel) { return; }
        record('STOP', reason + ' | ' + videoState());
        if (Date.now() < intentionalStopUntil) { return; }
        clearTimeout(sendTimer);
        sendTimer = setTimeout(sendDiag, 3000);   // also catches what comes right after
    }

    function sendDiag() {
        if (!diagLog.length || !api()) { return; }
        var text = 'Diagnostics: channel stopped on its own (zapping.js)\nchannel: ' + diagChannel +
            '\nbrowser: ' + navigator.userAgent + '\n\n' + diagLog.join('\n') + '\n';
        diagLog = [];
        diagChannel = null;
        try {
            api().ajax({ type: 'POST', url: api().getUrl('ClientLog/Document'),
                         data: text, contentType: 'text/plain' })
                .catch(function (e) { console.warn('zapping diag:', e); });
        } catch (e) {
            console.warn('zapping diag:', e);
        }
    }

    if (DIAGNOSTICS && window.XMLHttpRequest && !XMLHttpRequest.prototype.__zapDiag) {
        XMLHttpRequest.prototype.__zapDiag = true;
        var xhrOpen = XMLHttpRequest.prototype.open;
        var xhrSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function (method, url) {
            this.__zap = RE_WATCHED.test(url) ? { m: method, u: pathOnly(url) } : null;
            return xhrOpen.apply(this, arguments);
        };
        XMLHttpRequest.prototype.send = function () {
            var x = this;
            var info = x.__zap;
            if (info) {
                var t0 = Date.now();
                x.addEventListener('loadend', function () {
                    record('xhr', info.m + ' ' + info.u + ' -> ' + x.status + ' ' +
                        (Date.now() - t0) + 'ms');
                    if (info.m === 'DELETE' && /activeencodings/i.test(info.u)) {
                        onChannelStopped('DELETE ActiveEncodings');
                    }
                });
            }
            return xhrSend.apply(this, arguments);
        };
    }

    if (DIAGNOSTICS && originalFetch && !originalFetch.__zapDiag) {
        var diagFetch = function (input, options) {
            var url = typeof input === 'string' ? input : (input && input.url);
            var method = ((options && options.method) || (input && input.method) || 'GET').toUpperCase();
            var p = originalFetch.apply(this, arguments);
            if (url && RE_WATCHED.test(url) && !/clientlog/i.test(url)) {
                var t0 = Date.now();
                var u = pathOnly(url);
                p.then(function (r) {
                    record('fetch', method + ' ' + u + ' -> ' + r.status + ' ' + (Date.now() - t0) + 'ms');
                    if (method === 'DELETE' && /activeencodings/i.test(u)) {
                        onChannelStopped('DELETE ActiveEncodings');
                    } else if (/sessions\/playing\/stopped/i.test(u)) {
                        onChannelStopped('Sessions/Playing/Stopped');
                    }
                }, function (e) {
                    record('fetch', method + ' ' + u + ' FAILED ' + e);
                });
            }
            return p;
        };
        diagFetch.__zapDiag = true;
        window.fetch = diagFetch;
    }

    if (DIAGNOSTICS) {
        // media events do not bubble, but they do go through the document's capture phase
        ['error', 'stalled', 'waiting', 'ended', 'emptied', 'abort', 'playing', 'pause']
            .forEach(function (ev) {
                document.addEventListener(ev, function (e) {
                    if (e.target && e.target.tagName === 'VIDEO') {
                        record('video:' + ev, videoState());
                    }
                }, true);
            });
    }

    // ---------------------------------------------------------------- state refresh

    function refresh() {
        if (!api() || !uid()) { return; }
        mySession().then(function (s) {
            var np = s && s.NowPlayingItem;
            if (np && np.Type === 'TvChannel') {
                if (diagChannel !== np.Name) {
                    diagChannel = np.Name;
                    record('playing', np.Name + ' | ' + videoState());
                }
                if (!current || current.Id !== np.Id) {
                    return withSortName(np.Id).then(function (c) { if (c) { current = c; } });
                }
            } else if (np || (!inPlayer() && !inZapping())) {
                current = null;   // another kind of video, or outside the player and not zapping
            }
        }).then(osdButtons).catch(function (e) { console.warn('zapping:', e); });
    }

    function isTextField(el) {
        return el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
    }

    window.addEventListener('keydown', function (e) {
        if (inPlayer()) { record('key', e.key + '/' + e.keyCode); }
        if (menu) {
            menuKey(e);
            return;
        }
        if (isTextField(e.target)) { return; }
        if ((GROUPS_KEYS[e.key] || GROUPS_CODES[e.keyCode]) && GROUPS.length) {
            e.preventDefault();
            e.stopImmediatePropagation();
            openMenu();
            return;
        }
        if (!isActive()) { return; }
        var dir = (NEXT_KEYS[e.key] || NEXT_CODES[e.keyCode]) ? 1
            : ((PREV_KEYS[e.key] || PREV_CODES[e.keyCode]) ? -1 : 0);
        if (dir) {
            e.preventDefault();
            e.stopImmediatePropagation();
            zap(dir);
        } else if (/^[0-9]$/.test(e.key)) {
            e.preventDefault();
            e.stopImmediatePropagation();
            addDigit(e.key);
        }
    }, true);

    window.addEventListener('hashchange', function () {
        closeMenu();        // the TV browser's native Back changes page
        refresh();
    });

    setInterval(function () {
        ticks++;
        headerButton();
        if (inPlayer()) { osdButtons(); }
        if ((inPlayer() || inZapping()) && ticks % 4 === 0) { refresh(); }
    }, 1000);

    console.log('Channel zapping: loaded');
})();
