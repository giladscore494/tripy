"""Interrupted runs: clean script-control (UI stop) classification, current field state recomputed from ALL
events, and the strict "no stored evidence" vs operational "unresolved" split.
Scripted GLM / fake HTTP only: no network, no paid calls."""

import json

import pytest

from conftest import labelled_quote, seed_evidence_sources
from test_run_config import PostResponse, ScriptedPostSession, chat_reply, tool_call
from test_tools_smoke import ScriptedGLM, _call

from src.agent import AgentConfig, run_vehicle
from src.benchmark import HANDSHAKE_RECORD_ID, benchmark_vehicles, research_one
from src.glm_client import GLMClient, GLMSettings
from src.pricing import default_pricing
from src.storage.cache import DocumentCache
from src.storage.run_loader import load_runs
from src.storage.run_log import RunLog, read_events
from src.tools import ToolConfig
from src.presentation.run_views import target_status_lines


class StopException(BaseException):
    """A UI's script-control stop (a BaseException, not an Exception): what a UI callback may raise to abort."""

PAYLOAD = {"identity": {"manufacturer": "קאדילאק", "commercial_name": "ESCALADE IQ", "government_record_id": "85095"},
           "engine_drivetrain": {"propulsion_normalized": "battery_electric", "power_hp": 750}}
IL, US = "https://www.cadillac.co.il/escalade-iq", "https://www.cadillac.com/en-us/escalade-iq"
HEADERS = {IL: 'קאדילאק אסקלייד IQ רכב חשמלי 750 כ"ס', US: "Cadillac Escalade IQ all-electric 750 hp"}
QUOTES = {"gear_count": "Transmission: single-speed"}       # a gear count is admitted only when stated
REQUESTED = [{"name": "electric_range_standard", "recovery_attempts": 1}, "ac_max_charging_power_kw",
             "cargo_volume_l", "torque_nm", "rear_legroom_mm", "gear_count",
             {"name": "fuel_tank_l", "applies_to": ["conventional"]}]


def turn(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def say(obj):
    return {"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)}


def store(cid, field, value, market, **kw):
    return _call(cid, "store_evidence", {"field": field, "value": value, "market": market,
                                         "source_url": IL if market == "IL" else US,
                                         "quote": QUOTES.get(field, labelled_quote(field, value)), **kw})


def seeded(make_ctx, script):
    """A tool context whose cache holds the retrieved pages the scripted evidence quotes."""
    ctx = make_ctx()
    seed_evidence_sources(ctx.cache, script, HEADERS)
    return ctx


class StopsWhenScriptEnds(ScriptedGLM):
    """Raises a UI StopException (a BaseException) on the first call past the script."""

    def chat(self, messages, tools=None, **kwargs):
        if not self.messages:
            self.requests.append({"messages": messages, "tools": tools, **kwargs})
            raise StopException()
        return super().chat(messages, tools=tools, **kwargs)


SCRIPT = [
    # primary research
    turn(store("p1", "ac_max_charging_power_kw", 19.2, "US"), store("p2", "cargo_volume_l", 2523, "US"),
         store("p3", "torque_nm", 1066, "IL"), store("p4", "torque_nm", 1080, "IL")),
    say({"summary": "primary", "fields": {}}),
    # recovery: electric_range_standard (1 attempt) -> by-product IL evidence for ac_max_charging_power_kw
    turn(store("r1", "ac_max_charging_power_kw", 19.2, "IL", variant_match="exact")),
    say({"field": "electric_range_standard", "status": "unresolved"}),
    # ac_max_charging_power_kw is skipped (resolved indirectly); cargo_volume_l starts:
    # turn 1 stores mid-attempt by-product evidence for gear_count, then the user presses Stop.
    turn(store("r2", "gear_count", 1, "IL", variant_match="exact")),
]


def interrupted_run(tmp_path, make_ctx):
    ctx = seeded(make_ctx, SCRIPT)
    client = StopsWhenScriptEnds(list(SCRIPT))
    log = RunLog(tmp_path / "runs", "b", "85095")
    with pytest.raises(StopException):                       # re-raised, never swallowed
        run_vehicle({"upstream_record_id": "85095"}, PAYLOAD, client=client, cache=ctx.cache, run_log=log,
                    config=AgentConfig(max_steps=3, no_new_research_turns=0, requested_fields=REQUESTED, recovery_mode="legacy",
                                       layered_harvest_enabled=False),
                    tool_config=ToolConfig(), session=ctx.session)
    return json.loads((log.dir / "result.json").read_text("utf-8")), client, read_events(log.events_path), log


def check_current_state(bundle, recovery):
    states = {name: s["state"] for name, s in bundle["field_states"].items()}
    assert states["ac_max_charging_power_kw"] == "ok"                  # indirect resolution survives
    assert states["gear_count"] == "ok"                                # evidence after the last finished attempt
    assert states["cargo_volume_l"] == "foreign_market_only"
    assert states["torque_nm"] == "conflicting"
    assert states["fuel_tank_l"] == "not_applicable"
    assert bundle["targets_without_stored_evidence"] == ["electric_range_standard", "rear_legroom_mm"]
    assert bundle["unresolved_targets"] == ["electric_range_standard", "cargo_volume_l", "torque_nm",
                                            "rear_legroom_mm"]
    # invariant: any requested field with an evidence record is never "without stored evidence"
    with_evidence = {e["field"] for e in bundle["evidence"]}
    assert not with_evidence & set(bundle["targets_without_stored_evidence"])
    assert recovery["current_states"]["ac_max_charging_power_kw"] == "ok"
    assert "ac_max_charging_power_kw" in recovery["fields_resolved_indirectly"]


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_interrupted_field_recovery_persists_accurate_partial_state(tmp_path, make_ctx):
    saved, client, events, log = interrupted_run(tmp_path, make_ctx)
    assert (saved["status"], saved["partial"], saved["interrupted"], saved["interrupted_phase"],
            saved["interruption_type"]) == ("interrupted", True, True, "field_recovery", "StopException")
    assert saved["error"] is None and saved["api_error"] is None and saved["api_errors"] == []   # not an error
    assert saved["interruption_message"].startswith("Run interrupted during Field Recovery. All completed")
    assert saved["output"] is None
    kinds = [e["kind"] for e in events]
    assert "finalization_started" not in kinds and saved["finalization"] is None              # no finalizer
    assert kinds.index("interrupted") < kinds.index("run_finished")
    assert len(client.requests) == 6 and not [r for r in client.requests if r.get("tools") is None]
    # mid-attempt evidence is preserved
    assert [(e["field"], e["market"]) for e in saved["evidence"]][-2:] == [
        ("ac_max_charging_power_kw", "IL"), ("gear_count", "IL")]
    assert any(e["kind"] == "field_recovery_queue_resolved_indirectly" for e in events)
    check_current_state(saved["research_bundle"], saved["field_recovery"])
    lines = target_status_lines(saved["research_bundle"])
    assert lines["no_evidence"] == ["electric_range_standard", "rear_legroom_mm"]
    assert "cargo_volume_l — foreign_market_only" in lines["unresolved"]
    assert "torque_nm — conflicting" in lines["unresolved"]
    assert not any("ac_max_charging_power_kw" in line for line in lines["unresolved"])


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_reconstruction_without_result_json_recomputes_current_state(tmp_path, make_ctx):
    saved, _, _, log = interrupted_run(tmp_path, make_ctx)
    (log.dir / "result.json").unlink()                       # as if the process had died instead
    run = load_runs(tmp_path / "runs", "b")[0]
    assert run["synthesized"] and run["status"] == "interrupted" and run["error"] is None
    assert (run["partial"], run["interrupted"], run["interrupted_phase"], run["interruption_type"]) == \
        (True, True, "field_recovery", "StopException")
    check_current_state(run["research_bundle"], run["field_recovery"])
    assert run["field_recovery"]["final_states"]["ok"] == 2  # current, not the stale primary snapshot


def test_stop_raised_by_the_ui_callback_is_not_swallowed(tmp_path, make_ctx):
    ctx = seeded(make_ctx, SCRIPT)
    seen_after_stop = []
    state = {"stopped": False}

    def listener(kind, event):
        if state["stopped"]:
            seen_after_stop.append(kind)
        if kind == "evidence" and event["evidence"]["field"] == "gear_count":
            state["stopped"] = True
            raise StopException()                            # a UI raises from its update callback

    log = RunLog(tmp_path / "runs", "b", "85095", listener=listener)
    client = ScriptedGLM(list(SCRIPT) + [say({"field": "cargo_volume_l", "status": "unresolved"})])
    with pytest.raises(StopException):
        run_vehicle({"upstream_record_id": "85095"}, PAYLOAD, client=client, cache=ctx.cache, run_log=log,
                    config=AgentConfig(max_steps=3, no_new_research_turns=0, requested_fields=REQUESTED, recovery_mode="legacy",
                                       layered_harvest_enabled=False),
                    tool_config=ToolConfig(), session=ctx.session)
    saved = json.loads((log.dir / "result.json").read_text("utf-8"))
    assert saved["status"] == "interrupted" and saved["interruption_type"] == "StopException"
    assert seen_after_stop == []                             # no UI callback while persisting
    assert saved["evidence"][-1]["field"] == "gear_count"    # the event written before the stop is kept
    assert saved["research_bundle"]["field_states"]["gear_count"]["state"] == "ok"
    assert len(client.requests) == 5                          # no model call after the stop


def test_glm_errors_stay_distinct_from_interruptions(tmp_path, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "k")
    session = ScriptedPostSession({"chat/completions": [chat_reply(tool_call("c1", "search_web", {"query": "x"})),
                                                         PostResponse(401, {"error": "auth"})],
                                   "web_search": [PostResponse(200, {"search_result": []})]})
    client = GLMClient(GLMSettings(api_key="k", model="glm-5.3-flash"), session=session, sleeper=lambda s: None)
    vehicle = next(v for v in benchmark_vehicles() if v["upstream_record_id"] == HANDSHAKE_RECORD_ID)
    result = research_one(vehicle, {"upstream_record_id": HANDSHAKE_RECORD_ID}, client=client,
                          cache=DocumentCache(tmp_path / "c"), runs_dir=tmp_path, batch_id="b",
                          agent_cfg=AgentConfig(), tool_cfg=ToolConfig(), pricing=default_pricing("glm-5.3-flash"),
                          level15_source="snapshot")
    assert result["status"] == "research_failed" and result["error"].startswith("GLMError")
    assert (result["partial"], result["interrupted"], result["interruption_type"]) == (True, False, None)
    assert result["api_errors"][0]["status"] == 401


@pytest.mark.final_assembly("llm")   # encodes the finalizer model's output (FINAL_ASSEMBLY=llm)
@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_completed_runs_and_level3_listing(tmp_path, make_ctx):
    script = [turn(store("p1", "torque_nm", 1066, "IL")), say({"summary": "done", "fields": {"torque_nm": {"value": 1066}}})]
    ctx = seeded(make_ctx, script)
    client = ScriptedGLM(script)
    result = run_vehicle({"upstream_record_id": "85095"}, PAYLOAD, client=client, cache=ctx.cache,
                         run_log=RunLog(tmp_path / "runs", "b", "85095"),
                         config=AgentConfig(field_recovery_enabled=False, requested_fields=["torque_nm", "gear_count"],
                                            include_level3=True),     # Level 3 is opt-in; requested here
                         tool_config=ToolConfig(), session=ctx.session)
    assert result["status"] == "completed" and result["output"]["summary"] == "done"
    assert (result["partial"], result["interrupted"], result["interrupted_phase"]) == (False, False, None)
    assert AgentConfig().include_level3 is False                               # off unless asked for
    bundle = result["research_bundle"]
    assert bundle["targets_without_stored_evidence"] == ["gear_count"] and bundle["unresolved_targets"] == ["gear_count"]
    assert "known_issues_reliability" in bundle["level3_topics_without_evidence"]
    assert not any(str(t).startswith("level3:") for t in bundle["unresolved_targets"])
    lines = target_status_lines(bundle)
    assert lines["unresolved"] == ["gear_count — missing"] and "recalls" in lines["level3"]
    # bundles written before the split still render without mixing Level 3 into Level 2
    legacy = target_status_lines({"unresolved_targets": ["tire_size_front", "level3:recalls"]})
    assert legacy == {"no_evidence": [], "unresolved": ["tire_size_front — unresolved"], "level3": ["recalls"]}


def test_finalize_existing_on_an_interrupted_run_keeps_the_interruption_as_history(tmp_path, make_ctx):
    from src.recovery import finalize_existing_run

    saved, _, _, log = interrupted_run(tmp_path, make_ctx)
    client = ScriptedGLM([say({"summary": "recovered", "fields": {}})])
    result = finalize_existing_run(tmp_path / "runs", "b", "85095", client=client, cache=DocumentCache(tmp_path / "c"),
                                   config=AgentConfig())
    assert result["status"] == "recovered_finalized" and result["partial"] is False and result["interrupted"] is False
    assert result["recovery"]["prior_interruption"]["interrupted_phase"] == "field_recovery"
