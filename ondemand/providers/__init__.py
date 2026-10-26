"""Media providers: where torrents come from. See docs/writing-a-provider.md."""

from .base import MovieQuery, Provider, ProviderError, SeedRule, SeriesQuery, TorrentItem, magnet  # noqa: F401
