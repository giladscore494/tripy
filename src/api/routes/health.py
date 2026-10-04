"""GET /health: public, constant, no I/O (no provider, model, search, storage or RunManager access)."""

from fastapi import APIRouter

from ..schemas import Health

router = APIRouter(tags=["health"])


@router.get("/health", response_model=Health)
async def health() -> dict:
    return {"status": "ok", "service": "tripy"}
