"""What every route needs, built ONCE per process at startup (the application lifespan, src/api/app.py):

    paths       storage.paths.resolve_paths (TRIPY_DATA_DIR / MILO_RUNS_DIR / MILO_CACHE_DIR)
    catalog     the benchmark vehicles (research_targets.VehicleCatalog)
    MILO        one read-only query function over DATABASE_URL (catalog.database_query: the process-wide connection
                pool), shared by the catalog browser and the facts service; at boot the facts picker is warmed in the
                background (CatalogBrowser.start_picker_warmup) and the service / database regions are logged
    manager     jobs.manager.get_manager(...): the process-wide RunManager; creating it reconciles orphaned runs
                (startup reconciliation) and it shares the process-wide ConcurrencyController (shared_controller)

Configuration is read from the process environment only.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable

from fastapi import Request

from ..catalog import CatalogBrowser, database_query
from ..facts.service import FactsService
from ..jobs.manager import RunManager, get_manager, shared_controller
from ..research_targets import VehicleCatalog
from ..runstate.pipeline import PipelineCache
from ..storage.paths import DataPaths, resolve_paths
from .errors import ApiError


def env_secret(name: str) -> str:
    """The configuration lookup: the process environment, "" when unset."""
    return os.environ.get(name) or ""


@dataclass
class ApiContext:
    paths: DataPaths
    manager: RunManager
    catalog: VehicleCatalog
    secret: Callable[[str], str] = env_secret
    pipelines: PipelineCache = field(default_factory=PipelineCache)   # incremental event readers, one per process
    owns_manager: bool = False
    # PR #47 (B1): the live MILO catalog (read-only over DATABASE_URL); unavailable without it (snapshot mode)
    catalog_browser: CatalogBrowser = field(default_factory=lambda: CatalogBrowser(None))
    # the vehicle facts API (src/facts): the same read-only query; unavailable without it (503, never the snapshot)
    facts: FactsService = field(default_factory=lambda: FactsService(None))

    @property
    def runs_dir(self):
        return self.paths.runs_dir


def build_context(secret: Callable[[str], str] = env_secret) -> ApiContext:
    paths = resolve_paths(secret)
    catalog = VehicleCatalog.load()
    manager = get_manager(paths, controller=shared_controller(secret), vehicle_label=catalog.title)
    dsn = (secret("DATABASE_URL") or secret("SUPABASE_DB_URL") or "").strip()
    query = database_query(dsn) if dsn else None
    browser = CatalogBrowser(query)
    manager.derived_index.start()          # PR #47 (B2): the weekly derived catalog trim index (no-op without a DSN)
    facts = FactsService(query, paths.data_dir / "derived" / "facts_cache")
    from ..open_data.shard_index import start_warmup

    start_warmup()                          # E4: index the 2015-2025 EEA shards in the background (requests never wait)
    from ..facts.service import log as facts_log
    from .routes.facts import SEGMENT

    if dsn:
        from ..catalog import placement

        facts_log.info("facts placement %s", json.dumps(placement(dsn, secret), sort_keys=True))
    # the facts picker: the manufacturer list and the 15 largest manufacturers' model lists (6 h cache), in the
    # background after the shard warm-up has started; requests never wait for it, a failure only logs
    browser.start_picker_warmup(SEGMENT, facts_log)
    return ApiContext(paths=paths, manager=manager, catalog=catalog, secret=secret, owns_manager=True,
                      catalog_browser=browser, facts=facts)


def get_context(request: Request) -> ApiContext:
    context = getattr(request.app.state, "tripy", None)
    if context is None:
        raise ApiError(503, "not_ready", "The server is still starting.")
    return context
