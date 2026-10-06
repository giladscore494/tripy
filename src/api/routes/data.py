"""The Data page: the open-data snapshots, the data volume's storage and the production source policy.

    GET  /api/data/datasets                 the committed open-data snapshots (data/open/manifest.json: build date,
                                            Action run URL, rows, years, absent columns, units, licence / attribution)
                                            and whether the engine can read each one (sha256 verified)
    GET  /api/data/storage                  the data volume: total / used / free and the size per top-level folder
    POST /api/data/storage/delete-open-data delete <data>/derived/open/ (what server-side builds left there) only;
                                            {confirm: true}; logged
    GET  /api/data/retention                retention: settings (keep the newest N, pinned runs), what "Compact old runs"
                                            would free, the document cache (size, documents no kept run uses), logs/
    PUT  /api/data/retention                {keep_newest}
    POST /api/data/retention/compact        compact the runs outside the newest N / pinned / executing {confirm: true}
    GET  /api/data/retention/older-than     ?days=N: the runs "Delete runs older than N days" would delete, and bytes
    POST /api/data/retention/delete-older   {days, confirm: true}
    POST /api/data/retention/clean-cache    delete the cached documents no kept run (or the research memory) uses
                                            {confirm: true}
    POST /api/data/open-data/shadow-report  R: the open-data shadow report over every run on the volume (the match
                                            recomputed now, its offers compared with each run's ok fields; read-only,
                                            writes only derived/open_data_shadow_report.json; refused below 200 MB free)
    GET  /api/data/open-data/shadow-report  the last report ({report: null} before the first)
    GET  /api/data/policy                   the effective source policy (repository file + operator overlay) and its
                                            change log
    POST /api/data/policy                   switch ONE domain between blocked and allowed: {domain, policy,
                                            terms_clause, checked_at}; allowing needs the terms clause and the date;
                                            stored as an overlay in the data volume, every change logged

The snapshots are built by the build-open-data GitHub Action and committed to the repository: nothing here builds or
writes open data on the server.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...source_authority import policy_table, update_policy
from ...storage.disk import delete_open_data, usage
from ..deps import ApiContext, get_context
from ..errors import ApiError

router = APIRouter(prefix="/api/data", tags=["data"])


class PolicyChange(BaseModel):
    domain: str = Field(..., min_length=3, max_length=253)
    policy: str = Field(..., pattern="^(allowed|blocked)$")
    terms_clause: str | None = Field(None, max_length=2000)
    checked_at: str | None = Field(None, max_length=40)


class DeleteOpenData(BaseModel):
    confirm: bool = False


class Confirm(BaseModel):
    confirm: bool = False


class RetentionSettings(BaseModel):
    keep_newest: int = Field(..., ge=0, le=10_000)


class DeleteOlder(BaseModel):
    days: int = Field(..., ge=1, le=36_500)
    confirm: bool = False


def _retention(ctx: ApiContext):
    retention = getattr(ctx.manager, "retention", None)
    if retention is None:
        raise ApiError(503, "not_ready", "Retention is not available.")
    return retention


def _confirmed(body) -> None:
    if not body.confirm:
        raise ApiError(422, "confirmation_required", "Confirm the action first.")


def _retention_call(fn, *args, **kwargs):
    from ...storage.retention import RetentionError

    try:
        return fn(*args, **kwargs)
    except RetentionError as exc:
        raise ApiError(exc.status, exc.code, str(exc)) from None


@router.get("/retention")
def data_retention(ctx: ApiContext = Depends(get_context)) -> dict:
    return _retention(ctx).overview()


@router.put("/retention")
def data_retention_settings(body: RetentionSettings, ctx: ApiContext = Depends(get_context)) -> dict:
    return _retention_call(_retention(ctx).set_keep_newest, body.keep_newest, by="operator (Data page)")


@router.post("/retention/compact")
def data_retention_compact(body: Confirm, ctx: ApiContext = Depends(get_context)) -> dict:
    _confirmed(body)
    out = _retention_call(_retention(ctx).compact_old, by="operator (Data page)")
    return {**out, "storage": usage(ctx.paths.data_dir, _folders(ctx))}


@router.get("/retention/older-than")
def data_retention_older_than(days: int, ctx: ApiContext = Depends(get_context)) -> dict:
    return _retention_call(_retention(ctx).older_than_plan, days)


@router.post("/retention/delete-older")
def data_retention_delete_older(body: DeleteOlder, ctx: ApiContext = Depends(get_context)) -> dict:
    _confirmed(body)
    out = _retention_call(_retention(ctx).delete_older_than, body.days, by="operator (Data page)")
    return {**out, "storage": usage(ctx.paths.data_dir, _folders(ctx))}


@router.post("/retention/clean-cache")
def data_retention_clean_cache(body: Confirm, ctx: ApiContext = Depends(get_context)) -> dict:
    _confirmed(body)
    out = _retention_call(_retention(ctx).clean_cache, by="operator (Data page)")
    return {**out, "storage": usage(ctx.paths.data_dir, _folders(ctx))}


@router.get("/datasets")
def data_datasets(ctx: ApiContext = Depends(get_context)) -> dict:
    from ...open_data import datasets as ds

    return ds.status()


def _folders(ctx: ApiContext) -> list[tuple[str, object]]:
    paths = ctx.paths
    return [("runs/", paths.runs_dir), ("document cache", paths.cache_dir), ("logs/", paths.data_dir / "logs"),
            ("derived/", paths.data_dir / "derived")]


@router.get("/storage")
def data_storage(ctx: ApiContext = Depends(get_context)) -> dict:
    out = usage(ctx.paths.data_dir, _folders(ctx))
    out["open_data_dir"] = str(ctx.paths.data_dir / "derived" / "open")
    out["startup_cleanup"] = getattr(ctx.manager, "open_data_cleanup", None)
    return out


@router.post("/storage/delete-open-data")
def data_storage_delete_open_data(body: DeleteOpenData, ctx: ApiContext = Depends(get_context)) -> dict:
    if not body.confirm:
        raise ApiError(422, "confirmation_required", "Confirm the deletion of the open-data files.")
    result = delete_open_data(ctx.paths.data_dir / "derived" / "open", by="operator (Data page)")
    return {**result, "storage": usage(ctx.paths.data_dir, _folders(ctx))}


@router.post("/open-data/shadow-report")
def data_shadow_report_run(ctx: ApiContext = Depends(get_context)) -> dict:
    from ...open_data.shadow import run_and_store
    from ...storage.disk import InsufficientDisk

    try:
        report = run_and_store(ctx.paths.runs_dir, ctx.paths.data_dir / "derived")
    except InsufficientDisk as exc:
        raise ApiError(507, exc.code, str(exc)) from None
    return {"report": report}


@router.get("/open-data/shadow-report")
def data_shadow_report(ctx: ApiContext = Depends(get_context)) -> dict:
    from ...open_data.shadow import last_report

    return {"report": last_report(ctx.paths.data_dir / "derived")}


@router.get("/policy")
def data_policy(ctx: ApiContext = Depends(get_context)) -> dict:
    return policy_table()


@router.post("/policy")
def data_policy_change(change: PolicyChange, ctx: ApiContext = Depends(get_context)) -> dict:
    try:
        logged = update_policy(change.domain, change.policy, terms_clause=change.terms_clause,
                               checked_at=change.checked_at, changed_by="operator (Data page)")
    except ValueError as exc:
        raise ApiError(422, "policy_change_refused", str(exc)) from None
    return {"change": logged, "policy": policy_table()}
