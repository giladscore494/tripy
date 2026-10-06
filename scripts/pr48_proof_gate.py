"""Identity anchors PR proof gate (no network).

    1. policy replay of production run 20261005T214857Z-glm-5.3-flash-one, record 23678 (BMW M4 Competition 2024,
       31AZ): the 43 evidence records the run stored (tests/fixtures/pr48_m4_run_evidence.json, read from the run
       through the tripy MCP) under the production source policy (data/source_policy.json, no overlay): evidence whose
       source is not `allowed` is dropped, the fields are evaluated again with the current code, and every field is
       listed with its recorded state / value / sources next to its replayed state / value / sources
    2. unit replay of the 2023 importer sheet (tests/fixtures/pr48_m4_sheets.SHEET_2023_TEXT, hosted on bmw.co.il,
       allowed for this replay only through a temporary operator overlay): admission of its candidates with the
       target's identity fingerprint, then the field evaluation of those records together with the run's evidence

    python scripts/pr48_proof_gate.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from src import source_authority  # noqa: E402
from src.field_recovery import evaluate_fields  # noqa: E402
from src.fields import resolve_requested_fields  # noqa: E402

RUN = json.loads((ROOT / "tests/fixtures/pr48_m4_run_evidence.json").read_text("utf-8"))
SPECS = resolve_requested_fields(None, propulsion="conventional")


def _events(items: list[dict]) -> list[dict]:
    return [{"kind": "evidence", "seq": n + 1, "evidence": item} for n, item in enumerate(items)]


def _sources(ids, by_id: dict) -> str:
    domains = sorted({by_id[i]["source_domain"] for i in ids or [] if i in by_id})
    return ", ".join(domains) or "—"


def _value(entry: dict, by_id: dict):
    if entry.get("state") != "ok":
        return None
    values = {json.dumps(by_id[i]["value"], ensure_ascii=False) for i in entry.get("evidence_ids") or [] if i in by_id}
    return ", ".join(sorted(values)) or None


def policy_replay() -> dict:
    source_authority._TEST_OVERRIDE = None
    source_authority.set_policy_overlay_dir(None)
    evidence = RUN["evidence"]
    by_id = {e["evidence_id"]: e for e in evidence}
    decisions = {e["evidence_id"]: source_authority.policy_of(e["source_url"]) for e in evidence}
    kept = [e for e in evidence if decisions[e["evidence_id"]]["policy"] == "allowed"]
    replayed = {e["field"]: e for e in evaluate_fields(SPECS, _events(kept), "IL")}
    rows, wrong = [], 0
    for spec in SPECS:
        name = spec["name"]
        recorded = RUN["recorded"].get(name)
        rec_ids = [e["evidence_id"] for e in evidence if e["field"] == name]
        now = replayed.get(name) or {}
        value = _value(now, by_id)
        # a value is wrong when the replay states one the run's own evidence contradicts (no truth table offline)
        if value is not None and recorded and recorded["state"] == "ok" and value != json.dumps(recorded["value"]):
            wrong += 1
        rows.append({"field": name, "recorded_state": recorded["state"] if recorded else "(new field)",
                     "recorded_value": recorded["value"] if recorded else None,
                     "recorded_sources": _sources(rec_ids, by_id), "replay_state": now.get("state"),
                     "replay_value": value, "replay_sources": _sources(now.get("evidence_ids"), by_id),
                     "info": now.get("info")})
    return {"evidence": len(evidence), "kept": len(kept),
            "dropped_by_domain": dict(Counter(by_id[i]["source_domain"] for i, d in decisions.items()
                                              if d["policy"] != "allowed")),
            "policy_entries": {by_id[i]["source_domain"]: f"{d['policy']} ({d['entry']})" for i, d in decisions.items()},
            "rows": rows, "replay_states": dict(Counter(r["replay_state"] for r in rows)),
            "recorded_states": dict(Counter(r["recorded_state"] for r in rows)), "wrong": wrong}


def sheet_replay() -> dict:
    from fixtures import pr48_m4_sheets as M
    from fixtures.corolla_harvest import put
    from src.candidate_harvest import harvest_document
    from src.db import build_level15_payload
    from src.document_binding import apply_fingerprint
    from src.evidence_admission import AdmissionContext, admit
    from src.gov_registry import identity_fingerprint
    from src.storage.cache import DocumentCache

    family = json.loads((ROOT / "tests/fixtures/pr48_catalog_families.json").read_text("utf-8"))["rows"]
    code_types: dict = {}
    for row in family:
        code_types.setdefault(int(row["degem_cd"]), {"type_codes": [], "years": []})["type_codes"].append(
            row["degem_nm"])
    snapshot = {r["upstream_record_id"]: r for r in
                json.loads((ROOT / "data/benchmark_v1_level15_snapshot.json").read_text("utf-8"))["rows"]}
    payload = build_level15_payload(snapshot["23678"])
    with tempfile.TemporaryDirectory() as tmp:
        source_authority._TEST_OVERRIDE = None
        source_authority.set_policy_overlay_dir(Path(tmp) / "derived")
        source_authority.update_policy("bmw.co.il", "allowed", terms_clause="proof gate replay only",
                                       checked_at="2026-10-06", changed_by="pr48_proof_gate")
        cache = DocumentCache(Path(tmp) / "cache")
        adm = AdmissionContext.for_run(payload, None, SPECS, "IL")
        apply_fingerprint(adm.identity, identity_fingerprint(payload, family, code_types, catalog="fixture"))
        doc = put(cache, M.SHEET_2023_URL, M.SHEET_2023_TEXT, "pdf")
        candidates, _ = harvest_document(cache, doc, SPECS)
        records, seen = [], set()
        for n, cand in enumerate(candidates):
            if cand["field"] in seen:
                continue
            decision = admit(adm, cache, {"field": cand["field"], "value": cand["value"], "quote": cand["quote"],
                                          "document_id": doc}, [doc])
            if decision["accepted"]:
                seen.add(cand["field"])
                records.append({**decision["record"], "evidence_id": f"s{n + 1}", "source_domain": "bmw.co.il"})
        run_items = [e for e in RUN["evidence"] if source_authority.evidence_allowed(e["source_url"])]
        evaluated = {e["field"]: e for e in evaluate_fields(SPECS, _events(run_items + records), "IL")}
        source_authority.set_policy_overlay_dir(None)
    by_id = {r["evidence_id"]: r for r in records + run_items}
    show = ("curb_weight_kg", "screen_size_in", "torque_nm", "rim_diameter_front_in", "rim_diameter_rear_in",
            "rim_diameter_in", "fuel_consumption_combined_l_100km", "apple_carplay", "heated_seats", "climate_zones")
    return {"records": len(records), "binding": sorted({(r.get("binding_level"), r.get("binding_basis"))
                                                        for r in records}),
            "fields": {f: {"state": evaluated[f]["state"], "value": _value(evaluated[f], by_id),
                           "info": evaluated[f].get("info")} for f in show if f in evaluated}}


def main() -> int:
    gate = policy_replay()
    print(f"== 1. policy replay, run {RUN['run_id']} record {RUN['record_id']}")
    print(f"evidence {gate['evidence']}, kept as evidence under the policy {gate['kept']}, "
          f"dropped by domain {gate['dropped_by_domain']}")
    print("policy per domain:", gate["policy_entries"])
    print(f"| field | recorded | recorded sources | replay | replay sources |")
    print("|---|---|---|---|---|")
    for r in gate["rows"]:
        rec = r["recorded_state"] + (f" {r['recorded_value']}" if r["recorded_value"] is not None else "")
        rep = (r["replay_state"] or "") + (f" {r['replay_value']}" if r["replay_value"] is not None else "")
        print(f"| {r['field']} | {rec} | {r['recorded_sources']} | {rep} | {r['replay_sources']} |")
    print("recorded states:", gate["recorded_states"])
    print("replay states:  ", gate["replay_states"])
    print("wrong values in the replay:", gate["wrong"])
    curb = next(r for r in gate["rows"] if r["field"] == "curb_weight_kg")
    screen = next(r for r in gate["rows"] if r["field"] == "screen_size_in")
    ok = (gate["kept"] == 0 and curb["replay_state"] != "ok" and screen["replay_state"] != "ok"
          and gate["wrong"] == 0)
    sheet = sheet_replay()
    print(f"\n== 2. 2023 importer sheet unit replay: {sheet['records']} admitted records, binding {sheet['binding']}")
    for field, entry in sheet["fields"].items():
        print(f"  {field}: {entry['state']} {entry['value'] if entry['value'] is not None else ''}"
              f"{'  ' + str(entry['info']) if entry['info'] else ''}")
    expect = {"curb_weight_kg": "1816", "screen_size_in": "10.25", "torque_nm": "650",
              "rim_diameter_front_in": "19", "rim_diameter_rear_in": "20"}
    sheet_ok = all(sheet["fields"].get(f, {}).get("value") == v for f, v in expect.items())
    sheet_ok = sheet_ok and sheet["fields"]["rim_diameter_in"]["state"] == "not_applicable"
    print(f"\nproof gate: policy replay {'PASS' if ok else 'FAIL'}, sheet replay {'PASS' if sheet_ok else 'FAIL'}")
    return 0 if ok and sheet_ok else 1


if __name__ == "__main__":
    sys.exit(main())
