"""Parser-gap telemetry (Part B): a field label in a document, no harvested value. Observational only: zero model
calls, zero network, no decision changes. No network."""

import json

from conftest import cache_source
from fixtures import corolla_tail as tail
from test_phase_contracts import EU, GATE_OFF, PhaseClient, fetch, run, say

from src import diagnostics as D
from src import parser_gaps as P
from src.candidate_harvest import harvest_document
from src.fields import resolve_requested_fields
from src.storage.cache import DocumentCache
from src.storage.run_log import RunLog, read_events

SPECS = resolve_requested_fields(["fuel_tank_l", "wheelbase_mm"], propulsion="hybrid")
NO_VALUE = "Toyota Corolla Touring Sports 2024 technical data\nFuel tank: see the dealer for details\nColours: 8"
WITH_VALUE = "Toyota Corolla Touring Sports 2024\nFuel tank capacity: 43 l\nWheelbase: 2,700 mm"


def _cache(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    gap = cache_source(cache, "https://www.example.org/corolla-data", NO_VALUE)
    full = cache_source(cache, "https://www.example.org/corolla-specs", WITH_VALUE)
    return cache, gap, full


def test_a_label_without_a_harvested_value_is_a_gap_and_a_harvested_one_is_not(tmp_path):
    cache, gap, full = _cache(tmp_path)
    assert not [c for c in harvest_document(cache, gap, SPECS)[0] if c["field"] == "fuel_tank_l"]
    assert [c for c in harvest_document(cache, full, SPECS)[0] if c["field"] == "fuel_tank_l"]
    report = P.parser_gaps(cache=cache, adm=None, documents=[gap, full], specs=SPECS)
    assert [(r["field"], r["document_id"]) for r in report["rows"]] == [("fuel_tank_l", gap)]
    row = report["rows"][0]
    assert row["matched_alias"].lower() == "fuel tank" and row["source_url"] == "https://www.example.org/corolla-data"
    assert "Fuel tank" in row["snippet"] and len(row["snippet"]) <= 200
    assert NO_VALUE[row["offset"]:].startswith("Fuel tank")
    assert (report["gaps_total"], report["fields_with_gaps"], report["documents_scanned"], report["truncated"]) == \
        (1, 1, 2, 0)
    assert report["model_calls"] == 0 and isinstance(report["duration_ms"], int)


def test_other_variant_unusable_documents_and_fields_without_a_dictionary_are_skipped(tmp_path, monkeypatch):
    cache, gap, full = _cache(tmp_path)
    blocked = cache.put("fetch", "https://www.example.org/blocked", b"Fuel tank: ?",
                        {"status": 403, "final_url": "https://www.example.org/blocked", "doc_type": "text"},
                        "Fuel tank: ?")["document_id"]
    other = cache_source(cache, "https://www.example.org/other-variant", "Fuel tank: ask us")
    import src.tail_planner as planner

    real = planner.document_profile_for
    monkeypatch.setattr(planner, "document_profile_for", lambda adm, c, doc: (
        {"variant_match": "different"} if doc == other else real(adm, c, doc)))
    custom = {"name": "engine_oil_capacity_l", "description": "engine oil", "group": "technical"}
    report = P.parser_gaps(cache=cache, adm=None, documents=[gap, full, blocked, other], specs=SPECS + [custom])
    assert {r["document_id"] for r in report["rows"]} == {gap} and report["documents_scanned"] == 2
    assert "engine_oil_capacity_l" not in {r["field"] for r in report["rows"]}
    assert report["fields_checked"] == 2                         # fields with a dictionary entry only
    # the cap keeps field order then document order and counts what it left out
    second = cache_source(cache, "https://www.example.org/corolla-data-2", NO_VALUE + "\nWheelbase: long")
    capped = P.parser_gaps(cache=cache, adm=None, documents=[gap, second], specs=SPECS, max_rows=2)
    assert [(r["field"], r["document_id"]) for r in capped["rows"]] == [("fuel_tank_l", gap), ("fuel_tank_l", second)]
    assert capped["gaps_total"] == 3 and capped["truncated"] == 1 and P.MAX_ROWS == 200


def test_a_failure_is_logged_and_the_run_completes(tmp_path, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("gaps broke")

    monkeypatch.setattr(P, "parser_gaps", broken)
    client = PhaseClient([fetch("a", EU), say({"done": True, "reason": "x"})])
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False, **GATE_OFF)
    failed = [e for e in events if e["kind"] == "parser_gaps_failed"]
    assert failed and "gaps broke" in failed[0]["error"] and result["status"] == "completed"
    assert not any(e["kind"] == "parser_gaps" for e in events)
    assert D.parser_gap_summary(events) == {"recorded": False, "failed": True}


def test_a_run_logs_one_parser_gaps_event_between_the_harvest_and_the_sweep(tmp_path):
    client = PhaseClient([fetch("a", EU, tail.FORUM), say({"done": True, "reason": "x"})])
    _, events = run(tmp_path, client, acquisition_mode="contract", **GATE_OFF)
    kinds = [e["kind"] for e in events]
    assert kinds.count("parser_gaps") == 1
    assert kinds.index("deterministic_harvest_summary") < kinds.index("parser_gaps") < \
        kinds.index("document_sweep_started")


def _scripted(tmp_path, monkeypatch=None):
    if monkeypatch is not None:
        import src.agent as agent_mod
        monkeypatch.setattr(agent_mod, "log_parser_gaps", lambda *a, **k: None)
    client = PhaseClient([fetch("a", EU, tail.CARTUBE), fetch("b", tail.FORUM, tail.LAUNCH),
                          say({"done": True, "reason": "x"})])
    return run(tmp_path, client, acquisition_mode="contract")


def test_parser_gaps_change_no_decision(tmp_path, monkeypatch):
    with_gaps, events = _scripted(tmp_path / "on")
    assert any(e["kind"] == "parser_gaps" for e in events)
    without, quiet = _scripted(tmp_path / "off", monkeypatch)
    assert not any(e["kind"] == "parser_gaps" for e in quiet)
    for key in ("status", "stop_reason", "research_steps"):
        assert with_gaps[key] == without[key]
    strip = lambda items: [{k: v for k, v in e.items() if k not in ("ts", "seq", "stored_at")} for e in items]
    assert strip(with_gaps["evidence"]) == strip(without["evidence"])
    assert {f: s["state"] for f, s in with_gaps["research_bundle"]["field_states"].items()} == \
        {f: s["state"] for f, s in without["research_bundle"]["field_states"].items()}
    assert with_gaps["field_recovery"]["attempt_count"] == without["field_recovery"]["attempt_count"]
    assert [e["kind"] for e in events if e["kind"] != "parser_gaps"] == [e["kind"] for e in quiet]


# --- diagnostics: per vehicle, catalog-wide backlog, parser_gaps.jsonl ---------------------------------------------

def _row(field, doc, alias="fuel tank"):
    return {"field": field, "document_id": doc, "source_url": f"https://x/{doc}", "source_authority": "unknown",
            "market": None, "matched_alias": alias, "offset": 3, "snippet": "Fuel tank: ?"}


def _events(rows, missed, record="r1"):
    return [{"seq": 1, "kind": "run_started", "record_id": record, "acquisition_mode": "contract", "agent_config": {}},
            {"seq": 2, "kind": "parser_gaps", "rows": rows, "gaps_total": len(rows), "truncated": 0,
             "fields_with_gaps": len({r["field"] for r in rows}), "documents_scanned": 3, "duration_ms": 1},
            *[{"seq": 3 + i, "kind": "candidate_missed_by_deterministic_harvest", "field": f, "document_id": d,
               "value": 1} for i, (f, d) in enumerate(missed)],
            {"seq": 99, "kind": "run_finished", "status": "completed"}]


def test_recovered_by_sweep_counts_only_misses_on_a_gap_document_of_that_field(tmp_path):
    rows = [_row("fuel_tank_l", "d1"), _row("fuel_tank_l", "d2", "tank capacity"), _row("wheelbase_mm", "d1",
                                                                                         "wheelbase")]
    missed = [("fuel_tank_l", "d1"), ("fuel_tank_l", "d3"), ("wheelbase_mm", "d2"), ("Fuel_Tank_L", "d2")]
    summary = D.parser_gap_summary(_events(rows, missed))
    assert summary["fields"] == {"fuel_tank_l": {"label_hits_no_value": 2, "recovered_by_sweep": 2},
                                 "wheelbase_mm": {"label_hits_no_value": 1, "recovered_by_sweep": 0}}
    diag = D.vehicle_diagnostics(_events(rows, missed), run_id="run1", record_id="r1")
    row = D.vehicle_row(diag)
    assert (row["parser_gap_rows"], row["parser_gap_fields"], row["parser_gap_recovered_by_sweep"]) == (3, 2, 2)
    # old diagnostics without the section: no numbers invented
    diag.pop("parser_gaps")
    assert D.vehicle_row(diag)["parser_gap_rows"] is None


def test_parser_gaps_by_field_orders_by_runs_then_rows_and_lists_top_aliases():
    a = D.vehicle_diagnostics(_events([_row("fuel_tank_l", "d1"), _row("wheelbase_mm", "d1", "wheelbase"),
                                       _row("wheelbase_mm", "d2", "wheel base"), _row("wheelbase_mm", "d3", "wheelbase")],
                                      [("wheelbase_mm", "d2")]), run_id="run1", record_id="r1")
    b = D.vehicle_diagnostics(_events([_row("fuel_tank_l", "d9", "tank capacity")], [], "r2"), run_id="run2",
                              record_id="r2")
    backlog = D.aggregate([a, b])["parser_gaps_by_field"]
    assert [x["field"] for x in backlog] == ["fuel_tank_l", "wheelbase_mm"]        # 2 runs before 1 run with 3 rows
    assert backlog[0] == {"field": "fuel_tank_l", "gap_rows": 2, "runs_with_gap": 2, "recovered_by_sweep": 0,
                          "top_matched_aliases": [{"alias": "fuel tank", "rows": 1},
                                                  {"alias": "tank capacity", "rows": 1}]}
    assert backlog[1]["gap_rows"] == 3 and backlog[1]["recovered_by_sweep"] == 1
    assert backlog[1]["top_matched_aliases"][0] == {"alias": "wheelbase", "rows": 2}


def test_write_benchmark_writes_parser_gaps_jsonl(tmp_path):
    for run_id, record, rows in (("run1", "r1", [_row("fuel_tank_l", "d1")]),
                                 ("run2", "r2", [_row("wheelbase_mm", "d5", "wheelbase")])):
        log = RunLog(tmp_path / "runs", run_id, record)
        for event in _events(rows, [], record):
            log.event(event["kind"], **{k: v for k, v in event.items() if k not in ("kind", "seq")})
    out = D.write_benchmark(tmp_path / "runs", run_ids=["run1", "run2"], out_dir=tmp_path / "bench", rebuild=True)
    lines = [json.loads(l) for l in (tmp_path / "bench" / D.PARSER_GAPS_FILE).read_text("utf-8").splitlines()]
    assert [(l["run_id"], l["record_id"], l["field"], l["document_id"]) for l in lines] == [
        ("run1", "r1", "fuel_tank_l", "d1"), ("run2", "r2", "wheelbase_mm", "d5")]
    assert {x["field"] for x in out["parser_gaps_by_field"]} == {"fuel_tank_l", "wheelbase_mm"}
    assert read_events(tmp_path / "runs" / "run1" / "r1" / "events.jsonl")
