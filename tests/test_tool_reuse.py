"""Exact-repeat suppression before dispatch, novelty semantics, recovery operation memory and the
dynamic field-retry queue. Scripted GLM / fake HTTP only: no network, no paid calls."""

import json
from collections import Counter

import pytest

from conftest import FakeResponse, cache_source
from test_tools_smoke import ScriptedGLM, _call

import src.agent as agent_mod
from src.agent import AgentConfig, run_vehicle
from src.benchmark import compute_metrics
from src.context import REPLAY_SAFE_TOOLS, ResearchTracker, call_signature
from src.field_recovery import attempted_operations
from src.storage.run_loader import load_runs
from src.storage.run_log import RunLog, read_events
from src.ui import run_view

PAGE = "https://www.cadillac.example/escalade-iq"
PAYLOAD = {"identity": {"manufacturer": "קאדילאק", "commercial_name": "ESCALADE IQ", "year": 2025,
                        "trim": "SPORT", "model_code": "6EQ26", "government_record_id": "85095"},
           "engine_drivetrain": {"propulsion_normalized": "battery_electric", "drivetrain_normalized": "awd"}}


def turn(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def say(obj):
    return {"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)}


class SearchGLM(ScriptedGLM):
    """Scripted chat plus a counting web_search (the GLM search backend)."""

    def __init__(self, messages):
        super().__init__(messages)
        self.searches = []

    def web_search(self, query, count=8, domain=None, recency="noLimit"):
        self.searches.append(query)
        return [{"title": "Escalade IQ", "url": PAGE, "snippet": "205 kWh"}]


@pytest.fixture
def counted(monkeypatch):
    """Counts real tool executions (dispatch), by name and by (name, arguments)."""
    calls = Counter()
    real = agent_mod.dispatch

    def counting(ctx, name, raw_args):
        args = raw_args if isinstance(raw_args, str) else json.dumps(raw_args, ensure_ascii=False)
        calls[name] += 1
        calls[(name, args)] += 1
        return real(ctx, name, raw_args)

    monkeypatch.setattr(agent_mod, "dispatch", counting)
    return calls


def put_doc(cache, url, text):
    return cache.put("fetch", url, text.encode("utf-8"), {"status": 200, "final_url": url, "doc_type": "text",
                                                         "content_type": "text/plain"}, text)["document_id"]


def run(tmp_path, ctx, script, client_cls=ScriptedGLM, **cfg):
    client = client_cls(script)
    config = AgentConfig(**{"field_recovery_enabled": False, "max_steps": 6, "no_new_research_turns": 0,
                            "primary_research_no_artifact_stop": 0, "recovery_mode": "legacy", **cfg})
    log = RunLog(tmp_path / "runs", "b", "85095")
    result = run_vehicle({"upstream_record_id": "85095"}, PAYLOAD, client=client, cache=ctx.cache, run_log=log,
                         config=config, tool_config=ctx.config, session=ctx.session)
    return result, client, read_events(log.events_path)


def tool_messages(client, request_index=-1):
    return [m for m in client.requests[request_index]["messages"] if m["role"] == "tool"]


def tool_json(message):
    """A tool message's JSON (an idle turn's "[operational note]" suffix is split off)."""
    return json.loads(message["content"].split("\n[operational note]")[0])


# --- 1-7: pre-dispatch reuse ---------------------------------------------------------------------

@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_identical_find_in_document_is_replayed_not_executed(tmp_path, make_ctx, counted):
    ctx = make_ctx()
    doc = put_doc(ctx.cache, PAGE, "Battery 205 kWh usable. Range 742 km.")
    find = {"document_id": doc, "query": "kWh"}
    result, client, events = run(tmp_path, ctx, [turn(_call("c1", "find_in_document", find)),
                                                 turn(_call("c2", "find_in_document", find)),
                                                 say({"summary": "s", "fields": {}})])
    assert counted["find_in_document"] == 1
    replay = tool_json(tool_messages(client)[-1])
    assert "[operational note] This turn acquired nothing new" in tool_messages(client)[-1]["content"]
    assert replay["hit_count"] == 1 and replay["reused_from_step"] == 1
    assert "previous result reused without executing the tool again" in replay["operational_note"]
    assert tool_messages(client)[-1]["tool_call_id"] == "c2"
    reused = [e for e in events if e["kind"] == "tool_reused"]
    assert len(reused) == 1 and reused[0]["original_step"] == 1 and reused[0]["step"] == 2
    assert [e["kind"] for e in events].count("tool_call") == 1  # the replay is not logged as an execution
    assert result["tool_calls"][1]["reused"] and result["tool_calls"][1]["reused_from_step"] == 1
    assert result["research_tracking"]["duplicate_inspections_suppressed"] == 1
    m = compute_metrics(result)
    assert (m["tool_calls"], m["duplicate_calls_suppressed"]) == (1, 1)


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_identical_cached_document_and_fetch_execute_once(tmp_path, make_ctx, counted):
    ctx = make_ctx({PAGE: FakeResponse(b"<html><body>Escalade IQ 205 kWh</body></html>")})
    doc = put_doc(ctx.cache, "https://other.example/spec", "Battery 205 kWh")
    script = [turn(_call("c1", "get_cached_document", {"key": doc}), _call("c2", "fetch_url", {"url": PAGE})),
              turn(_call("c3", "get_cached_document", {"key": doc, "offset": 0}),
                   _call("c4", "fetch_url", {"url": PAGE + "#specs"})),
              say({"summary": "s", "fields": {}})]
    result, client, _ = run(tmp_path, ctx, script)
    assert counted["get_cached_document"] == 1 and counted["fetch_url"] == 1
    assert len(ctx.session.calls) == 1                              # no second HTTP request
    assert len(result["documents"]) == 2
    t = result["research_tracking"]
    assert (t["duplicate_fetches_suppressed"], t["duplicate_inspections_suppressed"]) == (1, 1)
    assert "text_preview" not in tool_json(tool_messages(client)[-1])


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_identical_search_is_replayed_and_billed_once(tmp_path, make_ctx, counted):
    ctx = make_ctx(search_backend="glm")
    q = {"query": "Escalade IQ 205 kWh"}
    result, client, _ = run(tmp_path, ctx, [turn(_call("c1", "search_web", q)),
                                            turn(_call("c2", "search_web", {"query": "  Escalade IQ   205 kWh "})),
                                            say({"summary": "s", "fields": {}})], client_cls=SearchGLM)
    assert client.searches == ["Escalade IQ 205 kWh"] and counted["search_web"] == 1
    assert result["search_api_calls"] == 1 and result["counters"].get("tool:search_web") == 1
    assert result["research_tracking"]["duplicate_searches_suppressed"] == 1
    assert tool_json(tool_messages(client)[-1])["results"][0]["url"] == PAGE
    assert result["cost"]["web_search_usd"] is None or result["search_api_calls"] == 1


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_different_operations_are_never_suppressed(tmp_path, make_ctx, counted):
    ctx = make_ctx(search_backend="glm")
    doc = put_doc(ctx.cache, PAGE, "Battery 205 kWh. " + "x" * 9000)
    script = [turn(_call("c1", "find_in_document", {"document_id": doc, "query": "battery"}),
                   _call("c2", "find_in_document", {"document_id": doc, "query": "205"}),
                   _call("c3", "find_in_document", {"document_id": doc, "query": "usable capacity"}),
                   _call("c4", "find_in_document", {"document_id": doc, "query": "battery", "context_chars": 500})),
              turn(_call("c5", "extract_html", {"document_id": doc, "offset": 0}),
                   _call("c6", "extract_html", {"document_id": doc, "offset": 4000}),
                   _call("c7", "search_web", {"query": "Escalade IQ 205 kWh"}),
                   _call("c8", "search_web", {"query": "Escalade IQ usable battery capacity"}),
                   _call("c9", "search_web", {"query": "escalade iq 205 kwh"})),
              say({"summary": "s", "fields": {}})]
    result, client, events = run(tmp_path, ctx, script, client_cls=SearchGLM)
    assert counted["find_in_document"] == 4 and counted["extract_html"] == 2 and len(client.searches) == 3
    assert not [e for e in events if e["kind"] == "tool_reused"]
    assert result["research_tracking"]["duplicate_calls_suppressed"] == 0


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_store_evidence_and_failed_calls_are_not_replayed(tmp_path, make_ctx, counted):
    ctx = make_ctx()
    cache_source(ctx.cache, PAGE, "Cadillac Escalade IQ: battery 205 kWh")      # a retrieved source
    ev = {"field": "battery_gross_kwh", "value": 205, "source_url": PAGE, "quote": "205 kWh"}
    missing = {"document_id": "d_0000000000000000", "query": "kWh"}
    result, _, events = run(tmp_path, ctx, [turn(_call("c1", "store_evidence", ev), _call("c2", "find_in_document", missing)),
                                            turn(_call("c3", "store_evidence", ev), _call("c4", "find_in_document", missing)),
                                            say({"summary": "s", "fields": {}})])
    # Both store_evidence calls are dispatched (never generic replay); the evidence layer keeps one fact.
    assert counted["store_evidence"] == 2 and len(result["evidence"]) == 1
    assert counted["find_in_document"] == 2  # an error is never replayed: it may succeed later
    assert not [e for e in events if e["kind"] == "tool_reused"]
    assert call_signature("store_evidence", ev) is None and call_signature("report_field_status", {}) is None
    assert set(REPLAY_SAFE_TOOLS) == {"search_web", "search_official_domains", "fetch_url", "fetch_pdf",
                                      "render_page", "get_cached_document", "find_in_document", "extract_tables",
                                      "get_structured_data", "extract_html",
                                      "inspect_document_for_fields"}


def test_signature_is_canonical_but_conservative():
    sig = call_signature
    assert sig("find_in_document", {"document_id": "d1", "query": "Battery "}) == \
        sig("find_in_document", '{"document_id": "d1", "query": "battery", "scope": "text", "context_chars": 300}')
    assert sig("find_in_document", {"document_id": "d1", "query": "battery"}) != \
        sig("find_in_document", {"document_id": "d2", "query": "battery"})
    assert sig("extract_html", {"document_id": "d1"}) == sig("extract_html", {"document_id": "d1", "offset": 0})
    assert sig("extract_html", {"document_id": "d1", "offset": 0}) != sig("extract_html", {"document_id": "d1",
                                                                                           "offset": 4000})
    assert sig("extract_html", {"document_id": "d1", "max_chars": 2000}) != sig("extract_html", {"document_id": "d1"})
    assert sig("fetch_url", {"url": "https://a.example/x#top"}) == sig("fetch_url", {"url": " https://a.example/x "})
    assert sig("fetch_url", {"url": "https://a.example/x"}) != sig("fetch_pdf", {"url": "https://a.example/x"})
    assert sig("search_web", {"query": "G6 kWh"}) != sig("search_web", {"query": "g6 kwh"})
    assert sig("search_web", {"query": "G6"}) != sig("search_web", {"query": "G6", "domain": "xpeng.com"})
    assert sig("search_web", {"query": "G6"}) != sig("search_web", {"query": "G6", "max_results": 20})
    assert sig("search_official_domains", {"query": "G6", "domains": ["B.com", "a.com"]}) == \
        sig("search_official_domains", {"query": "G6", "domains": ["a.com", "b.com"]})
    assert sig("find_in_document", "{not json") is None and sig("find_in_document", {"query": "x"}) is None


# --- 15-19: novelty ------------------------------------------------------------------------------

def test_novelty_means_new_material_not_new_operation():
    t = ResearchTracker()
    t.begin_turn(1)
    t.observe("find_in_document", {"document_id": "d1", "query": "zzz"},
              {"document_id": "d1", "hits": [], "hit_count": 0})
    t.observe("get_structured_data", {"document_id": "d1"},
              {"document_id": "d1", "found": {"json_ld": False, "meta": False}})
    t.observe("extract_tables", {"document_id": "d1"}, {"document_id": "d1", "tables_total": 0, "tables": []})
    n = t.end_turn()
    assert (n.new_operations, n.new_material, n.total) == (3, 0, 0) and t.idle_turns == 1  # zero results: idle

    t.begin_turn(2)
    t.observe("find_in_document", {"document_id": "d1", "query": "kWh"},
              {"document_id": "d1", "hits": [{"offset": 10, "snippet": "205 kWh"}], "hit_count": 1})
    n = t.end_turn()
    assert n.new_material == 1 and t.idle_turns == 0

    t.begin_turn(3)  # another query landing on the same passage exposes nothing new
    t.observe("find_in_document", {"document_id": "d1", "query": "205"},
              {"document_id": "d1", "hits": [{"offset": 10, "snippet": "205 kWh"}], "hit_count": 1})
    assert t.end_turn().total == 0

    t.begin_turn(4)  # a document the run already knows: reloading it is not new knowledge
    t.observe("get_cached_document", {"key": "d1"}, {"found": True, "document_id": "d1", "text": "abc", "offset": 0})
    assert t.end_turn().total == 0
    t.begin_turn(5)
    t.observe("get_cached_document", {"key": "d9"}, {"found": True, "document_id": "d9", "text": "abc", "offset": 0})
    assert t.end_turn().new_documents == 1

    t.begin_turn(6)
    t.observe("extract_html", {"document_id": "d9", "offset": 1}, {"document_id": "d9", "offset": 1, "text": "bc"})
    assert t.end_turn().total == 0  # span already exposed by the cached-document load
    t.begin_turn(7)
    t.observe("extract_html", {"document_id": "d9", "offset": 3}, {"document_id": "d9", "offset": 3, "text": "def"})
    t.observe("get_structured_data", {"document_id": "d9"}, {"document_id": "d9", "found": {"json_ld": True}})
    assert t.end_turn().new_material == 2

    t.begin_turn(8)  # a replayed exact repeat is never progress
    t.note_reused("find_in_document")
    n = t.end_turn()
    assert (n.reused_calls, n.total, t.idle_turns) == (1, 0, 1)


def test_repeated_duplicate_calls_end_a_recovery_attempt(tmp_path, make_ctx, counted):
    ctx = make_ctx()
    doc = put_doc(ctx.cache, PAGE, "Charging port details only.")
    same = _call("r", "find_in_document", {"document_id": doc, "query": "סוללה"})
    script = [say({"summary": "primary", "fields": {}}),
              turn(same), turn(same),                                       # attempt 1 (budget 4, ends early)
              say({"field": "battery_gross_kwh", "status": "unresolved"}),  # attempt 2
              say({"summary": "final", "fields": {}})]
    result, client, _ = run(tmp_path, ctx, script, field_recovery_enabled=True, no_new_research_turns=2,
                            field_recovery_max_steps=4, requested_fields=["battery_gross_kwh"])
    first = result["field_recovery"]["attempts"][0]
    assert first["turns"] == 2 and counted["find_in_document"] == 1  # zero hits, then a replay: two idle turns
    assert result["research_tracking"]["duplicate_inspections_suppressed"] == 1
    assert result["output"]["summary"] == "final"


# --- 8-14, 16 (Cadillac regression), 21-22 ----------------------------------------------------------

def cadillac(make_ctx):
    ctx = make_ctx()
    d = put_doc(ctx.cache, "https://www.cadillac.co.il/escalade-iq", "אסקלייד IQ מחיר 1,190,000 ש\"ח")
    a = put_doc(ctx.cache, "https://www.cadillac.co.il/escalade-iq/spec.pdf",
                "מפרט טכני אסקלייד IQ 2025 6EQ26 חשמלי AWD: סוללה 205 קוט\"ש. טווח נסיעה 742 ק\"מ WLTP.")
    b = put_doc(ctx.cache, "https://www.cadillac.com/escalade-iq", "Battery: 205 kWh usable. EPA range 460 miles.")
    return ctx, d, a, b


@pytest.mark.sweep_mode("legacy")   # encodes the legacy tool-loop sweep
@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_cadillac_trace_regression_reuse_and_dynamic_queue(tmp_path, make_ctx, counted):
    ctx, D, A, B = cadillac(make_ctx)
    find_a = {"document_id": A, "query": "סוללה"}
    script = [
        # primary research
        turn(_call("p1", "get_cached_document", {"key": D})),
        say({"summary": "primary", "fields": {}}),
        # layered pipeline: the document sweep (one turn) promotes nothing
        say({"reviewed": [], "notes": "nothing to promote"}),
        # recovery: battery_gross_kwh, attempt 1
        turn(_call("r1", "get_cached_document", {"key": D}), _call("r2", "find_in_document", find_a),
             _call("r3", "find_in_document", {"document_id": B, "query": "סוללה"})),
        turn(_call("r4", "find_in_document", find_a), _call("r5", "get_cached_document", {"key": D}),
             _call("r6", "store_evidence", {"field": "electric_range_km", "value": 742, "market": "IL",
                                             "document_id": A, "quote": "טווח נסיעה 742 ק\"מ WLTP"})),
        say({"field": "battery_gross_kwh", "status": "unresolved", "notes": "only usable capacity stated"}),
        # breadth-first: rear_legroom_mm, attempt 1 (still gets its normal retry) before any attempt 2
        say({"field": "rear_legroom_mm", "status": "unresolved"}),
        # recovery: battery_gross_kwh, attempt 2
        turn(_call("r7", "find_in_document", find_a)),
        say({"field": "battery_gross_kwh", "status": "unresolved"}),
        say({"field": "rear_legroom_mm", "status": "unresolved"}),
        # finalizer
        say({"summary": "final", "fields": {}}),
    ]
    result, client, events = run(tmp_path, ctx, script, field_recovery_enabled=True, no_new_research_turns=2,
                                 requested_fields=["battery_gross_kwh", "electric_range_km", "rear_legroom_mm",
                                                   {"name": "fuel_tank_l", "applies_to": ["conventional"]}])
    # Each exact operation executed once in the whole vehicle run; repeats were replayed.
    assert counted["get_cached_document"] == 1
    assert counted[("find_in_document", json.dumps(find_a))] == 1
    assert counted["find_in_document"] == 2                    # A and B: different documents both ran
    reused = [(e["name"], e["original_step"]) for e in events if e["kind"] == "tool_reused"]
    assert reused == [("get_cached_document", 1), ("find_in_document", 2), ("get_cached_document", 1),
                      ("find_in_document", 2)]
    assert result["research_tracking"]["duplicate_calls_suppressed"] == 4

    rec = result["field_recovery"]
    assert rec["queue"] == ["battery_gross_kwh", "electric_range_km", "rear_legroom_mm"]   # fuel_tank_l: n/a
    assert [(a["field"], a["attempt"]) for a in rec["attempts"]] == [
        ("battery_gross_kwh", 1), ("rear_legroom_mm", 1), ("battery_gross_kwh", 2), ("rear_legroom_mm", 2)]
    assert rec["fields_resolved_indirectly"] == {"electric_range_km": {"resolved_during_field": "battery_gross_kwh",
                                                                       "attempt": 1, "state": "ok"}}
    assert rec["fields_still_failed"] == ["battery_gross_kwh", "rear_legroom_mm"]
    retry_fields = [json.loads(r["messages"][1]["content"].split("\n", 1)[1])["requested_field"]["name"]
                    for r in client.requests if r["messages"][0]["content"] == agent_mod.FIELD_RECOVERY_SYSTEM_PROMPT
                    and len(r["messages"]) == 2]
    assert "electric_range_km" not in retry_fields              # no recovery model call of its own
    skipped = [e for e in events if e["kind"] == "field_recovery_queue_resolved_indirectly"]
    assert skipped and skipped[0]["field"] == "electric_range_km"

    # The attempt-2 packet carries the operation history from primary research and attempt 1, compactly.
    packet2 = json.loads(next(r for r in client.requests if r["messages"][0]["content"] ==
                              agent_mod.FIELD_RECOVERY_SYSTEM_PROMPT and '"attempt": 2' in r["messages"][1]["content"]
                              and "battery_gross_kwh" in r["messages"][1]["content"])["messages"][1]["content"]
                         .split("\n", 1)[1])
    ops = packet2["already_attempted_operations"]
    assert {"tool": "get_cached_document", "step": 1, "phase": "research", "key": D, "outcome": "loaded",
            "chars": len("אסקלייד IQ מחיר 1,190,000 ש\"ח")} in ops
    op_a = next(o for o in ops if o.get("document_id") == A)
    assert (op_a["outcome"], op_a["hit_count"], op_a["phase"], op_a["field"], op_a["attempt"]) == \
        ("hits", 1, "field_recovery", "battery_gross_kwh", 1)
    assert "205 קוט" not in json.dumps(ops, ensure_ascii=False)  # outcomes only, never contents
    assert packet2["other_requested_fields"]["electric_range_km"]

    m = compute_metrics(result)
    assert (m["fields_resolved_indirectly_by_other_recovery"], m["fields_resolved_directly_by_recovery"]) == (1, 0)
    # find(A) exposed new passages; find(B, Hebrew query on an English page) found nothing.
    assert m["duplicate_calls_suppressed"] == 4 and m["recovery_operations_with_new_material"] == 1
    assert m["recovery_operations_without_new_material"] == 1

    # Live feed: recovery rows name field + attempt; model turns too, while usage grouping is unchanged.
    tool_event = next(e for e in events if e["kind"] == "tool_call" and e.get("phase") == "field_recovery")
    assert run_view.tool_call_line(tool_event).startswith("🔧 battery_gross_kwh · attempt 1 · step 2\n   ")
    turn_event = next(e for e in events if e["kind"] == "model_response" and e.get("phase") == "field_recovery")
    assert turn_event["phase"] == "field_recovery" and (turn_event["field"], turn_event["attempt"]) == \
        ("battery_gross_kwh", 1)
    assert run_view.model_turn_line(turn_event).startswith(
        "🧠 field_recovery · battery_gross_kwh · attempt 1/2 · turn 1/4 · 3 tool call(s) · tokens 120")
    assert run_view.tool_call_line({"name": "search_web", "step": 3, "arguments": "{}"}).startswith("🔧 step 3 · ")
    assert result["usage_field_recovery"]["model_calls"] == 7 and result["usage_research"]["model_calls"] == 2

    # Incomplete-run reconstruction still counts the replays from events.
    (tmp_path / "runs" / "b" / "85095" / "result.json").unlink()
    rebuilt = load_runs(tmp_path / "runs", "b")[0]
    assert rebuilt["research_tracking"]["duplicate_calls_suppressed"] == 4
    assert rebuilt["field_recovery"]["fields_resolved_indirectly"]["electric_range_km"]["resolved_during_field"] == \
        "battery_gross_kwh"


def test_operation_history_is_bounded():
    events = []
    for i in range(70):
        events.append({"kind": "tool_call", "step": i, "call_id": f"c{i}", "name": "find_in_document",
                       "arguments": json.dumps({"document_id": "d1", "query": f"term {i}"})})
        events.append({"kind": "tool_result", "step": i, "call_id": f"c{i}", "name": "find_in_document",
                       "result": {"document_id": "d1", "hits": [{"offset": 1, "snippet": "SECRET TEXT " * 50}],
                                  "hit_count": 1}})
    ops = attempted_operations(events, {"name": "rear_legroom_mm", "description": "Rear legroom"})
    assert len(ops) == 40 and ops[-1]["query"] == "term 69"
    assert "SECRET" not in json.dumps(ops)
