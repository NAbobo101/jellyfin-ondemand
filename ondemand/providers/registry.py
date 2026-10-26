"""Builds the providers from config.toml and runs them.

Every call into a provider is isolated: one source that is down or broken becomes a
warning, never an error for the others.
"""

import importlib
import logging
import os
import sys

from .. import archive
from .base import Provider, SeedRule

log = logging.getLogger("providers")

BUILTIN = {
    "torznab": "ondemand.providers.torznab:Torznab",
    "unit3d": "ondemand.providers.unit3d:Unit3D",
    "qbit_rss": "ondemand.providers.qbit_rss:QBitRSS",
    "archive_org": "ondemand.providers.archive_org:ArchiveOrg",
}
# Releases added by hand (cli add-manual) use this name; public unless the release says private.
MANUAL = SeedRule(private=False)


def load_class(path):
    module, _, cls = path.partition(":")
    if not cls:
        raise SystemExit(f"provider class {path!r} must look like 'module:Class'")
    obj = getattr(importlib.import_module(module), cls)
    if not (isinstance(obj, type) and issubclass(obj, Provider)):
        raise SystemExit(f"{path} is not a subclass of ondemand.providers.Provider")
    return obj


class Registry:
    def __init__(self, providers, db=None):
        self.providers = list(providers)
        self.by_name = {p.name: p for p in self.providers}
        if len(self.by_name) != len(self.providers):
            raise SystemExit("two [[providers]] entries have the same name")
        self.db = db

    def __iter__(self):
        return iter(self.providers)

    def __len__(self):
        return len(self.providers)

    def seed_rule(self, name):
        """The lifecycle's rule for a provider name (None = unknown: the lifecycle is conservative)."""
        p = self.by_name.get(name)
        if p is not None:
            return p.seed_rule.as_lifecycle()
        if name == "manual":
            return MANUAL.as_lifecycle()
        return None

    def with_capability(self, cap):
        return [p for p in self.providers if cap in p.capabilities]

    def _run(self, p, what, fn, *args):
        try:
            return list(fn(*args) or []), None
        except NotImplementedError:
            return [], None
        except Exception as e:                       # a provider never takes the others down
            log.warning("provider %s: %s failed: %s: %s", p.name, what, type(e).__name__, e)
            return [], f"{p.name}: {type(e).__name__}"

    # ------------------------------------------------------------ the three entry points
    def feed(self, kind=None):
        """(items, warnings) from every provider with a feed. Feed-only providers (no search for that kind)
        have their items archived, so searches can find them later."""
        items, warnings = [], []
        for p in self.with_capability("feed"):
            got, w = self._run(p, "feed", p.feed)
            if w:
                warnings.append(w)
            if self.db is not None and not ({"movie", "series"} <= p.capabilities):
                n = archive.record(self.db, got)
                if n:
                    log.info("provider %s: %d new item(s) archived", p.name, n)
            items += [i for i in got if kind is None or i.kind in (None, kind)]
        return items, warnings

    def _from_archive(self, p, cap, name, kind):
        if self.db is None or cap in p.capabilities or "feed" not in p.capabilities:
            return []
        return archive.find(self.db, p.name, name, kind)

    def search_movie(self, query):
        items, warnings = [], []
        for p in self.providers:
            if "movie" in p.capabilities:
                got, w = self._run(p, "search_movie", p.search_movie, query)
                items += got
                warnings += [w] if w else []
            for name in {n for n in (query.original_title, query.title) if n}:
                items += self._from_archive(p, "movie", name, "movie")
        return items, warnings

    def search_series(self, query, progress=None):
        items, warnings = [], []
        searchers = [p for p in self.providers if "series" in p.capabilities]
        for i, p in enumerate(searchers, 1):
            if progress:
                progress(i, len(searchers), p.name)
            got, w = self._run(p, "search_series", p.search_series, query)
            items += got
            warnings += [w] if w else []
        for p in self.providers:
            items += self._from_archive(p, "series", query.name, "series")
        return items, warnings


def build(cfg, db=None, context=None):
    """Registry from cfg["providers"] (a list of tables). Plugins: `type = "python"` with
    `class = "module:Class"`, importable from cfg["general"]["plugins_dir"] (default /plugins)."""
    plugins = (cfg.get("general") or {}).get("plugins_dir", "/plugins")
    if plugins and os.path.isdir(plugins) and plugins not in sys.path:
        sys.path.insert(0, plugins)
    secrets = (cfg.get("secrets") or {}).get("providers") or {}
    providers = []
    for entry in cfg.get("providers") or []:
        if not entry.get("enabled", True):
            continue
        name, kind = entry.get("name"), entry.get("type")
        if not name or not kind:
            raise SystemExit("every [[providers]] entry needs a name and a type")
        path = entry.get("class") if kind == "python" else BUILTIN.get(kind)
        if not path:
            raise SystemExit(f"provider {name!r}: unknown type {kind!r} (built-in: {', '.join(BUILTIN)}, or python)")
        cls = load_class(path)
        p = cls(name, entry, secrets.get(name) or {}, context or {})
        providers.append(p)
        log.info("provider %s (%s): %s", name, kind, ", ".join(sorted(p.capabilities)) or "nothing to do")
    return Registry(providers, db)
