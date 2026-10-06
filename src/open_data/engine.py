"""D-5: the open-data layer inside a run.

    off      nothing
    shadow   (default) match + offers recorded (event `open_data_match`, diagnostics, MCP); nothing admitted
    admit    an offer with status `offered` enters the run's evidence store as a document of class `open_dataset`, ONLY
             for (source, field, route) triples listed in data/open_data_admission.json (starts empty: the proof gate
             decides what goes in), and only when data/source_policy.json lists the dataset as `allowed`
             (identity_only never yields a value). market_portability treats it as official evidence of its own market;
             the field's portability_scope is overridden only by the D-4 entry of that triple.

Pre-research reuse (both modes): the matched designation is a search term of the contract acquisition task; a field
already resolved by an admitted offer is removed from the research plan (`skipped_open_data_resolved`).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from . import DEFAULT_MODE, MODES, OPEN_DATA_VERSION
from . import datasets as ds
from .match import match
from .offers import field_offers

SOURCE_AUTHORITY = "open_dataset"
EVENT_SURVIVORS = 8


def missing_snapshots(folder=None) -> list[str]:
    """The datasets (identity-only ones aside) without a local snapshot: recorded as `open_data_no_snapshot` in the run's
    start event (the run view shows "open data not built"). Never raises."""
    try:
        return sorted(name for name, cfg in ds.datasets().items()
                      if not cfg.get("identity_only") and not ds.available(name, folder))
    except Exception:  # noqa: BLE001 - informational
        return []


def mode_of(value: Any) -> str:
    value = str(value or DEFAULT_MODE).strip().lower()
    return value if value in MODES else DEFAULT_MODE


def _trim_sources(sources: dict) -> dict:
    """The per-source match for the event: survivors capped (the full rows stay in the snapshot)."""
    out = {}
    for name, src in sources.items():
        src = dict(src)
        if src.get("survivors"):
            src["survivors"] = src["survivors"][:EVENT_SURVIVORS]
        out[name] = src
    return out


def document_url(source: str, row_ids: list[Any]) -> str:
    base = str((ds.datasets().get(source) or {}).get("source_url") or f"https://{source}.invalid/").split("{")[0]
    return base + "#rows=" + quote(",".join(str(r) for r in row_ids[:20]))


def _document(cache, offer: dict, result: dict) -> str:
    """The offer as a cached document (kind `open_dataset`): what the evidence cites and what replay re-reads."""
    dataset = ds.datasets().get(offer["source"]) or {}
    meta_snapshot = ds.snapshot_meta(offer["source"])
    statement = (f"{offer['field']} | {offer['value']} ({offer['source']} {offer['column']} = "
                 f"{offer['raw']['value']} {offer['raw'].get('unit') or ''}, definition {offer.get('definition')}, "
                 f"{offer.get('identified_by')}, rows {', '.join(map(str, offer.get('row_ids') or []))})")
    text = "\n".join([f"{dataset.get('label') or offer['source']} (open dataset snapshot)",
                      f"designation: {result.get('designation') or ''}", f"route: {result.get('route')}",
                      f"match level: {result.get('level')}", statement])
    url = document_url(offer["source"], offer.get("row_ids") or [])
    meta = {"status": 200, "final_url": url, "doc_type": "text", "content_type": "text/plain",
            "title": f"Open data: {dataset.get('label') or offer['source']}", "open_dataset": offer["source"],
            "open_data_offer": offer, "open_data_level": result.get("level"), "open_data_route": result.get("route"),
            "open_data_designation": result.get("designation"), "snapshot_built_at": meta_snapshot.get("built_at"),
            "open_data_version": OPEN_DATA_VERSION}
    return cache.put("open_dataset", url, text.encode("utf-8"), meta, text)["document_id"]


def open_data_binding(meta: dict, identity, requirement: str | None) -> dict:
    """The binding of an open-dataset offer: the match level recorded on its document (deterministic; a replay re-reads
    it). variant_match exact only when that level reaches the field's requirement."""
    from ..document_binding import LEVELS, level_index

    level = meta.get("open_data_level") if meta.get("open_data_level") in LEVELS else "unknown"
    required = requirement if requirement in LEVELS else "exact_technical_variant"
    match_ = "exact" if level_index(level) >= level_index(required) else "unclear" if level != "unknown" else "unbound"
    return {"binding_level": level, "variant_match": match_, "binding_requirement": required, "binding_veto": [],
            "binding_dimensions": {"open_data_match": {"status": "match" if level != "unknown" else "absent",
                                                       "basis": "open_data_match", "route": meta.get("open_data_route"),
                                                       "designation": meta.get("open_data_designation")}},
            "binding_basis": "open_data_match", "binding_version": OPEN_DATA_VERSION}


def admit_offer(ctx, offer: dict, result: dict) -> dict | None:
    """One `offered` offer as evidence (admit mode). None when the dataset's policy is not `allowed`."""
    from ..evidence_admission import ADMISSION_VERSION
    from ..source_authority import dataset_policy
    from ..typed_values import typed_value

    dataset = ds.datasets().get(offer["source"]) or {}
    policy = dataset_policy(dataset.get("policy_id") or offer["source"])
    if policy["policy"] != "allowed" or dataset.get("identity_only"):
        return None
    adm = ctx.admission
    spec = adm.spec(offer["field"])
    if spec.get("applicable") is False:
        return None
    doc = _document(ctx.cache, offer, result)
    meta = ctx.cache.get(doc) or {}
    binding = open_data_binding(meta, adm.identity, spec.get("binding_requirement"))
    unit = spec.get("normalized_unit")
    snapshot = ds.snapshot_meta(offer["source"])
    record = {"field": offer["field"], "value": offer["value"], "unit": unit, "source_url": meta.get("final_url"),
              "document_id": doc, "quote": f"{offer['field']} | {offer['value']}", "market": dataset.get("market"),
              "market_basis": "open_dataset", "admission_status": "accepted", "admission_version": ADMISSION_VERSION,
              "admission_checks": {"provenance": "open_dataset_snapshot", "entailment": "dataset_cell"},
              "entailment": "dataset_cell",
              "typed_value": typed_value(offer["value"], unit=unit, value_type=spec.get("value_type"),
                                         matcher=spec.get("matcher")),
              **{k: v for k, v in binding.items() if v not in (None, [], {})},
              "source_authority": SOURCE_AUTHORITY, "authority_basis": f"open_dataset:{offer['source']}",
              "source_domain": meta.get("final_url", "").split("/")[2] if "//" in str(meta.get("final_url")) else None,
              "open_data": {"source": offer["source"], "column": offer["column"], "definition": offer.get("definition"),
                            "row_ids": offer.get("row_ids"), "identified_by": offer.get("identified_by"),
                            "route": result.get("route")},
              "portability_scope_override": offer.get("portability_scope"),
              "licence": policy.get("licence"),
              "attribution": _attribution(policy, snapshot),
              "source_date": snapshot.get("built_at"), "source_date_basis": "open_dataset_snapshot"}
    item, reused, _ = ctx.evidence.add_or_reuse({k: v for k, v in record.items() if v not in (None, "", [])})
    ctx.note_document(doc)
    if not reused:
        ctx.emit("evidence", evidence=item, phase="open_data")
    return item


def _attribution(policy: dict, snapshot: dict) -> str | None:
    from ..source_authority import attribution_for

    built = str(snapshot.get("built_at") or "")
    return attribution_for(policy.get("entry"), year=built[:4] or "n/a", date=built[:10] or "n/a")


def run_open_data(ctx, run_log, payload: dict | None, fingerprint: dict, mode: str, *, folder=None,
                  rows_by_source: dict | None = None) -> dict:
    """The run's open-data layer (module docstring). Returns {mode, route, level, designation, offers, admitted,
    skipped_fields}; never raises."""
    mode = mode_of(mode)
    out: dict[str, Any] = {"version": OPEN_DATA_VERSION, "mode": mode}
    if mode == "off":
        return out
    try:
        result = match(fingerprint, payload, identity=getattr(ctx.admission, "identity", None), folder=folder,
                       rows_by_source=rows_by_source)
        if result["route"] == "american":
            from .vpic import vpic_check

            result["sources"]["nhtsa_vpic"] = vpic_check(ctx, fingerprint, payload, result)
            from .match import match_level, target_keys

            result["level"], result["level_basis"] = match_level(target_keys(fingerprint, payload),
                                                                 result["sources"])
        offers = field_offers(result)
        allow = ds.admission_allowlist()
        admitted = []
        if mode == "admit":
            for offer in offers:
                if offer.get("status") == "offered" and (offer["source"], offer["field"], result["route"]) in allow:
                    item = admit_offer(ctx, offer, result)
                    if item is not None:
                        offer["admitted"] = True
                        admitted.append({"field": offer["field"], "source": offer["source"],
                                         "evidence_id": item.get("evidence_id"), "value": offer["value"]})
        out.update(route=result["route"], route_detail=result.get("route_detail"), level=result["level"],
                   level_basis=result.get("level_basis"), designation=result.get("designation"),
                   lead_source=result.get("lead_source"), keys=result.get("keys"),
                   sources=_trim_sources(result["sources"]), offers=offers, admitted=admitted,
                   snapshots={s: (ds.snapshot_meta(s, folder) or {}).get("built_at") for s in result["sources"]})
    except Exception as exc:  # noqa: BLE001 - the open-data layer never costs the run
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    if run_log is not None:
        run_log.event("open_data_match", **out)
    return out


def research_note(state: dict, current_ok: set[str] | None = None) -> tuple[str, list[str]]:
    """(the acquisition task's note, the fields skipped because an admitted offer resolved them)."""
    parts = []
    if state.get("designation"):
        parts.append(f"International designation of this variant (open data, {state.get('route')} route): "
                     f"{state['designation']}. Use it as a search term.")
    resolved = sorted({a["field"] for a in state.get("admitted") or []} & (current_ok or set()))
    if resolved:
        parts.append("Already resolved by admitted open data (do not research these fields): " + ", ".join(resolved)
                     + ".")
    return " ".join(parts), resolved
