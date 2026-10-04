"""Configuration status and the research-target catalog. Presence and names only: never a secret value."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...access_control import access_required, token_configured
from ...agent import PROMPT_VERSION
from ...app_config import blocking_errors, is_production, max_active_runs
from ...mcp_server import configured_token
from ...run_profiles import PRODUCTION, PROFILE_LABELS, PROFILES
from ...run_settings import settings_checks, settings_from_env
from ...storage.paths import storage_status
from ..deps import ApiContext, get_context
from ..schemas import ConfigStatus, VehicleList
from ..service import redacted

router = APIRouter(prefix="/api", tags=["config"])

STORAGE_KEYS = ("level", "message", "writable", "on_railway", "volume_mount", "on_volume", "data_dir", "source")


@router.get("/config/status", response_model=ConfigStatus)
def config_status(ctx: ApiContext = Depends(get_context)) -> dict:
    """What a run started now would use (the dashboard's System panel): checks, models, search backend, storage,
    access control. Validation is app_config.validate_config over run_settings.settings_from_env."""
    settings = settings_from_env(ctx.secret, ctx.manager.controller)
    checks = settings_checks(settings, ctx.secret, ctx.paths)
    storage = storage_status(ctx.paths, ctx.secret)
    return redacted({
        "environment": "production" if is_production(ctx.secret) else "development",
        "configured": not blocking_errors(checks), "blocking": [c.name for c in blocking_errors(checks)],
        "checks": [c.as_dict() for c in checks],
        "research_model": settings.model_id or None, "finalizer_model": settings.finalizer_model_id.strip() or None,
        "search_backend": settings.search_backend, "level15_source": "database" if settings.dsn else "snapshot",
        "storage": {k: storage.get(k) for k in STORAGE_KEYS},
        "access_control": {"required": access_required(ctx.secret), "configured": token_configured(ctx.secret)},
        "mcp_enabled": bool(configured_token(ctx.secret)), "max_active_runs": max_active_runs(ctx.secret),
        "prompt_version": PROMPT_VERSION,
        "profiles": [{"id": p, "label": PROFILE_LABELS[p]} for p in PROFILES], "default_profile": PRODUCTION,
        "env_overrides": settings.env_overrides})


@router.get("/vehicles", response_model=VehicleList)
def vehicles(ctx: ApiContext = Depends(get_context)) -> dict:
    """The benchmark vehicles a run can target (scope=one takes a record_id, scope=manufacturer a manufacturer)."""
    catalog = ctx.catalog
    return {"vehicles": [{"record_id": v["upstream_record_id"], "label": catalog.labels[v["upstream_record_id"]],
                          "title": catalog.titles[v["upstream_record_id"]], "manufacturer": v["manufacturer"],
                          "model": v["model"], "year": v.get("year"), "trim": v.get("trim"),
                          "ordinal": v.get("ordinal")} for v in catalog.vehicles],
            "manufacturers": catalog.manufacturers()}
