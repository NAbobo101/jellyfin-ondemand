# Contributing

Thanks for helping! A few ground rules keep the project useful and safe to host:

- **No content sources in the repository.** Built-in providers must be *generic* (a protocol or a piece
  of software, like Torznab or UNIT3D), never a specific site. Share providers for specific sites
  elsewhere; users load them as plugins. Issues/PRs naming or linking to sources of infringing content
  are closed.
- **Standard library only.** The service must keep running with nothing but Python ≥ 3.11 (it runs on
  small home servers, some without AVX). Tests too (`unittest`).
- **Tests with every change.** `python3 -I -m unittest discover -s tests -t .` must pass, and new
  behaviour needs a test. Providers are tested with fake HTTP (see `tests/test_providers.py`).
- **Web scripts stay ES5** (TV browsers run old Chromium) and must pass `node --check`;
  `web/tests/*.test.js` run with `npm install --no-save jsdom`.
- **Comments explain why.** Most odd-looking code is there because of a measured Jellyfin, qBittorrent
  or libtorrent behaviour; say which, so nobody "fixes" it back.
- **Never log secrets** (passkeys travel inside torrent URLs: never log a URL).

## Development

```bash
python3 -I -m unittest discover -s tests -t .          # ~35 s
node --check web/*.js web/extras/*.js
(cd web/tests && npm install --no-save jsdom@24 && cd ../.. && node web/tests/search.test.js && node web/tests/abr.test.js)
```

Translations of the web texts live in the `TEXTS` table at the top of `web/search.js` (and
`web/extras/zapping.js`); subtitle languages in `ondemand/languages.py`.
