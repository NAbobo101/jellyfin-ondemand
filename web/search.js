/*
 * jellyfin-ondemand — "Search outside the library": movies and series that are NOT in Jellyfin,
 * shown on Jellyfin's search page, ready to add and play.
 *
 * Injected by the "JavaScript Injector" plugin (RequiresAuthentication = true).
 * Install/update: JELLYFIN_URL=… JELLYFIN_API_KEY=… bash web/install-script.sh web/search.js "On demand: search"
 *
 * Why it exists: Jellyfin's search only looks in its own database. This section calls the
 * jellyfin-ondemand API at /ondemand/api (published on the SAME origin as Jellyfin by your
 * reverse proxy), which looks the term up on TMDb (through Jellyfin) and asks every provider,
 * returning only what is not in the library. A card opens a details window (read only); its ▶
 * button writes the movie to the catalog (a .strm: it only downloads on play) and, when the
 * item is ready, plays it in this very session with PlayNow. From then on the movie is there
 * for every device, including those that do not load this script (TV apps).
 *
 * Series: the card says "Series" and the button is "+ Add series"; the server searches EVERY
 * season and episode available and, when ready, the series page opens. Also, opening the page
 * of a CATALOG series asks /api/complete: the server looks for what is missing (at most once a
 * week per series; a local series is refused with 422, silently) and the page reloads.
 *
 * Jellyfin 10.11's search page is React (#searchPage): the section is our own node at the end
 * of the page and moves back to the end whenever React appends something after it.
 *
 * ES5 on purpose (no async/await, arrow functions, template strings): TV browsers are often
 * old Chromium.
 */
(function () {
    'use strict';
    if (window.__ondemandSearch) {
        return;
    }
    window.__ondemandSearch = true;

    var API_PATH = '/ondemand/api/';
    var TYPING_WAIT_MS = 700;
    var MIN_LETTERS = 3;
    var REQUEST_POLL_MS = 3000;
    var REQUEST_GIVE_UP_MS = 360000;   // the server gives up after 5 min; here, a bit later (series: 15 min)
    var COMPLETE_POLL_MS = 4000;
    var COMPLETE_GIVE_UP_MS = 1200000;

    // ---------------------------------------------------------------- texts (add a language here)
    var TEXTS = {
        en: {
            section: 'Outside the library: watch on demand',
            searching: 'Searching outside the library…',
            nothing: 'Nothing outside the library for “{0}”.',
            hint: 'Tap for details. Movie: ▶ adds and plays it, downloading while it plays. Series: adds every available season. It stays in the library for everyone.',
            failed: 'Could not search outside the library: {0}',
            details: 'View details', preparing: 'Preparing…', preparingFor: 'Preparing… {0} s', ready: 'Ready',
            series: 'Series', addPlay: '▶ Add and play', addSeries: '+ Add series', trailer: 'Trailer', close: 'Close',
            loadingOverview: 'Loading the overview…', noOverview: 'No overview available.',
            detailsFailed: 'Could not load the details: {0}', cast: 'Cast: ',
            season: 'season', seasons: 'seasons', episodes: 'episodes',
            footMovie: 'It only downloads when you press play, and stays in the library for everyone.',
            footSeries: 'Searches every season and episode available (20 s to 2 min) and builds the series in the library. It only downloads when you press play.',
            tryAgain: '{0}. Try again.', failedShort: 'It did not work', tooLong: 'It took too long. Try again.',
            seriesAdded: '“{0}” added{1}. The episodes show up in a moment.',
            alreadyThere: '“{0}” is already in the library.', readyInMovies: '“{0}” is ready in Movies.',
            opening: 'Opening “{0}”… the first play takes a few seconds.',
            completing: 'Looking for every season of “{0}”…',
            newEpisodes: '“{0}”: {1} new episodes. Refreshing…',
            alreadyComplete: '“{0}”: it was already complete ({1}).', completeFailed: 'Could not complete “{0}”: {1}',
            decimal: '.'
        },
        'pt-BR': {
            section: 'Fora da biblioteca: assistir sob demanda',
            searching: 'Procurando fora da biblioteca…',
            nothing: 'Nada fora da biblioteca para “{0}”.',
            hint: 'Toque para ver os detalhes. Filme: ▶ adiciona e assiste, baixa enquanto toca. Série: adiciona com todas as temporadas disponíveis. Fica na biblioteca para todos.',
            failed: 'Não consegui procurar fora da biblioteca: {0}',
            details: 'Ver detalhes', preparing: 'Preparando…', preparingFor: 'Preparando… {0} s', ready: 'Pronto',
            series: 'Série', addPlay: '▶ Adicionar e assistir', addSeries: '＋ Adicionar série', trailer: 'Trailer', close: 'Fechar',
            loadingOverview: 'Carregando a sinopse…', noOverview: 'Sem sinopse disponível.',
            detailsFailed: 'Não consegui carregar os detalhes: {0}', cast: 'Elenco: ',
            season: 'temporada', seasons: 'temporadas', episodes: 'episódios',
            footMovie: 'Baixa só quando você der play e fica na biblioteca para todos.',
            footSeries: 'Pesquisa todas as temporadas e episódios disponíveis (de 20 s a 2 min) e monta a série na biblioteca. Baixa só quando você der play.',
            tryAgain: '{0}. Tente de novo.', failedShort: 'Não deu certo', tooLong: 'Demorou demais. Tente de novo.',
            seriesAdded: '“{0}” adicionada{1}. Os episódios aparecem em instantes.',
            alreadyThere: '“{0}” já está na biblioteca.', readyInMovies: '“{0}” está pronto em Filmes.',
            opening: 'Abrindo “{0}”… o primeiro play leva alguns segundos.',
            completing: 'Procurando todas as temporadas de “{0}”…',
            newEpisodes: '“{0}”: {1} episódios novos. Atualizando…',
            alreadyComplete: '“{0}”: já estava completa ({1}).', completeFailed: 'Não consegui completar “{0}”: {1}',
            decimal: ','
        }
    };

    function language() {
        var l = (document.documentElement.getAttribute('lang') || navigator.language || 'en').replace('_', '-');
        if (TEXTS[l]) { return l; }
        var base = l.split('-')[0].toLowerCase();
        for (var k in TEXTS) {
            if (TEXTS.hasOwnProperty(k) && k.split('-')[0] === base) { return k; }
        }
        return 'en';
    }

    function t(key) {
        var s = (TEXTS[language()] || TEXTS.en)[key] || TEXTS.en[key] || key;
        for (var i = 1; i < arguments.length; i++) { s = s.replace('{' + (i - 1) + '}', String(arguments[i])); }
        return s;
    }

    function rating(n) { return '★ ' + String(n).replace('.', t('decimal')); }

    // ---------------------------------------------------------------- state
    var typedTerm = '';
    var searchedTerm = null;
    var searchTimer = null;
    var generation = 0;               // drops a search answer that is already stale
    var section = null;
    var requests = {};                // imdb -> {start, play, name, series}
    var dialog = null;                // the open details window
    var completing = {};              // item id -> true: series already checked/requested on this page load

    function api() { return window.ApiClient; }
    function onSearch() { return location.hash.indexOf('#/search') === 0; }

    function call(method, path, body) {
        var op = { method: method, headers: { 'X-Emby-Token': api().accessToken() } };
        if (body) {
            op.headers['Content-Type'] = 'application/json';
            op.body = JSON.stringify(body);
        }
        return fetch(api().serverAddress() + API_PATH + path, op).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (d) {
                if (!r.ok) {
                    var e = new Error(d.error || ('HTTP ' + r.status));
                    e.status = r.status;
                    throw e;
                }
                return d;
            });
        });
    }

    function mySession() {
        return api().getSessions({ DeviceId: api().deviceId() }).then(function (list) {
            var u = api().getCurrentUserId();
            return (list || []).filter(function (x) { return x.UserId === u; })[0] || null;
        });
    }

    // ---------------------------------------------------------------- toast
    function toast(text) {
        var el = document.getElementById('ondemandToast');
        if (!el) {
            el = document.createElement('div');
            el.id = 'ondemandToast';
            el.style.cssText = 'position:fixed;bottom:6vh;left:50%;transform:translateX(-50%);' +
                'z-index:2147483647;padding:.6em 1em;border-radius:.35em;background:rgba(0,0,0,.85);' +
                'color:#fff;font-size:1.1em;max-width:80vw;pointer-events:none;transition:opacity .3s;opacity:0';
            document.body.appendChild(el);
        }
        el.textContent = text;      // textContent: titles come from outside
        el.style.opacity = '1';
        clearTimeout(el._t);
        el._t = setTimeout(function () { el.style.opacity = '0'; }, 6000);
    }

    // ---------------------------------------------------------------- section and cards
    function div(cls, text) {
        var d = document.createElement('div');
        if (cls) { d.className = cls; }
        if (text) { d.textContent = text; }
        return d;
    }

    function createSection() {
        var s = div('verticalSection ondemandSearch');
        s.style.display = 'none';
        var h = document.createElement('h2');
        h.className = 'sectionTitle sectionTitle-cards padded-left';
        h.textContent = t('section');
        var hint = div('padded-left ondemandSearch-hint');
        hint.style.cssText = 'opacity:.75;margin:-.2em 0 .9em';
        s.appendChild(h);
        s.appendChild(hint);
        s.appendChild(div('itemsContainer vertical-wrap padded-left padded-right ondemandSearch-list'));
        return s;
    }

    function page() {
        var ps = document.querySelectorAll('#searchPage');
        for (var i = ps.length - 1; i >= 0; i--) {
            if (!ps[i].classList.contains('hide')) { return ps[i]; }
        }
        return null;
    }

    function ensureSection(p) {
        if (!section) { section = createSection(); }
        if (section.parentNode !== p || section.nextSibling) { p.appendChild(section); }
    }

    function showHint(text) {
        section.style.display = '';
        section.querySelector('.ondemandSearch-hint').textContent = text;
    }

    function hide() {
        if (section) { section.style.display = 'none'; }
    }

    function label(imdb, text) {
        if (dialog && dialog.imdb === imdb) { dialog.button.textContent = text; }
        if (!section) { return; }
        var c = section.querySelector('[data-imdb="' + imdb + '"] .ondemandSearch-state');
        if (c) { c.textContent = text; }
    }

    function card(c) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'card portraitCard card-hoverable ondemandSearch-card';
        b.style.cssText = 'background:none;border:0;padding:0;margin:0;color:inherit;font:inherit;' +
            'cursor:pointer;text-align:center';
        b.setAttribute('data-imdb', c.imdb);
        if (c.original_name && c.original_name !== c.name) { b.title = c.original_name; }

        var box = div('cardBox cardBox-bottompadded');
        var scal = div('cardScalable');
        scal.appendChild(div('cardPadder cardPadder-portrait'));
        var image = div('cardImageContainer coveredImage cardContent');
        if (c.poster && /^https:\/\//.test(c.poster)) {
            var im = document.createElement('img');
            im.src = c.poster;              // <img>, not background-image: an outside URL never becomes CSS
            im.alt = '';
            im.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover';
            image.appendChild(im);
        } else {
            image.className += ' defaultCardBackground defaultCardBackground1';
            image.appendChild(div('cardText', c.name));
        }
        scal.appendChild(image);
        if (c.kind === 'series' || (c.qualities && c.qualities.indexOf('4K') >= 0)) {
            var badge = div('', c.kind === 'series' ? t('series') : '4K');
            badge.style.cssText = 'position:absolute;top:.4em;right:.4em;padding:.1em .4em;border-radius:.2em;' +
                'background:rgba(0,0,0,.75);color:#fff;font-size:.8em;font-weight:700';
            scal.appendChild(badge);
        }
        var state = div('ondemandSearch-state');
        state.style.cssText = 'position:absolute;left:0;right:0;bottom:0;padding:.4em .3em;' +
            'background:rgba(0,0,0,.75);color:#fff;font-size:.85em;line-height:1.2';
        scal.appendChild(state);
        box.appendChild(scal);

        var line2 = [];
        if (c.year) { line2.push(String(c.year)); }
        if (c.rating) { line2.push(rating(c.rating)); }
        box.appendChild(div('cardText cardTextCentered cardText-first', c.name));
        box.appendChild(div('cardText cardTextCentered cardText-secondary', line2.join(' · ') || ' '));
        b.appendChild(box);

        b.addEventListener('click', function () { openDetails(c, b); });
        state.textContent = (requests[c.imdb] || c.request === 'preparing') ? t('preparing') : t('details');
        if (c.request === 'preparing' && !requests[c.imdb]) {
            // a request from before (another tab, page reloaded): follow it, but do not play by itself
            requests[c.imdb] = { start: Date.now(), play: false, name: c.name, series: c.kind === 'series' };
            poll(c.imdb);
        }
        return b;
    }

    function show(results, term) {
        var list = section.querySelector('.ondemandSearch-list');
        while (list.firstChild) { list.removeChild(list.firstChild); }
        if (!results.length) {
            showHint(t('nothing', term));
            return;
        }
        showHint(t('hint'));
        results.forEach(function (c) { list.appendChild(card(c)); });
    }

    // ---------------------------------------------------------------- details window
    function minutes(m) {
        if (!m) { return ''; }
        return m >= 60 ? Math.floor(m / 60) + 'h' + ('0' + (m % 60)).slice(-2) : m + ' min';
    }

    function img(url, style) {
        var im = document.createElement('img');
        if (url && /^https:\/\//.test(url)) { im.src = url; }
        im.alt = '';
        im.style.cssText = style;
        return im;
    }

    function button(text, main) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'raised button-submit emby-button ondemandSearch-button';
        b.textContent = text;
        b.style.cssText = 'margin:.3em .6em .3em 0;padding:.7em 1.3em;border:0;border-radius:.3em;cursor:pointer;' +
            'font:inherit;font-size:1em;' + (main ? 'background:#00a4dc;color:#fff' : 'background:rgba(255,255,255,.15);color:#fff');
        return b;
    }

    function buttonText(c) {
        return requests[c.imdb] ? t('preparing') : (c.kind === 'series' ? t('addSeries') : t('addPlay'));
    }

    function fill(j, d) {
        var c = j.card;
        j.title.textContent = d.name || c.name;
        var orig = d.original_name && d.original_name !== (d.name || c.name) ? d.original_name : '';
        j.original.textContent = orig;
        var info = [];
        if (d.year || c.year) { info.push(String(d.year || c.year)); }
        if (d.rating || c.rating) { info.push(rating(d.rating || c.rating)); }
        if (d.runtime_min) { info.push(minutes(d.runtime_min)); }
        if (d.kind === 'series' && d.seasons) {
            info.push(d.seasons + ' ' + (d.seasons === 1 ? t('season') : t('seasons')) + ' · ' + d.episodes + ' ' + t('episodes'));
        }
        if (d.genres && d.genres.length) { info.push(d.genres.join(', ')); }
        j.info.textContent = info.join(' · ');
        var q = d.qualities && d.qualities.length ? d.qualities : (c.qualities || []);
        j.qualities.textContent = q.join(' · ');
        if (d.overview) {
            j.overview.textContent = d.overview;
        } else if (d.overview === null) {
            j.overview.textContent = t('noOverview');
        }
        if (d.cast && d.cast.length) {
            j.cast.textContent = t('cast') + d.cast.map(function (a) {
                return a.character ? a.name + ' (' + a.character + ')' : a.name;
            }).join(', ');
        }
        if (d.poster && /^https:\/\//.test(d.poster)) { j.poster.src = d.poster; }
        if (d.backdrop && /^https:\/\//.test(d.backdrop)) { j.backdrop.src = d.backdrop; j.backdrop.style.display = ''; }
        if (d.trailer_youtube && /^[\w-]{6,20}$/.test(d.trailer_youtube)) {
            j.trailer.style.display = '';
            j.trailer.onclick = function () {
                window.open('https://www.youtube.com/watch?v=' + d.trailer_youtube, '_blank', 'noopener');
            };
        }
    }

    function dialogKey(e) {
        var k = e.keyCode || e.which;
        // Escape, TV remotes' Back (Tizen 10009, webOS 461), Backspace outside a text field
        if (k === 27 || k === 10009 || k === 461 || (k === 8 && !/INPUT|TEXTAREA/.test((e.target || {}).tagName || ''))) {
            e.preventDefault();
            e.stopPropagation();
            closeDetails();
        }
    }

    function dialogBack() {
        // the browser's/remote's Back: the pushState of openDetails becomes the close
        if (dialog) { closeDetails(true); }
    }

    function closeDetails(byHistory) {
        if (!dialog) { return; }
        var j = dialog;
        dialog = null;
        document.removeEventListener('keydown', dialogKey, true);
        window.removeEventListener('popstate', dialogBack);
        if (j.el.parentNode) { j.el.parentNode.removeChild(j.el); }
        if (!byHistory && history.state && history.state.ondemandSearch) { history.back(); }
        if (j.previous && j.previous.focus) { try { j.previous.focus(); } catch (e) { /* left the page */ } }
    }

    function openDetails(c, origin) {
        closeDetails();
        var overlay = div('ondemandSearch-dialog');
        overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483000;' +
            'background:rgba(0,0,0,.8);overflow-y:auto;-webkit-overflow-scrolling:touch';
        overlay.addEventListener('click', function (e) { if (e.target === overlay) { closeDetails(); } });

        var box = div('');
        box.setAttribute('role', 'dialog');
        box.setAttribute('aria-modal', 'true');
        box.style.cssText = 'position:relative;max-width:56em;margin:5vh auto;background:#181818;color:#fff;' +
            'border-radius:.5em;overflow:hidden;box-shadow:0 0 2em rgba(0,0,0,.6)';
        var backdrop = img(null, 'position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;opacity:.18;display:none');
        box.appendChild(backdrop);

        var content = div('');
        content.style.cssText = 'position:relative;padding:1.5em';
        var x = button('✕', false);
        x.setAttribute('aria-label', t('close'));
        x.style.cssText += ';position:absolute;top:.6em;right:.6em;margin:0;padding:.4em .7em';
        x.addEventListener('click', function () { closeDetails(); });
        content.appendChild(x);

        var top = div('');
        top.style.cssText = 'display:flex;flex-wrap:wrap;gap:1.2em;align-items:flex-start';
        var poster = img(c.poster, 'width:11em;max-width:40%;border-radius:.3em;flex:none;background:#333');
        top.appendChild(poster);
        var side = div('');
        side.style.cssText = 'flex:1;min-width:12em;padding-right:2.5em';
        var title = document.createElement('h2');
        title.style.cssText = 'margin:0 0 .2em;font-size:1.7em';
        var original = div('');
        original.style.cssText = 'opacity:.7;margin-bottom:.5em';
        var info = div('');
        info.style.cssText = 'margin-bottom:.4em';
        var qualities = div('');
        qualities.style.cssText = 'opacity:.8;font-weight:600;margin-bottom:1em';
        var actions = div('');
        var watch = button(buttonText(c), true);
        watch.addEventListener('click', function () { request(c); });
        var trailer = button(t('trailer'), false);
        trailer.style.display = 'none';
        var leave = button(t('close'), false);
        leave.addEventListener('click', function () { closeDetails(); });
        actions.appendChild(watch);
        actions.appendChild(trailer);
        actions.appendChild(leave);
        side.appendChild(title);
        side.appendChild(original);
        side.appendChild(info);
        side.appendChild(qualities);
        side.appendChild(actions);
        top.appendChild(side);
        content.appendChild(top);

        var overview = div('', t('loadingOverview'));
        overview.style.cssText = 'margin-top:1.2em;line-height:1.5;font-size:1.05em';
        var cast = div('');
        cast.style.cssText = 'margin-top:1em;opacity:.85;line-height:1.4';
        var foot = div('', c.kind === 'series' ? t('footSeries') : t('footMovie'));
        foot.style.cssText = 'margin-top:1.2em;opacity:.55;font-size:.85em';
        content.appendChild(overview);
        content.appendChild(cast);
        content.appendChild(foot);
        box.appendChild(content);
        overlay.appendChild(box);
        document.body.appendChild(overlay);

        dialog = { el: overlay, imdb: c.imdb, button: watch, card: c, previous: origin || document.activeElement,
                   title: title, original: original, info: info, qualities: qualities, overview: overview,
                   cast: cast, poster: poster, backdrop: backdrop, trailer: trailer };
        fill(dialog, {});
        document.addEventListener('keydown', dialogKey, true);
        try {
            history.pushState({ ondemandSearch: true }, '', location.href);
            window.addEventListener('popstate', dialogBack);
        } catch (e) { /* no history: closes only with the button and the key */ }
        watch.focus();

        var j = dialog;
        call('GET', 'details/' + c.imdb + (c.kind === 'series' ? '?kind=series' : '')).then(function (d) {
            if (dialog === j) { fill(j, d); }
        }, function (e) {
            if (dialog === j) { j.overview.textContent = t('detailsFailed', e.message); }
        });
    }

    // ---------------------------------------------------------------- search
    function search(term) {
        if (term === searchedTerm) { return; }
        searchedTerm = term;
        var g = ++generation;
        if (term.length < MIN_LETTERS) {
            hide();
            return;
        }
        show([], term);
        showHint(t('searching'));
        call('GET', 'search?q=' + encodeURIComponent(term)).then(function (d) {
            if (g === generation) { show(d.results || [], term); }
        }, function (e) {
            if (g !== generation) { return; }
            if (e.status === 401) {
                hide();
            } else {
                showHint(t('failed', e.message));
            }
        });
    }

    // ---------------------------------------------------------------- request
    function request(c) {
        if (requests[c.imdb]) { return; }          // already requested: the label shows the progress
        requests[c.imdb] = { start: Date.now(), play: true, name: c.name, series: c.kind === 'series' };
        label(c.imdb, t('preparing'));
        call('POST', 'request', { imdb: c.imdb, kind: c.kind === 'series' ? 'series' : 'movie' }).then(function (s) {
            handle(c.imdb, s);
        }, function (e) {
            delete requests[c.imdb];
            label(c.imdb, e.message);
        });
    }

    function poll(imdb) {
        setTimeout(function () {
            call('GET', 'request/' + imdb).then(function (s) {
                handle(imdb, s);
            }, function () {
                handle(imdb, { state: 'preparing' });  // network failure: try again
            });
        }, REQUEST_POLL_MS);
    }

    function handle(imdb, s) {
        var p = requests[imdb];
        if (!p) { return; }
        if (s.state === 'ready') {
            delete requests[imdb];
            label(imdb, t('ready'));
            // close without history.back(): an asynchronous back could arrive after the PlayNow and take
            // the person out of the player; a repeated history entry is left behind, harmless
            if (dialog && dialog.imdb === imdb && p.play) { closeDetails(true); }
            if (p.series) {
                toast(t('seriesAdded', p.name, s.detail ? ': ' + s.detail : ''));
                if (p.play && onSearch()) { openItem(s.item_id); }
            } else if (p.play) {
                play(s.item_id, p.name);
            } else {
                toast(t('alreadyThere', p.name));
            }
        } else if (s.state === 'error') {
            delete requests[imdb];
            label(imdb, t('tryAgain', s.detail || t('failedShort')));
        } else if (Date.now() - p.start > (p.series ? 900000 : REQUEST_GIVE_UP_MS)) {
            delete requests[imdb];
            label(imdb, t('tooLong'));
        } else {
            label(imdb, s.detail ? s.detail : t('preparingFor', Math.round((Date.now() - p.start) / 1000)));
            poll(imdb);
        }
    }

    function openItem(itemId) {
        location.hash = '#/details?id=' + itemId + '&serverId=' + api().serverId();
    }

    function play(itemId, name) {
        if (!onSearch()) {
            // the person left the search while it was being prepared: do not interrupt what they went to do
            toast(t('readyInMovies', name));
            return;
        }
        toast(t('opening', name));
        mySession().then(function (s) {
            if (!s) { throw new Error('session not found'); }
            if (s.NowPlayingItem) {
                toast(t('readyInMovies', name));
                return null;
            }
            return api().sendPlayCommand(s.Id, { PlayCommand: 'PlayNow', ItemIds: itemId });
        }).catch(function (e) {
            console.warn('ondemand search: PlayNow failed, opening the item:', e);
            openItem(itemId);
        });
    }

    // ---------------------------------------------------------------- complete on opening a series
    function pageItemId() {
        var m = /^#\/details\?(?:.*&)?id=([0-9a-f]{32})/.exec(location.hash);
        return m ? m[1] : null;
    }

    function checkSeries() {
        var id = pageItemId();
        if (!id || completing[id] || !api() || !api().accessToken()) { return; }
        completing[id] = true;                       // one check per series and per page load
        api().getItem(api().getCurrentUserId(), id).then(function (item) {
            if (!item || item.Type !== 'Series') { return; }
            return call('POST', 'complete', { item_id: id }).then(function (s) {
                if (s.recent || s.state === 'ready' || s.state === 'error') { return; }
                toast(t('completing', item.Name));
                followComplete(id, item.Name, s.tvdb, Date.now());
            });
        }).catch(function () { /* 422 = local series or not a series; network: tries on the next opening */ });
    }

    function followComplete(id, name, tvdb, start) {
        setTimeout(function () {
            if (pageItemId() !== id || Date.now() - start > COMPLETE_GIVE_UP_MS) { return; }
            call('GET', 'complete/' + tvdb).then(function (s) {
                if (s.state === 'searching') { followComplete(id, name, tvdb, start); return; }
                if (s.state === 'ready' && s.found > 0) {
                    toast(t('newEpisodes', name, s.found));
                    setTimeout(function () { if (pageItemId() === id) { location.reload(); } }, 2500);
                } else if (s.state === 'ready') {
                    toast(t('alreadyComplete', name, s.detail || ''));
                } else {
                    toast(t('completeFailed', name, s.detail || 'error'));
                }
            }, function () { followComplete(id, name, tvdb, start); });
        }, COMPLETE_POLL_MS);
    }

    // ---------------------------------------------------------------- loop
    setInterval(function () {
        if (!api() || !api().accessToken() || !onSearch()) { return; }
        var p = page();
        var field = p && p.querySelector('.searchfields-txtSearch');
        if (!field) { return; }
        ensureSection(p);
        var term = (field.value || '').replace(/\s+/g, ' ').trim();
        if (term !== typedTerm) {
            typedTerm = term;
            clearTimeout(searchTimer);
            searchTimer = setTimeout(function () { search(term); }, TYPING_WAIT_MS);
        }
    }, 500);

    window.addEventListener('hashchange', function () {
        if (!onSearch()) { typedTerm = ''; searchedTerm = null; closeDetails(true); }
        setTimeout(checkSeries, 800);
    });
    setTimeout(checkSeries, 3000);                  // a series page opened directly (link, reload)

    window.__ondemandSearchTexts = TEXTS;           // for the offline test
    console.log('jellyfin-ondemand search: loaded');
})();
