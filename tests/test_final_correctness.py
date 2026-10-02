"""Final correctness patch for field recovery: declaration freshness, evidence-backed conflict
resolution, stricter early exit, budget semantics, supplementary evidence and interrupted state.
Scripted GLM / fake HTTP only: no network, no paid calls."""

import json

import pytest
from streamlit.runtime.scriptrunner_utils.exceptions import StopException

from conftest import cache_source, labelled_quote, seed_evidence_sources
from test_conflict_and_budget import budget_script, FIELDS
from test_tools_smoke import ScriptedGLM, _call

from src.agent import AgentConfig, run_vehicle
from src.benchmark import compute_metrics
from src.field_recovery import early_resolution_check, evaluate_field
from src.storage.run_loader import load_runs
from src.storage.run_log import RunLog, read_events
from src.tools import ToolConfig, dispatch

PAYLOAD = {"identity": {"manufacturer": "קאדילאק", "commercial_name": "ESCALADE IQ", "trim": "PREMIUM SPORT",
                        "government_record_id": "85095"},
           "engine_drivetrain": {"propulsion_normalized": "battery_electric", "power_hp": 750}}
IL, US = "https://www.cadillac.co.il/escalade-iq", "https://www.cadillac.com/escalade-iq"
IL_NEWS = "https://www.cadillac.co.il/news/escalade-iq"
# Retrieved pages: IL and US name the exact technical variant (server binding -> exact); the IL news item names
# only the model (server binding -> unclear), whatever variant_match the model claims.
HEADERS = {IL: 'קאדילאק אסקלייד IQ רכב חשמלי 750 כ"ס', US: "Cadillac Escalade IQ all-electric 750 hp",
           IL_NEWS: "קאדילאק אסקלייד IQ: כתבה"}
SPEC = {"name": "torque_nm", "applicable": True}


def ev(eid, value, market="IL", **kw):
    return {"evidence_id": eid, "field": kw.pop("field", "torque_nm"), "value": value, "market": market,
            "source_url": IL if market == "IL" else US, "quote": str(value), **kw}


def turn(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def say(obj):
    return {"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)}


def store(cid, field, value, market="IL", url=None, **kw):
    return _call(cid, "store_evidence", {"field": field, "value": value, "market": market,
                                         "source_url": url or (IL if market == "IL" else US),
                                         "quote": labelled_quote(field, value), **kw})


def run(tmp_path, make_ctx, script, client_cls=ScriptedGLM, **cfg):
    ctx = make_ctx()
    seed_evidence_sources(ctx.cache, script, HEADERS)
    client = client_cls(script)
    log = RunLog(tmp_path / "runs", "b", "85095")
    # every model turn is scripted, so the layered document sweep (tested on its own) is off here
    config = AgentConfig(**{"max_steps": 3, "no_new_research_turns": 0, "layered_harvest_enabled": False, **cfg})
    result = run_vehicle({"upstream_record_id": "85095"}, PAYLOAD, client=client, cache=ctx.cache, run_log=log,
                         config=config, tool_config=ToolConfig(), session=ctx.session)
    return result, client, read_events(log.events_path), log


# --- 3: declaration freshness for every status -------------------------------------------------------

def test_declarations_only_count_while_newer_than_the_fields_evidence():
    foreign = [ev("e1", 1066, market="US")]
    state = lambda evidence, declared, last, **kw: evaluate_field(SPEC, evidence, declared, None, "IL", last,
                                                                  **kw)["state"]
    # stale found: newer evidence is evaluated again (here: foreign only)
    assert state(foreign, {"status": "found", "seq": 5}, 9) == "foreign_market_only"
    assert state(foreign, {"status": "found", "seq": 12}, 9) == "ok"                     # current found
    il = [ev("e1", 1066)]
    # stale not_applicable: real evidence stored afterwards wins
    assert state(il, {"status": "not_applicable", "seq": 5}, 9) == "ok"
    assert state(il, {"status": "not_applicable", "seq": 12}, 9) == "not_applicable"   # current declaration
    for status in ("unresolved", "conflicting", "foreign_market_only", "variant_not_exact", "weak_provenance"):
        assert state(il, {"status": status, "seq": 5}, 9) == "ok"                       # stale retry states
        assert state(il, {"status": status, "seq": 12}, 9) == status
    assert state(il, {"status": "unresolved", "seq": 5}, None) == "unresolved"          # unknown order: kept
    info = evaluate_field(SPEC, il, {"status": "found", "seq": 5}, None, "IL", 9)["info"]
    assert "stale_declaration:found" in info
    # the primary JSON is a statement too
    stale_out = evaluate_field(SPEC, il, None, {"provenance": "not_applicable"}, "IL", 9, output_seq=4)
    assert stale_out["state"] == "ok"


# --- 4: conflict_resolved must be evidence-backed ----------------------------------------------------------

def test_conflict_resolution_needs_valid_cited_evidence():
    base = [ev("e26", 1066), ev("e27", 1080)]
    seqs = {"e26": 10, "e27": 11, "e41": 20, "e50": 21}
    other = ev("e50", 7, field="gear_count")

    def state(declared, evidence=base):
        return evaluate_field(SPEC, evidence, {"seq": 30, **declared}, None, "IL", 20, evidence_seq=seqs)["state"]

    assert state({"status": "conflict_resolved", "evidence_ids": []}) == "conflicting"
    assert state({"status": "conflict_resolved", "evidence_ids": ["e999"]}) == "conflicting"
    assert state({"status": "conflict_resolved", "evidence_ids": ["e50"]}) == "conflicting"   # other field's id
    assert state({"status": "conflict_resolved", "evidence_ids": ["e26"]}) == "conflicting"   # pre-conflict only
    with_new = base + [ev("e41", 1066, variant="Premium Sport", variant_match="exact")]
    result = evaluate_field(SPEC, with_new, {"status": "conflict_resolved", "evidence_ids": ["e41"], "seq": 30},
                            None, "IL", 20, evidence_seq=seqs)
    assert result["state"] == "ok" and result["values"] == [1066, 1080, 1066]             # nothing removed
    assert "conflict_resolved_by_model" in result["info"]
    assert other["field"] == "gear_count"


def test_report_field_status_requires_evidence_for_conflict_resolved(make_ctx):
    ctx = make_ctx()
    cache_source(ctx.cache, IL, "Torque 1066 Nm. Transmission: 8-speed automatic.")
    assert dispatch(ctx, "store_evidence", {"field": "torque_nm", "value": 1066, "market": "IL", "source_url": IL,
                                            "quote": "Torque 1066 Nm"})["stored"]
    assert dispatch(ctx, "store_evidence", {"field": "gear_count", "value": 8, "market": "IL", "source_url": IL,
                                            "quote": "8-speed automatic"})["stored"]
    for ids in (None, [], ["e999"], ["e2"]):
        out = dispatch(ctx, "report_field_status", {"field": "torque_nm", "status": "conflict_resolved",
                                                    "evidence_ids": ids})
        assert out["error"] == "invalid_arguments" and "evidence_ids" in out["message"]
    ok = dispatch(ctx, "report_field_status", {"field": "torque_nm", "status": "conflict_resolved",
                                               "evidence_ids": ["e1"]})
    assert ok["recorded"] and ok["evidence_ids"] == ["e1"]
    assert dispatch(ctx, "report_field_status", {"field": "torque_nm", "status": "unresolved"})["recorded"]


def test_unbacked_reply_keeps_conflict_and_backed_reply_resolves(tmp_path, make_ctx):
    script = [
        turn(store("p1", "torque_nm", 1066), store("p2", "torque_nm", 1080)),
        say({"summary": "primary", "fields": {}}),
        # attempt 1: new exact-scope evidence, but the reply cites nothing -> still conflicting
        turn(store("r1", "torque_nm", 1066, variant="Premium Sport", variant_match="exact",
                   quote="מומנט רציף 1,066 ניוטון-מטר")),
        say({"field": "torque_nm", "status": "conflict_resolved", "value": 1066}),
        # attempt 2: the same resolution citing the evidence stored after the conflict arose
        say({"field": "torque_nm", "status": "conflict_resolved", "value": 1066, "evidence_ids": ["e3"]}),
        say({"summary": "final", "fields": {}}),
    ]
    result, client, events, _ = run(tmp_path, make_ctx, script, requested_fields=["torque_nm"])
    rec = result["field_recovery"]
    assert [(a["attempt"], a["state_after"]) for a in rec["attempts"]] == [(1, "conflicting"), (2, "ok")]
    assert not rec["attempts"][0]["early_resolved"]           # conflicting never exits early
    assert [e["value"] for e in result["evidence"]] == [1066, 1080, 1066]
    bundle = json.loads(client.requests[-1]["messages"][1]["content"].split("\n", 1)[1].rsplit("\n\nReturn", 1)[0])
    assert [f["value"] for f in bundle["candidate_facts"]["torque_nm"]] == [1066, 1080, 1066]


# --- 5: stricter early exit --------------------------------------------------------------------------------

def test_early_exit_rule(tmp_path, make_ctx):
    def events_for(*items):
        return [{"kind": "evidence", "seq": i + 1, "evidence": item} for i, item in enumerate(items)]

    spec = {"name": "ac_max_charging_power_kw", "applicable": True}
    f = "ac_max_charging_power_kw"
    may, state = early_resolution_check(spec, events_for(ev("e1", 11.5, field=f, variant_match="unclear")), "IL")
    assert state["state"] == "ok" and may is False                      # usable, but not enough to stop
    assert early_resolution_check(spec, events_for(ev("e1", 19.2, field=f, variant_match="exact")), "IL")[0]
    assert early_resolution_check(spec, events_for(ev("e1", 19.2, field=f)), "IL")[0]   # no variant distinction
    may, state = early_resolution_check(spec, events_for(ev("e1", 19.2, field=f, variant_match="different")), "IL")
    assert (may, state["state"]) == (False, "variant_not_exact")
    assert not early_resolution_check(spec, events_for(ev("e1", 19.2, field=f, market="US")), "IL")[0]


def test_unclear_variant_continues_the_attempt(tmp_path, make_ctx):
    script = [say({"summary": "primary", "fields": {}}),
              turn(store("r1", "ac_max_charging_power_kw", 11.5, url=IL_NEWS, variant_match="exact")),  # server: unclear
              turn(store("r2", "ac_max_charging_power_kw", 19.2, variant="Premium Sport", variant_match="exact")),
              say({"field": "ac_max_charging_power_kw", "status": "conflict_resolved", "evidence_ids": ["e2"]}),
              say({"summary": "final", "fields": {}})]
    result, client, events, _ = run(tmp_path, make_ctx, script, requested_fields=["ac_max_charging_power_kw"])
    first = result["field_recovery"]["attempts"][0]
    assert first["turns"] == 3 and not first["early_resolved"] and first["state_after"] == "ok"
    assert not [e for e in events if e["kind"] == "field_recovery_early_resolved"]


def test_exact_variant_still_exits_early_with_honest_metrics(tmp_path, make_ctx):
    script = [say({"summary": "primary", "fields": {}}),
              turn(store("r1", "ac_max_charging_power_kw", 19.2, variant_match="exact")),
              say({"summary": "final", "fields": {}})]
    result, client, events, _ = run(tmp_path, make_ctx, script, requested_fields=["ac_max_charging_power_kw"])
    rec = result["field_recovery"]
    assert rec["attempts"][0]["early_resolved"] and rec["attempts"][0]["turns"] == 1
    assert (rec["early_resolution_count"], rec["turn_budget_skipped_by_early_resolution"]) == (1, 3)
    m = compute_metrics(result)
    assert (m["field_recovery_early_resolutions"], m["field_recovery_turn_budget_skipped_by_early_resolution"]) == (1, 3)
    early = next(e for e in events if e["kind"] == "field_recovery_early_resolved")
    assert early["turn_budget_skipped"] == 3


# --- 8: budget semantics -------------------------------------------------------------------------------------

def test_not_cut_short_when_the_next_attempt_never_started(tmp_path, make_ctx):
    result, _, _, _ = run(tmp_path, make_ctx, budget_script(4), requested_fields=FIELDS, max_steps=4,
                          field_recovery_max_total_steps=4)
    rec = result["field_recovery"]
    assert [(a["field"], a["attempt"], a["turns"]) for a in rec["attempts"]] == [("paint_code", 1, 4)]
    assert rec["stopped"] == "max_total_steps" and rec["field_cut_short_by_budget"] is None


# --- 9: supplementary evidence survives reconstruction ------------------------------------------------------

def test_supplementary_evidence_is_rebuilt_from_events(tmp_path, make_ctx):
    same = {"field": "torque_nm", "value": 1066, "unit": "Nm", "market": "IL", "source_url": IL}
    script = [turn(_call("c1", "store_evidence", {**same, "quote": "Torque 1,066 Nm"})),
              turn(_call("c2", "store_evidence", {**same, "quote": "מומנט 1,066", "note": "Hebrew page"})),
              say({"summary": "done", "fields": {}})]
    result, _, _, log = run(tmp_path, make_ctx, script, field_recovery_enabled=False, requested_fields=["torque_nm"])
    live = result["evidence"]
    assert len(live) == 1 and live[0]["supplementary"] == [{"quote": "מומנט 1,066", "note": "Hebrew page"}]
    bundle_items = result["research_bundle"]["evidence"]            # the bundle (built from events) agrees ...
    assert [(e["evidence_id"], e["value"], e["quote"]) for e in bundle_items] == [("e1", 1066, "Torque 1,066 Nm")]
    assert "supplementary" not in bundle_items[0] and "note" not in bundle_items[0]   # ... minus model notes
    (log.dir / "result.json").unlink()
    rebuilt = load_runs(tmp_path / "runs", "b")[0]
    assert rebuilt["evidence"] == live and len(rebuilt["evidence"]) == 1


# --- 1 / 10: interruption mid-attempt for the field being recovered ----------------------------------------

class StopAtEnd(ScriptedGLM):
    def chat(self, messages, tools=None, **kwargs):
        if not self.messages:
            self.requests.append({"messages": messages, "tools": tools, **kwargs})
            raise StopException()
        return super().chat(messages, tools=tools, **kwargs)


def test_interrupt_after_target_evidence_before_attempt_finished(tmp_path, make_ctx):
    script = [turn(store("p1", "cargo_volume_l", 2523, market="US")),
              say({"summary": "primary", "fields": {}}),
              turn(store("r1", "cargo_volume_l", 2523, url=IL_NEWS))]   # server binding unclear: no early exit; Stop
    with pytest.raises(StopException):
        run(tmp_path, make_ctx, script, client_cls=StopAtEnd, requested_fields=["cargo_volume_l", "gear_count"])
    log_dir = tmp_path / "runs" / "b" / "85095"
    saved = json.loads((log_dir / "result.json").read_text("utf-8"))
    assert saved["status"] == "interrupted" and saved["partial"] and saved["finalization"] is None
    assert not [e for e in read_events(log_dir / "events.jsonl") if e["kind"] == "field_recovery_finished"]
    for run_view in (saved, None):
        if run_view is None:
            (log_dir / "result.json").unlink()
            run_view = load_runs(tmp_path / "runs", "b")[0]
        bundle, rec = run_view["research_bundle"], run_view["field_recovery"]
        assert bundle["field_states"]["cargo_volume_l"]["state"] == "ok"          # newest evidence counts
        assert "cargo_volume_l" not in bundle["targets_without_stored_evidence"]
        assert "cargo_volume_l" not in bundle["unresolved_targets"]
        assert bundle["unresolved_target_states"] == [{"field": "gear_count", "state": "missing"}]
        assert bundle["level2_targets_without_evidence"] == ["gear_count"]
        assert rec["current_states"]["cargo_volume_l"] == "ok"
        assert rec["attempts"] == [] and rec["evaluation_primary"][0]["state"] == "foreign_market_only"  # history


def test_foreign_evidence_is_unresolved_but_never_without_evidence(tmp_path, make_ctx):
    script = [turn(store("p1", "cargo_volume_l", 2523, market="US")), say({"summary": "s", "fields": {}})]
    result, _, _, _ = run(tmp_path, make_ctx, script, field_recovery_enabled=False,
                          requested_fields=["cargo_volume_l", "gear_count"])
    bundle = result["research_bundle"]
    assert "cargo_volume_l" not in bundle["targets_without_stored_evidence"]
    assert {"field": "cargo_volume_l", "state": "foreign_market_only"} in bundle["unresolved_target_states"]
