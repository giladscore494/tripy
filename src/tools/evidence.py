"""Evidence store. A store_evidence request becomes Evidence ONLY through the admission gate
(src/evidence_admission.py): the cited document must be retrieved, the quote must occur in it and state the
value, the field must apply, and market / variant binding / source authority are computed server-side. A
rejected request is logged as `evidence_rejected` and returned to the model with its reasons; it never
reaches the store. `EvidenceStore` itself refuses any payload that did not pass admission.

Writes are idempotent per evidence FACT: the same field, value, unit, source, market, variant and
variant_match stored again returns the original evidence id instead of creating a second item, so one
source never looks like independent corroboration and a repeat never counts as new evidence. A
different quote or note on a repeat is kept as supplementary metadata of the original item only.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urldefrag

_PLAIN_NUMBER = re.compile(r"^-?(\d+|\d{1,3}(,\d{3})+)(\.\d+)?$")


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _value_identity(value: Any) -> str:
    """Conservative: 205 == "205" == "205.0" and "1,066" == 1066; anything else is compared as text."""
    if isinstance(value, bool):
        return f"txt:{str(value).lower()}"
    if isinstance(value, (int, float)):
        return f"num:{float(value)!r}"
    if isinstance(value, str) and _PLAIN_NUMBER.match(value.strip()):
        return f"num:{float(value.strip().replace(',', ''))!r}"
    if isinstance(value, str):
        return "txt:" + _text(value)
    return "json:" + json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def evidence_fact_key(payload: dict) -> str:
    """Canonical identity of an evidence fact. Quote and note are deliberately not part of it."""
    from ..fields import normalize_field_name

    source = payload.get("document_id") or urldefrag(str(payload.get("source_url") or "").strip())[0]
    return json.dumps([normalize_field_name(payload.get("field")), _value_identity(payload.get("value")),
                       _text(payload.get("unit")), str(source or ""), _text(payload.get("market")),
                       _text(payload.get("variant")), _text(payload.get("variant_match")),
                       _text(payload.get("condition"))], ensure_ascii=False)


class EvidenceNotAdmitted(ValueError):
    """Raised when something tries to store a payload that did not pass the admission gate."""


class EvidenceStore:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.reuse_count = 0
        self.rejected_count = 0
        self._by_key: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _append(self, payload: dict) -> dict:
        evidence_id = f"e{len(self.items) + 1}"
        item = {"evidence_id": evidence_id,
                "stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **payload}
        self.items.append(item)
        self._by_key.setdefault(evidence_fact_key(payload), item)
        return item

    def add_or_reuse(self, payload: dict) -> tuple[dict, bool, dict | None]:
        """(item, reused, supplementary). A repeat of an existing fact returns the original item.
        Only admitted payloads (admission_status == "accepted") can be stored."""
        if payload.get("admission_status") != "accepted":
            raise EvidenceNotAdmitted("evidence must pass src/evidence_admission.admit() before it is stored")
        key = evidence_fact_key(payload)
        with self._lock:
            existing = self._by_key.get(key)
            if existing is None:
                return self._append(payload), False, None
            self.reuse_count += 1
            extra = {k: payload.get(k) for k in ("quote", "note")
                     if payload.get(k) and payload.get(k) != existing.get(k)}
            if extra:
                existing.setdefault("supplementary", []).append(extra)
            return existing, True, extra or None


ACCEPT_ECHO = ("variant_match", "binding_level", "binding_veto", "market", "source_authority", "entailment",
               "valid_as_of", "temporal_status", "condition")


def admission_context(ctx):
    """The run's admission context (set by run_vehicle), else one built once from the tool context's vehicle."""
    from ..evidence_admission import AdmissionContext

    if getattr(ctx, "admission", None) is None:
        ctx.admission = AdmissionContext.default(ctx.vehicle)
    return ctx.admission


def store_evidence(ctx, field: str, value: Any, unit: str | None = None, source_url: str | None = None,
                   document_id: str | None = None, quote: str | None = None, market: str | None = None,
                   variant: str | None = None, variant_match: str | None = None, note: str | None = None,
                   condition: str | None = None, valid_as_of: str | None = None, valid_from: str | None = None,
                   valid_to: str | None = None) -> dict:
    from ..evidence_admission import admit

    request = {"field": field, "value": value, "unit": unit, "source_url": source_url, "document_id": document_id,
               "quote": quote, "market": market, "variant": variant, "variant_match": variant_match, "note": note,
               "condition": condition, "valid_as_of": valid_as_of, "valid_from": valid_from, "valid_to": valid_to}
    decision = admit(admission_context(ctx), ctx.cache, {k: v for k, v in request.items() if v is not None},
                     ctx.documents_opened)
    if not decision["accepted"]:
        ctx.evidence.rejected_count += 1
        ctx.counters["evidence_rejected"] += 1
        ctx.emit("evidence_rejected", request={k: v for k, v in request.items() if v is not None},
                 field=field, value=value, reasons=decision["reasons"],
                 document_id=decision.get("document_id") or document_id, source_url=source_url,
                 semantic_note=decision.get("semantic_note"))
        return {"stored": False, "rejected": True, "reasons": decision["reasons"], "message": decision["message"]}
    item, reused, supplementary = ctx.evidence.add_or_reuse(decision["record"])
    if item.get("document_id"):
        ctx.note_document(item["document_id"])
    if reused:
        ctx.counters["duplicate_evidence_suppressed"] += 1
        ctx.emit("evidence_reused", evidence_id=item["evidence_id"], field=item.get("field"), value=item.get("value"),
                 document_id=item.get("document_id"), source_url=item.get("source_url"), market=item.get("market"),
                 supplementary=supplementary)
        return {"evidence_id": item["evidence_id"], "stored": False, "reused": True,
                "note": f"This evidence fact is already stored as {item['evidence_id']}; it was not stored again. "
                        "Cite that id. A repeat is not independent corroboration."}
    ctx.emit("evidence", evidence=item)
    return {"evidence_id": item["evidence_id"], "stored": True,
            **{k: item[k] for k in ACCEPT_ECHO if item.get(k) not in (None, "", [])}}


def reuse_verified_fact(ctx, record: dict) -> dict:
    """Bring a verified fact of a related variant (src/research_memory.py) into this run. NOT a model tool.

    The fact is re-admitted against THIS run's target with the same gate as store_evidence (the document is in the
    shared cache, the quote states the value, server-side binding to this target) and must again bind exactly at the
    field's reuse level. It keeps its original source identity (`reused_from`); a reused fact is one source, not a
    new one. Returns {"reused": bool, "reasons": [...], "evidence_id"?}."""
    from ..evidence_admission import admit
    from ..research_memory import _level_ok

    request = {k: record.get(k) for k in ("field", "value", "unit", "document_id", "quote", "condition")
               if record.get(k) not in (None, "")}
    decision = admit(admission_context(ctx), ctx.cache, request, ctx.documents_opened)
    if not decision["accepted"]:
        return {"reused": False, "reasons": decision["reasons"]}
    item = decision["record"]
    if not _level_ok(item, record.get("scope_type")):
        return {"reused": False, "reasons": [f"binding_below_reuse_scope:{item.get('binding_level')}"]}
    item["reused_from"] = {"record_id": record.get("record_id"), "scope_type": record.get("scope_type"),
                           "origin": record.get("origin"), "recorded_at": record.get("recorded_at")}
    stored, repeated, _ = ctx.evidence.add_or_reuse(item)
    if stored.get("document_id"):
        ctx.note_document(stored["document_id"])
    if repeated:
        return {"reused": False, "reasons": ["already_stored"], "evidence_id": stored["evidence_id"]}
    ctx.counters["verified_fact_cache_hits"] += 1
    ctx.emit("evidence", evidence=stored, phase="fact_reuse")
    return {"reused": True, "evidence_id": stored["evidence_id"], "reasons": []}


def report_field_status(ctx, field: str, status: str, note: str | None = None,
                        evidence_ids: list | None = None) -> dict:
    """The model's own declaration about a requested field. Logged as a `field_status` event.

    `evidence_ids` is optional, except for conflict_resolved, which must cite the evidence records that
    establish the resolution (technical check only: ids must exist and belong to this field)."""
    from ..fields import normalize_field_name
    from ..schemas import FIELD_STATUSES

    status = (status or "").strip().lower()
    if status not in FIELD_STATUSES:
        return {"error": "invalid_arguments", "message": f"status must be one of {', '.join(FIELD_STATUSES)}"}
    name = normalize_field_name(field)
    cited = [str(i) for i in evidence_ids or [] if i not in (None, "")]
    if status == "conflict_resolved":
        known = {str(e.get("evidence_id")) for e in ctx.evidence.items if normalize_field_name(e.get("field")) == name}
        bad = [i for i in cited if i not in known]
        if not cited or bad:
            return {"error": "invalid_arguments",
                    "message": "conflict_resolved needs evidence_ids of stored evidence for this field"
                               + (f"; unknown for {name}: {', '.join(bad)}" if bad else "")
                               + ". Store the supporting evidence first, then cite it."}
    declaration = {"field": name, "status": status, "note": note, "source": "tool", "evidence_ids": cited}
    ctx.emit("field_status", **declaration)
    return {"recorded": True, **declaration}
