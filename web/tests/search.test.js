// web/search.js in jsdom, with a fake fetch and a fake ApiClient (no server involved).
// Usage: npm install --no-save jsdom && node web/tests/search.test.js [web/search.js]
'use strict';
const { JSDOM, VirtualConsole } = require('jsdom');
const fs = require('fs');
const path = require('path');

const source = fs.readFileSync(process.argv[2] || path.join(__dirname, '..', 'search.js'), 'utf8');
const results = [];
const check = (name, cond, extra) => {
  results.push(cond);
  console.log((cond ? 'OK   ' : 'FAIL ') + name + (extra && !cond ? ' — ' + extra : ''));
};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const SERIES = { kind: 'series', imdb: 'tt0773262', tvdb: '79349', tmdb: '1405', name: 'Dexter', year: 2006, rating: null,
                 poster: 'https://image.tmdb.org/t/p/w342/x.jpg', qualities: [], request: null };
const MOVIE = { kind: 'movie', imdb: 'tt0068646', name: 'O Poderoso Chefão', original_name: 'The Godfather', year: 1972,
                rating: 8.7, poster: null, qualities: ['1080p', '4K'], request: null };
const ITEM = 'b'.repeat(32), OPENED = 'c'.repeat(32);

function page(lang) {
  const calls = [];
  let requestPolls = 0, completePolls = 0;
  const answer = (body, status) => Promise.resolve({ ok: !status || status < 400, status: status || 200, json: () => Promise.resolve(body) });
  function fakeFetch(url, op) {
    const p = url.split('/ondemand/api/')[1];
    const method = (op && op.method) || 'GET';
    calls.push(method + ' ' + p + (op && op.body ? ' ' + op.body : ''));
    if (p.indexOf('search?') === 0) { return answer({ results: [MOVIE, SERIES] }); }
    if (p.indexOf('details/tt0773262?kind=series') === 0) {
      return answer({ kind: 'series', imdb: 'tt0773262', name: 'Dexter', year: 2006, overview: 'A forensic expert who kills.',
                      seasons: 8, episodes: 96, genres: ['Crime'] });
    }
    if (p.indexOf('details/tt0068646') === 0) {
      return answer({ kind: 'movie', imdb: 'tt0068646', name: 'O Poderoso Chefão', original_name: 'The Godfather', year: 1972,
                      overview: null, qualities: ['1080p', '4K'] });
    }
    if (method === 'POST' && p === 'request') { return answer({ state: 'preparing', imdb: 'tt0773262' }, 202); }
    if (p === 'request/tt0773262') {
      requestPolls++;
      return answer(requestPolls < 2 ? { state: 'preparing', detail: 'Searching public-index (1/1)…' }
                                     : { state: 'ready', item_id: ITEM, detail: '96 of 96 episodes' });
    }
    if (method === 'POST' && p === 'complete') {
      const body = JSON.parse(op.body);
      if (body.item_id === 'd'.repeat(32)) { return answer({ error: 'only catalog series are completed' }, 422); }
      return answer({ state: 'searching', tvdb: '79349' }, 202);
    }
    if (p === 'complete/79349') {
      completePolls++;
      return answer(completePolls < 2 ? { state: 'searching' } : { state: 'ready', found: 5, detail: '12 of 12 episodes' });
    }
    return answer({ error: 'route ' + p }, 404);
  }
  const html = '<!doctype html><html lang="' + lang + '"><body><div id="searchPage" class="page"><div class="searchFields">' +
    '<input class="searchfields-txtSearch" type="search"></div></div></body></html>';
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => { if (!/Not implemented: navigation/.test(e.message)) { console.error(e); } });   // location.reload()
  const dom = new JSDOM(html, { url: 'http://jellyfin.example:8096/web/#/search', runScripts: 'outside-only', virtualConsole: vc });
  const w = dom.window;
  w.fetch = fakeFetch;
  const items = {};
  items[OPENED] = { Type: 'Series', Name: 'Dexter' };
  items['d'.repeat(32)] = { Type: 'Series', Name: 'The Boys' };
  items['e'.repeat(32)] = { Type: 'Movie', Name: 'A movie' };
  w.ApiClient = {
    serverAddress: () => 'http://jellyfin.example:8096', accessToken: () => 'tok', deviceId: () => 'd', getCurrentUserId: () => 'u1',
    serverId: () => 'srv', getSessions: () => Promise.resolve([]), sendPlayCommand: () => Promise.resolve(),
    getItem: (u, id) => Promise.resolve(items[id]),
  };
  w.console.log = () => {};
  w.eval(source);
  return { w, calls };
}

(async () => {
  // ---------------------------------------------------------------- pt-BR, the whole series flow
  const { w, calls } = page('pt-BR');
  const T = w.__ondemandSearchTexts['pt-BR'];
  w.document.querySelector('.searchfields-txtSearch').value = 'dexter';
  await wait(2200);
  const sec = w.document.querySelector('.ondemandSearch');
  check('section title in Portuguese', sec.querySelector('h2').textContent === T.section);
  const cards = [...sec.querySelectorAll('.ondemandSearch-card')];
  check('two cards (movie and series)', cards.length === 2);
  const cs = cards.filter((c) => c.dataset.imdb === 'tt0773262')[0];
  check('series card has the "Série" badge', cs.textContent.indexOf('Série') >= 0);
  const cm = cards.filter((c) => c.dataset.imdb === 'tt0068646')[0];
  check('movie card keeps the 4K badge and a decimal comma', cm.textContent.indexOf('4K') >= 0 && cm.textContent.indexOf('★ 8,7') >= 0);

  cs.click();
  await wait(300);
  const dlg = w.document.querySelector('.ondemandSearch-dialog');
  const btn = [...dlg.querySelectorAll('.ondemandSearch-button')].filter((b) => b.textContent === T.addSeries)[0];
  check('dialog opens with "+ Adicionar série"', !!btn);
  check('asked for details with kind=series', calls.indexOf('GET details/tt0773262?kind=series') >= 0);
  check('shows seasons and episodes', /8 temporadas · 96 episódios/.test(dlg.textContent), dlg.textContent);
  check('footer explains the full search', dlg.textContent.indexOf(T.footSeries) >= 0);

  btn.click(); btn.click();
  await wait(500);
  check('POST request carries kind=series, only once',
        calls.filter((c) => c === 'POST request {"imdb":"tt0773262","kind":"series"}').length === 1);
  await wait(3400);
  const state = cs.querySelector('.ondemandSearch-state').textContent;
  check('server progress shows on the card', /Searching public-index/.test(state), state);
  await wait(3400);
  check('a ready series opens its page (does not play)', w.location.hash === '#/details?id=' + ITEM + '&serverId=srv', w.location.hash);
  const toast = w.document.getElementById('ondemandToast');
  check('toast says how many episodes', toast && /96 of 96 episodes/.test(toast.textContent), toast && toast.textContent);

  // opening a catalog series triggers "complete" (a local series and a movie do not)
  w.location.hash = '#/details?id=' + 'd'.repeat(32);
  await wait(1500);
  check('local series (422) stays silent', calls.filter((c) => /POST complete/.test(c)).length === 1);
  w.location.hash = '#/details?id=' + 'e'.repeat(32);
  await wait(1500);
  check('a movie does not call complete', calls.filter((c) => /POST complete/.test(c)).length === 1);
  w.location.hash = '#/details?id=' + OPENED;
  await wait(1500);
  check('a catalog series calls complete', calls.indexOf('POST complete {"item_id":"' + OPENED + '"}') >= 0);
  check('tells it is searching', /Procurando todas as temporadas/.test(w.document.getElementById('ondemandToast').textContent));
  await wait(9000);
  check('finished and announced the new episodes', /5 episódios novos/.test(w.document.getElementById('ondemandToast').textContent),
        w.document.getElementById('ondemandToast').textContent);
  w.location.hash = '#/details?id=' + 'd'.repeat(32);
  w.location.hash = '#/details?id=' + OPENED;
  await wait(1500);
  check('reopening in the same session does not repeat', calls.filter((c) => c.indexOf('POST complete {"item_id":"' + OPENED) === 0).length === 1);
  w.close();

  // ---------------------------------------------------------------- English, movie dialog
  const en = page('en-US');
  en.w.document.querySelector('.searchfields-txtSearch').value = 'godfather';
  await wait(2200);
  const enCards = [...en.w.document.querySelectorAll('.ondemandSearch-card')];
  check('English: section title', en.w.document.querySelector('.ondemandSearch h2').textContent === 'Outside the library: watch on demand');
  check('English: "Series" badge and "★ 8.7"', enCards[1].textContent.indexOf('Series') >= 0 && enCards[0].textContent.indexOf('★ 8.7') >= 0);
  enCards[0].click();
  await wait(300);
  const enDlg = en.w.document.querySelector('.ondemandSearch-dialog');
  check('English: "▶ Add and play" and the original name', /▶ Add and play/.test(enDlg.textContent) && /The Godfather/.test(enDlg.textContent));
  check('English: no overview text', /No overview available/.test(enDlg.textContent));
  en.w.close();

  console.log(results.every(Boolean) ? '\nALL OK (' + results.length + ' checks)' : '\nTHERE ARE FAILURES');
  process.exit(results.every(Boolean) ? 0 : 1);
})();
