"""Acquisition / document-sweep diagnostics: real engine runs (scripted GLM) plus targeted event fixtures."""

import json

import pytest

from fixtures.cadillac_lyriq import PAYLOAD, VEHICLE, put_documents
from test_layered_pipeline import (INTERIOR, INTERIOR_URL, SMALL, PhaseGLM, read_docs, run, say, tool_messages,
                                   turn)
from test_tools_smoke import _call

from src import diagnostics as D
from src.acquisition import artifacts
from src.storage.run_log import RunLog, read_events


def ev(seq, kind, **data):
    return {"seq": seq, "ts": f"2026-10-01T10:00:{seq % 60:02d}+00:00", "kind": kind, **data}


# --- acquisition turns from a real engine run -------------------------------------------------------------------

@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_acquisition_turn_state_before_after_and_stop_are_recorded(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)
    client = PhaseGLM([read_docs(ids[:2]), read_docs(ids[2:4]), say({"summary": "primary", "fields": {}})])
    result, events, _ = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_attempts=0,
                            layered_harvest_enabled=False)
    turns = D.acquisition_turns(events)
    assert [t["turn_number"] for t in turns] == [1, 2, 3]
    rows = [e for e in events if e["kind"] == "primary_research_turn"]
    first, second, final = turns
    # before/after come from the tracker's own snapshots; turn 2 starts where turn 1 ended
    assert first["state_before"]["useful_documents"] == 0 and first["state_after"]["useful_documents"] == 2
    assert second["state_before"] == first["state_after"] and second["state_after"]["useful_documents"] == 4
    assert first["delta"]["new_useful_documents"] == 2 and second["delta"]["new_useful_documents"] == 2
    # scoped coverage: the same number the acquisition policy used for its gate
    assert first["state_after"]["scoped_coverage_pct"] == rows[0]["scoped_coverage_pct"]
    assert second["state_after"]["scoped_coverage_pct"] == rows[1]["scoped_coverage_pct"]
    assert second["delta"]["scoped_coverage_gain"] == round(rows[1]["scoped_coverage_pct"]
                                                            - rows[0]["scoped_coverage_pct"], 1)
    # artifacts, streak and gate exactly as logged by the existing policy
    assert first["artifacts"] == rows[0]["artifacts"] and first["qualifying_artifact"] is True
    assert first["no_artifact_streak"] == rows[0]["no_artifact_streak"] == 0
    assert first["base_gate_met"] == rows[0]["minimum_acquisition_met"]
    # the final turn answers without tools: not measured by the policy, and it carries the stop reason
    assert final["measured"] is False and final["stop_reason"] == "model_finished" and final["stopped_after_this_turn"]
    assert first["model"]["configured_model"] == "glm-5.3-flash" and first["model"]["input_tokens"] == 100
    assert first["model"]["resolved_model"] is None                       # the provider returned no model id here
    summary = D.acquisition_summary(events, turns)
    assert summary["turns"] == 3 and summary["stop_reason"] == "model_finished"
    assert summary["scoped_coverage_by_turn"][:2] == [rows[0]["scoped_coverage_pct"], rows[1]["scoped_coverage_pct"]]


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_no_artifact_turn_and_its_stop_reason(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)
    # turn 2 re-reads the same documents: no acquisition artifact; with the gate off, the policy stops there
    client = PhaseGLM([read_docs(ids[:2]), read_docs(ids[:2]), say({"summary": "unused", "fields": {}})])
    result, events, _ = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_attempts=0,
                            layered_harvest_enabled=False, primary_research_no_artifact_stop=1,
                            primary_research_min_base_documents=0, primary_research_min_base_scoped_coverage=0)
    turns = D.acquisition_turns(events)
    assert len(turns) == 2
    assert turns[1]["qualifying_artifact"] is False and turns[1]["artifacts"] == []
    assert turns[1]["no_artifact_streak"] == 1 and turns[1]["delta"]["new_useful_documents"] == 0
    assert turns[1]["stop_reason"] == "no_new_artifact" and turns[1]["stop_wanted"] == "no_new_artifact"
    assert D.acquisition_summary(events, turns)["no_artifact_turns"] == 1


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_a_stop_deferred_by_the_base_gate_and_extension_are_visible(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)
    client = PhaseGLM([read_docs(ids[:1]), read_docs(ids[:1]), read_docs(ids[1:2]), read_docs(ids[2:3]),
                       say({"summary": "primary", "fields": {}})])
    _, events, _ = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_attempts=0,
                       layered_harvest_enabled=False, max_steps=3, primary_research_no_artifact_stop=1,
                       primary_research_min_base_documents=10, primary_research_hard_max_turns=4)
    turns = D.acquisition_turns(events)
    got = [(t["turn_number"], t["qualifying_artifact"], t["base_gate_met"], t["stop_wanted"],
            t["stop_deferred_by_base_gate"], t["extended_beyond_normal_budget"], t["stop_reason"]) for t in turns]
    assert got == [(1, True, False, None, False, False, None),
                   (2, False, False, "no_new_artifact", True, False, None),    # wanted to stop; the gate held it
                   (3, True, False, "max_turns", True, False, None),           # normal ceiling, still under-acquired
                   (4, True, False, "max_steps", False, True, "max_steps")]    # extension, stops at the hard max
    summary = D.acquisition_summary(events, turns)
    assert summary["stop_reason"] == "hard_max_turns_under_acquired" and summary["extended_turns"] == 1
    assert summary["stop_deferred_count"] == 2 and summary["minimum_base_met"] is False


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_the_telemetry_never_changes_the_acquisition_decision(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)
    client = PhaseGLM([read_docs(ids[:3]), say({"summary": "primary", "fields": {}})])
    _, events, log = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_attempts=0,
                         layered_harvest_enabled=False)
    row = next(e for e in events if e["kind"] == "primary_research_turn")
    # what the policy would decide from the same snapshots without any telemetry
    assert row["artifacts"] and all(not a.startswith("state") for a in row["artifacts"])
    assert set(row["state_after"]) >= {"useful_documents", "official_documents", "target_market_documents",
                                       "candidates", "candidate_fields", "scoped_coverage_pct"}
    assert artifacts({"useful_urls": set(), "target_market_urls": set(), "official_sources": set(),
                      "candidates": set(), "evidence": set(), "best_binding": {}},
                     {"useful_urls": {"u"}, "target_market_urls": set(), "official_sources": set(),
                      "candidates": set(), "evidence": set(), "best_binding": {},
                      "candidate_count_total": 99}, mode="legacy") == ["new_usable_document:1"]   # never read


def test_search_and_fetch_details(tmp_path):
    events = [ev(1, "run_started", agent_config={"max_steps": 6}, target_market="IL",
                 vehicle_label={"manufacturer": "Cadillac"}),
              ev(2, "model_request_started", phase="research", model="glm-5.3-flash", attempt=1),
              ev(3, "api_error", phase="research", request_kind="chat", status=None, timeout=True, attempt=1),
              ev(4, "model_request_started", phase="research", model="glm-5.3-flash", attempt=2),
              ev(5, "model_response", phase="research", model="glm-5.3-flash", latency_ms=1500,
                 usage={"prompt_tokens": 900, "completion_tokens": 40}, response_meta={"model": "glm-5.3-flash-0930"},
                 tool_calls=[{"id": "a"}, {"id": "b"}, {"id": "c"}]),
              ev(6, "tool_call", step=1, phase="research", call_id="a", name="search_web",
                 arguments=json.dumps({"query": "cadillac lyriq specs"})),
              ev(7, "api_call", phase="research", request_kind="search"),
              ev(8, "tool_result", step=1, phase="research", call_id="a", name="search_web",
                 result={"cache_hit": False, "results": [{"url": "https://www.cadillac.co.il/lyriq/specs"},
                                                         {"url": "https://forum.example.com/lyriq"}]}),
              ev(9, "tool_call", step=1, phase="research", call_id="b", name="fetch_url",
                 arguments=json.dumps({"url": "https://www.cadillac.co.il/lyriq/specs"})),
              ev(10, "tool_result", step=1, phase="research", call_id="b", name="fetch_url",
                 result={"document_id": "d1", "status": 200, "text_chars": 900,
                         "url": "https://www.cadillac.co.il/lyriq/specs"}),
              ev(11, "tool_call", step=1, phase="research", call_id="c", name="fetch_url",
                 arguments=json.dumps({"url": "https://blocked.example.com/x"})),
              ev(12, "tool_result", step=1, phase="research", call_id="c", name="fetch_url",
                 result={"document_id": "d2", "status": 403, "text_chars": 0}),
              ev(13, "primary_research_turn", turn=1, artifacts=["new_usable_document:1"], no_artifact_streak=0,
                 minimum_acquisition_met=False, state_before={"useful_documents": 0, "scoped_coverage_pct": 0.0},
                 state_after={"useful_documents": 1, "scoped_coverage_pct": 12.5}),
              ev(14, "model_response", phase="research", model="glm-5.3-flash", tool_calls=[{"id": "d"}]),
              ev(15, "tool_call", step=2, phase="research", call_id="d", name="search_web",
                 arguments=json.dumps({"query": "Cadillac LYRIQ specs "})),
              ev(16, "tool_result", step=2, phase="research", call_id="d", name="search_web",
                 result={"cache_hit": True, "results": [{"url": "https://www.cadillac.co.il/lyriq/specs"}]}),
              ev(17, "research_stopped", reason="max_steps")]
    first, second = D.acquisition_turns(events)
    assert first["model"]["resolved_model"] == "glm-5.3-flash-0930" and first["model"]["retries"] == 1
    assert first["model"]["timeouts"] == 1 and first["model"]["latency_ms"] == 1500
    search = first["search"]["details"][0]
    official, forum = search["results"]
    assert official["official"] and official["target_market"] and official["selected"]
    assert not forum["official"] and not forum["selected"]
    assert first["search"]["billable_search_requests"] == 1 and second["search"]["billable_search_requests"] == 0
    assert first["fetch"] == {**first["fetch"], "attempted": 2, "succeeded": 1, "failed": 1,
                              "failure_categories": {"http_forbidden": 1}}
    assert second["search"]["repeated_queries"] == 1 and second["search"]["details"][0]["results"][0]["repeated"]
    assert second["stop_reason"] == "max_steps" and first["delta"]["scoped_coverage_gain"] == 12.5


# --- document sweep calls --------------------------------------------------------------------------------------

@pytest.mark.sweep_mode("legacy")   # encodes the legacy tool-loop sweep
@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_sweep_call_records_a_real_deterministic_harvest_miss(tmp_path, make_ctx):
    """The parser has no candidate for ventilated_seats in the cached interior document; the sweep recovers it."""
    ctx = make_ctx({})
    doc = ctx.cache.put("fetch", INTERIOR_URL, INTERIOR.encode(), {"doc_type": "text", "final_url": INTERIOR_URL},
                        INTERIOR)["document_id"]

    def sweep(packet, turn_no):
        if turn_no == 1:
            return turn(_call("i1", "find_in_document", {"document_id": doc, "query": "cooling"}))
        body = tool_messages(client.last_messages)[0]["content"].split("\n[operational note] ", 1)[0]
        snippet = next(h["snippet"] for h in json.loads(body)["hits"] if "Seat ventilation" in h["snippet"])
        quote = snippet[snippet.index("Seat ventilation"):snippet.index("standard") + len("standard")]
        return turn(_call("s1", "store_evidence", {"field": "ventilated_seats", "value": True, "document_id": doc,
                                                   "quote": quote, "market": "IL", "variant_match": "exact"}),
                    _call("s2", "store_evidence", {"field": "torque_nm", "value": 999, "document_id": doc,
                                                   "quote": "not in the document", "market": "IL"}))

    client = PhaseGLM([read_docs([doc]), say({"summary": "primary", "fields": {}})], sweep=sweep)
    result, events, _ = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_total_steps=4)
    calls = D.sweep_calls(events)
    assert len(calls) == 1
    call = calls[0]
    assert call["model_call_count"] == 2 and call["call_success"] and not call["timeout"]
    fields = {f["field_id"]: f for f in call["fields"]}
    vent = fields["ventilated_seats"]
    assert vent["resolved_by_this_call"] and vent["status_after"] == "ok" and vent["candidate_count"] == 0
    assert vent["classifications"] == [D.DETERMINISTIC_HARVEST_MISS]
    assert call["evidence_admitted"] == 1 and call["evidence_rejected"] == 1
    assert fields["torque_nm"]["evidence_rejected"] and not fields["torque_nm"]["resolved_by_this_call"]
    assert call["fields_entering"] == len(call["input"]["field_ids"]) and call["fields_resolved"] == 1
    assert call["input"]["packet_document_ids"] == [doc] and call["input"]["estimated_input_tokens"] > 0
    summary = D.sweep_summary(events, calls)
    assert summary["deterministic_harvest_misses_recovered"] == 1
    assert summary["document_sweep_deterministic_misses_found"] == 1          # the pre-existing counter agrees


@pytest.mark.sweep_mode("legacy")   # encodes the legacy tool-loop sweep
@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_a_promoted_candidate_is_never_called_a_harvest_miss(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)

    def sweep(packet, turn_no):
        wheel = packet["deterministic_candidates"]["wheelbase_mm"][0]
        return turn(_call("s", "store_evidence", {"field": "wheelbase_mm", "value": wheel["value"], "unit": "mm",
                                                  "document_id": wheel["document_id"], "quote": wheel["quote"],
                                                  "market": "IL", "variant_match": "exact"}))

    client = PhaseGLM([read_docs(ids[:1]), say({"summary": "primary", "fields": {}})], sweep=sweep)
    _, events, _ = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_attempts=0)
    calls = D.sweep_calls(events)
    wheel = next(f for c in calls for f in c["fields"] if f["field_id"] == "wheelbase_mm")
    assert wheel["resolved_by_this_call"] and wheel["candidate_count"] >= 1
    assert wheel["classifications"] == [D.PROMOTED_CANDIDATE]
    assert D.sweep_summary(events, calls)["deterministic_harvest_misses_recovered"] == 0


def test_classification_refuses_unsupported_miss_labels():
    def cand(seq, field, value, doc):
        return ev(seq, "candidates_harvested", document_id=doc, candidates=[{"field": field, "value": value,
                                                                              "document_id": doc}])
    events = [ev(1, "document", document={"document_id": "d1"}), cand(2, "wheelbase_mm", 2890, "d1"),
              ev(3, "document_sweep_started"), ev(4, "document", document={"document_id": "d9"})]
    item = {"evidence_id": "e1", "field": "wheelbase_mm", "value": 2900, "document_id": "d1", "variant_match": "exact"}
    assert D.classify_sweep_evidence(item, events=events, sweep_start_seq=3, resolved=True)["classification"] \
        == D.DETERMINISTIC_VALUE_MISMATCH                                          # parser had another value
    assert D.classify_sweep_evidence({**item, "value": 2890}, events=events, sweep_start_seq=3,
                                     resolved=True)["classification"] == D.PROMOTED_CANDIDATE
    assert D.classify_sweep_evidence({**item, "field": "length_mm"}, events=events, sweep_start_seq=3,
                                     resolved=True)["classification"] == D.DETERMINISTIC_HARVEST_MISS
    for bad in ({**item, "field": "length_mm", "document_id": "d9"},             # first seen during the sweep
                {**item, "field": "length_mm", "document_id": None, "source_url": None},
                {**item, "field": "length_mm", "variant_match": "different"}):
        assert D.classify_sweep_evidence(bad, events=events, sweep_start_seq=3, resolved=True)["classification"] \
            == D.UNCLASSIFIED
    assert D.classify_sweep_evidence({**item, "field": "length_mm"}, events=events, sweep_start_seq=3,
                                     resolved=False)["classification"] == D.UNCLASSIFIED   # did not resolve


def test_a_timed_out_sweep_call_is_represented():
    specs = [{"name": "torque_nm", "applicable": True}]
    events = [ev(1, "run_started", requested_field_specs=specs, target_market="IL"),
              ev(2, "document_sweep_started", fields_to_review=["torque_nm"], documents=3, packet_chars=4000,
                 candidates_presented=2, candidates_per_field={"torque_nm": 2}),
              ev(3, "model_request_started", phase="document_sweep", model="glm-5.3-flash", attempt=1),
              ev(4, "api_error", phase="document_sweep", request_kind="chat", timeout=True, attempt=1),
              ev(5, "model_request_started", phase="document_sweep", model="glm-5.3-flash", attempt=2),
              ev(6, "api_error", phase="document_sweep", request_kind="chat", timeout=True, attempt=2),
              ev(7, "document_sweep_failed", error="GLMError: read timeout"),
              ev(8, "document_sweep_finished", deterministic_misses_found=0)]
    (call,) = D.sweep_calls(events)
    assert call["timeout"] and call["timeouts"] == 2 and call["retries"] == 1 and not call["call_success"]
    assert call["error"] == "GLMError: read timeout" and call["fields_resolved"] == 0 and call["fields_remaining"] == 1
    assert call["model_calls"][0]["failed"] and call["input_tokens"] is None          # no usage invented
    summary = D.sweep_summary(events, [call])
    assert summary["timeouts"] == 2 and summary["failed_calls"] == 1


# --- persistence, aggregation, secrets -------------------------------------------------------------------------------

def test_diagnostics_are_written_by_research_one_and_survive_reload(tmp_path, monkeypatch):
    from src.agent import AgentConfig
    from src.benchmark import research_one
    from src.db import load_level15
    from src.storage.cache import DocumentCache
    from src.tools import ToolConfig

    monkeypatch.setenv("GLM_API_KEY", "sk-diag-secret-123456")
    row = load_level15(["101122"], source="snapshot").rows[0]
    client = PhaseGLM([say({"summary": "done", "fields": {}})])
    runs = tmp_path / "runs"
    research_one({"upstream_record_id": "101122", "manufacturer": "XPeng"}, row, client=client,
                 cache=DocumentCache(tmp_path / "cache"), runs_dir=runs, batch_id="b1",
                 agent_cfg=AgentConfig(field_recovery_enabled=False, primary_research_min_base_documents=0,
                                       primary_research_min_base_scoped_coverage=0), tool_cfg=ToolConfig(), pricing={},
                 level15_source="snapshot")
    run_dir = runs / "b1" / "101122"
    saved = json.loads((run_dir / D.DIAGNOSTICS_FILE).read_text())
    stream = [json.loads(line) for line in (run_dir / D.DIAGNOSTICS_STREAM).read_text().splitlines()]
    assert saved["schema"] == D.SCHEMA and saved["run_id"] == "b1" and saved["complete"]
    assert saved["acquisition"]["summary"]["stop_reason"] == "model_finished"
    assert saved["document_sweep"]["summary"]["skipped"] == "field_recovery_disabled"
    assert [r["type"] for r in stream] == ["acquisition_turn"] and stream[0]["record_id"] == "101122"
    assert D.load_vehicle_diagnostics(run_dir) == saved                        # reload is lossless
    rebuilt = D.vehicle_diagnostics(read_events(run_dir / "events.jsonl"), run_id="b1", record_id="101122")
    assert rebuilt["acquisition"] == saved["acquisition"] and rebuilt["summary_text"] == saved["summary_text"]
    assert "SOURCE ACQUISITION" in saved["summary_text"] and "DOCUMENT SWEEP" in saved["summary_text"]


def test_secrets_are_never_written(tmp_path, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "sk-diag-secret-123456")
    log = RunLog(tmp_path, "b2", "1")
    log.event("run_started", agent_config={"max_steps": 2}, requested_field_specs=[])
    log.event("model_request_started", phase="research", model="m", attempt=1)
    log.event("api_error", phase="research", request_kind="chat", status=401, attempt=1,
              body="invalid key sk-diag-secret-123456", headers={"Authorization": "Bearer sk-diag-secret-123456"})
    log.event("error", phase="research", message="Authorization: Bearer sk-diag-secret-123456 rejected")
    log.event("research_stopped", reason="api_failure")
    D.write_vehicle_diagnostics(log.dir)
    for name in (D.DIAGNOSTICS_FILE, D.DIAGNOSTICS_STREAM):
        assert "sk-diag-secret-123456" not in (log.dir / name).read_text()
    out = tmp_path / "bench"
    D.write_benchmark(tmp_path, ["b2"], out, rebuild=True)
    assert all("sk-diag-secret-123456" not in p.read_text() for p in out.iterdir())


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_benchmark_aggregate_keeps_per_vehicle_rows(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)
    for rid, docs in (("85095", ids[:2]), ("85096", ids[2:5])):
        client = PhaseGLM([read_docs(docs), say({"summary": "p", "fields": {}})])
        from src.agent import AgentConfig, run_vehicle
        from src.tools import ToolConfig
        log = RunLog(tmp_path / "runs", "bench", rid)
        run_vehicle({"upstream_record_id": rid}, PAYLOAD, client=client, cache=ctx.cache, run_log=log,
                    config=AgentConfig(max_steps=4, requested_fields=SMALL, field_recovery_max_attempts=0,
                                       recovery_mode="legacy"), tool_config=ToolConfig(), vehicle_meta=VEHICLE,
                    session=ctx.session)
    out = tmp_path / "out"
    result = D.write_benchmark(tmp_path / "runs", ["bench"], out, rebuild=True)
    assert result["vehicles"] == 2 and [r["record_id"] for r in result["per_vehicle"]] == ["85095", "85096"]
    acq = result["acquisition"]
    assert acq["turns"]["n"] == 2 and acq["useful_documents"]["total"] == 5
    assert "1" in acq["scoped_coverage_after_turn"] and acq["hard_max_turn_rate"] == 0
    assert result["document_sweep"]["calls_per_vehicle"]["n"] == 2
    for name in ("benchmark.json", "per_vehicle.jsonl", "per_vehicle.csv"):
        assert (out / name).is_file()
    assert len((out / "per_vehicle.jsonl").read_text().splitlines()) == 2
