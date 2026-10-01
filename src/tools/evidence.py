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
                   variant: str | None = None, note: str | None = None) -> dict:
    payload = {"field": field, "value": value, "unit": unit, "source_url": source_url,
               "document_id": document_id, "quote": quote, "market": market, "variant": variant, "note": note}
    if document_id and not source_url:
        meta = ctx.cache.get(document_id)
        if meta:
            payload["source_url"] = meta.get("final_url") or meta.get("url")
    item = ctx.evidence.add({k: v for k, v in payload.items() if v is not None})
    ctx.emit("evidence", evidence=item)
    return {"evidence_id": item["evidence_id"], "stored": True}
