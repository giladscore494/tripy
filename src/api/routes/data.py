"""The Data page: the open-data snapshots, the data volume's storage and the production source policy.

    GET  /api/data/datasets                 the committed open-data snapshots (data/open/manifest.json: build date,
                                            Action run URL, rows, years, absent columns, units, licence / attribution)
                                            and whether the engine can read each one (sha256 verified)
    GET  /api/data/storage                  the data volume: total / used / free and the size per top-level folder
    POST /api/data/storage/delete-open-data delete <data>/derived/open/ (what server-side builds left there) only;
                                            {confirm: true}; logged
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
