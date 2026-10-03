"""Orchestration: primary research as source acquisition, deterministic batch inspection, local-first adaptive
document sweep, phase-specific model settings. Scheduling only: none of it may create evidence, change a field state,
a binding, a market or a conflict (current_evaluation() stays the one field-state authority)."""

import json
import re

import pytest

from conftest import FakeResponse, cache_source
from fixtures import corolla_tail as tail
from fixtures.corolla_touring import PAYLOAD, VEHICLE
from test_tools_smoke import ScriptedGLM, _call

from src.acquisition import artifacts, source_priority, sufficient
from src.agent import (DOCUMENT_SWEEP_SYSTEM_PROMPT, AgentConfig, ModelCaller, agent_config_from_env,
                       research_system_prompt, run_vehicle)
from src.benchmark import compute_metrics
from src.candidate_harvest import candidate_matrix
from src.document_inspection import inspect_document
from src.document_sweep import packet_size, plan_chunks, route_candidates, routing_score, sweep_packet
from src.field_recovery import current_evaluation
from src.fields import resolve_requested_fields
from src.glm_client import ChatResponse, GLMError
from src.phase_settings import for_phase, phase_settings_from_env
from src.storage.cache import DocumentCache, document_id_for
from src.storage.run_log import RunLog, read_events
from src.tools import ToolConfig, dispatch

SPECS = resolve_requested_fields(None, propulsion="hybrid")
EU = tail.EU_SPEC
EU_DOC = document_id_for("fetch", EU)


def say(obj):
    return {"role": "assistant", "content": json.dumps(obj)}


def turn(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


class SearchGLM(ScriptedGLM):
    """ScriptedGLM with a search backend: a query containing '429' fails like a rate-limited provider."""

    def web_search(self, query, count=8, domain=None):
        if "429" in query:
            raise GLMError("HTTP 429 from web_search", status=429)
        return [{"url": EU, "title": "Corolla Touring Sports 1.8 Hybrid technical specifications", "snippet": "s"}]


def research_run(tmp_path, script, routes=None, **cfg):
    routes = {url: FakeResponse(body.encode("utf-8"), content_type=ctype, url=url, status=status)
              for url, (body, ctype, status) in (routes or {}).items()}
    from conftest import FakeSession

    client = SearchGLM(script)
    cache = DocumentCache(tmp_path / "cache")
    log = RunLog(tmp_path / "runs", "b", "38626")
    config = AgentConfig(**{"field_recovery_enabled": False, "no_new_research_turns": 0,
                            "research_memory_enabled": False, "requested_fields": tail.FIELDS, **cfg})
    result = run_vehicle({"upstream_record_id": "38626"}, PAYLOAD, client=client, cache=cache, run_log=log,
                         vehicle_meta=VEHICLE, config=config, tool_config=ToolConfig(), session=FakeSession(routes))
    return result, read_events(log.events_path), client, cache


EU_ROUTE = {EU: (tail.EU_SPEC_HTML, "text/html; charset=utf-8", 200)}
# the artifact-definition tests below run on ONE document: the minimum-acquisition safety gate is switched off there
# (it is tested on its own further down)
GATE_OFF = {"primary_research_min_base_documents": 0, "primary_research_min_base_scoped_coverage": 0}
RICH_ROUTES = {**EU_ROUTE, tail.CARTUBE: (tail.CARTUBE_TEXT, "text/plain", 200),
               tail.LAUNCH: (tail.LAUNCH_TEXT, "text/plain", 200)}
FETCH_THREE = turn(_call("a", "fetch_url", {"url": EU}), _call("b", "fetch_url", {"url": tail.CARTUBE}),
                   _call("c", "fetch_url", {"url": tail.LAUNCH}))


# --- primary research: adaptive no-artifact stop ---------------------------------------------------------------

def test_two_consecutive_no_artifact_turns_stop_primary_research(tmp_path):
    script = [turn(_call("a", "fetch_url", {"url": EU})),
              turn(_call("b", "find_in_document", {"document_id": EU_DOC, "query": "Fuel tank"})),
              turn(_call("c", "find_in_document", {"document_id": EU_DOC, "query": "Kerb weight"})),
              turn(_call("d", "find_in_document", {"document_id": EU_DOC, "query": "Top speed"})),
              say({"summary": "never reached", "fields": {}}), say({"summary": "final", "fields": {}})]
    result, events, client, _ = research_run(tmp_path, script, EU_ROUTE, max_steps=6, **GATE_OFF)
    assert result["stop_reason"] == "no_new_artifact" and result["research_steps"] == 3
    primary = result["primary_research"]
    assert primary["stop_reason"] == "no_new_artifact" and primary["final_no_artifact_streak"] == 2
    assert [t["artifacts"] != [] for t in primary["turn_artifacts"]] == [True, False, False]
    assert primary["turns"] == 3 < primary["max_turns"]            # stopped before the hard turn ceiling
    assert result["usage_research"]["model_calls"] == 3
    turns = [e for e in events if e["kind"] == "primary_research_turn"]
    assert turns[0]["artifacts"][0].startswith("new_usable_document")


def test_a_new_document_resets_the_no_artifact_counter(tmp_path):
    routes = {**EU_ROUTE, tail.CARTUBE: (tail.CARTUBE_TEXT, "text/plain", 200)}
    script = [turn(_call("a", "fetch_url", {"url": EU})),
              turn(_call("b", "find_in_document", {"document_id": EU_DOC, "query": "Fuel tank"})),
              turn(_call("c", "fetch_url", {"url": tail.CARTUBE})),                       # new document: reset
              turn(_call("d", "find_in_document", {"document_id": EU_DOC, "query": "Top speed"})),
              say({"summary": "done", "fields": {}})]
    result, _, _, _ = research_run(tmp_path, script, routes, max_steps=6)
    assert result["stop_reason"] == "model_finished"
    streaks = [t["artifacts"] != [] for t in result["primary_research"]["turn_artifacts"]]
    assert streaks == [True, False, True, False]
    assert result["primary_research"]["documents_added"] == 2


# the sweep packet limits before Phase Contracts v1 (12 fields / 16 candidates since): tests asserting a one-packet
# sweep pin them explicitly
SINGLE_PACKET_SWEEP = {"document_sweep_max_fields": 30, "document_sweep_max_candidates": 48}


def _snap(**over):
    base = {"useful_documents": [], "useful_urls": set(), "target_market_urls": set(), "target_market_documents": [],
            "official_sources": set(), "candidates": set(), "evidence": set(), "best_binding": {}}
    return {**base, **over}


def test_a_new_candidate_for_an_applicable_field_resets_the_counter_and_nothing_else_counts():
    before = _snap(candidates={"torque_nm|d_1|142"})
    assert artifacts(before, _snap(candidates={"torque_nm|d_1|142", "fuel_tank_l|d_1|43"}),
                     mode="legacy") == ["new_candidate:1"]
    assert artifacts(before, _snap(candidates={"torque_nm|d_1|142"}), mode="legacy") == []             # nothing new
    assert artifacts(_snap(evidence={"e1"}), _snap(evidence={"e1", "e2"}), mode="legacy") == ["new_admitted_evidence:1"]


def test_failed_fetches_and_rate_limited_searches_do_not_reset_the_counter(tmp_path):
    routes = {**EU_ROUTE, "https://www.toyota.co.il/blocked": ("Access denied", "text/html", 403)}
    script = [turn(_call("a", "fetch_url", {"url": EU})),
              turn(_call("b", "fetch_url", {"url": "https://www.toyota.co.il/blocked"}),             # 403
                   _call("c", "fetch_url", {"url": "https://www.example.com/missing"})),             # 404
              turn(_call("d", "search_web", {"query": "corolla 429 specifications"})),                # 429
              say({"summary": "never reached", "fields": {}}), say({"summary": "final", "fields": {}})]
    result, events, _, _ = research_run(tmp_path, script, routes, max_steps=6, **GATE_OFF)
    assert result["stop_reason"] == "no_new_artifact" and result["research_steps"] == 3
    assert [t["artifacts"] for t in result["primary_research"]["turn_artifacts"]][1:] == [[], []]
    errors = [e for e in events if e["kind"] == "tool_result" and e["name"] == "search_web"]
    assert errors and errors[0]["result"].get("error")


def test_cached_rereads_and_repeated_searches_do_not_reset_the_counter(tmp_path):
    script = [turn(_call("a", "search_web", {"query": "corolla touring sports specifications"}),
                   _call("b", "fetch_url", {"url": EU})),
              turn(_call("c", "get_cached_document", {"key": EU_DOC}),                              # cached reread
                   _call("d", "fetch_url", {"url": EU})),                                            # same page
              turn(_call("e", "search_web", {"query": "corolla touring sports specifications"})),     # replay
              say({"summary": "never reached", "fields": {}}), say({"summary": "final", "fields": {}})]
    result, events, client, _ = research_run(tmp_path, script, EU_ROUTE, max_steps=6, **GATE_OFF)
    assert result["stop_reason"] == "no_new_artifact" and result["research_steps"] == 3
    assert [bool(t["artifacts"]) for t in result["primary_research"]["turn_artifacts"]] == [True, False, False]
    assert any(e["kind"] == "tool_reused" and e["name"] == "search_web" for e in events)


def test_no_artifact_stop_is_configurable_and_zero_disables_it(tmp_path):
    script = [turn(_call("a", "fetch_url", {"url": EU}))] + [
        turn(_call(f"f{i}", "find_in_document", {"document_id": EU_DOC, "query": q}))
        for i, q in enumerate(["Fuel tank", "Kerb weight", "Top speed"])] + [say({"summary": "s", "fields": {}})]
    result, _, _, _ = research_run(tmp_path, script, EU_ROUTE, max_steps=6, primary_research_no_artifact_stop=0)
    assert result["stop_reason"] == "model_finished" and result["research_steps"] == 5


def test_default_runtime_configuration(monkeypatch):
    for name in ("AGENT_MAX_STEPS", "PRIMARY_RESEARCH_MAX_TURNS", "PRIMARY_RESEARCH_NO_ARTIFACT_STOP",
                 "PRIMARY_RESEARCH_MIN_USEFUL_DOCUMENTS", "PRIMARY_RESEARCH_CANDIDATE_FIELD_COVERAGE_THRESHOLD",
                 "FIELD_RECOVERY_MAX_TOTAL_STEPS", "GLM_THINKING", "PRIMARY_RESEARCH_MIN_BASE_DOCUMENTS",
                 "PRIMARY_RESEARCH_MIN_BASE_SCOPED_COVERAGE", "PRIMARY_RESEARCH_HARD_MAX_TURNS"):
        monkeypatch.delenv(name, raising=False)
    cfg = agent_config_from_env()
    assert (cfg.max_steps, cfg.primary_research_no_artifact_stop) == (6, 2)
    assert (cfg.primary_research_min_useful_documents, cfg.primary_research_candidate_field_coverage_threshold) == (0, 0)
    assert cfg.field_recovery_max_total_steps == 24 and cfg.thinking == "" and cfg.phase_settings == {}
    assert (cfg.primary_research_min_base_documents, cfg.primary_research_min_base_scoped_coverage,
            cfg.primary_research_hard_max_turns) == (3, 50.0, 12)
    monkeypatch.setenv("PRIMARY_RESEARCH_MAX_TURNS", "8")
    monkeypatch.setenv("AGENT_MAX_STEPS", "30")
    monkeypatch.setenv("PRIMARY_RESEARCH_CANDIDATE_FIELD_COVERAGE_THRESHOLD", "0.6")
    monkeypatch.setenv("PRIMARY_RESEARCH_MIN_USEFUL_DOCUMENTS", "3")
    cfg = agent_config_from_env()
    assert cfg.max_steps == 8 and cfg.primary_research_min_useful_documents == 3
    assert cfg.primary_research_candidate_field_coverage_threshold == 0.6


def test_acquisition_sufficiency_is_optional_and_scheduling_only():
    snap = {"useful_documents": ["d1", "d2", "d3"], "covered_fields": {f"f{i}" for i in range(6)},
            "applicable_fields": 10}
    assert sufficient(snap, 3, 60) and sufficient(snap, 3, 0.6)
    assert not sufficient(snap, 0, 60) and not sufficient(snap, 3, 0)            # disabled when either is 0
    assert not sufficient(snap, 4, 60) and not sufficient(snap, 3, 70)


def test_acquisition_sufficiency_ends_research_without_touching_field_states(tmp_path):
    script = [turn(_call("a", "fetch_url", {"url": EU})), turn(_call("b", "fetch_url", {"url": tail.CARTUBE})),
              say({"summary": "never reached", "fields": {}}), say({"summary": "final", "fields": {}})]
    routes = {**EU_ROUTE, tail.CARTUBE: (tail.CARTUBE_TEXT, "text/plain", 200)}
    result, events, _, _ = research_run(tmp_path, script, routes, max_steps=6,
                                        primary_research_min_useful_documents=2,
                                        primary_research_candidate_field_coverage_threshold=50, **GATE_OFF)
    assert result["stop_reason"] == "acquisition_sufficient" and result["research_steps"] == 2
    assert result["status"] == "acquisition_sufficient_finalized" and result["evidence"] == []
    assert all(s["state"] != "ok" for s in result["research_bundle"]["field_states"].values())


def test_source_priority_is_an_order_never_an_allowlist():
    toyota = "טויוטה"
    importer = source_priority("https://www.toyota.co.il/corolla", toyota)
    maker = source_priority("https://www.toyota-europe.com/corolla/specs", toyota)
    pdf = source_priority("https://www.toyota-europe.com/corolla/specs.pdf", toyota)
    forum = source_priority("https://www.example-forum.net/corolla", toyota)
    assert importer["priority_technical"] < maker["priority_technical"] < pdf["priority_technical"] \
        < forum["priority_technical"]
    assert importer["priority_commercial"] == 1 and maker["priority_commercial"] > importer["priority_commercial"]
    assert forum["priority_technical"] == 7            # still a rank, never excluded


def test_research_search_results_carry_acquisition_priority_and_the_prompt_is_acquisition(tmp_path):
    script = [turn(_call("a", "search_web", {"query": "corolla specifications"})), say({"summary": "s", "fields": {}})]
    _, events, client, _ = research_run(tmp_path, script, EU_ROUTE)
    result = next(e["result"] for e in events if e["kind"] == "tool_result" and e["name"] == "search_web")
    assert result["results"][0]["acquisition_priority"]["source_class"] == "official_manufacturer"
    prompt = research_system_prompt()
    assert "SOURCE ACQUISITION" in prompt and "inspect_document_for_fields" in prompt
    assert "find_in_document(document_id, query) for specific values" not in prompt
    assert "6 model turns" in client.requests[0]["messages"][1]["content"]


# --- deterministic batch document inspection -------------------------------------------------------------------

def _eu_cache(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    doc = cache_source(cache, EU, tail.EU_SPEC_HTML, doc_type="html")
    return cache, doc


def test_one_inspection_returns_several_fields_with_offset_provenance_and_no_model(tmp_path):
    cache, doc = _eu_cache(tmp_path)
    fields = ["fuel_tank_l", "curb_weight_kg", "top_speed_kmh", "cargo_volume_l", "ground_clearance_mm"]
    out = inspect_document(cache, doc, SPECS, fields)
    text = cache.read_text(doc)
    assert out["model_calls"] == 0 and out["document_id"] == doc
    assert {"fuel_tank_l", "curb_weight_kg", "top_speed_kmh", "cargo_volume_l"} <= set(out["matches"])
    assert out["fields_without_matches"] == ["ground_clearance_mm"]
    for items in out["matches"].values():
        for m in items:
            assert text[m["offset"]:m["end"]] == m["snippet"] and m["offset"] <= m["label_offset"] < m["end"]
    assert out["candidates"]["fuel_tank_l"][0]["value"] == 43


def test_overlapping_snippets_of_one_field_are_merged(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    doc = cache_source(cache, "https://x.example/t", "Maximum torque 142 Nm (torque of the engine), peak torque "
                                                     "at 4000 rpm\nOther line\n" + "filler " * 80 + "\nTorque: 185 Nm")
    out = inspect_document(cache, doc, SPECS, ["torque_nm"], max_matches_per_field=5)
    spans = [(m["offset"], m["end"]) for m in out["matches"]["torque_nm"]]
    assert len(spans) == 2                                         # three labels on line 1 -> one snippet
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))     # never overlapping


def test_inspection_tool_creates_no_evidence_and_changes_no_field_state(tmp_path, make_ctx):
    ctx = make_ctx()
    doc = cache_source(ctx.cache, EU, tail.EU_SPEC_HTML, doc_type="html")
    log = RunLog(tmp_path / "runs", "b", "1")
    events = read_events(log.events_path) if log.events_path.exists() else []
    before = current_evaluation(events, SPECS, "IL")
    out = dispatch(ctx, "inspect_document_for_fields", {"document_id": doc,
                                                        "fields": ["fuel_tank_l", "curb_weight_kg"]})
    assert set(out["matches"]) == {"fuel_tank_l", "curb_weight_kg"} and ctx.evidence.items == []
    assert current_evaluation(events, SPECS, "IL") == before
    assert ctx.session.calls == [] and ctx.counters["search_api_calls"] == 0
    assert dispatch(ctx, "inspect_document_for_fields", {"document_id": "d_missing"})["error"] == "DocumentNotFound"


# --- the local-first, adaptive document sweep ------------------------------------------------------------------

def _matrix_events(cache, doc):
    from src.candidate_harvest import harvest_document

    cands, _ = harvest_document(cache, doc, SPECS)
    return [{"kind": "candidates_harvested", "document_id": doc, "candidates": cands}]


def test_settled_fields_are_removed_before_the_sweep(tmp_path):
    cache, doc = _eu_cache(tmp_path)
    events = _matrix_events(cache, doc)
    evaluation = [{"field": s["name"], "state": "ok" if s["name"] == "fuel_tank_l" else "missing",
                   "retry_eligible": s["name"] != "fuel_tank_l"} for s in SPECS if s.get("applicable", True)]
    matrix = candidate_matrix(events, SPECS)
    packet = sweep_packet(payload=PAYLOAD, specs=SPECS, evaluation=evaluation, matrix=matrix, events=events,
                          doc_metas=[], target_market="IL", max_turns=2)
    assert "fuel_tank_l" not in packet["fields_to_review"] and "fuel_tank_l" not in packet["requested_fields"]
    assert "fuel_tank_l" not in packet["deterministic_candidates"]
    assert "wheelbase_mm" in packet["deterministic_candidates"]


def _build(cache, doc, per_field=3):
    events = _matrix_events(cache, doc)
    matrix = candidate_matrix(events, SPECS)
    evaluation = [{"field": s["name"], "state": "missing", "retry_eligible": True} for s in SPECS
                  if s.get("applicable", True)]
    return lambda fields: sweep_packet(payload=PAYLOAD, specs=SPECS, evaluation=evaluation, matrix=matrix,
                                       events=events, doc_metas=[], target_market="IL", max_turns=2,
                                       per_field=per_field, max_chars=10 ** 9, fields=fields), matrix, evaluation


def test_a_small_packet_stays_one_sweep(tmp_path):
    cache, doc = _eu_cache(tmp_path)
    build, _, evaluation = _build(cache, doc)
    fields = [e["field"] for e in evaluation]
    chunks = plan_chunks(fields, SPECS, build, {"chars": 28000, "fields": 40, "candidates": 48})
    assert len(chunks) == 1 and chunks[0]["fields"] == fields


def test_a_large_packet_chunks_deterministically_by_recovery_cluster_without_dropping_anything(tmp_path):
    from src.tail_planner import cluster_of

    cache, doc = _eu_cache(tmp_path)
    build, matrix, evaluation = _build(cache, doc)
    fields = [e["field"] for e in evaluation]
    limits = {"chars": 0, "fields": 12, "candidates": 0}
    chunks = plan_chunks(fields, SPECS, build, limits)
    assert len(chunks) >= 3 and chunks == plan_chunks(fields, SPECS, build, limits)         # deterministic
    flat = [f for c in chunks for f in c["fields"]]
    assert sorted(flat) == sorted(fields) and len(flat) == len(set(flat))                   # no field dropped / doubled
    by_name = {s["name"]: s for s in SPECS}
    for chunk in chunks:
        assert {cluster_of(by_name[f]) for f in chunk["fields"]} <= set(chunk["clusters"])
        assert len(chunk["fields"]) <= 12
    # chunking only shapes packets: every stored candidate is still there
    assert candidate_matrix(_matrix_events(cache, doc), SPECS)["candidate_count"] == matrix["candidate_count"]


def test_the_candidate_presentation_limit_shapes_the_packet_not_candidate_storage(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    doc = cache_source(cache, "https://x.example/t", "Toyota Corolla Touring Sports 2024\nTorque: 142 Nm\n"
                                                     "Max torque: 185 Nm\nPeak torque: 205 Nm")
    build, matrix, _ = _build(cache, doc, per_field=1)
    packet = build(["torque_nm"])
    stored = len(matrix["fields"]["torque_nm"])
    assert stored >= 3 and len(packet["deterministic_candidates"]["torque_nm"]) == 1
    assert packet["candidates_not_shown"]["torque_nm"] == stored - 1
    assert len(candidate_matrix(_matrix_events(cache, doc), SPECS)["fields"]["torque_nm"]) == stored


def test_routing_is_presentation_priority_only():
    official = {"source_authority": "official_manufacturer", "binding_level": "exact_technical_variant"}
    aggregator = {"source_authority": "aggregator", "binding_level": "model_family"}
    a = {"field": "torque_nm", "value": 185, "document_id": "agg", "parser_confidence": 0.85, "occurrences": 9}
    b = {"field": "torque_nm", "value": 142, "document_id": "off", "parser_confidence": 0.85, "occurrences": 1}
    a2 = {**a, "document_id": "agg2"}
    # agreement between sources / repeated occurrences never raise a candidate: no majority vote
    assert routing_score(a, aggregator, "IL") == routing_score({**a, "occurrences": 1}, aggregator, "IL")
    ordered = route_candidates([a, a2, b], {"agg": aggregator, "agg2": aggregator, "off": official}, "IL")
    assert [c["value"] for c in ordered] == [142, 185, 185]           # official first; distinct values first
    key = lambda c: json.dumps(c, sort_keys=True)  # noqa: E731
    assert sorted(map(key, ordered)) == sorted(map(key, [a, a2, b]))  # nothing dropped, nothing changed
    assert all("routing" not in c and "score" not in c for c in ordered)     # candidates are not annotated


class SweepGLM(ScriptedGLM):
    model = "glm-5.3-flash"

    def __init__(self, research, sweep=None, cluster=None):
        super().__init__(research)
        self.sweep_fn = sweep or (lambda packet, messages: say({"reviewed": []}))
        self.cluster_fn = cluster
        self.sweep_packets, self.cluster_packets = [], []

    def chat(self, messages, tools=None, **kwargs):
        system = messages[0]["content"]
        self.requests.append({"messages": messages, "tools": tools, **kwargs})
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
        if system in (research_system_prompt(), research_system_prompt("contract")):
            return ChatResponse(message=self.messages.pop(0), finish_reason="stop", usage=usage)
        packet = json.loads(messages[1]["content"].split("\n", 1)[1]) if "(JSON)" in messages[1]["content"] else {}
        if system == DOCUMENT_SWEEP_SYSTEM_PROMPT:
            self.sweep_packets.append(packet)
            return ChatResponse(message=self.sweep_fn(packet, messages), finish_reason="stop", usage=usage)
        if "cluster" in packet:
            self.cluster_packets.append(packet)
            message = self.cluster_fn(packet, messages) if self.cluster_fn else say(
                {"cluster": packet["cluster"], "fields": [{"field": f["field"], "status": "unresolved"}
                                                          for f in packet["fields"]]})
            return ChatResponse(message=message, finish_reason="stop", usage=usage)
        return ChatResponse(message=say({"summary": "final", "fields": {}}), finish_reason="stop", usage=usage)


def sweep_run(tmp_path, client, routes=None, **cfg):
    from conftest import FakeSession

    routes = {url: FakeResponse(body.encode("utf-8"), content_type=ctype, url=url)
              for url, (body, ctype) in (routes or tail.ROUTES).items()}
    cache = DocumentCache(tmp_path / "cache")
    log = RunLog(tmp_path / "runs", "b", "38626")
    config = AgentConfig(**{"research_memory_enabled": False, "requested_fields": tail.FIELDS,
                            "primary_research_no_artifact_stop": 0, **cfg})
    result = run_vehicle({"upstream_record_id": "38626"}, PAYLOAD, client=client, cache=cache, run_log=log,
                         vehicle_meta=VEHICLE, config=config, tool_config=ToolConfig(), session=FakeSession(routes))
    return result, read_events(log.events_path)


PRIMARY_FETCH = [turn(_call("a", "fetch_url", {"url": EU}), _call("b", "fetch_url", {"url": tail.CARTUBE}))]


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_sweep_is_local_first_and_settled_fields_never_reach_it(tmp_path):
    store = tail._store("s", "torque_nm", 142, tail.CARTUBE, "מומנט מנוע בנזין: 142 ניוטון-מטר", "Nm")
    client = SweepGLM(PRIMARY_FETCH + [turn(store), say({"summary": "p", "fields": {}})])
    result, events = sweep_run(tmp_path, client, **SINGLE_PACKET_SWEEP)
    assert result["research_bundle"]["field_states"]["torque_nm"]["state"] == "ok"
    packet = client.sweep_packets[0]
    assert "torque_nm" not in packet["fields_to_review"]                       # settled before the sweep
    plan = next(e for e in events if e["kind"] == "document_sweep_plan")
    assert "torque_nm" in plan["removed_settled"] or "torque_nm" not in plan["fields"]
    pre = next(e for e in events if e["kind"] == "document_inspection")
    assert pre["model_calls"] == 0 and pre["stage"] == "pre_sweep"
    kinds = [e["kind"] for e in events]
    assert kinds.index("document_inspection") < kinds.index("document_sweep_started")
    # ground clearance has no label anywhere in the cached documents: the sweep is told not to search for it
    assert "ground_clearance_mm" in packet.get("fields_not_found_locally", [])
    sweep = result["document_sweep"]
    assert sweep["document_sweep_calls"] == 1 and sweep["document_sweep_chunks"] == 1
    assert sweep["document_sweep_packet_chars"] > 0 and sweep["document_sweep_estimated_input_tokens"] > 0
    assert sweep["document_sweep_timeouts"] == 0 and sweep["document_sweep_output_tokens"] == 10
    m = compute_metrics(result)
    assert m["document_sweep_calls"] == 1 and m["primary_research_turns"] == 3
    assert m["primary_research_stop_reason"] == "model_finished" and m["primary_research_documents_added"] == 2


def test_chunked_sweep_reevaluates_between_chunks_and_a_reply_is_never_a_state(tmp_path):
    def sweep(packet, messages):
        # chunk 1 promotes the EU fuel tank and claims (without evidence) that wheelbase is settled
        cands = packet["deterministic_candidates"]
        calls = [_call(f"s{f}", "store_evidence", {"field": f, "value": c[0]["value"], "unit": c[0].get("unit"),
                                                   "document_id": c[0]["document_id"], "quote": c[0]["quote"],
                                                   "market": "EU", "variant_match": "exact"})
                 for f, c in cands.items() if f == "fuel_tank_l"]
        if calls:
            return turn(*calls)
        return say({"reviewed": [{"field": "wheelbase_mm", "decision": "promoted"}]})

    client = SweepGLM(PRIMARY_FETCH + [say({"summary": "p", "fields": {}})], sweep=sweep)
    result, events = sweep_run(tmp_path, client, document_sweep_max_fields=4)
    sweep_summary = result["document_sweep"]
    chunks = [c for c in sweep_summary["document_sweep_chunk_details"] if c.get("model_calls")]
    assert len(chunks) >= 2 and all(c["fields_count"] <= 4 for c in chunks)
    seen = [f for p in client.sweep_packets for f in p["fields_to_review"]]
    assert len(seen) == len(set(seen))                                          # every field reviewed once at most
    assert all(p["chunk"]["of"] == len(sweep_summary["document_sweep_chunk_details"]) for p in client.sweep_packets)
    states = result["research_bundle"]["field_states"]
    assert states["wheelbase_mm"]["state"] != "ok"                              # a model reply is not evidence


def test_recovery_still_runs_breadth_first_and_harvests_new_documents_for_all_open_fields(tmp_path):
    def cluster(packet, messages):
        if packet["mode"] == "web" and not any(m["role"] == "tool" for m in messages):
            return turn(_call("w", "fetch_url", {"url": tail.FORUM}))
        return say({"cluster": packet["cluster"], "fields": [{"field": f["field"], "status": "unresolved"}
                                                             for f in packet["fields"]]})

    client = SweepGLM([turn(_call("b", "fetch_url", {"url": tail.CARTUBE})), say({"summary": "p", "fields": {}})],
                      cluster=cluster)
    result, events = sweep_run(tmp_path, client)
    rec = result["field_recovery"]
    assert rec["queue"] and rec["attempt_count"] > 0 and rec["order"] == "breadth_first_clusters"
    first = [a for a in rec["attempts"] if a["attempt"] == 1]
    assert [a["cluster"] for a in rec["attempts"][:len(first)]] == [a["cluster"] for a in first]
    harvested = [e for e in events if e["kind"] == "candidates_harvested" and e["phase"] == "field_recovery"]
    assert harvested                                                            # the forum page, for ALL fields
    assert rec["field_recovery_turn_budget"] == 24


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_no_paid_stage_runs_when_nothing_is_open(tmp_path):
    fields = ["torque_nm"]
    store = tail._store("s", "torque_nm", 142, tail.CARTUBE, "מומנט מנוע בנזין: 142 ניוטון-מטר", "Nm")
    client = SweepGLM([turn(_call("b", "fetch_url", {"url": tail.CARTUBE})), turn(store),
                       say({"summary": "p", "fields": {}})])
    result, events = sweep_run(tmp_path, client, requested_fields=fields)
    assert result["document_sweep"]["skipped"] == "no_unresolved_fields" and client.sweep_packets == []
    assert result["usage_field_recovery"]["model_calls"] == 0 and result["usage_document_sweep"]["model_calls"] == 0
    assert not any(e["kind"] == "document_inspection" for e in events)


# --- phase-specific model settings --------------------------------------------------------------------------------

class Recorder:
    model = "glm-5.3-flash"

    def __init__(self):
        self.kwargs = []

    def chat(self, messages, **kwargs):
        self.kwargs.append(kwargs)
        return ChatResponse(message={"role": "assistant", "content": "{}"}, finish_reason="stop", usage={})


def _caller(tmp_path, config):
    client = Recorder()
    return ModelCaller(client, RunLog(tmp_path, "b", "1"), config), client


def test_phase_settings_default_to_the_global_configuration(tmp_path):
    caller, client = _caller(tmp_path, AgentConfig(thinking="enabled", max_tokens=900, temperature=0.2))
    for phase in ("research", "document_sweep", "field_recovery", "finalization"):
        caller([{"role": "user", "content": "x"}], phase=phase)
    base = {"tools": None, "temperature": 0.2, "max_tokens": 900, "extra": {"thinking": {"type": "enabled"}}}
    # no model / timeout override sent; the document sweep's only built-in default is one HTTP attempt (no retry)
    assert client.kwargs == [base, {**base, "max_attempts": 1}, base, base]


def test_phase_settings_override_only_their_own_phase(tmp_path):
    config = AgentConfig(thinking="enabled", phase_settings={
        "document_sweep": {"thinking": "disabled", "timeout_s": 90, "model": "glm-5.3-flashx"},
        "finalizer": {"max_tokens": 2000, "temperature": 0.0}})
    caller, client = _caller(tmp_path, config)
    caller([{"role": "user", "content": "x"}], phase="research")
    caller([{"role": "user", "content": "x"}], phase="document_sweep")
    caller([{"role": "user", "content": "x"}], phase="finalization", model="glm-5.3")
    research, sweep, final = client.kwargs
    assert research["extra"] == {"thinking": {"type": "enabled"}} and "timeout_s" not in research
    assert sweep["extra"] == {"thinking": {"type": "disabled"}} and sweep["timeout_s"] == 90
    assert sweep["model"] == "glm-5.3-flashx"
    assert final["max_tokens"] == 2000 and final["temperature"] == 0.0 and final["model"] == "glm-5.3"
    assert for_phase(config, "field_recovery")["thinking"] == "enabled"         # recovery inherits


def test_phase_settings_from_env():
    env = {"GLM_DOCUMENT_SWEEP_THINKING": "disabled", "GLM_RECOVERY_TIMEOUT_S": "120",
           "GLM_FINALIZER_MAX_TOKENS": "3000", "GLM_RESEARCH_TEMPERATURE": "0.3", "GLM_RECOVERY_MODEL": "glm-5.3",
           "GLM_RESEARCH_MODEL": "ignored", "GLM_DOCUMENT_SWEEP_THINKING_BAD": "x"}
    out = phase_settings_from_env(env.get)
    assert out == {"research": {"temperature": 0.3}, "document_sweep": {"thinking": "disabled"},
                   "recovery": {"model": "glm-5.3", "timeout_s": 120.0}, "finalizer": {"max_tokens": 3000}}


def test_glm_client_per_call_timeout_keeps_retry_semantics(monkeypatch):
    from src.glm_client import GLMClient, GLMSettings

    seen = []

    class Session:
        def post(self, url, headers=None, data=None, timeout=None):
            seen.append(timeout)
            import requests

            raise requests.ReadTimeout("slow")

    client = GLMClient(GLMSettings(api_key="k", model="m", chat_max_attempts=2), session=Session(),
                       sleeper=lambda s: None)
    with pytest.raises(GLMError):
        client.chat([{"role": "user", "content": "x"}], timeout_s=45)
    with pytest.raises(GLMError):
        client.chat([{"role": "user", "content": "x"}])
    assert seen == [(15, 45), (15, 45), (15, 240.0), (15, 240.0)]       # same attempts; only the read timeout


# --- the orchestration benchmark (offline) -------------------------------------------------------------------------

@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_corolla_orchestration_benchmark_keeps_trustworthy_coverage(tmp_path):
    from fixtures import corolla_orchestration as bench

    report = bench.benchmark(tmp_path, **SINGLE_PACKET_SWEEP)
    follows = report["policy_follows_prompt"]
    # BEFORE (origin/main at PR #22, same fixture): 12 research calls, 36 candidates on 26 fields, 2 sweep calls,
    # 15 trustworthy fields, 7 rejected evidence requests (see the PR description)
    assert follows["research_model_calls"] <= 6 and follows["find_in_document_calls_research"] == 0
    assert follows["candidate_count"] >= 36 and follows["candidate_fields"] >= 26
    assert follows["final_trustworthy_fields"] >= 15 and follows["final_polluted_fields"] == []
    assert follows["evidence_rejected"] <= 7 and follows["document_sweep_calls"] <= 2
    ignores = report["policy_ignores_prompt"]
    # the defensive path: a research model that keeps doing per-field Ctrl+F is kept acquiring by the minimum
    # acquisition base until the source set is no longer thin, so trustworthy coverage stays at the baseline
    assert ignores["final_trustworthy_fields"] >= 15 and ignores["final_polluted_fields"] == []
    assert ignores["documents_fetched"] == 6 and ignores["candidate_fields"] >= 26
    assert ignores["research_model_calls"] < 12 and ignores["evidence_rejected"] <= 7
    assert re.match(r"no_new_artifact|max_turns", ignores["primary_research_stop_reason"])


# --- audit follow-ups ---------------------------------------------------------------------------------------------

def test_fields_without_a_dictionary_entry_are_never_reported_as_not_found_locally(tmp_path):
    custom = {"name": "engine_oil_capacity_l", "description": "engine oil capacity", "group": "technical"}
    client = SweepGLM(PRIMARY_FETCH + [say({"summary": "p", "fields": {}})])
    sweep_run(tmp_path, client, requested_fields=["ground_clearance_mm", custom])
    packet = client.sweep_packets[0]
    assert "engine_oil_capacity_l" in packet["fields_without_candidates"]
    assert packet.get("fields_not_found_locally") == ["ground_clearance_mm"]


def test_an_unpriced_phase_model_that_was_never_called_does_not_null_the_cost():
    from src.pricing import default_pricing, phase_run_cost

    used = {"prompt_tokens": 1000, "completion_tokens": 100, "model_calls": 1}
    cost, details = phase_run_cost(usage_research=used, usage_sweep={}, usage_recovery=used, usage_finalizer={},
                                   search_api_calls=2, pricing=default_pricing("glm-5.3-flash"),
                                   phase_models={"document_sweep": "no-price-model", "field_recovery": "glm-5.3"})
    assert cost["tokens_usd"] is not None and "document_sweep_model" not in details
    assert details["field_recovery_model"] == "glm-5.3"
    expected = round((1000 * 0.15 + 100 * 0.5) / 1e6 + (1000 * 1.40 + 100 * 4.40) / 1e6, 6)
    assert cost["tokens_usd"] == expected


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_an_acquisition_snapshot_failure_never_fails_research(tmp_path, monkeypatch):
    from src import acquisition

    def broken(**kwargs):
        raise RuntimeError("snapshot broke")

    monkeypatch.setattr(acquisition, "snapshot", broken)
    script = [turn(_call("a", "fetch_url", {"url": EU})), say({"summary": "s", "fields": {}})]
    result, events, _, _ = research_run(tmp_path, script, EU_ROUTE)
    assert result["status"] == "completed" and result["stop_reason"] == "model_finished"
    assert any(e["kind"] == "primary_research_turn_unmeasured" for e in events)


def test_a_zero_packet_char_limit_means_no_limit(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    doc = cache_source(cache, "https://x.example/t", "Torque: 142 Nm\nMax torque: 185 Nm\nPeak torque: 205 Nm")
    events = _matrix_events(cache, doc)
    evaluation = [{"field": "torque_nm", "state": "missing", "retry_eligible": True}]
    packet = sweep_packet(payload=PAYLOAD, specs=SPECS, evaluation=evaluation, matrix=candidate_matrix(events, SPECS),
                          events=events, doc_metas=[], target_market="IL", max_turns=2, per_field=3, max_chars=0)
    assert len(packet["deterministic_candidates"]["torque_nm"]) == 3


def test_a_large_inspection_result_stays_valid_and_names_what_it_left_out(tmp_path, make_ctx):
    ctx = make_ctx(max_text_chars=1500)
    doc = cache_source(ctx.cache, EU, tail.EU_SPEC_HTML, doc_type="html")
    out = dispatch(ctx, "inspect_document_for_fields", {"document_id": doc})
    out.pop("_elapsed_ms", None)
    assert len(json.dumps(out, ensure_ascii=False)) <= int(1500 * 1.4)
    assert out.get("fields_omitted_for_size") and "hint" in out


# --- fail-safe: the minimum acquisition base -------------------------------------------------------------------------

STALL = [turn(_call("d", "find_in_document", {"document_id": EU_DOC, "query": "Fuel tank"})),
         turn(_call("e", "find_in_document", {"document_id": EU_DOC, "query": "Top speed"}))]


def test_two_stalled_turns_with_enough_material_may_stop(tmp_path):
    script = [FETCH_THREE] + STALL + [say({"summary": "never reached", "fields": {}}), say({"summary": "f", "fields": {}})]
    result, events, _, _ = research_run(tmp_path, script, RICH_ROUTES, max_steps=6)     # default gate
    primary = result["primary_research"]
    assert result["stop_reason"] == "no_new_artifact" and result["research_steps"] == 3
    assert primary["minimum_acquisition_met"] and primary["stop_deferred_count"] == 0
    assert primary["minimum_acquisition"]["useful_documents"] == 3
    assert primary["minimum_acquisition"]["scoped_coverage_pct"] >= 50 and primary["extended_turns"] == 0


def test_two_stalled_turns_on_a_thin_source_set_do_not_stop(tmp_path):
    script = [turn(_call("a", "fetch_url", {"url": EU}))] + STALL + [
        turn(_call("f", "fetch_url", {"url": tail.CARTUBE}), _call("g", "fetch_url", {"url": tail.LAUNCH})),
        say({"summary": "done", "fields": {}})]
    result, events, client, _ = research_run(tmp_path, script, RICH_ROUTES, max_steps=6)
    primary = result["primary_research"]
    assert result["stop_reason"] == "model_finished" and result["research_steps"] == 5   # kept alive, then acquired
    deferred = [e for e in events if e["kind"] == "primary_research_stop_deferred"]
    assert [(e["turn"], e["wanted_stop"]) for e in deferred] == [(3, "no_new_artifact")]
    assert deferred[0]["useful_documents"] == 1 and deferred[0]["scoped_coverage_pct"] < 50
    assert primary["stop_deferred_count"] == 1 and primary["minimum_acquisition_met"]
    note = client.requests[3]["messages"][-1]["content"]          # the model is told why it continues
    assert "source set is still thin" in note and "Acquire NEW" in note


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_an_under_acquired_run_continues_until_the_hard_ceiling(tmp_path):
    stall = [turn(_call(f"s{i}", "find_in_document", {"document_id": EU_DOC, "query": f"label {i}"}))
             for i in range(8)]
    script = [turn(_call("a", "fetch_url", {"url": EU}))] + stall + [say({"summary": "final", "fields": {}})]
    result, events, client, _ = research_run(tmp_path, script, EU_ROUTE, max_steps=3,
                                             primary_research_hard_max_turns=5)
    primary = result["primary_research"]
    assert result["research_steps"] == 5 and result["stop_reason"] == "max_steps"          # hard ceiling holds
    assert primary["stop_reason"] == "hard_max_turns_under_acquired" and not primary["minimum_acquisition_met"]
    assert primary["extended_turns"] == 2 and primary["under_acquired_turns"] == 5
    # the normal ceiling (turn 3) and turn 4 are deferred; turn 5 is the hard ceiling and ends research
    assert [(d["turn"], d["wanted_stop"]) for d in primary["stops_deferred"]] == [(3, "max_turns"), (4, "max_turns")]
    assert result["usage_research"]["model_calls"] == 5
    m = compute_metrics(result)
    assert (m["primary_research_minimum_acquisition_met"], m["primary_research_stop_deferred_count"],
            m["primary_research_under_acquired_turns"], m["primary_research_extended_turns"]) == (False, 2, 5, 2)
    assert m["primary_research_stop_reason"] == "hard_max_turns_under_acquired"


def test_extension_ends_as_soon_as_the_base_is_met_and_a_normal_run_keeps_the_normal_ceiling(tmp_path):
    script = [turn(_call("a", "fetch_url", {"url": EU})), STALL[0],
              turn(_call("f", "fetch_url", {"url": tail.CARTUBE}), _call("g", "fetch_url", {"url": tail.LAUNCH})),
              turn(_call("h", "find_in_document", {"document_id": EU_DOC, "query": "Height"})),
              say({"summary": "never reached", "fields": {}}), say({"summary": "final", "fields": {}})]
    result, _, _, _ = research_run(tmp_path, script, RICH_ROUTES, max_steps=2)
    primary = result["primary_research"]
    # turn 2 (the normal ceiling) is under-acquired -> extended; turn 3 meets the base -> stop at once
    assert result["research_steps"] == 3 and primary["stop_reason"] == "max_turns" and primary["extended_turns"] == 1
    rich = [FETCH_THREE, STALL[0], say({"summary": "never reached", "fields": {}}), say({"summary": "f", "fields": {}})]
    result, _, _, _ = research_run(tmp_path / "rich", rich, RICH_ROUTES, max_steps=2)
    assert result["research_steps"] == 2 and result["primary_research"]["extended_turns"] == 0


def test_new_in_scope_material_after_stalled_turns_resets_and_meets_the_base(tmp_path):
    script = [turn(_call("a", "fetch_url", {"url": EU}))] + STALL + [
        turn(_call("f", "fetch_url", {"url": tail.CARTUBE}), _call("g", "fetch_url", {"url": tail.LAUNCH}))] + [
        turn(_call(f"x{i}", "find_in_document", {"document_id": EU_DOC, "query": f"label {i}"})) for i in range(2)] + [
        say({"summary": "never reached", "fields": {}}), say({"summary": "final", "fields": {}})]
    result, events, _, _ = research_run(tmp_path, script, RICH_ROUTES, max_steps=8)
    turns = [e for e in events if e["kind"] == "primary_research_turn"]
    assert [t["no_artifact_streak"] for t in turns] == [0, 1, 2, 0, 1, 2]
    assert [t["minimum_acquisition_met"] for t in turns] == [False, False, False, True, True, True]
    assert turns[3]["scoped_coverage_pct"] > turns[2]["scoped_coverage_pct"]
    assert result["stop_reason"] == "no_new_artifact" and result["research_steps"] == 6   # allowed once the base holds


def test_failures_rereads_and_replays_are_still_no_progress_while_under_acquired(tmp_path):
    routes = {**EU_ROUTE, "https://www.toyota.co.il/blocked": ("Access denied", "text/html", 403)}
    script = [turn(_call("a", "search_web", {"query": "corolla touring sports specifications"}),
                   _call("b", "fetch_url", {"url": EU})),
              turn(_call("c", "fetch_url", {"url": "https://www.toyota.co.il/blocked"}),
                   _call("d", "fetch_url", {"url": "https://www.example.com/missing"}),
                   _call("e", "search_web", {"query": "corolla 429 specifications"})),
              turn(_call("f", "get_cached_document", {"key": EU_DOC}),
                   _call("g", "search_web", {"query": "corolla touring sports specifications"})),
              say({"summary": "final", "fields": {}})]
    result, events, _, _ = research_run(tmp_path, script, routes, max_steps=6)
    turns = [e for e in events if e["kind"] == "primary_research_turn"]
    assert [bool(t["artifacts"]) for t in turns] == [True, False, False]
    assert [t["no_artifact_streak"] for t in turns] == [0, 1, 2]
    assert [e["wanted_stop"] for e in events if e["kind"] == "primary_research_stop_deferred"] == ["no_new_artifact"]
    assert result["stop_reason"] == "model_finished" and result["research_steps"] == 4


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_the_safety_gate_never_touches_evidence_state_binding_or_conflicts(tmp_path):
    from src.acquisition import minimum_base

    snap = {"useful_documents": ["d1"], "target_market_documents": [], "applicable_fields": 10,
            "scoped_fields": {"torque_nm"}}
    frozen = json.dumps(snap, default=sorted, sort_keys=True)
    assert minimum_base(snap, 3, 50)[0] is False and minimum_base(snap, 0, 0)[0] is True
    assert json.dumps(snap, default=sorted, sort_keys=True) == frozen                  # pure
    stall = [turn(_call(f"s{i}", "find_in_document", {"document_id": EU_DOC, "query": f"label {i}"}))
             for i in range(4)]
    script = [turn(_call("a", "fetch_url", {"url": EU}))] + stall + [say({"summary": "final", "fields": {}})]
    result, events, _, _ = research_run(tmp_path, script, EU_ROUTE, max_steps=3, primary_research_hard_max_turns=5)
    assert result["primary_research"]["stop_deferred_count"] >= 2
    assert result["evidence"] == [] and not [e for e in events if e["kind"] in ("evidence", "field_status")]
    states = {f: s["state"] for f, s in result["research_bundle"]["field_states"].items()}
    assert states == {e["field"]: e["state"] for e in current_evaluation(events, resolve_requested_fields(
        tail.FIELDS, propulsion="hybrid"), "IL")}
    assert set(states.values()) <= {"missing", "not_applicable"}
    assert [f for f, v in states.items() if v == "not_applicable"] == [
        f for f, v in {e["field"]: e["state"] for e in current_evaluation([], resolve_requested_fields(
            tail.FIELDS, propulsion="hybrid"), "IL")}.items() if v == "not_applicable"]    # no N/A from the gate
    deferred = [e for e in events if e["kind"] == "primary_research_stop_deferred"]
    assert all(not ({"state", "evidence", "binding_level", "conflict_class"} & set(e)) for e in deferred)
