# Security

Please report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab) rather
than in a public issue.

What the project tries to guarantee — reports about any of these are especially welcome:

- **Secrets stay put.** Tracker passkeys and API keys live in `secrets.toml` (refused unless chmod 600)
  or in the database; they are never logged, never printed by the CLI, and never put in a URL the
  service fetches. `.torrent` files are only fetched by qBittorrent.
- **Only Jellyfin reads the video.** `/v/<id>` answers only `[gateway] video_clients`; ids are random and
  unguessable.
- **The search API only serves logged-in Jellyfin users**: every call carries the user's own session
  token, checked against Jellyfin.
- **Nothing outside known paths is served** (ABR files match a strict pattern; no `..`).
- **Public torrents are checked** before playback: no executables/archives, a plausible video size.

Hardening tips: keep ports 8097/8098 inside your Docker network (publish only the reverse proxy), use
HTTPS on whatever faces the internet, and run qBittorrent behind the network path you want torrents to
use.
