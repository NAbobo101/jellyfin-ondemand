"""jellyfin-ondemand's SQLite database: the catalog's inventory.

- titles: a movie (key imdb:tt…) or a series (key tvdb:N) — catalog folder, state.
- releases: each version (1080p, 720p, 4K) of a movie, or each episode (label S01E02) of a
  series — the short opaque id is what goes in the .strm URL; torrent_url may carry a
  passkey and NEVER goes to a log.
- torrents: what was put to download and which lifecycle phase it is in.
- subtitles: the state of each release's subtitle in the configured language.
- saved_limits: the download limit outside torrents had before we throttled them during a
  stream (restored even after a kill).
- requests: movies and series requested from Jellyfin's search (search.py) — one per IMDb id,
  with who asked (daily limit) and the item that was born.
- archive: every item a provider showed (archive.py) — feeds of every provider and series search results —,
  with its address and, for series, what the filter decided about it (winner, fallback, quarantine...).
- series_searches: the last full search of each series (at most one per `complete_days`).
- kv: small key/value state.
"""

import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL DEFAULT 'movie',
  imdb TEXT, tmdb TEXT, tvdb TEXT,
  name TEXT NOT NULL, year INTEGER,
  runtime_min INTEGER, rating REAL, language TEXT, genres TEXT,
  origin TEXT NOT NULL,
  folder TEXT,
  created_at REAL NOT NULL, written_at REAL, last_play REAL,
  locked INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS titles_state ON titles(state);
CREATE INDEX IF NOT EXISTS titles_state_locked ON titles(state, locked);
CREATE INDEX IF NOT EXISTS titles_imdb ON titles(imdb);
CREATE TABLE IF NOT EXISTS releases (
  id TEXT PRIMARY KEY,
  title_id INTEGER NOT NULL REFERENCES titles(id),
  label TEXT NOT NULL,
  name TEXT,
  quality TEXT, source_type TEXT, codec TEXT,
  size INTEGER, seeds INTEGER,
  infohash TEXT,
  torrent_url TEXT,
  private INTEGER NOT NULL DEFAULT 0,
  provider TEXT,
  file TEXT,
  strm TEXT,
  created_at REAL NOT NULL,
  season INTEGER, episode INTEGER, torrent_tag TEXT,
  UNIQUE(title_id, label)
);
CREATE TABLE IF NOT EXISTS torrents (
  infohash TEXT PRIMARY KEY,
  release_id TEXT NOT NULL,
  state TEXT NOT NULL,
  foreign_ INTEGER NOT NULL DEFAULT 0,
  path TEXT, file TEXT,
  started_at REAL, committed_at REAL, completed_at REAL, removed_at REAL,
  last_access REAL,
  rest_released INTEGER NOT NULL DEFAULT 0,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS subtitles (
  release_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  source TEXT, detail TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS saved_limits (
  infohash TEXT PRIMARY KEY,
  limit_bps INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS archive (
  provider TEXT NOT NULL, id TEXT NOT NULL,
  title TEXT NOT NULL, norm_name TEXT, kind TEXT,
  seeds INTEGER, published REAL, item TEXT NOT NULL,
  seen_at REAL NOT NULL,
  PRIMARY KEY (provider, id)
);
CREATE INDEX IF NOT EXISTS archive_name ON archive(norm_name);
CREATE TABLE IF NOT EXISTS series_searches (
  tvdb TEXT PRIMARY KEY,
  imdb TEXT, last_at REAL,
  state TEXT NOT NULL,
  found INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS requests (
  imdb TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'movie',
  username TEXT, user_id TEXT,
  requested_at REAL NOT NULL,
  state TEXT NOT NULL,
  item_id TEXT, detail TEXT,
  updated_at REAL
);
"""

# `foreign` is an SQL keyword: the column is `foreign_`.
_UPDATABLE = ("seeds", "torrent_url", "infohash", "size", "name", "quality", "source_type", "codec",
              "private", "provider", "file", "torrent_tag")


def new_id():
    """8 characters [A-Za-z0-9_-]: short for the URL, impossible to guess."""
    return secrets.token_urlsafe(6)


class Database:
    # Columns added after a schema was first created: (table, column, type). Empty for now; add
    # here when the schema grows, so existing databases are migrated in place.
    MIGRATIONS = (("archive", "tag", "TEXT"), ("archive", "series_key", "TEXT"), ("archive", "season", "INTEGER"),
                  ("archive", "state", "TEXT NOT NULL DEFAULT 'new'"), ("archive", "reason", "TEXT"),
                  ("archive", "attempts", "INTEGER NOT NULL DEFAULT 0"), ("archive", "retry_at", "REAL"),
                  ("archive", "failures", "INTEGER NOT NULL DEFAULT 0"), ("archive", "failed_at", "REAL"),
                  ("archive", "evaluated_at", "REAL"))
    # Indexes on migrated columns: created after the migration (on an old database the column is new).
    INDEXES = ("CREATE INDEX IF NOT EXISTS archive_series ON archive(series_key)",
               "CREATE INDEX IF NOT EXISTS archive_tag ON archive(tag)",
               "CREATE INDEX IF NOT EXISTS archive_state ON archive(state, retry_at)")

    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        c = self.con()
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        self._migrate(c)
        for sql in self.INDEXES:
            c.execute(sql)
        c.commit()
        from . import archive               # here: the archive uses the release-name parser, not the database
        archive.migrate(self)

    def _migrate(self, c):
        for table, column, kind in self.MIGRATIONS:
            existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def con(self):
        """One connection per thread (the gateway serves each request in a thread)."""
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=NORMAL")   # safe with WAL; no fsync on every commit to a USB disk
            self._local.c = c
        return c

    def one(self, sql, *args):
        return self.con().execute(sql, args).fetchone()

    def all(self, sql, *args):
        return self.con().execute(sql, args).fetchall()

    def execute(self, sql, *args):
        c = self.con()
        with c:
            return c.execute(sql, args)

    # ------------------------------------------------------------ key/value state
    def kv_get(self, key, default=None):
        r = self.one("SELECT value FROM kv WHERE key=?", key)
        return r["value"] if r else default

    def kv_set(self, key, value):
        self.execute("INSERT INTO kv(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, str(value))

    # ------------------------------------------------------------ catalog
    def save_title(self, t):
        """Insert or update (rating/runtime change) a title; returns the id."""
        now = time.time()
        c = self.con()
        with c:
            c.execute("""INSERT INTO titles(key,kind,imdb,tmdb,tvdb,name,year,runtime_min,rating,language,genres,origin,created_at,state)
                         VALUES(:key,:kind,:imdb,:tmdb,:tvdb,:name,:year,:runtime_min,:rating,:language,:genres,:origin,:now,:state)
                         ON CONFLICT(key) DO UPDATE SET rating=COALESCE(excluded.rating, rating),
                                                        runtime_min=COALESCE(excluded.runtime_min, runtime_min),
                                                        tmdb=COALESCE(tmdb, excluded.tmdb)""",
                      {"kind": "movie", "imdb": None, "tmdb": None, "tvdb": None, "language": None, "genres": None,
                       "runtime_min": None, "rating": None, "year": None,
                       "state": "new", **t, "now": now})
            return c.execute("SELECT id FROM titles WHERE key=?", (t["key"],)).fetchone()["id"]

    def _untouched(self, rid):
        """A release that never played: no torrent and the subtitle still a placeholder."""
        if self.one("SELECT 1 FROM torrents WHERE release_id=?", rid):
            return False
        sub = self.one("SELECT state FROM subtitles WHERE release_id=?", rid)
        return sub is None or sub["state"] == "placeholder"

    def _replace(self, rid, r, fields=_UPDATABLE):
        values = [int(bool(r.get(f))) if f == "private" else r.get(f) for f in fields]
        self.execute(f"UPDATE releases SET {', '.join(f + '=?' for f in fields)} WHERE id=?", *values, rid)

    def save_release(self, title_id, r):
        """One release per (title, label); if it already exists, update seeds/url keeping the id.
        A release that already has a .strm keeps the torrent it had (only the seeds change):
        switching the infohash would leave the real subtitle and a running torrent pointing at a
        different encode. Different providers compete: the one with MORE seeds stays (tie = the
        one already there); the switch carries provider and private along, and with a .strm it
        only happens if the release never played (the id, and so the .strm, does not change)."""
        cur = self.one("SELECT id, strm, provider, seeds, torrent_tag FROM releases WHERE title_id=? AND label=?",
                       title_id, r["label"])
        if cur and cur["provider"] != r.get("provider") and not r.get("torrent_tag"):   # series: the filter already chose
            if (r.get("seeds") or 0) <= (cur["seeds"] or 0) or (cur["strm"] and not self._untouched(cur["id"])):
                return cur["id"]
            self._replace(cur["id"], r)
            return cur["id"]
        if cur and cur["strm"]:
            # Series: the episode got another torrent (PROPER, better pack) and the release never played ->
            # switch (the id and the .strm do not change). Movies carry no torrent_tag, so never land here.
            if r.get("torrent_tag") and r["torrent_tag"] != cur["torrent_tag"] and self._untouched(cur["id"]):
                self._replace(cur["id"], r, tuple(f for f in _UPDATABLE if f != "file"))
                return cur["id"]
            self.execute("UPDATE releases SET seeds=? WHERE id=?", r.get("seeds"), cur["id"])
            return cur["id"]
        if cur:
            if r.get("torrent_tag"):          # series: no .strm yet, may switch torrents freely
                self._replace(cur["id"], r, tuple(f for f in _UPDATABLE if f != "file"))
            else:
                self._replace(cur["id"], r, ("seeds", "torrent_url", "infohash", "size", "name"))
            return cur["id"]
        rid = new_id()
        self.execute("""INSERT INTO releases(id,title_id,label,name,quality,source_type,codec,size,seeds,infohash,
                                             torrent_url,private,provider,file,created_at,season,episode,torrent_tag)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     rid, title_id, r["label"], r.get("name"), r.get("quality"), r.get("source_type"), r.get("codec"),
                     r.get("size"), r.get("seeds"), r.get("infohash"), r.get("torrent_url"),
                     int(bool(r.get("private"))), r.get("provider"), r.get("file"), time.time(),
                     r.get("season"), r.get("episode"), r.get("torrent_tag"))
        return rid

    def switch_torrent(self, rid, r):
        """Fallback (fallback.py): the episode's torrent is dead and the next candidate takes over, EVEN if the
        release already played (save_release refuses that). The id and the .strm do not change; the subtitle
        goes back to the placeholder (the real one was matched to the old encode) — the caller rewrites the file."""
        self._replace(rid, {**r, "file": None})
        self.execute("UPDATE subtitles SET state='placeholder', source=NULL, detail='torrent replaced by a fallback', "
                     "attempts=0, updated_at=? WHERE release_id=?", time.time(), rid)
