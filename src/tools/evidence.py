"""Evidence journal. Stores whatever the model chooses to cite; verifies nothing."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any


class EvidenceStore:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self._lock = threading.Lock()

    def add(self, payload: dict) -> dict:
        with self._lock:
            evidence_id = f"e{len(self.items) + 1}"
            item = {"evidence_id": evidence_id,
                    "stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    **payload}
            self.items.append(item)
        return item


def store_evidence(ctx, field: str, value: Any, unit: str | None = None, source_url: str | None = None,
                   document_id: str | None = None, quote: str | None = None, market: str | None = None,
                   variant: str | None = None, variant_match: str | None = None, note: str | None = None) -> dict:
    payload = {"field": field, "value": value, "unit": unit, "source_url": source_url,
               "document_id": document_id, "quote": quote, "market": market, "variant": variant,
               "variant_match": variant_match, "note": note}
    if document_id and not source_url:
        meta = ctx.cache.get(document_id)
        if meta:
            payload["source_url"] = meta.get("final_url") or meta.get("url")
    item = ctx.evidence.add({k: v for k, v in payload.items() if v is not None})
    ctx.emit("evidence", evidence=item)
    return {"evidence_id": item["evidence_id"], "stored": True}


def report_field_status(ctx, field: str, status: str, note: str | None = None) -> dict:
    """The model's own declaration about a requested field. Logged as a `field_status` event."""
    from ..fields import normalize_field_name
    from ..schemas import FIELD_STATUSES

    status = (status or "").strip().lower()
    if status not in FIELD_STATUSES:
        return {"error": "invalid_arguments", "message": f"status must be one of {', '.join(FIELD_STATUSES)}"}
    declaration = {"field": normalize_field_name(field), "status": status, "note": note, "source": "tool"}
    ctx.emit("field_status", **declaration)
    return {"recorded": True, **declaration}
