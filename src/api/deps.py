"""What every route needs, built once per process from the same initialization the dashboard performs (app.py):

    paths       storage.paths.resolve_paths (TRIPY_DATA_DIR / MILO_RUNS_DIR / MILO_CACHE_DIR)
    catalog     the benchmark vehicles (research_targets.VehicleCatalog)
    manager     jobs.manager.get_manager(...): the process-wide RunManager; creating it reconciles orphaned runs
                (startup reconciliation) and it shares the process-wide ConcurrencyController (shared_controller)

Configuration is read from the process environment only (the dashboard additionally reads .streamlit/secrets.toml,
which needs Streamlit).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

from fastapi import Request

from ..jobs.manager import RunManager, get_manager, shared_controller
from ..research_targets import VehicleCatalog
from ..runstate.pipeline import PipelineCache
from ..storage.paths import DataPaths, resolve_paths
from .errors import ApiError


def env_secret(name: str) -> str:
    """The dashboard's `secret` lookup without Streamlit secrets: the environment, "" when unset."""
    return os.environ.get(name) or ""


@dataclass
class ApiContext:
    paths: DataPaths
    manager: RunManager
    catalog: VehicleCatalog
    secret: Callable[[str], str] = env_secret
    pipelines: PipelineCache = field(default_factory=PipelineCache)   # incremental event readers (like app.py)
    owns_manager: bool = False

    @property
    def runs_dir(self):
        return self.paths.runs_dir


def build_context(secret: Callable[[str], str] = env_secret) -> ApiContext:
    paths = resolve_paths(secret)
    catalog = VehicleCatalog.load()
    manager = get_manager(paths, controller=shared_controller(secret), vehicle_label=catalog.title)
    return ApiContext(paths=paths, manager=manager, catalog=catalog, secret=secret, owns_manager=True)


def get_context(request: Request) -> ApiContext:
    context = getattr(request.app.state, "tripy", None)
    if context is None:
        raise ApiError(503, "not_ready", "The server is still starting.")
    return context
