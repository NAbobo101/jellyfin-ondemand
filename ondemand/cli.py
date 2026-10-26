"""jellyfin-ondemand's command line (maintenance and trials), with the SAME configuration as the service.

    docker compose exec ondemand python3 -m ondemand.cli status
    docker compose exec ondemand python3 -m ondemand.cli providers          # what each provider can do
    docker compose exec ondemand python3 -m ondemand.cli preview [--provider NAME] [--title TEXT] [--detail]
    docker compose exec ondemand python3 -m ondemand.cli import             # the scheduler's step, now
    docker compose exec ondemand python3 -m ondemand.cli search-movie "Night of the Living Dead" [--register]
    docker compose exec ondemand python3 -m ondemand.cli search-series "Dexter" [--register]
    docker compose exec ondemand python3 -m ondemand.cli write [--batch 20] [--force]
    docker compose exec ondemand python3 -m ondemand.cli write-series [--batch 5] [--force]
    docker compose exec ondemand python3 -m ondemand.cli add-manual --name … --year … --imdb … --url …
    docker compose exec ondemand python3 -m ondemand.cli rewrite-strm       # after changing [gateway] public_url

`preview` only reads: it runs the providers' feeds through both filters and prints what would
enter the catalog and why everything else was left out — the first thing to run after adding a
provider. A torrent URL is never printed (it may carry a passkey).
"""

import argparse
import logging
import os
from collections import Counter

from . import config, movie_filter, series_filter, series_ondemand
from .providers import MovieQuery
from .release_name import normalize
from .service import build, import_catalog
from .subtitles.formats import write_atomic


def _app():
    return build(config.load())


def _release_line(c):
    parts = [c.get("resolution") or "SD", c.get("source"), c.get("codec"), "HDR" if c.get("hdr") else None,
             "PROPER" if c.get("proper") else None]
    kind = f"pack S{c['season']:02d}" if c.get("pack") else ("single" if "season" in c else "movie")
    return f"{' '.join(p for p in parts if p)} · {c['provider']} {kind} · {c['seeds']} seeds · {c['title'][:60]}"


def _reasons(discards, top=12):
    n = Counter(m.split(" (")[0] for _, m in discards)
    return "; ".join(f"{k}× {m}" for m, k in n.most_common(top)) or "none"


# ---------------------------------------------------------------- commands
def status(args):
    app = _app()
    for r in app.db.all("SELECT kind, state, count(*) n FROM titles GROUP BY kind, state"):
        print(f"titles   {r['kind']:7} {r['state']:16} {r['n']}")
    print(f"releases                          {app.db.one('SELECT count(*) n FROM releases')['n']}")
    for r in app.db.all("SELECT state, count(*) n FROM torrents GROUP BY state"):
        print(f"torrents {r['state']:24} {r['n']}")
    for r in app.db.all("SELECT state, count(*) n FROM subtitles GROUP BY state"):
        print(f"subtitles {r['state']:23} {r['n']}")


def providers_cmd(args):
    app = _app()
    if not len(app.registry):
        print("no [[providers]] configured (see config.example.toml)")
    for p in app.registry:
        rule = p.seed_rule
        seed = f"min {rule.min_hours:g} h {'OR' if rule.either else 'AND'} ratio {rule.min_ratio:g}"
        print(f"{p.name:24} {p.type:12} {'private' if p.private else 'public':8} "
              f"{', '.join(sorted(p.capabilities)) or '-':22} seed: {seed}")


def preview(args):
    """The providers' feeds through both filters, without writing anything."""
    app = _app()
    if args.provider:
        p = app.registry.by_name.get(args.provider)
        if p is None:
            raise SystemExit(f"no provider named {args.provider!r}")
        items = p.feed()
        warnings = []
    else:
        items, warnings = app.registry.feed()
    cands = [i.candidate() for i in items]
    if args.title:
        cands = [c for c in cands if normalize(args.title) in normalize(c["title"])]
    print(f"{len(cands)} item(s) from the feed(s)" + (f"; warnings: {warnings}" if warnings else ""))
    if app.cfg["movies"]["enabled"]:
        r = movie_filter.build_movies([c for c in cands if c["kind"] != "series"],
                                      movie_filter.MovieResolver(app.jf), app.cfg["movies"])
        print(f"\n## Movies: {len(r['movies'])} · quarantine: {len(r['quarantine'])}")
        print("discards:", _reasons(r["discards"]))
        for imdb, (t, vs) in sorted(r["movies"].items(), key=lambda kv: kv[1][0]["name"].lower()):
            print(f"  {t['name']} ({t['year']}) [{imdb}]")
            for v in vs:
                print(f"     {v['label']:5} {v['provider']} · {v['seeds']} seeds · {v['name'][:70]}")
        for name, q in sorted(r["quarantine"].items()):
            print(f"  ? {name}: {q['reason']}")
    if app.cfg["series"]["enabled"]:
        local = app.jf.local_episodes(catalog_prefix=app.cat.jellyfin_prefix)
        try:
            r = series_filter.build_catalog([c for c in cands if c["kind"] != "movie"], series_filter.Resolver(app.tv),
                                            app.cfg["series"], local)
        finally:
            app.tv.save()
        print(f"\n## Series: {len(r['series'])} · quarantine: {len(r['quarantine'])} · TVmaze requests: {app.tv.requests}")
        print("discards:", _reasons(r["discards"]))
        problems = series_filter.check(r)
        print("guarantees:", "ok (no duplicates)" if not problems else problems)
        for key, s in sorted(r["series"].items(), key=lambda kv: kv[1]["series"]["name"].lower()):
            print(f"  {series_filter.folder_name(s['series'])}")
            for t, eps in sorted(s["seasons"].items()):
                by_line = Counter(_release_line(e["winner"]) for e in eps.values())
                local_n = sum(1 for e in eps.values() if e["local"])
                print(f"     S{t:02d}: {len(eps)} ep(s){f', {local_n} already local' if local_n else ''}")
                for line, n in by_line.items():
                    print(f"        {n:2d}× {line}")
                if args.detail:
                    for n, e in sorted(eps.items()):
                        print(f"          E{n:02d}: {e['competitors']} competitor(s); lost: "
                              + "; ".join(f"{m} ({c['title'][:40]})" for c, m in e["losses"]))
        for name, q in sorted(r["quarantine"].items()):
            print(f"  ? {name}: {q['reason']}  [{q['candidates']} release(s); e.g. {q['example'][:60]}]")


def import_cmd(args):
    import_catalog(_app())


def search_movie(args):
    app = _app()
    term = args.movie
    if term.startswith("tt"):
        remote = app.jf.movie_by_imdb(term)
    else:
        found = app.jf.remote_search_movie(term, limit=5)
        print("TMDb:", " | ".join(f"{r.get('Name')} ({r.get('ProductionYear')})" for r in found) or "nothing")
        remote = found[0] if found else None
    if not remote:
        raise SystemExit("TMDb does not know that movie")
    ids = remote.get("ProviderIds") or {}
    imdb = ids.get("Imdb") or app.jf.imdb_from_tmdb(ids.get("Tmdb"))
    english = (app.jf.movie_by_tmdb(ids["Tmdb"], language="en") or {}).get("Name") if ids.get("Tmdb") else None
    q = MovieQuery(imdb=imdb, tmdb=ids.get("Tmdb"), title=remote.get("Name") or "", original_title=english,
                   year=remote.get("ProductionYear"))
    print(f"Movie: {english or q.title} ({q.year}) [{imdb}]")
    items, warnings = app.registry.search_movie(q)
    target = {"imdb": imdb, "tmdb": q.tmdb, "name": english or q.title, "year": q.year, "names": [english, q.title]}
    (title, versions), discards = movie_filter.build_for_movie([i.candidate() for i in items], target, app.cfg["movies"])
    print(f"{len(items)} item(s) from the providers; discards: {_reasons(discards)}")
    for w in warnings:
        print("WARNING:", w)
    for v in versions:
        print(f"  {v['label']:5} {v['provider']} · {v['seeds']} seeds · {v['name'][:70]}")
    if args.register and versions:
        tid = app.cat.register(title, versions, app.jf.local_ids(app.cat.jellyfin_prefix))
        print(f"registered as title {tid} (written by the catalog loop, or now with `write --force`)")


def _identify_series(term, app, resolver):
    """The filter's series from what was typed: tt… (IMDb), a number (TheTVDB) or a name (TMDb)."""
    if term.startswith("tt"):
        show = app.tv.by_imdb(term)
        ids = {"imdb": term, "tvdb": str((show or {}).get("externals", {}).get("thetvdb") or "") or None}
    elif term.isdigit():
        ids = {"tvdb": term}
    else:
        found = app.jf.remote_search_series(term, limit=5)
        if not found:
            raise SystemExit(f"TMDb found no series for “{term}”")
        print("TMDb:", " | ".join(f"{a.get('Name')} ({(a.get('PremiereDate') or '')[:4]})" for a in found[:5]),
              "→ using the first one")
        x = app.jf.series_ids_from_tmdb(found[0]["ProviderIds"]["Tmdb"])
        ids = {"tvdb": x.get("Tvdb"), "imdb": x.get("Imdb"), "tmdb": found[0]["ProviderIds"]["Tmdb"]}
    series = series_ondemand.identify(resolver, ids)
    if series is None:
        raise SystemExit("no TheTVDB or IMDb id for that series: its episodes cannot be checked")
    return series


def search_series(args):
    """Everything that exists of ONE series, through the filter. Only reads and prints; with --register
    it stores the episodes in the database (folders come with `write-series`)."""
    app = _app()
    resolver = series_filter.Resolver(app.tv)
    try:
        series = _identify_series(args.series, app, resolver)
        print(f"Series: {series['name']} ({series['year']}) [{series['key']}] imdb={series.get('imdb')}")
        local = app.jf.local_episodes(catalog_prefix=app.cat.jellyfin_prefix)
        r, res = series_ondemand.research(series, resolver, app.cfg, app.registry, local,
                                          progress=lambda i, n, name: print(f"  [{i}/{n}] {name}", flush=True))
    finally:
        app.tv.save()
    s = (r["series"].get(series["key"]) or {}).get("seasons", {})
    print(f"\nCandidates: {res['candidates']} · aired episodes: {res['episodes_aired']} · found: "
          f"{res['episodes_found']} (to register: {res['new']})")
    for t, eps in sorted(s.items()):
        by = Counter(f"{e['winner']['provider']} {e['winner']['resolution']}" for e in eps.values())
        print(f"  S{t:02d}: {len(eps)} ep(s)  " + "; ".join(f"{n}× {k}" for k, n in by.items()))
    if res["missing"]:
        print("  missing:", ", ".join(f"S{t:02d}E{e:02d}" for t, e in res["missing"][:60]) + (" …" if len(res["missing"]) > 60 else ""))
    print("Discards:", _reasons(r["discards"], 8))
    for w in res["warnings"]:
        print("WARNING:", w)
    problems = series_filter.check(r)
    print("Guarantees:", "ok (no duplicates)" if not problems else problems)
    if args.register:
        print(f"Registered in the database: {series_ondemand.register(app.cat, r, series)} episode(s).")


def write(args):
    print(f"{_app().cat.write_pending(batch=args.batch, require_idle=not args.force)} title(s) written")


def write_series(args):
    print(f"{_app().cat.write_pending_series(batch=args.batch, require_idle=not args.force)} series written")


def add_manual(args):
    """One title with any release (a trial, or a movie outside the providers)."""
    app = _app()
    t = {"key": f"imdb:{args.imdb}", "imdb": args.imdb, "name": args.name, "year": args.year, "origin": "manual"}
    v = {"label": args.label, "torrent_url": args.url, "private": args.private, "file": args.file,
         "provider": "manual", "name": args.release or f"{args.name} {args.year}"}
    tid = app.cat.register(t, [v], local_ids=set())
    strm = app.cat.write_title(tid)               # this title, now (not the first of the queue)
    r = app.db.one("SELECT id FROM releases WHERE title_id=? AND label=?", tid, args.label)
    print(f"release {r['id']} -> {strm}")


def rewrite_strm(args):
    """Every .strm again, with the current [gateway] public_url (the release id in it does not change)."""
    app = _app()
    n = 0
    for r in app.db.all("SELECT id, strm FROM releases WHERE strm IS NOT NULL"):
        path = app.cat.to_local(r["strm"])
        if os.path.exists(path):
            write_atomic(path, f"{app.cat.gateway_url}/v/{r['id']}\n")
            n += 1
    print(f"{n} .strm file(s) rewritten to {app.cat.gateway_url}")


def main():
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    p = argparse.ArgumentParser(prog="ondemand")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(f=status)
    sub.add_parser("providers", help="the configured providers and what each can do").set_defaults(f=providers_cmd)
    pv = sub.add_parser("preview", help="only reads: what the feeds would put in the catalog")
    pv.add_argument("--provider", help="one provider only")
    pv.add_argument("--title", help="only items whose title contains this text")
    pv.add_argument("--detail", action="store_true", help="for each episode, who lost and why")
    pv.set_defaults(f=preview)
    sub.add_parser("import", help="the scheduler's import step, now").set_defaults(f=import_cmd)
    sm = sub.add_parser("search-movie", help="what the providers have of one movie (only reads)")
    sm.add_argument("movie", help="a name or an IMDb id (tt…)")
    sm.add_argument("--register", action="store_true")
    sm.set_defaults(f=search_movie)
    ss = sub.add_parser("search-series", help="everything that exists of one series (only reads)")
    ss.add_argument("series", help="a name, an IMDb id (tt…) or a TheTVDB id")
    ss.add_argument("--register", action="store_true")
    ss.set_defaults(f=search_series)
    w = sub.add_parser("write")
    w.add_argument("--batch", type=int, default=50)
    w.add_argument("--force", action="store_true", help="write even with someone watching")
    w.set_defaults(f=write)
    ws = sub.add_parser("write-series")
    ws.add_argument("--batch", type=int, default=5)
    ws.add_argument("--force", action="store_true", help="write even with someone watching")
    ws.set_defaults(f=write_series)
    m = sub.add_parser("add-manual")
    m.add_argument("--name", required=True)
    m.add_argument("--year", type=int, required=True)
    m.add_argument("--imdb", required=True)
    m.add_argument("--url", required=True, help="the .torrent URL or a magnet (never printed)")
    m.add_argument("--label", default="1080p", choices=["1080p", "720p", "SD", "4K"])
    m.add_argument("--file", help="the file to use, if the torrent has several videos")
    m.add_argument("--release", help="the release name (to match subtitles)")
    m.add_argument("--private", action="store_true")
    m.set_defaults(f=add_manual)
    sub.add_parser("rewrite-strm", help="after changing [gateway] public_url").set_defaults(f=rewrite_strm)
    args = p.parse_args()
    args.f(args)


if __name__ == "__main__":
    main()
