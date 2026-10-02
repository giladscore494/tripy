"""Schema-driven targeted field recovery: detection from the model's own research state, one
focused compact retry per failed requested field, then the compact finalizer.
Scripted GLM / fake HTTP only."""

import json
import re
from pathlib import Path

import pytest

from conftest import FakeResponse, cache_source
from test_tools_smoke import ScriptedGLM, _call

from src.agent import FIELD_RECOVERY_SYSTEM_PROMPT, SYSTEM_PROMPT, AgentConfig, agent_config_from_env, run_vehicle
from src.benchmark import compute_metrics
from src.field_recovery import RETRY_STATES, evaluate_field, retry_queue
from src.fields import load_schema, resolve_requested_fields
from src.storage.run_loader import load_runs
from src.storage.run_log import RunLog, read_events
from src.tools import ToolConfig

ROOT = Path(__file__).resolve().parent.parent
FUTURE_FIELDS = ["battery_usable_kwh", "tire_size_front", "paint_code", "service_interval_km", "rear_legroom_mm"]
PAYLOAD = {"identity": {"manufacturer": "אקספנג", "commercial_name": "G6", "year": 2026, "trim": "MAX",
                        "model_code": "NSGHA", "government_record_id": "101122"},
           "engine_drivetrain": {"propulsion_normalized": "battery_electric", "drivetrain_normalized": "awd",
                                 "power_hp": 486}}
PAGE = "https://www.xpeng.co.il/g6"
SPECS = "https://www.xpeng.co.il/g6/specs"
IDENTITY = 'XPeng G6 2026 MAX AWD רכב חשמלי 486 כ"ס'
QUOTES = {"battery_usable_kwh": "Usable battery 80.8 kWh", "tire_size_front": "Front tyres 255/45 R20",
          "paint_code": "Paint: Nebula White", "service_interval_km": "service every 20,000 km",
          "rear_legroom_mm": "Rear legroom 950 mm"}
# The retrieved spec page every primary-research quote comes from (evidence needs a retrieved source).
SPECS_TEXT = IDENTITY + "\n" + "\n".join(QUOTES.values())


def ev(field, value, market="IL", url=SPECS, **extra):
    return {"field": field, "value": value, "market": market, "source_url": url, "quote": QUOTES.get(field, str(value)),
            **extra}


def turn(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def say(obj):
    return {"role": "assistant", "content": json.dumps(obj)}


def spec(name, **kw):
    return {"name": name, "description": name, "applicable": True, **kw}


# --- detection ---------------------------------------------------------------------------------

def test_detection_uses_only_the_models_research_state():
    s = spec("paint_code")
    state = lambda evidence, declared=None, out=None: evaluate_field(s, evidence, declared, out, "IL")["state"]
    assert state([ev("paint_code", "X1")]) == "ok"                                   # candidate + evidence record
    assert state([ev("paint_code", "X1", market=None)]) == "ok"                      # unrecorded market is fine
    # Two different target-market values that may apply to the target: an unresolved same-scope conflict.
    assert state([ev("paint_code", "X1"), ev("paint_code", "X2")]) == "conflicting"
    assert state([ev("paint_code", "X1"), ev("paint_code", "X2", market="MY")]) == "ok"   # other market: info only
    assert "multiple_values" in evaluate_field(s, [ev("paint_code", 1), ev("paint_code", 2)], None, None, "IL")["info"]
    assert state([]) == "missing"
    assert state([ev("paint_code", None)]) == "missing"                              # null: could not locate
    assert state([], out={"value": "X1"}) == "weak_provenance"                       # answer without evidence record
    assert state([], {"status": "unresolved"}) == "unresolved"
    assert state([], out={"value": None, "provenance": "unresolved"}) == "unresolved"
    assert state([ev("paint_code", "X1", market="MY")]) == "foreign_market_only"
    # a model "found" cannot turn another market's evidence into target evidence (server scope is authoritative;
    # until the Reliability Foundation this declaration returned ok)
    assert state([ev("paint_code", "X1", market="MY")], {"status": "found"}) == "foreign_market_only"
    assert state([ev("paint_code", "X1", variant_match="different")]) == "variant_not_exact"
    # unclear: the server-side binding did not reach the field's binding_requirement -> never ok
    assert state([ev("paint_code", "X1", variant_match="unclear")]) == "variant_not_exact"
    assert state([ev("paint_code", "X1", variant_match="unclear")], {"status": "found"}) == "variant_not_exact"
    assert state([ev("paint_code", "X1")], {"status": "conflicting"}) == "conflicting"
    assert state([], {"status": "not_applicable"}) == "not_applicable"
    assert state([], out={"provenance": "not_applicable"}) == "not_applicable"
    assert evaluate_field(spec("x", applicable=False), [], None, None, "IL")["state"] == "not_applicable"
    assert set(RETRY_STATES) == {"unresolved", "missing", "conflicting", "foreign_market_only", "variant_not_exact",
                                 "weak_provenance"}


def test_any_requested_field_is_eligible_with_schema_driven_attempts():
    specs = resolve_requested_fields(["battery_usable_kwh", {"name": "paint_code", "recovery_attempts": 0},
                                      {"name": "rear_legroom_mm", "recovery_attempts": 1}, "service_interval_km"],
                                     propulsion="battery_electric")
    assert [s["name"] for s in specs] == ["battery_usable_kwh", "paint_code", "rear_legroom_mm", "service_interval_km"]
    assert specs[0]["description"] == "Battery capacity usable (kWh)"   # metadata from the schema when known
    assert specs[3]["description"] == "service_interval_km"             # unknown fields need no code change
    evaluation = [evaluate_field(s, [], None, None, "IL") for s in specs]
    queue = retry_queue(evaluation, specs, default_attempts=2)
    assert [(q["field"], q["max_attempts"]) for q in queue] == [
        ("battery_usable_kwh", 2), ("rear_legroom_mm", 1), ("service_interval_km", 2)]
    ice = resolve_requested_fields(["battery_usable_kwh", "fuel_tank_l"], propulsion="conventional")
    assert [s["applicable"] for s in ice] == [False, True]              # by the schema's applies_to only


def test_recovery_engine_contains_no_field_names():
    names = {s["name"] for s in load_schema()} | set(FUTURE_FIELDS)
    for module in ("src/field_recovery.py", "src/fields.py", "src/storage/trace.py", "src/bundle.py",
                   "src/candidate_harvest.py", "src/document_sweep.py", "src/ui/live_state.py", "src/ui/labels_he.py",
                   "src/ui/live_dashboard.py", "src/evidence_admission.py", "src/document_binding.py",
                   "src/source_authority.py", "src/typed_values.py", "src/tail_planner.py",
                   "src/conflict_normalizer.py", "src/market_portability.py"):
        code = (ROOT / module).read_text("utf-8")
        found = [n for n in names if re.search(rf"\b{re.escape(n)}\b", code)]
        assert not found, (module, found)
    assert "if field ==" not in (ROOT / "src/field_recovery.py").read_text("utf-8")


# --- end to end ----------------------------------------------------------------------------------

def scripted_run(tmp_path, make_ctx, script, **cfg):
    ctx = make_ctx({PAGE: FakeResponse(f"<html><body>{IDENTITY}. G6 service every 20,000 km</body></html>"
                                       .encode("utf-8"))})
    cache_source(ctx.cache, SPECS, SPECS_TEXT)
    client = ScriptedGLM(script)
    config = AgentConfig(**{"max_steps": 3, "requested_fields": FUTURE_FIELDS, "recovery_mode": "legacy", **cfg})
    result = run_vehicle({"upstream_record_id": "101122"}, PAYLOAD, client=client, cache=ctx.cache,
                         run_log=RunLog(tmp_path / "runs", "b", "101122"), config=config, tool_config=ToolConfig(),
                         session=ctx.session)
    return result, client, read_events(tmp_path / "runs" / "b" / "101122" / "events.jsonl")


def test_only_failed_requested_fields_get_focused_retries(tmp_path, make_ctx):
    final = {"vehicle_id": "101122", "summary": "final", "fields": {"service_interval_km": {"value": 20000}}}
    script = [
        # primary research: three of five requested fields obtained
        turn(_call("p1", "fetch_url", {"url": PAGE}),
             _call("p2", "search_web", {"query": "XPeng G6 service interval km"})),
        turn(_call("p3", "store_evidence", ev("battery_usable_kwh", 80.8)),
             _call("p4", "store_evidence", ev("tire_size_front", "255/45 R20")),
             _call("p5", "store_evidence", ev("paint_code", "Nebula White"))),
        say({"vehicle_id": "101122", "summary": "primary", "fields": {}}),
        # layered pipeline: one document-sweep turn over the cached page finds nothing it can promote
        say({"reviewed": [], "notes": "no usable candidate in the cached page"}),
        # retry tire_size_front #1: its evidence binds only at exact_technical_variant (the page does not name the
        # trim; "MAX" is a generic trim word) while the field requires exact_market_trim -> variant_not_exact
        say({"field": "tire_size_front", "status": "unresolved", "value": None}),
        # retry service_interval_km #1: inspects cached doc, stores evidence, reports found
        turn(_call("r1", "find_in_document", {"document_id": "PLACEHOLDER", "query": "service"}),
             _call("r2", "store_evidence", ev("service_interval_km", 20000, url=PAGE))),
        # (no "found" reply turn: the stored evidence already makes the field ok, so the attempt ends early)
        # retry rear_legroom_mm #1 and #2: unresolved both times
        say({"field": "rear_legroom_mm", "status": "unresolved", "value": None, "notes": "not published"}),
        say({"field": "tire_size_front", "status": "unresolved", "value": None}),
        say({"field": "rear_legroom_mm", "status": "unresolved", "value": None}),
        # compact finalizer
        say(final),
    ]
    result, client, events = scripted_run(tmp_path, make_ctx, script)

    rec = result["field_recovery"]
    # the two successful fields: no retry. tire_size_front has evidence, but its server-side binding
    # (exact_technical_variant, variant_match=unclear) is below the field's exact_market_trim requirement: retried.
    assert rec["queue"] == ["tire_size_front", "service_interval_km", "rear_legroom_mm"]
    assert rec["fields_recovered"] == ["service_interval_km"]
    assert rec["fields_still_failed"] == ["rear_legroom_mm", "tire_size_front"]
    assert [(a["field"], a["attempt"]) for a in rec["attempts"]] == [
        ("tire_size_front", 1), ("service_interval_km", 1), ("rear_legroom_mm", 1), ("tire_size_front", 2),
        ("rear_legroom_mm", 2)]
    assert rec["primary_states"] == {"ok": 2, "variant_not_exact": 1, "missing": 2}
    assert result["requested_fields"] == {
        "battery_usable_kwh": "Battery capacity usable (kWh)", "tire_size_front": "Front tire size",
        "paint_code": "paint_code", "service_interval_km": "service_interval_km", "rear_legroom_mm": "rear_legroom_mm"}

    # Each retry is a fresh compact conversation about ONE field, never the primary conversation.
    retries = [r for r in client.requests if r["messages"][0]["content"] == FIELD_RECOVERY_SYSTEM_PROMPT]
    assert len(retries) == 5
    tire = json.loads(retries[0]["messages"][1]["content"].split("\n", 1)[1])
    assert tire["requested_field"]["name"] == "tire_size_front" and tire["failure_reason"] == "variant_not_exact"
    first = json.loads(retries[1]["messages"][1]["content"].split("\n", 1)[1])
    assert first["requested_field"]["name"] == "service_interval_km" and first["failure_reason"] == "missing"
    assert first["vehicle_identity"] == {"manufacturer": "אקספנג", "model": "G6", "year": 2026,
                                         "government_trim": "MAX", "model_code": "NSGHA",
                                         "government_record_id": "101122", "powertrain": "battery_electric",
                                         "power_hp": 486, "drivetrain": "awd", "market": "Israel"}
    assert first["previous_queries_for_this_field"] == ["XPeng G6 service interval km"]
    assert first["relevant_documents"][0]["url"] == PAGE and first["existing_evidence"] == []
    for request in retries:
        sent = json.dumps(request["messages"], ensure_ascii=False)
        assert SYSTEM_PROMPT not in sent and "Nebula White" not in sent and request["tools"]
    second_attempt = json.loads(retries[4]["messages"][1]["content"].split("\n", 1)[1])
    assert second_attempt["requested_field"]["name"] == "rear_legroom_mm" and second_attempt["attempt"] == 2
    assert second_attempt["failure_reason"] == "unresolved"
    assert second_attempt["previous_attempts"][0]["reply"]["notes"] == "not published"

    # Retries happen before the finalizer, which then sees the updated state.
    kinds = [e["kind"] for e in events]
    assert kinds.index("field_retry_queue") < kinds.index("field_recovery_started") < kinds.index("finalization_started")
    bundle = json.loads(client.requests[-1]["messages"][1]["content"].split("\n", 1)[1].rsplit("\n\nReturn", 1)[0])
    assert bundle["field_states"]["service_interval_km"]["state"] == "ok"
    assert bundle["field_states"]["rear_legroom_mm"]["state"] == "unresolved"
    assert bundle["targets"]["requested_fields"]["rear_legroom_mm"] == "rear_legroom_mm"
    assert bundle["field_states"]["tire_size_front"]["state"] == "unresolved"
    assert {a["field"] for a in bundle["field_recovery"]} == {"tire_size_front", "service_interval_km", "rear_legroom_mm"}
    assert bundle["primary_output"]["summary"] == "primary"
    assert result["status"] == "completed" and result["output"]["summary"] == "final"
    assert result["usage_field_recovery"]["model_calls"] == 5 and result["usage_research"]["model_calls"] == 3
    assert result["usage_document_sweep"]["model_calls"] == 1
    assert kinds.index("document_sweep_finished") < kinds.index("field_recovery_started")
    assert rec["attempts"][1]["early_resolved"] and rec["turns_saved_by_early_resolution"] == 1
    assert [c["phase"] for c in result["tool_calls"]].count("field_recovery") == 2
    assert result["tool_calls"][-1]["field"] == "service_interval_km"

    m = compute_metrics(result)
    assert (m["requested_fields"], m["fields_failed_primary"], m["fields_retried"], m["fields_recovered"],
            m["fields_still_failed"], m["field_retry_attempts"], m["field_recovery_model_calls"]) == (5, 3, 3, 1, 2, 5, 5)


def test_no_retry_for_success_or_not_applicable(tmp_path, make_ctx):
    script = [
        turn(_call("p1", "store_evidence", ev("battery_usable_kwh", 80.8)),
             _call("p3", "store_evidence", ev("service_interval_km", 20000)),
             _call("p4", "store_evidence", ev("rear_legroom_mm", 950)),
             _call("p5", "report_field_status", {"field": "paint_code", "status": "not_applicable"})),
        say({"vehicle_id": "101122", "summary": "all done", "fields": {"rear_legroom_mm": {"value": 950}}}),
    ]
    # tire_size_front is not requested here: on this page it can only bind at exact_technical_variant, below its
    # exact_market_trim requirement, so it is never a success (see test_only_failed_requested_fields_get_focused_retries)
    result, client, events = scripted_run(tmp_path, make_ctx, script,
                                          requested_fields=[f for f in FUTURE_FIELDS if f != "tire_size_front"])
    assert result["field_recovery"]["queue"] == [] and result["field_recovery"]["attempt_count"] == 0
    assert result["field_recovery"]["final_states"] == {"ok": 3, "not_applicable": 1}
    assert len(client.requests) == 2  # no retry and no finalizer: the research model's own JSON stands
    assert result["status"] == "completed" and result["output"]["summary"] == "all done"
    assert result["finalization"] is None


def test_recovery_disabled_and_budget_caps(tmp_path, make_ctx, monkeypatch):
    monkeypatch.setenv("FIELD_RECOVERY_ENABLED", "false")
    monkeypatch.setenv("FIELD_RECOVERY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("ENRICHMENT_FIELDS", "paint_code, rear_legroom_mm")
    cfg = agent_config_from_env()
    assert (cfg.field_recovery_enabled, cfg.field_recovery_max_attempts, cfg.requested_fields) == \
        (False, 3, ["paint_code", "rear_legroom_mm"])
    monkeypatch.delenv("FIELD_RECOVERY_ENABLED")
    assert agent_config_from_env().field_recovery_enabled is True and AgentConfig().field_recovery_max_attempts == 2

    off, client, _ = scripted_run(tmp_path / "a", make_ctx, [say({"summary": "x", "fields": {}})],
                                  field_recovery_enabled=False)
    assert off["field_recovery"]["queue"] == [] and len(client.requests) == 1
    assert off["field_recovery"]["primary_states"] == {"missing": 5}  # detection still recorded



def test_total_retry_turn_cap(tmp_path, make_ctx):
    script = [
        say({"summary": "x", "fields": {}}),
        say({"field": "battery_usable_kwh", "status": "unresolved"}),
        say({"field": "tire_size_front", "status": "unresolved"}),    # breadth-first: next field, attempt 1
        say({"summary": "final", "fields": {}}),
    ]
    result, client, _ = scripted_run(tmp_path, make_ctx, script, field_recovery_max_total_steps=2)
    rec = result["field_recovery"]
    assert rec["turns"] == 2 and rec["stopped"] == "max_total_steps" and rec["attempt_count"] == 2
    assert rec["attempt_order"] == ["battery_usable_kwh#1", "tire_size_front#1"]
    assert rec["fields_retried"] == ["battery_usable_kwh", "tire_size_front"] and result["output"]["summary"] == "final"


def test_interrupt_during_field_recovery_is_persisted_and_reconstructed(tmp_path, make_ctx):
    class Interrupting(ScriptedGLM):
        def chat(self, messages, tools=None, **kwargs):
            if not self.messages:
                raise KeyboardInterrupt
            return super().chat(messages, tools=tools, **kwargs)

    ctx = make_ctx()
    client = Interrupting([say({"summary": "x", "fields": {}}), say({"field": "battery_usable_kwh",
                                                                    "status": "unresolved"})])
    log = RunLog(tmp_path / "runs", "b", "101122")
    with pytest.raises(KeyboardInterrupt):
        run_vehicle({"upstream_record_id": "101122"}, PAYLOAD, client=client, cache=ctx.cache, run_log=log,
                    config=AgentConfig(requested_fields=FUTURE_FIELDS, recovery_mode="legacy"), tool_config=ToolConfig(), session=ctx.session)
    saved = json.loads((log.dir / "result.json").read_text("utf-8"))
    assert saved["status"] == "interrupted" and saved["error"] is None   # an interruption is not an error
    assert (saved["partial"], saved["interrupted"], saved["interrupted_phase"], saved["interruption_type"]) == \
        (True, True, "field_recovery", "KeyboardInterrupt")
    assert saved["stop_reason"] == "model_finished"  # research itself had finished
    (log.dir / "result.json").unlink()  # as if the process had been killed instead
    run = load_runs(tmp_path / "runs", "b")[0]
    assert run["synthesized"] and run["field_recovery"]["queue"] == FUTURE_FIELDS
    assert run["field_recovery"]["attempts"][0]["field"] == "battery_usable_kwh"
    assert run["requested_fields"]["paint_code"] == "paint_code"
