"""The Data page (identity anchors PR): open datasets and the production source policy.

    GET  /api/data/datasets                      per dataset: last build, rows, source URL, licence and attribution
    POST /api/data/datasets/rebuild?dataset=     "Rebuild now" of one dataset (or all) (202)
    GET  /api/data/policy                        the effective source policy (repository file + operator overlay) and
                                                 its change log
    POST /api/data/policy                        switch ONE domain between blocked and allowed: {domain, policy,
                                                 terms_clause, checked_at}; allowing needs the terms clause and the date;
                                                 stored as an overlay in the data volume, every change logged

No environment edit and no secret: the policy is data (data/source_policy.json) plus the logged overlay.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...source_authority import policy_table, update_policy
from ..deps import ApiContext, get_context
from ..errors import ApiError

router = APIRouter(prefix="/api/data", tags=["data"])


class PolicyChange(BaseModel):
    domain: str = Field(..., min_length=3, max_length=253)
    policy: str = Field(..., pattern="^(allowed|blocked)$")
    terms_clause: str | None = Field(None, max_length=2000)
    checked_at: str | None = Field(None, max_length=40)


@router.get("/datasets")
def data_datasets(ctx: ApiContext = Depends(get_context)) -> dict:
    return ctx.manager.open_data.status()


@router.post("/datasets/rebuild", status_code=202)
def data_rebuild(dataset: str | None = None, ctx: ApiContext = Depends(get_context)) -> dict:
    from ...open_data import datasets as ds

    if dataset is not None and dataset not in ds.datasets():
        raise ApiError(404, "unknown_dataset", "No such dataset.")
    started = ctx.manager.open_data.rebuild_async(dataset)
    return {"started": started, "message": "Rebuild started." if started else "A build is already running."}


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
