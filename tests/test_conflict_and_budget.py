"""Same-scope conflicts and the hard per-vehicle field-recovery turn budget.
Scripted GLM / fake HTTP only: no network, no paid calls."""

import json

from conftest import seed_evidence_sources
from test_tools_smoke import ScriptedGLM, _call

from src import cli
from src.agent import FIELD_RECOVERY_SYSTEM_PROMPT, AgentConfig, agent_config_from_env, run_vehicle
from src.benchmark import compute_metrics
from src.field_recovery import evaluate_field, material_key, same_scope_conflict
from src.storage.run_log import RunLog, read_events
from src.tools import ToolConfig

PAYLOAD = {"identity": {"manufacturer": "קאדילאק", "commercial_name": "ESCALADE IQ", "year": 2025,
                        "trim": "SPORT", "model_code": "X1", "government_record_id": "85095"},
           "engine_drivetrain": {"propulsion_normalized": "battery_electric", "drivetrain_normalized": "awd",
                                 "power_hp": 750}}
IL_PAGE = "https://www.cadillac.co.il/escalade-iq"
US_PAGE = "https://www.cadillac.com/escalade-iq"
# Identity text of the retrieved pages (they name the exact technical variant: model, propulsion, power)
HEADERS = {IL_PAGE: 'קאדילאק אסקלייד IQ 2025 רכב חשמלי 750 כ"ס AWD', US_PAGE: "2025 Cadillac Escalade IQ, all-electric, 750 hp AWD"}


def ev(value, market="IL", **extra):
    return {"evidence_id": extra.pop("evidence_id", f"e{value}"), "field": "torque_nm", "value": value,
            "market": market, "source_url": IL_PAGE if market == "IL" else US_PAGE, "quote": f"{value} Nm", **extra}


def state(evidence, declared=None, last_seq=None):
    return evaluate_field({"name": "torque_nm", "applicable": True}, evidence, declared, None, "IL", last_seq)


def turn(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def say(obj):
    return {"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)}


def store(cid, value, market="IL", **extra):
    args = {"field": "torque_nm", "value": value, "unit": "Nm", "market": market,
            "source_url": IL_PAGE if market == "IL" else US_PAGE, "quote": f"{value} Nm", **extra}
    return _call(cid, "store_evidence", args)


def run(tmp_path, make_ctx, script, **cfg):
    ctx = make_ctx()
    seed_evidence_sources(ctx.cache, script, HEADERS)
    client = ScriptedGLM(script)
    log = RunLog(tmp_path / "runs", "b", "85095")
    # every model turn is scripted, so the layered document sweep (tested on its own) is off here
    config = AgentConfig(**{"max_steps": 4, "no_new_research_turns": 0, "requested_fields": ["torque_nm"],
                            "layered_harvest_enabled": False, **cfg})
    result = run_vehicle({"upstream_record_id": "85095"}, PAYLOAD, client=client, cache=ctx.cache, run_log=log,
                         config=config, tool_config=ToolConfig(), session=ctx.session)
    return result, client, read_events(log.events_path)


def packets(client):
    return [json.loads(r["messages"][1]["content"].split("\n", 1)[1]) for r in client.requests
            if r["messages"][0]["content"] == FIELD_RECOVERY_SYSTEM_PROMPT and len(r["messages"]) == 2]


# --- the conflict rule ---------------------------------------------------------------------------

def test_same_scope_different_values_are_conflicting_and_retry_eligible():
    result = state([ev(1066), ev(1080)])                                    # 1, 2
    assert result["state"] == "conflicting" and result["retry_eligible"]
    assert result["conflict_evidence_ids"] == ["e1066", "e1080"]
    assert state([ev(1066), ev("1,066 Nm")])["state"] == "ok"               # not materially different
    assert material_key("1,066 Nm") == material_key(1066) != material_key(1080)
    assert material_key("255/45 R20") != material_key("255/40 R21")         # never reduced to a first number
    assert state([ev(1066), ev(1080, variant_match="unclear")])["state"] == "conflicting"


def test_other_market_or_other_variant_is_not_a_same_scope_conflict():
    il_us = state([ev(1066), ev(1080, market="US")])                        # 4
    assert il_us["state"] == "ok" and "multiple_values" not in il_us["info"]
    assert state([ev(1066, market="US"), ev(1080, market="UK")])["state"] == "foreign_market_only"
    other_variant = state([ev(1066), ev(1080, variant_match="different")])  # 5
    assert other_variant["state"] == "ok" and other_variant["conflict_evidence_ids"] == []
    assert same_scope_conflict([ev(1066), ev(1080, variant_match="different")], "IL") == []


def test_only_an_explicit_newer_resolution_clears_a_conflict():
    both = [ev(1066), ev(1080)]
    assert state(both, {"status": "found", "seq": 9}, last_seq=5)["state"] == "conflicting"
    backed = {"status": "conflict_resolved", "seq": 9, "evidence_ids": ["e1066"]}
    resolved = state(both, backed, last_seq=5)                                            # 6
    assert resolved["state"] == "ok" and "conflict_resolved_by_model" in resolved["info"]
    assert resolved["values"] == [1066, 1080]                                           # nothing removed
    # A newer conflicting value after the resolution re-opens the conflict.
    assert state(both + [ev(1100)], backed, last_seq=12)["state"] == "conflicting"
    # Without evidence references the resolution does not close the conflict.
    assert state(both, {"status": "conflict_resolved", "seq": 9}, last_seq=5)["state"] == "conflicting"
    assert state(both, {"status": "conflicting", "seq": 9}, last_seq=5)["state"] == "conflicting"


# --- mocked Cadillac: 1066 vs 1080 ------------------------------------------------------------------

def cadillac_script(attempt2):
    return [
        # primary research: only a US value -> foreign_market_only
        turn(store("p1", 1080, market="US")),
        say({"summary": "primary", "fields": {"torque_nm": {"value": 1080, "market": "US"}}}),
        # attempt 1 finds two Israeli figures and replies "found" (the real run's foreign_market_only -> ok bug)
        turn(store("r1", 1066), store("r2", 1080, quote="מומנט 1,080 ניוטון-מטר")),
        say({"field": "torque_nm", "status": "found", "value": 1066}),
        *attempt2,
        say({"summary": "final", "fields": {"torque_nm": {"value": None}}}),
    ]


def test_cadillac_unresolved_conflict_reaches_finalizer_with_both_candidates(tmp_path, make_ctx):
    attempt2 = [turn(_call("r3", "search_web", {"query": "Escalade IQ torque 1066 1080 boost mode"})),
                say({"field": "torque_nm", "status": "conflicting",
                     "notes": "1080 may be the Velocity Max figure; not established for this trim"})]
    result, client, events = run(tmp_path, make_ctx, cadillac_script(attempt2))
    rec = result["field_recovery"]
    assert rec["queue"] == ["torque_nm"] and rec["primary_states"] == {"foreign_market_only": 1}
    assert [(a["state_before"], a["state_after"]) for a in rec["attempts"]] == [
        ("foreign_market_only", "conflicting"), ("conflicting", "conflicting")]        # 7: no "ok" after "found"
    second = packets(client)[1]                                                         # 3
    assert second["failure_reason"] == "conflicting" and second["attempt"] == 2
    assert [c["value"] for c in second["conflict"]["candidates"]] == [1066, 1080]
    assert {c["market"] for c in second["conflict"]["candidates"]} == {"IL"}
    assert "Investigate why 1066 vs 1080 differ" in second["conflict"]["task"]
    assert "boost/performance mode" in second["conflict"]["task"]
    assert [c["value"] for c in second["existing_candidates"]] == [1080, 1066, 1080]   # every candidate kept
    assert rec["final_states"] == {"conflicting": 1} and rec["fields_still_failed"] == ["torque_nm"]
    # The finalizer gets both candidates and the conflicting state; code chose nothing.
    bundle = json.loads(client.requests[-1]["messages"][1]["content"].split("\n", 1)[1].rsplit("\n\nReturn", 1)[0])
    assert bundle["field_states"]["torque_nm"]["state"] == "conflicting"
    assert [f["value"] for f in bundle["candidate_facts"]["torque_nm"]] == [1080, 1066, 1080]
    assert len(bundle["field_states"]["torque_nm"]["conflict_evidence_ids"]) == 2
    assert len(result["evidence"]) == 3 and result["status"] == "completed"
    assert compute_metrics(result)["fields_conflicting_final"] == 1


def test_cadillac_conflict_explicitly_resolved_in_attempt_two(tmp_path, make_ctx):
    attempt2 = [turn(store("r3", 1066, quote="מומנט רציף 1,066 ניוטון-מטר; 1,080 במצב Velocity Max",
                           variant="Sport AWD", variant_match="exact")),
                say({"field": "torque_nm", "status": "conflict_resolved", "value": 1066, "evidence_ids": ["e4"],
                     "notes": "1080 Nm is the temporary boost figure"})]
    result, client, _ = run(tmp_path, make_ctx, cadillac_script(attempt2))
    rec = result["field_recovery"]
    assert [a["state_after"] for a in rec["attempts"]] == ["conflicting", "ok"]          # 6
    assert rec["fields_recovered"] == ["torque_nm"] and rec["final_states"] == {"ok": 1}
    assert [e["value"] for e in result["evidence"]] == [1080, 1066, 1080, 1066]          # both candidates kept


# --- the hard per-vehicle budget ---------------------------------------------------------------------

def test_default_recovery_budget_is_24(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("FIELD_RECOVERY_MAX_TOTAL_STEPS", raising=False)                  # 8
    assert AgentConfig().field_recovery_max_total_steps == 24
    assert agent_config_from_env().field_recovery_max_total_steps == 24
    assert (AgentConfig().field_recovery_max_attempts, AgentConfig().field_recovery_max_steps) == (2, 4)
    monkeypatch.setenv("FIELD_RECOVERY_MAX_TOTAL_STEPS", "0")
    assert agent_config_from_env().field_recovery_max_total_steps == 0                    # 0 still means no cap
    monkeypatch.delenv("FIELD_RECOVERY_MAX_TOTAL_STEPS")
    for name in ("GLM_API_KEY", "DATABASE_URL", "SUPABASE_DB_URL", "ENRICHMENT_FIELDS"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["--dry-run", "--model", "glm-5.3-flash", "--runs-dir", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["agent_config"]["field_recovery_max_total_steps"] == 24
    assert out["field_recovery_worst_case_model_turns"] == 24
    assert "FIELD_RECOVERY_MAX_TOTAL_STEPS=24" in open(".env.example", encoding="utf-8").read()


FIELDS = ["torque_nm", "paint_code", "service_interval_km", "rear_legroom_mm", "tire_size_front", "boot_floor_mm"]


def budget_script(turns):
    """Primary research stores one usable field; every recovery turn only reports 'unresolved'."""
    script = [turn(_call("p1", "store_evidence", {"field": "torque_nm", "value": 1066, "market": "IL",
                                                   "source_url": IL_PAGE, "quote": "1,066 Nm"})),
              say({"summary": "primary", "fields": {}})]
    # Breadth-first recovery: round 1 gives every field its first attempt (4 turns each), round 2 starts again
    # with the first field still unresolved.
    order = [f for f in FIELDS[1:] for _ in range(4)] + [f for f in FIELDS[1:] for _ in range(4)]
    for i in range(turns):
        field = order[i]
        calls = [_call(f"r{i}", "report_field_status", {"field": field, "status": "unresolved"})]
        if i == 8:  # by-product evidence collected mid-recovery must still reach the finalizer
            calls.append(_call("rx", "store_evidence", {"field": "service_interval_km", "value": 20000, "market": "US",
                                                          "source_url": US_PAGE, "quote": "every 20,000 km"}))
        script.append(turn(*calls))
    script.append(say({"summary": "final", "fields": {}}))
    return script


def test_budget_of_24_stops_recovery_and_the_run_still_finalizes(tmp_path, make_ctx):
    result, client, events = run(tmp_path, make_ctx, budget_script(24), requested_fields=FIELDS)  # default cap 24
    rec = result["field_recovery"]
    assert rec["queue"] == FIELDS[1:]
    assert (rec["field_recovery_turn_budget"], rec["field_recovery_turns_used"],
            rec["field_recovery_turns_remaining"], rec["stopped"]) == (24, 24, 0, "max_total_steps")      # 9
    # breadth-first: all five fields got attempt 1 (20 turns) before paint_code got attempt 2 (4 turns)
    assert rec["attempt_order"] == ["paint_code#1", "service_interval_km#1", "rear_legroom_mm#1",
                                    "tire_size_front#1", "boot_floor_mm#1", "paint_code#2"]
    assert rec["fields_retried"] == sorted(FIELDS[1:]) and rec["attempt_count"] == 6
    assert rec["fields_not_attempted_due_to_budget"] == []                                           # 11
    assert rec["recovery_fields_given_first_attempt"] == 5 and rec["recovery_second_attempts_started"] == 1
    assert rec["field_cut_short_by_budget"] is None
    recovery_turns = [e for e in events if e["kind"] == "model_response" and e.get("phase") == "field_recovery"]
    assert len(recovery_turns) == 24 and len(client.requests) == 1 + 1 + 24 + 1
    assert result["status"] == "completed" and result["output"]["summary"] == "final" and not result["error"]
    # 10: everything collected (primary + mid-recovery by-product) reaches the finalizer.
    bundle = json.loads(client.requests[-1]["messages"][1]["content"].split("\n", 1)[1].rsplit("\n\nReturn", 1)[0])
    assert {e["field"] for e in bundle["evidence"]} == {"torque_nm", "service_interval_km"}
    assert bundle["field_states"]["service_interval_km"]["state"] in ("foreign_market_only", "unresolved")
    assert bundle["field_states"]["tire_size_front"]["state"] == "unresolved"   # attempted (round 1), unresolved
    budget_event = next(e for e in events if e["kind"] == "field_recovery_budget_exhausted")
    assert budget_event["fields_not_attempted"] == []
    m = compute_metrics(result)
    assert (m["field_recovery_turns_used"], m["fields_not_attempted_due_to_budget"]) == (24, 0)


def test_budget_cuts_a_field_mid_attempt(tmp_path, make_ctx):
    result, client, _ = run(tmp_path, make_ctx, budget_script(6), requested_fields=FIELDS,
                            field_recovery_max_total_steps=6)
    rec = result["field_recovery"]
    assert rec["field_recovery_turns_used"] == 6 and rec["stopped"] == "max_total_steps"
    # breadth-first: the cap cuts the SECOND field's first attempt, never a second attempt of the first field
    assert [(a["field"], a["attempt"], a["turns"]) for a in rec["attempts"]] == [("paint_code", 1, 4),
                                                                                 ("service_interval_km", 1, 2)]
    assert rec["field_cut_short_by_budget"] == "service_interval_km"
    assert rec["fields_not_attempted_due_to_budget"] == FIELDS[3:]
    assert result["output"]["summary"] == "final"
