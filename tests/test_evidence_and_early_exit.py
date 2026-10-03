"""Idempotent evidence facts and early success exit inside a field-recovery attempt.
Scripted GLM / fake HTTP only: no network, no paid calls."""

import json

import pytest

from test_tools_smoke import ScriptedGLM, _call

from src.agent import FIELD_RECOVERY_SYSTEM_PROMPT, AgentConfig, run_vehicle
from src.benchmark import compute_metrics
from src.context import ResearchTracker
from src.storage.run_log import RunLog, read_events
from src.tools import ToolConfig, dispatch
from src.tools.evidence import evidence_fact_key

PAYLOAD = {"identity": {"manufacturer": "קאדילאק", "commercial_name": "ESCALADE IQ", "year": 2025,
                        "trim": "SPORT", "model_code": "X1", "government_record_id": "85095"},
           "engine_drivetrain": {"propulsion_normalized": "battery_electric", "drivetrain_normalized": "awd",
                                 "power_hp": 750}}
IL_URL = "https://www.cadillac.co.il/escalade-iq/spec"
US_URL = "https://www.cadillac.com/en-us/escalade-iq"
# Retrieved documents: evidence is admitted only from these, with a quote that occurs in them. The IL page names the
# exact technical variant (model, propulsion, power), so its facts bind as variant_match=exact server-side.
IL_TEXT = ('קאדילאק אסקלייד IQ 2025 רכב חשמלי 750 כ"ס AWD. מפרט טכני: סוללה 205 קוט"ש, 205 kWh battery, '
           'טווח 742 ק"מ. מומנט 1,066 Nm (torque 1066 Nm).')
US_TEXT = "2025 Cadillac Escalade IQ, all-electric, 750 hp AWD. Battery 205 kWh. Range 742 km."


def turn(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def say(obj):
    return {"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)}


def store(cid, field, value, **kw):
    return _call(cid, "store_evidence", {"field": field, "value": value, **kw})


def put_doc(cache, url, text):
    return cache.put("fetch", url, text.encode("utf-8"), {"status": 200, "final_url": url, "doc_type": "text",
                                                         "content_type": "text/plain"}, text)["document_id"]


# These cases test foreign-market / conflict early-exit semantics, so the market-portability policy (an official
# foreign fact counting for IL, tested on its own) is switched off for the field.
NOT_PORTABLE = {"portability_scope": "none"}


def run(tmp_path, ctx, script, **cfg):
    put_doc(ctx.cache, IL_URL, IL_TEXT)
    put_doc(ctx.cache, US_URL, US_TEXT)
    client = ScriptedGLM(script)
    log = RunLog(tmp_path / "runs", "b", "85095")
    # Early-exit tests script every model turn; admitted evidence now records its document, which would schedule
    # a document sweep turn, so the layered stage is off here (it has its own tests).
    # scripted turns end at the ceiling: the minimum-acquisition safety gate (tests/test_orchestration.py) is off
    config = AgentConfig(**{"max_steps": 4, "no_new_research_turns": 0, "layered_harvest_enabled": False,
                            "recovery_mode": "legacy", "primary_research_min_base_documents": 0,
                            "primary_research_min_base_scoped_coverage": 0, **cfg})
    result = run_vehicle({"upstream_record_id": "85095"}, PAYLOAD, client=client, cache=ctx.cache, run_log=log,
                         config=config, tool_config=ToolConfig(), session=ctx.session)
    return result, client, read_events(log.events_path)


def recovery_requests(client):
    return [r for r in client.requests if r["messages"][0]["content"] == FIELD_RECOVERY_SYSTEM_PROMPT]


def last_bundle(client):
    return json.loads(client.requests[-1]["messages"][1]["content"].split("\n", 1)[1].rsplit("\n\nReturn", 1)[0])


# --- evidence idempotency --------------------------------------------------------------------------

def test_same_fact_is_stored_once_and_reuses_its_id(make_ctx):
    ctx = make_ctx()
    events = []
    ctx.log = lambda kind, **data: events.append({"kind": kind, **data})
    doc = put_doc(ctx.cache, IL_URL, IL_TEXT)
    fact = {"field": "battery_gross_kwh", "value": 205, "unit": "kWh", "document_id": doc, "market": "IL",
            "quote": "סוללה 205 קוט\"ש"}
    first = dispatch(ctx, "store_evidence", fact)
    second = dispatch(ctx, "store_evidence", {**fact, "value": "205", "quote": "205 kWh battery", "note": "again"})
    assert (first["evidence_id"], first["stored"]) == ("e1", True)                                     # 1
    assert (second["evidence_id"], second["stored"], second["reused"]) == ("e1", False, True)
    assert len(ctx.evidence.items) == 1 and ctx.counters["duplicate_evidence_suppressed"] == 1
    assert ctx.evidence.items[0]["supplementary"] == [{"quote": "205 kWh battery", "note": "again"}]
    reused = [e for e in events if e["kind"] == "evidence_reused"]
    assert reused[0]["evidence_id"] == "e1" and reused[0]["document_id"] == doc
    assert [e["kind"] for e in events].count("evidence") == 1


def test_different_scope_or_value_stays_separate(make_ctx):
    base = {"field": "battery_gross_kwh", "value": 205, "unit": "kWh", "document_id": "d_1", "market": "IL"}
    key = evidence_fact_key
    assert key({**base, "document_id": "d_2"}) != key(base)                                 # 2: other source
    assert key({**base, "value": 200}) != key(base)                                         # 3: other value
    assert key({**base, "market": "US"}) != key(base)                                       # 4: other market
    assert key({**base, "variant": "Sport"}) != key(base) and key({**base, "variant_match": "different"}) != key(base)
    assert key({**base, "condition": "with roof rails"}) != key(base)
    assert key({**base, "value": "205 kWh"}) != key(base)                                   # no aggressive merging
    assert key({**base, "value": "205.0"}) == key(base) and key({**base, "quote": "x", "note": "y"}) == key(base)
    url = {k: v for k, v in base.items() if k != "document_id"}
    assert key({**url, "source_url": IL_URL + "#tech"}) == key({**url, "source_url": IL_URL})
    ctx = make_ctx()
    d1 = put_doc(ctx.cache, IL_URL, IL_TEXT)
    d2 = put_doc(ctx.cache, IL_URL + "/v2", IL_TEXT.replace("סוללה 205", "סוללה 200"))
    us = put_doc(ctx.cache, US_URL, US_TEXT)
    il = {"field": "battery_gross_kwh", "value": 205, "unit": "kWh", "quote": "205 kWh battery"}
    for request in ({**il, "document_id": d1}, {**il, "document_id": d2},
                    {**il, "document_id": d2, "value": 200, "quote": "סוללה 200 קוט\"ש"},
                    {**il, "document_id": us, "quote": "Battery 205 kWh", "market": "US"}):
        assert dispatch(ctx, "store_evidence", request)["stored"] is True
    assert [e["evidence_id"] for e in ctx.evidence.items] == ["e1", "e2", "e3", "e4"]
    assert [e["market"] for e in ctx.evidence.items] == ["IL", "IL", "IL", "US"]
    # the market is the SOURCE's (an .il page), so a model claim of another market is not another fact
    again = dispatch(ctx, "store_evidence", {**il, "document_id": d1, "market": "US"})
    assert (again["evidence_id"], again["reused"]) == ("e1", True)


def test_reused_evidence_is_not_novelty_and_keeps_idle_streak():
    t = ResearchTracker()
    t.begin_turn(1)
    t.observe("store_evidence", {"field": "f", "value": 1, "source_url": IL_URL}, {"evidence_id": "e1", "stored": True})
    assert t.end_turn().new_evidence == 1 and t.idle_turns == 0
    t.begin_turn(2)                                                                          # 5, 6
    t.observe("store_evidence", {"field": "f", "value": 1, "source_url": IL_URL},
              {"evidence_id": "e1", "stored": False, "reused": True})
    n = t.end_turn()
    assert (n.new_evidence, n.total, t.idle_turns) == (0, 0, 1)
    t.begin_turn(3)
    t.observe("store_evidence", {"field": "f", "value": 1, "source_url": IL_URL},
              {"evidence_id": "e1", "stored": False, "reused": True})
    assert t.end_turn().total == 0 and t.idle_turns == 2


@pytest.mark.final_assembly("llm")   # encodes the finalizer model's output (FINAL_ASSEMBLY=llm)
@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_finalizer_bundle_contains_a_repeated_fact_once(tmp_path, make_ctx):
    ctx = make_ctx()
    same = {"field": "torque_nm", "value": 1066, "unit": "Nm", "source_url": IL_URL, "market": "IL",
            "quote": "מומנט 1,066 Nm"}
    script = [turn(store("c1", **same)), turn(store("c2", **same)),
              turn(store("c3", **{**same, "quote": "torque 1066 Nm"})),
              turn(_call("c4", "search_web", {"query": "x"})), say({"summary": "final", "fields": {}})]
    result, client, events = run(tmp_path, ctx, script, field_recovery_enabled=False)
    bundle = last_bundle(client)                                                              # 7
    assert [e["evidence_id"] for e in bundle["evidence"]] == ["e1"]
    assert len(bundle["candidate_facts"]["torque_nm"]) == 1
    assert len(result["evidence"]) == 1 and len(result["evidence"][0]["supplementary"]) == 1
    assert compute_metrics(result)["duplicate_evidence_suppressed"] == 2
    assert [e["kind"] for e in events].count("evidence_reused") == 2


# --- early recovery exit ----------------------------------------------------------------------------

@pytest.mark.final_assembly("llm")   # encodes the finalizer model's output (FINAL_ASSEMBLY=llm)
@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_cadillac_duplicate_store_and_early_resolution(tmp_path, make_ctx):
    """Real trace: turn 3 stored battery_gross_kwh=205 (IL, document D) twice, then turn 4 only said "found"."""
    ctx = make_ctx()
    doc = put_doc(ctx.cache, IL_URL, IL_TEXT)
    il_205 = {"field": "battery_gross_kwh", "value": 205, "unit": "kWh", "document_id": doc, "market": "IL",
              "quote": "סוללה 205 קוט\"ש"}
    script = [
        turn(store("p1", "battery_gross_kwh", 205, unit="kWh", source_url=US_URL, market="US",
                   quote="Battery 205 kWh")),
        say({"summary": "primary", "fields": {}}),
        turn(_call("r1", "find_in_document", {"document_id": doc, "query": "סוללה"})),       # recovery turn 1
        turn(_call("r2", "extract_tables", {"document_id": doc})),                          # recovery turn 2
        turn(store("r3", **il_205), store("r4", **il_205)),                                 # recovery turn 3
        say({"summary": "final", "fields": {"battery_gross_kwh": {"value": 205}}}),          # finalizer
    ]
    result, client, events = run(tmp_path, ctx, script,
                                 requested_fields=[{"name": "battery_gross_kwh", **NOT_PORTABLE}])
    rec = result["field_recovery"]
    assert rec["primary_states"] == {"foreign_market_only": 1}
    assert [e["evidence_id"] for e in result["evidence"]] == ["e1", "e2"]                    # no e3
    reused = [e for e in events if e["kind"] == "evidence_reused"]
    assert len(reused) == 1 and reused[0]["evidence_id"] == "e2"
    second_store = next(e for e in events if e["kind"] == "tool_result" and e.get("call_id") == "r4")["result"]
    assert (second_store["evidence_id"], second_store["stored"], second_store["reused"]) == ("e2", False, True)

    attempt = rec["attempts"][0]                                                             # 8, 9
    assert (attempt["turns"], attempt["early_resolved"], attempt["state_before"], attempt["state_after"]) == \
        (3, True, "foreign_market_only", "ok")
    assert len(rec["attempts"]) == 1 and rec["fields_recovered"] == ["battery_gross_kwh"]
    early = next(e for e in events if e["kind"] == "field_recovery_early_resolved")
    assert (early["field"], early["attempt"], early["after_turn"], early["state"]) == ("battery_gross_kwh", 1, 3, "ok")
    assert len(recovery_requests(client)) == 3                                               # no 4th turn
    recovery_turns = [e for e in events if e["kind"] == "model_response" and e.get("phase") == "field_recovery"]
    assert [e["turn"] for e in recovery_turns] == [1, 2, 3]                                  # 14
    assert result["usage_field_recovery"]["model_calls"] == 3
    assert result["usage_field_recovery"]["total_tokens"] == 3 * 120
    assert rec["turns_saved_by_early_resolution"] == 1 and rec["field_recovery_turns_used"] == 3
    m = compute_metrics(result)
    assert (m["field_recovery_turns_saved_by_early_resolution"], m["duplicate_evidence_suppressed"]) == (1, 1)
    assert result["status"] == "completed" and result["output"]["summary"] == "final"        # 13
    assert [e["evidence_id"] for e in last_bundle(client)["evidence"]] == ["e1", "e2"]


def test_no_early_exit_while_still_foreign_or_conflicting(tmp_path, make_ctx):
    ctx = make_ctx()
    put_doc(ctx.cache, US_URL + "/specs", "Cadillac Escalade IQ 2025 all-electric 750 hp. Battery capacity 205 kWh.")
    put_doc(ctx.cache, IL_URL + "/pdf", 'קאדילאק אסקלייד IQ 2025 רכב חשמלי 750 כ"ס. סוללה 200 קוט"ש')
    script = [
        turn(store("p1", "battery_gross_kwh", 205, source_url=US_URL, market="US", quote="Battery 205 kWh")),
        say({"summary": "primary", "fields": {}}),
        turn(store("r1", "battery_gross_kwh", 205, source_url=US_URL + "/specs", market="US",
                   quote="Battery capacity 205 kWh")),                                       # 10
        turn(store("r2", "battery_gross_kwh", 205, source_url=IL_URL, market="IL", quote="סוללה 205 קוט\"ש"),
             store("r3", "battery_gross_kwh", 200, source_url=IL_URL + "/pdf", market="IL",
                   quote="סוללה 200 קוט\"ש")),                                               # 11
        say({"field": "battery_gross_kwh", "status": "conflicting"}),
        say({"field": "battery_gross_kwh", "status": "conflicting"}),                       # attempt 2
        say({"summary": "final", "fields": {}}),
    ]
    result, client, events = run(tmp_path, ctx, script,
                                 requested_fields=[{"name": "battery_gross_kwh", **NOT_PORTABLE}])
    first = result["field_recovery"]["attempts"][0]
    assert first["turns"] == 3 and not first["early_resolved"] and first["state_after"] == "conflicting"
    assert first["reply"]["status"] == "conflicting"                                         # final reply path kept
    assert not [e for e in events if e["kind"] == "field_recovery_early_resolved"]
    assert result["field_recovery"]["final_states"] == {"conflicting": 1}


def test_early_exit_still_runs_all_field_reevaluation(tmp_path, make_ctx):
    ctx = make_ctx()
    script = [
        say({"summary": "primary", "fields": {}}),
        # recovery for battery_gross_kwh: the same spec sheet also states the range (by-product evidence)
        turn(store("r1", "battery_gross_kwh", 205, source_url=IL_URL, market="IL", quote="205 קוט\"ש"),
             store("r2", "electric_range_km", 742, source_url=IL_URL, market="IL", quote="742 ק\"מ")),
        say({"field": "rear_legroom_mm", "status": "unresolved"}),
        say({"field": "rear_legroom_mm", "status": "unresolved"}),
        say({"summary": "final", "fields": {}}),
    ]
    result, client, events = run(tmp_path, ctx, script,
                                 requested_fields=["battery_gross_kwh", "electric_range_km", "rear_legroom_mm"])
    rec = result["field_recovery"]                                                           # 12
    assert rec["attempts"][0]["early_resolved"] and rec["attempts"][0]["turns"] == 1
    assert rec["fields_resolved_indirectly"]["electric_range_km"]["resolved_during_field"] == "battery_gross_kwh"
    asked = [json.loads(r["messages"][1]["content"].split("\n", 1)[1])["requested_field"]["name"]
             for r in recovery_requests(client) if len(r["messages"]) == 2]
    assert asked == ["battery_gross_kwh", "rear_legroom_mm", "rear_legroom_mm"]
    assert result["output"]["summary"] == "final"
