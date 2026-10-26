"""Jellyfin 10.11 API client (only what jellyfin-ondemand uses).

House rules: configuration and items only through the API, sending the WHOLE
object (GET -> change -> POST); nothing heavy while someone is watching.
"""

import hashlib
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

log = logging.getLogger("jellyfin")

TYPE_MOVIE = "MediaBrowser.Controller.Entities.Movies.Movie"
TYPE_EPISODE = "MediaBrowser.Controller.Entities.TV.Episode"
TYPE_SERIES = "MediaBrowser.Controller.Entities.TV.Series"
TYPE_FOLDER = "MediaBrowser.Controller.Entities.Folder"
IDS_PER_REQUEST = 50            # ids per GET /Items?Ids= (keeps the URL short enough)
_EPISODE_STRM = re.compile(r"/Season \d+/[^/]+ S\d+E\d+\.strm$")   # the layout catalog.py writes


def item_id(kind, path):
    """The id Jellyfin gives a disk item: Guid(MD5(UTF-16LE(type + path))). Checked against
    Jellyfin 10.11 on movie folders, movies and episodes."""
    return uuid.UUID(bytes_le=hashlib.md5((kind + path).encode("utf-16-le")).digest()).hex


def strm_item_id(path):
    """Id of the movie (or of the episode, for the catalog's `Season NN/Name SNNENN.strm`) of a
    .strm, with the path as Jellyfin sees it: no need to list the library to find the item
    (a listing with MediaSources can cost megabytes and seconds on a large library)."""
    return item_id(TYPE_EPISODE if _EPISODE_STRM.search(path) else TYPE_MOVIE, path)


def _route(path, params):
    """Metric label: no ids in the path; /Items split into full listing and by ids."""
    parts = ["{id}" if len(p) >= 32 and all(c in "0123456789abcdef-" for c in p.lower()) else p
             for p in path.split("/")]
    route = "/".join(parts)
    if route == "/Items" and params:
        route += "?Ids" if "Ids" in params else ("?Recursive" if params.get("Recursive") else "")
    return route


class JellyfinError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code          # HTTP status, when Jellyfin answered


class Jellyfin:
    def __init__(self, base, api_key, timeout=30, metrics=None, message_header="On demand"):
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"MediaBrowser Token={api_key}", "Content-Type": "application/json"}
        self.timeout = timeout
        self.metrics = metrics
        self.message_header = message_header
        self._admin = None

    def _req(self, method, path, body=None, params=None, token=None):
        """token: call as the owner of that token (a user's session) instead of the API key."""
        if self.metrics is not None:
            self.metrics.inc("jellyfin_requests_total", route=_route(path, params))
        url = self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        headers = self.headers if token is None else {"X-Emby-Token": token, "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                t = r.read()
        except urllib.error.HTTPError as e:
            raise JellyfinError(f"{method} {path}: HTTP {e.code}", e.code) from None
        except (urllib.error.URLError, OSError) as e:
            raise JellyfinError(f"{method} {path}: {e}") from None
        return json.loads(t) if t else None

    # ------------------------------------------------------------ sessions
    def sessions(self):
        return self._req("GET", "/Sessions", params={"ActiveWithinSeconds": 300}) or []

    def playing(self):
        """Sessions with something playing (playback always comes first)."""
        return [s for s in self.sessions() if s.get("NowPlayingItem")]

    def idle(self):
        return not self.playing()

    def sessions_of_item(self, item_id):
        return [s for s in self.playing() if s["NowPlayingItem"].get("Id") == item_id]

    def toggle_subtitle(self, session_id, index):
        """Turn the track off and on again: the player fetches the file again without restarting the video."""
        for i in (-1, index):
            self._req("POST", f"/Sessions/{session_id}/Command",
                      {"Name": "SetSubtitleStreamIndex", "Arguments": {"Index": str(i)}})

    def reload(self, session_id, item_id, position_ticks, media_source_id=None, audio=None, subtitle=None):
        """PlayNow at the same position (for clients without SetSubtitleStreamIndex)."""
        p = {"playCommand": "PlayNow", "itemIds": item_id, "startPositionTicks": int(position_ticks)}
        if media_source_id:
            p["mediaSourceId"] = media_source_id
        if audio is not None:
            p["audioStreamIndex"] = audio
        if subtitle is not None:
            p["subtitleStreamIndex"] = subtitle
        self._req("POST", f"/Sessions/{session_id}/Playing", params=p)

    def message(self, session_id, text, ms=6000):
        self._req("POST", f"/Sessions/{session_id}/Message",
                  {"Header": self.message_header, "Text": text, "TimeoutMs": ms})

    # ------------------------------------------------------------ items
    def admin_id(self):
        if not self._admin:
            users = self._req("GET", "/Users")
            self._admin = next(u["Id"] for u in users if u["Policy"].get("IsAdministrator"))
        return self._admin

    def _items(self, **params):
        """GET /Items ALWAYS with userId: in 10.11 a recursive query with only the API key leaves out
        what was created after the server started (measured: 172 of 363 movies missing). Lean by
        default: without UserData and images a movie listing drops from 1.8 MB / 1.9 s to 0.67 MB / 0.3 s."""
        params.setdefault("userId", self.admin_id())
        params.setdefault("EnableUserData", "false")
        params.setdefault("EnableImages", "false")
        return (self._req("GET", "/Items", params=params) or {}).get("Items", [])

    def items(self, ids, fields="Path,ProviderIds"):
        """Items by id (in blocks), with the poster tag in ImageTags. Also finds an item just
        created and still without metadata (which a listing by date does not show)."""
        ids, found = list(ids), []
        for i in range(0, len(ids), IDS_PER_REQUEST):
            found += self._items(Ids=",".join(ids[i:i + IDS_PER_REQUEST]), Fields=fields, EnableImages="true",
                                 EnableImageTypes="Primary", ImageTypeLimit=1)
        return found

    def positions(self, ids):
        """{item_id: largest saved position in ticks} across ALL users — "continue watching".
        An item marked as played does not count. Queried by `Ids` (one request per user and
        block); only the lifecycle calls it, when a torrent is about to go."""
        ids, pos = list(ids), {}
        for u in self._req("GET", "/Users") or []:
            for i in range(0, len(ids), IDS_PER_REQUEST):
                for it in self._items(Ids=",".join(ids[i:i + IDS_PER_REQUEST]), userId=u["Id"], EnableUserData="true"):
                    ud = it.get("UserData") or {}
                    t = ud.get("PlaybackPositionTicks") or 0
                    if t > 0 and not ud.get("Played"):
                        pos[it["Id"]] = max(t, pos.get(it["Id"], 0))
        return pos

    def movies(self, fields="ProviderIds,Path"):
        """The WHOLE movie library (grows with the catalog): only for what cannot be known
        by id beforehand (IMDb of the local movies)."""
        return self._items(Recursive="true", IncludeItemTypes="Movie", Fields=fields)

    def local_movies(self, catalog_prefix):
        """{imdb: item} of the movies that are NOT in the catalog."""
        local = {}
        for i in self.movies():
            imdb = (i.get("ProviderIds") or {}).get("Imdb")
            if imdb and not (i.get("Path") or "").startswith(catalog_prefix):
                local.setdefault(imdb, i)
        return local

    def local_episodes(self, catalog_prefix):
        """{"tvdb:N"|"imdb:tt"|"tmdb:N": {(season, episode)}} of what already exists in the series that
        are NOT in the catalog: the series filter only fills what is missing (read only)."""
        series = {i["Id"]: i for i in self._items(Recursive="true", IncludeItemTypes="Series", Fields="ProviderIds,Path")
                  if not (i.get("Path") or "").startswith(catalog_prefix)}
        local = {}
        for e in self._items(Recursive="true", IncludeItemTypes="Episode", Fields="Path"):
            s = series.get(e.get("SeriesId"))
            if s is None or e.get("ParentIndexNumber") is None or e.get("IndexNumber") is None:
                continue
            if (e.get("Path") or "").startswith(catalog_prefix):
                continue
            end = e.get("IndexNumberEnd") or e["IndexNumber"]
            for prov, v in (s.get("ProviderIds") or {}).items():
                if prov in ("Tvdb", "Imdb", "Tmdb") and v:
                    local.setdefault(f"{prov.lower()}:{v}", set()).update(
                        (e["ParentIndexNumber"], n) for n in range(e["IndexNumber"], end + 1))
        return local

    def local_ids(self, catalog_prefix):
        """IMDb/TMDb of every movie that is NOT in the catalog (to avoid duplicates)."""
        ids = set()
        for i in self.movies():
            if (i.get("Path") or "").startswith(catalog_prefix):
                continue
            for prov, v in (i.get("ProviderIds") or {}).items():
                if prov in ("Imdb", "Tmdb") and v:
                    ids.add(f"{prov.lower()}:{v}")
        return ids

    def refresh_item(self, item_id, recursive=False):
        """A refresh through the API enters the metadata queue with HIGH priority; a folder notice
        enters the normal one, behind any catalog batch (measured: 3 min in the queue).
        `recursive`: a series with new episodes (creates seasons and episodes from the folders)."""
        params = {"MetadataRefreshMode": "Default", "ImageRefreshMode": "Default"}
        if recursive:
            params["Recursive"] = "true"
        self._req("POST", f"/Items/{item_id}/Refresh", params=params)

    # ------------------------------------------------------------ search outside the library
    def user_from_token(self, token):
        """Owner of a session token (browser), or None if Jellyfin does not recognise it."""
        try:
            return self._req("GET", "/Users/Me", token=token)
        except JellyfinError as e:
            if e.code in (400, 401, 403):
                return None
            raise

    def remote_search_movie(self, term, year=None, limit=8, language=None):
        """Movies on TMDb through RemoteSearch: comes in the library's metadata language (or `language`)
        and uses the plugin's key. The result only carries the TMDb id; the IMDb id comes from imdb_from_tmdb."""
        info = {"Name": term}
        if year:
            info["Year"] = int(year)
        if language:
            info["MetadataLanguage"] = language
        r = self._req("POST", "/Items/RemoteSearch/Movie",
                      {"SearchInfo": info, "SearchProviderName": "TheMovieDb"}) or []
        return r[:limit]

    def movie_by_tmdb(self, tmdb, language=None):
        """The movie on TMDb by TMDb id, optionally in another `language` ("en": the name release
        groups use), or None."""
        info = {"ProviderIds": {"Tmdb": str(tmdb)}}
        if language:
            info["MetadataLanguage"] = language
        r = self._req("POST", "/Items/RemoteSearch/Movie", {"SearchInfo": info, "SearchProviderName": "TheMovieDb"}) or []
        return r[0] if r else None

    def remote_search_series(self, term, limit=8):
        """Series on TMDb through RemoteSearch (name, overview and poster in the metadata language;
        ProviderIds only with Tmdb)."""
        r = self._req("POST", "/Items/RemoteSearch/Series",
                      {"SearchInfo": {"Name": term}, "SearchProviderName": "TheMovieDb"}) or []
        return r[:limit]

    def series_by_imdb(self, imdb):
        """The series on TMDb by IMDb id, or None."""
        r = self._req("POST", "/Items/RemoteSearch/Series",
                      {"SearchInfo": {"ProviderIds": {"Imdb": imdb}}, "SearchProviderName": "TheMovieDb"}) or []
        return r[0] if r else None

    def item(self, item_id, fields="Path,ProviderIds"):
        """One item by id (a query by Ids also finds one just created), or None."""
        found = self._items(Ids=item_id, Fields=fields)
        return found[0] if found else None

    def all_series_ids(self):
        """Ids (Tvdb/Imdb/Tmdb) of EVERY series in Jellyfin, catalog and local: what the native search already shows."""
        ids = set()
        for i in self._items(Recursive="true", IncludeItemTypes="Series", Fields="ProviderIds"):
            for prov, v in (i.get("ProviderIds") or {}).items():
                if prov in ("Tvdb", "Imdb", "Tmdb") and v:
                    ids.add(f"{prov.lower()}:{v}")
        return ids

    def series_ids_from_tmdb(self, tmdb):
        """{"Tmdb","Imdb","Tvdb"} of a series by TMDb id (a 2nd RemoteSearch: the 1st only brings Tmdb)."""
        r = self._req("POST", "/Items/RemoteSearch/Series",
                      {"SearchInfo": {"ProviderIds": {"Tmdb": str(tmdb)}}, "SearchProviderName": "TheMovieDb"}) or []
        return next((x.get("ProviderIds") or {} for x in r if (x.get("ProviderIds") or {}).get("Tvdb")
                     or (x.get("ProviderIds") or {}).get("Imdb")), {})

    def imdb_from_tmdb(self, tmdb):
        r = self._req("POST", "/Items/RemoteSearch/Movie",
                      {"SearchInfo": {"ProviderIds": {"Tmdb": str(tmdb)}}, "SearchProviderName": "TheMovieDb"}) or []
        return next((x["ProviderIds"]["Imdb"] for x in r if (x.get("ProviderIds") or {}).get("Imdb")), None)

    def movie_by_imdb(self, imdb):
        """The movie on TMDb by IMDb id (name, overview and poster), or None."""
        r = self._req("POST", "/Items/RemoteSearch/Movie",
                      {"SearchInfo": {"ProviderIds": {"Imdb": imdb}}, "SearchProviderName": "TheMovieDb"}) or []
        return r[0] if r else None

    def refresh_folder(self, path):
        """Priority refresh (not recursive) of a library's physical folder: creates the items of the
        new folders and fetches their metadata in ~4 s. A folder notice goes through the
        LibraryMonitor, which waits 60 s and enters the normal queue (measured: 77 s).
        False if the computed id is not that folder's (then the caller uses the notice)."""
        ident = item_id(TYPE_FOLDER, path)
        found = (self._req("GET", "/Items", params={"Ids": ident, "Fields": "Path"}) or {}).get("Items", [])
        if not found or found[0].get("Path") != path:
            log.warning("folder %s does not have id %s in Jellyfin: using the notice", path, ident)
            return False
        self._req("POST", f"/Items/{ident}/Refresh",
                  params={"Recursive": "false", "MetadataRefreshMode": "Default", "ImageRefreshMode": "Default"})
        return True

    def notify_paths(self, paths, kind="Created"):
        """POST /Library/Media/Updated: Jellyfin processes only these folders (no full scan)."""
        if paths:
            self._req("POST", "/Library/Media/Updated",
                      {"Updates": [{"Path": c, "UpdateType": kind} for c in paths]})

    def lock(self, item_id):
        """LockData=true: the FullRefresh of every play stops fetching metadata from TMDb."""
        item = self._req("GET", f"/Users/{self.admin_id()}/Items/{item_id}")
        if not item.get("LockData"):
            item["LockData"] = True
            self._req("POST", f"/Items/{item_id}", item)
