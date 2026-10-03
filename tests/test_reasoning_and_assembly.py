"""PR 30: reasoning effort instead of "disabled thinking" (Part A), complete run profiles (Part B), Deterministic Final
Assembly (Part C) and recovery failure isolation (Part D). Scripted GLM / fake HTTP only: no network, no paid calls."""

import json

import pytest
import requests

from fixtures import corolla_tail as tail
from test_phase_contracts import EU, GATE_OFF, PhaseClient, fetch, run, say
from test_tail_recovery import item

from src import diagnostics as D
from src import run_profiles as R
from src.agent import CLUSTER_RECOVERY_SYSTEM_PROMPT, AgentConfig, ModelCaller, agent_config_from_env
from src.benchmark import compute_metrics
from src.final_assembly import NARRATION_SYSTEM_PROMPT, assemble_output, run_deterministic_finalization
from src.fields import load_schema
from src.glm_client import ChatResponse, GLMClient, GLMError, GLMSettings
from src.phase_settings import for_phase
from src.recovery import finalize_existing_run
from src.storage.cache import DocumentCache
from src.storage.run_log import RunLog, read_events
from src.ui.settings_panel import merge_effort_settings

PHASES = ("research", "document_sweep", "field_recovery", "finalization")
DEFAULT_EFFORTS = {"research": "high", "document_sweep": "low", "field_recovery": "low", "finalization": "low"}
SCHEMA = {s["name"]: s for s in load_schema()}


class PostSession:
    """Records every chat payload; answers from `replies` (status, body) in order, else a plain 200."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.payloads = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.payloads.append(json.loads(data))
        status, body = self.replies.pop(0) if self.replies else (200, None)
        if body is None:
            body = {"choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                              "completion_tokens_details": {"reasoning_tokens": 3}}}
        resp = requests.Response()
        resp.status_code = status
        resp._content = json.dumps(body).encode("utf-8")
        resp.headers["Content-Type"] = "application/json"
        return resp


def http_caller(tmp_path, config, replies=None, max_attempts=2):
    session = PostSession(replies)
    client = GLMClient(GLMSettings(api_key="k", model="glm-5.3", chat_max_attempts=max_attempts), session=session,
                       sleeper=lambda s: None)
    log = RunLog(tmp_path, "b", "1")
    client.hook = lambda kind, **data: log.event(kind, **data)          # per-attempt api_call / api_error
    return ModelCaller(client, log, config), session, log


def every_phase(caller):
    for phase in PHASES:
        caller([{"role": "user", "content": "x"}], phase=phase)


# --- Part A: reasoning effort ----------------------------------------------------------------------------------

DISABLED_ENVS = {
    "GLM_THINKING": {"GLM_THINKING": "disabled"},
    "GLM_DOCUMENT_SWEEP_THINKING": {"GLM_DOCUMENT_SWEEP_THINKING": "disabled"},
    "GLM_EXTRA_BODY": {"GLM_EXTRA_BODY": '{"thinking": {"type": "disabled"}, "custom_flag": true}'},
}


@pytest.mark.parametrize("name", sorted(DISABLED_ENVS))
@pytest.mark.parametrize("profile", R.PROFILES)
def test_no_request_ever_carries_thinking_disabled(tmp_path, name, profile):
    env = DISABLED_ENVS[name]
    config = R.build_agent_config(env.get, {}, profile)
    caller, session, log = http_caller(tmp_path, config)
    every_phase(caller)
    assert len(session.payloads) == 4
    for payload in session.payloads:
        assert (payload.get("thinking") or {}).get("type") != "disabled"
        assert "thinking" not in payload                       # "disabled" is mapped to: no thinking object at all
    efforts = [p.get("reasoning_effort") for p in session.payloads]
    mapped = [e for e in read_events(log.events_path) if e["kind"] == "thinking_disabled_mapped"]
    if profile != R.CUSTOM:        # a named profile pins every phase to the code defaults: nothing to map
        assert efforts == ["high", "low", "low", "low"] and mapped == []
        if name == "GLM_EXTRA_BODY":                           # a named profile ignores extra_body entirely
            assert not any("custom_flag" in p for p in session.payloads)
        return
    if name == "GLM_DOCUMENT_SWEEP_THINKING":   # only the sweep is configured "disabled"
        assert efforts == ["high", "low", "low", "low"]
        assert len(mapped) == 1 and mapped[0]["phase"] == "document_sweep"
    else:                                       # every phase: its phase default unless an effort is set explicitly
        assert efforts == ["high", "low", "low", "low"]
        assert len(mapped) == 1 and mapped[0]["phase"] == "research"      # logged once per run
    if name == "GLM_EXTRA_BODY":
        assert all(p["custom_flag"] is True for p in session.payloads)      # the rest of extra_body is kept


def test_an_explicit_effort_wins_over_the_disabled_mapping(tmp_path):
    env = {"GLM_THINKING": "disabled", "GLM_RESEARCH_REASONING_EFFORT": "max"}.get
    caller, session, _ = http_caller(tmp_path, agent_config_from_env(env))
    every_phase(caller)
    assert [p.get("reasoning_effort") for p in session.payloads] == ["max", "low", "low", "low"]
    assert not any("thinking" in p for p in session.payloads)


def test_thinking_enabled_is_still_sent(tmp_path):
    caller, session, _ = http_caller(tmp_path, AgentConfig(thinking="enabled"))
    every_phase(caller)
    assert all(p["thinking"] == {"type": "enabled"} for p in session.payloads)


def test_extra_json_effort_matches_the_recorded_configuration(tmp_path):
    from src.agent import effective_glm_config
    from src.tools import ToolConfig

    cfg = AgentConfig(extra_body={"reasoning_effort": "max", "thinking": {"type": "disabled"}})
    caller, session, _ = http_caller(tmp_path, cfg)
    recorded = effective_glm_config(caller.client, cfg, ToolConfig())["phase_settings"]
    every_phase(caller)
    for phase, payload in zip(PHASES, session.payloads):
        resolved = for_phase(cfg, phase)
        assert recorded[resolved["phase"]]["reasoning_effort"] == payload["reasoning_effort"] == "max"
        assert "thinking" not in payload


def test_extra_json_cannot_expand_narration_or_add_tools(tmp_path):
    cfg = AgentConfig(extra_body={"max_tokens": 10000, "tools": [{"type": "function"}], "tool_choice": "auto"})
    caller, session, _ = http_caller(tmp_path, cfg)
    caller([{"role": "user", "content": "digest"}], phase="finalization", max_tokens=800)
    assert session.payloads[0]["max_tokens"] == 800
    assert "tools" not in session.payloads[0] and "tool_choice" not in session.payloads[0]


def test_default_efforts_are_top_level_and_env_or_ui_override_them(tmp_path):
    caller, session, log = http_caller(tmp_path / "a", AgentConfig())
    every_phase(caller)
    assert [p["reasoning_effort"] for p in session.payloads] == ["high", "low", "low", "low"]
    responses = [e for e in read_events(log.events_path) if e["kind"] == "model_response"]
    assert [r["reasoning_effort"] for r in responses] == ["high", "low", "low", "low"]
    assert all(r["reasoning_tokens"] == 3 for r in responses)

    env = {"GLM_REASONING_EFFORT": "medium", "GLM_RECOVERY_REASONING_EFFORT": "max"}.get
    caller, session, _ = http_caller(tmp_path / "b", agent_config_from_env(env))
    every_phase(caller)        # the global effort covers every phase, a phase's own wins
    assert [p["reasoning_effort"] for p in session.payloads] == ["medium", "medium", "max", "medium"]

    ui = merge_effort_settings({"document_sweep": {"max_attempts": 1}},
                               {"research": "low", "document_sweep": "high", "recovery": "low",
                                "finalizer": "provider default"})
    config = R.build_agent_config(env, {"phase_settings": ui}, R.CUSTOM)
    caller, session, _ = http_caller(tmp_path / "c", config)
    every_phase(caller)        # the UI's per-phase choice wins over env; "provider default" sends no effort
    assert [p.get("reasoning_effort") for p in session.payloads] == ["low", "high", "low", None]


def test_a_1210_rejection_is_retried_once_without_thinking_and_with_effort_low(tmp_path):
    rejected = {"error": {"code": "1210", "message": "This model always engages in thinking and cannot be disabled; "
                                                     "please use low, high, or max"}}
    config = AgentConfig(thinking="enabled")
    caller, session, log = http_caller(tmp_path / "a", config, replies=[(400, rejected)])
    message = caller([{"role": "user", "content": "x"}], phase="document_sweep")    # sweep: max_attempts 1
    assert message["content"] == "{}" and len(session.payloads) == 2
    first, retry = session.payloads
    assert first["thinking"] == {"type": "enabled"} and first["reasoning_effort"] == "low"
    assert "thinking" not in retry and retry["reasoning_effort"] == "low"
    events = read_events(log.events_path)
    assert [e["kind"] for e in events if e["kind"] in ("reasoning_retry", "model_response")] == [
        "reasoning_retry", "model_response"]
    # the retry does not consume max_attempts: both HTTP attempts report attempt 1 of 1
    assert [(e["attempt"], e["max_attempts"]) for e in events if e["kind"] in ("api_error", "api_call")] == [
        (1, 1), (1, 1)]

    caller, session, _ = http_caller(tmp_path / "b", config, replies=[(400, rejected), (400, rejected)])
    with pytest.raises(GLMError) as err:          # a second rejection surfaces as a normal error
        caller([{"role": "user", "content": "x"}], phase="research")
    assert err.value.status == 400 and len(session.payloads) == 2

    other = {"error": {"code": "1214", "message": "bad request"}}
    caller, session, _ = http_caller(tmp_path / "c", config, replies=[(400, other)])
    with pytest.raises(GLMError):                 # any other 400 is not retried
        caller([{"role": "user", "content": "x"}], phase="research")
    assert len(session.payloads) == 1


@pytest.mark.final_assembly("deterministic")
def test_run_configuration_config_key_and_reasoning_token_totals(tmp_path):
    class Reasoning(PhaseClient):
        def chat(self, messages, tools=None, **kwargs):
            response = super().chat(messages, tools, **kwargs)
            response.usage["completion_tokens_details"] = {"reasoning_tokens": 7}
            return response

    client = Reasoning([fetch("a", EU), say({"done": True, "reason": "x"})])
    result, events = run(tmp_path, client, acquisition_mode="contract", **GATE_OFF)
    config = D.run_configuration(events)
    assert {k: config[k] for k in ("research_reasoning_effort", "sweep_reasoning_effort",
                                   "recovery_reasoning_effort", "finalizer_reasoning_effort", "final_assembly")} == {
        "research_reasoning_effort": "high", "sweep_reasoning_effort": "low", "recovery_reasoning_effort": "low",
        "finalizer_reasoning_effort": "low", "final_assembly": "deterministic"}
    key = D.config_key(config)
    assert "research_reasoning_effort=high" in key and "sweep_reasoning_effort=low" in key
    assert "recovery_reasoning_effort=low" in key and "finalizer_reasoning_effort=low" in key
    totals = D.run_totals(events, result)
    by_phase = totals["by_phase"]
    assert by_phase["research"]["reasoning_tokens"] == 7 * by_phase["research"]["model_calls"]
    assert totals["reasoning_tokens"] == 7 * totals["model_calls"]
    row = D.vehicle_row(D.vehicle_diagnostics(events, run_id="b", result=result))
    assert row["total_reasoning_tokens"] == totals["reasoning_tokens"]
    assert row["research_reasoning_tokens"] == by_phase["research"]["reasoning_tokens"]
    assert row["cfg_sweep_reasoning_effort"] == "low"
    old = D.run_configuration([{"kind": "run_started", "agent_config": {}, "glm_config": {"thinking": "disabled"}}])
    assert old["research_reasoning_effort"] is None and old["final_assembly"] == "llm"
    assert old["sweep_thinking"] == "disabled"                            # kept for old runs
    assert D.run_totals([{"kind": "model_response", "phase": "research", "usage": {}}])["reasoning_tokens"] is None


# --- Part B: profiles --------------------------------------------------------------------------------------------

def test_named_profiles_pin_sweep_mode_efforts_and_assembly_and_custom_uses_env():
    env = {"SWEEP_MODE": "legacy", "GLM_REASONING_EFFORT": "max", "FINAL_ASSEMBLY": "llm",
           "GLM_RECOVERY_MAX_ATTEMPTS": "3", "ADJUDICATION_MAX_U_ITEMS": "99"}.get
    for profile in R.NAMED_PROFILES:
        cfg = R.build_agent_config(env, {}, profile)
        assert (cfg.sweep_mode, cfg.final_assembly, cfg.reasoning_effort) == ("adjudication", "deterministic", "")
        assert cfg.adjudication_max_u_items == AgentConfig().adjudication_max_u_items
        assert {p: for_phase(cfg, p)["reasoning_effort"] for p in PHASES} == DEFAULT_EFFORTS
        assert for_phase(cfg, "field_recovery")["max_attempts"] == 1
        assert all(for_phase(cfg, p)["thinking"] == "" for p in PHASES)
    custom = R.build_agent_config(env, {}, R.CUSTOM)
    assert (custom.sweep_mode, custom.final_assembly, custom.adjudication_max_u_items) == ("legacy", "llm", 99)
    assert {p: for_phase(custom, p)["reasoning_effort"] for p in PHASES} == {p: "max" for p in PHASES}
    assert for_phase(custom, "field_recovery")["max_attempts"] == 3


def test_the_env_override_list_shows_the_new_variables():
    env = {"SWEEP_MODE": "legacy", "FINAL_ASSEMBLY": "llm", "GLM_REASONING_EFFORT": "max",
           "GLM_RESEARCH_REASONING_EFFORT": "high", "GLM_DOCUMENT_SWEEP_REASONING_EFFORT": "medium",
           "GLM_THINKING": "disabled", "GLM_RECOVERY_MAX_ATTEMPTS": "2", "ADJUDICATION_MAX_A_FIELDS": "9",
           "GLM_EXTRA_BODY": '{"thinking": {"type": "disabled"}}'}
    shown = [o["var"] for o in R.env_overrides(env.get)]
    assert shown == ["SWEEP_MODE", "FINAL_ASSEMBLY", "GLM_REASONING_EFFORT", "GLM_DOCUMENT_SWEEP_REASONING_EFFORT",
                     "GLM_THINKING", "GLM_EXTRA_BODY", "GLM_RECOVERY_MAX_ATTEMPTS", "ADJUDICATION_MAX_A_FIELDS"]
    # GLM_EXTRA_BODY whenever it is a non-empty object (a named profile ignores all of it); the research default
    # "high" is not a difference
    assert [o["var"] for o in R.env_overrides({"GLM_EXTRA_BODY": '{"custom_flag": true}'}.get)] == ["GLM_EXTRA_BODY"]
    assert R.env_overrides({"GLM_EXTRA_BODY": "{}"}.get) == []
    assert R.env_overrides({"GLM_RESEARCH_REASONING_EFFORT": "high", "SWEEP_MODE": "adjudication",
                            "FINAL_ASSEMBLY": "deterministic", "GLM_RECOVERY_MAX_ATTEMPTS": "1"}.get) == []
    vars_ = {v for v, _, _ in R.ENV_OVERRIDE_VARS}
    assert {"GLM_FINALIZER_REASONING_EFFORT", "GLM_RECOVERY_REASONING_EFFORT", "GLM_DOCUMENT_SWEEP_THINKING",
            "ADJUDICATION_U_MAX_TOKENS", "ADJUDICATION_MAX_M_SNIPPETS"} <= vars_
    assert "GLM_MODEL" not in vars_                       # the research model stays outside profiles


# --- Part C: Deterministic Final Assembly --------------------------------------------------------------------------

FIELDS_C = ["list_price", "fuel_tank_l", "torque_nm", "wheelbase_mm", "gear_count", "length_mm", "local_trim_name"]
PAYLOAD_C = {"identity": {"government_record_id": "38626", "trim": "BUSINESS EDI", "model_code": "ZWE211L DWXNBW",
                          "manufacturer": "Toyota", "commercial_name": "COROLLA", "year": 2024}}


def specs_c(not_applicable=("gear_count",)):
    return [{**SCHEMA[f], "applicable": f not in not_applicable} for f in FIELDS_C]


def events_of(items, *, specs=None, extra=()):
    events = [{"seq": 1, "kind": "run_started", "target_market": "IL",
               "requested_field_specs": specs or specs_c()}]
    for n, it in enumerate(items, start=2):
        events.append({"seq": n, "kind": "evidence", "evidence": it})
    for n, e in enumerate(extra, start=len(events) + 1):
        events.append({"seq": n, **e})
    return events


def admitted_items():
    return [
        item("e1", "list_price", 189900, binding_level="exact_market_trim", valid_as_of="2026-09-01",
             source_url="https://www.toyota.co.il/prices", document_id="d1"),
        item("e2", "fuel_tank_l", 43, market="DE", source_authority="official_manufacturer",
             source_url="https://www.toyota.de/corolla", document_id="d2"),
        item("e3", "torque_nm", 142, document_id="d3", source_domain="a.example", source_authority="aggregator"),
        item("e4", "torque_nm", 205, document_id="d4", source_domain="b.example", source_authority="aggregator"),
        item("e5", "wheelbase_mm", 2700, market="DE", source_authority="aggregator", document_id="d5"),
    ]


def test_each_field_state_produces_its_code_written_entry():
    output, report = assemble_output(events_of(admitted_items()), PAYLOAD_C, specs_c(), "IL")
    fields = output["fields"]
    assert {n: e["state"] for n, e in fields.items()} == {
        "list_price": "ok", "fuel_tank_l": "ok", "torque_nm": "conflicting", "wheelbase_mm": "foreign_market_only",
        "gear_count": "not_applicable", "length_mm": "missing", "local_trim_name": "missing"}
    assert fields["list_price"] == {"value": 189900, "unit": "ILS", "market": "IL", "provenance": "israel_direct",
                                    "alternatives": [], "valid_as_of": "2026-09-01", "notes": None,
                                    "evidence_ids": ["e1"], "state": "ok"}
    portable = fields["fuel_tank_l"]
    assert (portable["value"], portable["market"], portable["provenance"], portable["evidence_ids"]) == (
        43, "DE", "foreign_direct", ["e2"])
    assert portable["notes"].startswith("portable foreign-market fact (official official_manufacturer source (DE)")
    assert fields["torque_nm"] == {
        "value": None, "unit": None, "market": None, "provenance": "unresolved",
        "alternatives": [{"value": 142, "market": "IL", "variant": None, "evidence_ids": ["e3"]},
                         {"value": 205, "market": "IL", "variant": None, "evidence_ids": ["e4"]}],
        "valid_as_of": None, "notes": "conflicting: true_conflict", "evidence_ids": [], "state": "conflicting"}
    assert output["conflicts"] == [{"field": "torque_nm", "values": [
        {"value": 142, "market": "IL", "evidence_ids": ["e3"]}, {"value": 205, "market": "IL", "evidence_ids": ["e4"]}],
        "conflict_class": "true_conflict", "conflict_detail": "the values disagree", "model_comment": None}]
    assert fields["wheelbase_mm"] == {
        "value": None, "unit": None, "market": None, "provenance": "unresolved",
        "alternatives": [{"value": 2700, "market": "DE", "variant": None, "evidence_ids": ["e5"]}],
        "valid_as_of": None, "notes": "foreign_market_only", "evidence_ids": [], "state": "foreign_market_only"}
    assert fields["gear_count"] == {"value": None, "unit": None, "market": None, "provenance": "unresolved",
                                    "alternatives": [], "valid_as_of": None, "notes": "not_applicable",
                                    "evidence_ids": [], "state": "not_applicable"}
    assert fields["length_mm"] == {"value": None, "unit": None, "market": None, "provenance": "unresolved",
                                   "alternatives": [], "valid_as_of": None, "notes": "missing", "evidence_ids": [],
                                   "state": "missing"}
    assert output["provenance_summary"] == {
        "israeli_market_values": ["list_price"], "foreign_market_values": ["fuel_tank_l"],
        "inferred_variant_mappings": [], "conflicts": ["torque_nm"],
        "unresolved_fields": ["torque_nm", "wheelbase_mm", "length_mm", "local_trim_name"]}
    assert output["variant_identity"] == {
        "government": "BUSINESS EDI / ZWE211L DWXNBW", "local_commercial_name": None, "mapping_basis": "inference",
        "evidence_ids": [], "notes": "no admitted local_trim_name evidence: no local commercial name is stated"}
    assert output["vehicle_id"] == "38626" and report["inconsistent"] == []
    # no value without admitted evidence: every value equals the value of the evidence item it cites
    by_id = {i["evidence_id"]: i for i in admitted_items()}
    for entry in fields.values():
        if entry["value"] is not None:
            assert entry["evidence_ids"] and all(by_id[i]["value"] == entry["value"] for i in entry["evidence_ids"])


def test_the_local_commercial_name_only_comes_from_admitted_local_trim_name_evidence():
    trim = item("e9", "local_trim_name", "Business Edition", binding_level="exact_market_trim", document_id="d9")
    output, _ = assemble_output(events_of(admitted_items() + [trim]), PAYLOAD_C, specs_c(), "IL")
    assert output["variant_identity"]["local_commercial_name"] == "Business Edition"
    assert output["variant_identity"]["mapping_basis"] == "explicit_source"
    assert output["variant_identity"]["evidence_ids"] == ["e9"]


def test_a_research_reply_or_model_declaration_never_supplies_a_value():
    # the research model's own JSON claims values (weak_provenance for the evaluator): the output keeps them out
    reply = {"kind": "model_response", "phase": "research", "tool_calls": None, "content": json.dumps(
        {"fields": {"length_mm": {"value": 4650, "provenance": "israel_direct"}}})}
    output, _ = assemble_output(events_of(admitted_items(), extra=[reply]), PAYLOAD_C, specs_c(), "IL")
    assert output["fields"]["length_mm"]["state"] == "weak_provenance"
    assert output["fields"]["length_mm"]["value"] is None and output["fields"]["length_mm"]["alternatives"] == []
    assert "4650" not in json.dumps(output)


def test_disagreeing_carriers_of_an_ok_state_are_emitted_as_conflicting(tmp_path):
    a = item("e1", "torque_nm", 142, document_id="d1", source_domain="a.example")
    b = item("e2", "torque_nm", 205, document_id="d2", source_domain="b.example")
    declared = {"kind": "field_status", "field": "torque_nm", "status": "conflict_resolved",
                "evidence_ids": ["e1", "e2"], "source": "test"}
    events = events_of([a, b], extra=[declared])
    from src.bundle import current_field_states
    assert current_field_states(events, specs_c(), "IL")["torque_nm"]["state"] == "ok"   # the evaluator: ok
    log = RunLog(tmp_path, "b", "1")
    fin = run_deterministic_finalization(None, run_log=log, events=events, payload=PAYLOAD_C, specs=specs_c(),
                                         target_market="IL", model="m", narrate=False)
    entry = fin["output"]["fields"]["torque_nm"]
    assert entry["state"] == "conflicting" and entry["value"] is None      # never picked by majority or order
    assert [c["conflict_class"] for c in fin["output"]["conflicts"]] == ["final_assembly_inconsistent"]
    bad = [e for e in read_events(log.events_path) if e["kind"] == "final_assembly_inconsistent"]
    assert [(e["field"], e["evidence_ids"]) for e in bad] == [("torque_nm", ["e1", "e2"])]


def test_zero_admitted_evidence_gives_null_values_and_every_applicable_field_unresolved():
    output, report = assemble_output(events_of([]), PAYLOAD_C, specs_c(), "IL")
    assert all(e["value"] is None for e in output["fields"].values())
    applicable = [f for f in FIELDS_C if f != "gear_count"]
    assert output["provenance_summary"]["unresolved_fields"] == applicable
    assert output["conflicts"] == [] and report["admitted_evidence"] == 0
    assert output["summary"].startswith("Assembled in code from 0 admitted evidence item(s): 0 of 6")


class NarrationClient(tail.PolicyGLM):
    """The Corolla tail policy model (it stores evidence from cached documents) whose narration step answers with
    `narration` (a reply dict, or an exception to raise)."""

    def __init__(self, narration, **kw):
        super().__init__(**kw)
        self.narration = narration
        self.narrations = []

    def chat(self, messages, tools=None, **kwargs):
        if messages[0]["content"] == NARRATION_SYSTEM_PROMPT:
            self.narrations.append({"messages": messages, "tools": tools, **kwargs})
            if isinstance(self.narration, BaseException):
                raise self.narration
            return ChatResponse(message=say(self.narration), finish_reason="stop", usage={"total_tokens": 1})
        return super().chat(messages, tools, **kwargs)


def deterministic_run(tmp_path, narration, **cfg):
    client = NarrationClient(narration)
    run_ = tail.run_mode("cluster", tmp_path, client=client, batch="b", **cfg)
    return run_["result"], run_["events"], client


@pytest.mark.final_assembly("deterministic")
def test_a_run_assembles_values_in_code_and_the_model_only_narrates(tmp_path):
    narration = {"summary": "Narrated.", "research_trace": ["fetched the EU spec page"], "vehicle_id": "X",
                 "fields": {"torque_nm": {"value": 999, "evidence_ids": ["e1"]}}}
    result, events, client = deterministic_run(tmp_path, narration)
    assert result["final_assembly"] == "deterministic" and result["output_source"] == "code"
    assert result["status"] == "completed"           # the research model finished: status semantics unchanged
    assert len(client.narrations) == 1 and client.narrations[0]["tools"] is None
    assert client.narrations[0]["max_tokens"] <= 2000
    digest = json.loads(client.narrations[0]["messages"][1]["content"].split("\n", 1)[1])
    assert set(digest) == {"vehicle", "field_state_counts", "ok_fields", "open_fields", "conflicts",
                           "admitted_evidence_items", "sources_used"}       # names and counts only, never a value
    assert not any(str(i["value"]) in json.dumps(digest["ok_fields"]) for i in result["evidence"])
    # the finalizer model is never asked for values: no finalization prompt was sent
    assert not any("You are the finalization step" in r["messages"][0]["content"] for r in client.requests)
    output = result["output"]
    assert output["summary"] == "Narrated." and output["research_trace"] == ["fetched the EU spec page"]
    assert output["vehicle_id"] != "X"                                           # any other reply key is ignored
    assert (output["fields"].get("torque_nm") or {}).get("value") != 999
    by_id = {i["evidence_id"]: i for i in result["evidence"]}
    with_value = {n: e for n, e in output["fields"].items() if e["value"] is not None}
    assert with_value                                                       # the EU page supplied values
    for entry in with_value.values():
        assert entry["evidence_ids"] and all(by_id[i]["value"] == entry["value"] for i in entry["evidence_ids"])
    states = {f: s["state"] for f, s in result["research_bundle"]["field_states"].items()}
    assert {n for n, e in output["fields"].items() if e["state"] == "ok"} == {f for f, s in states.items() if s == "ok"}
    assert [e["kind"] for e in events].index("finalization_checkpoint_written") < \
        [e["kind"] for e in events].index("finalization_started")
    metrics = compute_metrics(result)
    assert metrics["output_source"] == "code" and metrics["target_filled"] == len(
        [f for f in with_value if f in result["requested_fields"]])


@pytest.mark.final_assembly("deterministic")
def test_a_failed_narration_keeps_the_code_summary_and_the_status(tmp_path):
    ok, _, _ = deterministic_run(tmp_path / "ok", {"summary": "Narrated."})
    timeout = GLMError("ReadTimeout: slow")
    timeout.timeout = True
    failed, events, _ = deterministic_run(tmp_path / "failed", timeout)
    assert failed["status"] == ok["status"] and failed["error"] is None and not failed["partial"]
    assert failed["output"]["summary"].startswith("Assembled in code from")
    assert {n: e["value"] for n, e in failed["output"]["fields"].items()} == \
        {n: e["value"] for n, e in ok["output"]["fields"].items()}
    assert failed["finalization"]["narration"]["status"] == "failed"
    assert any(e["kind"] == "narration_failed" for e in events)
    unparsed, _, _ = deterministic_run(tmp_path / "unparsed", {"something": "else"})
    assert unparsed["output"]["summary"].startswith("Assembled in code from")
    assert unparsed["finalization"]["narration"]["status"] == "unparsed"


def test_final_assembly_llm_keeps_the_finalizer(tmp_path):
    result, _, client = deterministic_run(tmp_path, {"summary": "unused"}, final_assembly="llm")
    assert result["output"]["summary"] == "final" and result["output_source"] == "model"
    assert result["final_assembly"] == "llm" and client.narrations == []
    assert sum(1 for r in client.requests if r["messages"][0]["content"].startswith(
        "You are the finalization step")) == 1
    assert agent_config_from_env({"FINAL_ASSEMBLY": "llm"}.get).final_assembly == "llm"
    assert AgentConfig.__dataclass_fields__["final_assembly"].default == "deterministic"


@pytest.mark.final_assembly("deterministic")
def test_finalize_existing_uses_the_same_assembly(tmp_path):
    live, _, _ = deterministic_run(tmp_path, {"summary": "Narrated."})
    finisher = NarrationClient({"summary": "Again.", "fields": {"torque_nm": {"value": 1}}}, primary=[])
    again = finalize_existing_run(tmp_path / "runs", "b", "38626", client=finisher,
                                  cache=DocumentCache(tmp_path / "cache"), config=AgentConfig(), force=True)
    assert again["status"] == "recovered_finalized" and again["output_source"] == "code"
    assert again["final_assembly"] == "deterministic" and len(finisher.narrations) == 1
    assert finisher.requests == []                                    # no research / sweep / recovery / finalizer
    assert again["output"]["fields"] == live["output"]["fields"] and again["output"]["summary"] == "Again."


def test_coverage_counts_only_values_backed_by_existing_evidence_ids():
    result = {"requested_fields": {"a": "", "b": "", "c": "", "d": ""}, "evidence": [{"evidence_id": "e1"}],
              "output": {"fields": {"a": {"value": 1, "evidence_ids": ["e1"]},
                                    "b": {"value": 2, "evidence_ids": ["e7"]},
                                    "c": {"value": 3},
                                    "d": {"value": None, "evidence_ids": ["e1"]}}}}
    metrics = compute_metrics(result)
    assert (metrics["target_filled"], metrics["coverage_pct"]) == (1, 25.0)
    assert (metrics["filled_by_output"], metrics["coverage_pct_by_output"]) == (3, 75.0)


def test_results_caption_names_the_output_source():
    from src.ui.run_view import output_source_caption

    assert "admitted evidence" in output_source_caption([{"output": {}, "output_source": "code"}])
    assert "exactly as the model returned" in output_source_caption([{"output": {}}])     # an old result
    assert "mixed" in output_source_caption([{"output": {}, "output_source": "code"}, {"output": {}}])


# --- Part D: recovery failure isolation -----------------------------------------------------------------------

class FailingRecovery(tail.PolicyGLM):
    """The Corolla tail policy model whose first `fail` cluster-recovery requests time out."""

    def __init__(self, fail=1, **kw):
        super().__init__(**kw)
        self.fail = fail
        self.recovery_kwargs = []

    def chat(self, messages, tools=None, **kwargs):
        if messages[0]["content"] == CLUSTER_RECOVERY_SYSTEM_PROMPT:
            self.recovery_kwargs.append(kwargs)
            if self.fail:
                self.fail -= 1
                exc = GLMError("ReadTimeout: read timed out")
                exc.timeout = exc.usage_unknown = True
                raise exc
        return super().chat(messages, tools, **kwargs)


def test_one_failed_cluster_attempt_does_not_stop_the_other_clusters(tmp_path):
    client = FailingRecovery(fail=1)
    run_ = tail.run_mode("cluster", tmp_path, client=client)
    rec = run_["result"]["field_recovery"]
    first, *rest = rec["attempts"]
    assert first["error"] and first["failed"] and first["fields_resolved"] == []
    assert rest and rest[0]["cluster"] != first["cluster"] and rest[0]["fields_resolved"]   # B ran and resolved
    assert rec["stopped"] is None and rec["api_failure_stop"] is False and rec["failed_attempts"] == 1
    assert any(f in a["fields_resolved"] for a in rest for f in first["fields"])   # A's fields got a later attempt
    diag = D.recovery_summary(run_["events"])
    assert diag["failed_attempts"] == 1 and diag["api_failure_stop"] is False


def test_two_consecutive_api_failures_stop_all_recovery(tmp_path):
    run_ = tail.run_mode("cluster", tmp_path, client=FailingRecovery(fail=2))
    rec = run_["result"]["field_recovery"]
    assert [a["failed"] for a in rec["attempts"]] == [True, True]
    assert rec["stopped"] == "api_failure" and rec["api_failure_stop"] is True and rec["failed_attempts"] == 2
    assert any(e["kind"] == "field_recovery_api_failure_stop" for e in run_["events"])
    assert D.recovery_summary(run_["events"])["api_failure_stop"] is True
    assert run_["result"]["status"] not in ("research_failed", "finalization_failed")     # the run still finalizes


def test_recovery_requests_use_one_http_attempt_by_default(tmp_path):
    client = FailingRecovery(fail=0)
    tail.run_mode("cluster", tmp_path / "a", client=client)
    assert client.recovery_kwargs and all(k["max_attempts"] == 1 for k in client.recovery_kwargs)
    client = FailingRecovery(fail=0)
    config = agent_config_from_env({"GLM_RECOVERY_MAX_ATTEMPTS": "2"}.get)
    tail.run_mode("cluster", tmp_path / "b", client=client, phase_settings=config.phase_settings)
    assert all(k["max_attempts"] == 2 for k in client.recovery_kwargs)
