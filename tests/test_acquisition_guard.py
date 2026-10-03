"""PR "Acquisition guard ... run profiles and A/B launcher": the contract-mode early-"done" guard (Part A) and the
document card (Part C). Scripted GLM / fake HTTP only: no network, no paid calls."""

import json

from conftest import FakeResponse, FakeSession
from fixtures import corolla_tail as tail
from fixtures.corolla_touring import PAYLOAD, VEHICLE
from test_phase_contracts import EU, PhaseClient, fetch, run, say, search
from test_tools_smoke import _call

from src import acquisition as A
from src.agent import (ACQUISITION_SYSTEM_PROMPT, DOCUMENT_CARD_PARAGRAPH, AgentConfig, ToolSession,
                       research_system_prompt)
from src.evidence_admission import AdmissionContext
from src.fields import resolve_requested_fields
from src.storage.run_log import RunLog
from src.tools import ToolContext, ToolConfig
from src.tools.evidence import EvidenceStore
from src.storage.cache import DocumentCache

DONE = say({"done": True, "reason": "enough"})


def deferred(events):
    return [e for e in events if e["kind"] == "primary_research_stop_deferred"]


def research_requests(client):
    return [r for r in client.requests if r["messages"][0]["content"].startswith("You are the SOURCE ACQUISITION")]


# --- Part A: an early "done" cannot bypass the minimum acquisition base (contract) --------------------------------

def test_an_early_done_with_the_base_unmet_is_deferred_once_with_a_note(tmp_path):
    client = PhaseClient([fetch("a", EU), DONE, fetch("b", tail.CARTUBE, tail.LAUNCH), DONE])
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False, max_steps=6)
    held = deferred(events)
    assert [(e["turn"], e["wanted_stop"]) for e in held] == [(2, "model_finished")]
    assert held[0]["useful_documents"] == 1 and held[0]["min_documents"] == 3
    # the next research request carries the operational note as a user message (there is no tool message)
    note = research_requests(client)[2]["messages"][-1]
    assert note["role"] == "user" and note["content"].startswith("[operational note]")
    assert "1 useful document(s), minimum 3" in note["content"]
    assert f"{held[0]['scoped_coverage_pct']}% of the requested fields" in note["content"]
    assert "Source categories still missing:" in note["content"] and '{"done": true, ...}' in note["content"]
    # turn 3 meets the base; research continues normally and the next "done" ends it at once
    turns = [e for e in events if e["kind"] == "primary_research_turn"]
    assert [t["turn"] for t in turns] == [1, 3] and turns[-1]["minimum_acquisition_met"]
    assert result["stop_reason"] == "model_finished" and result["research_steps"] == 4
    primary = result["primary_research"]
    assert primary["done_deferred_count"] == 1 and primary["stop_deferred_count"] == 1
    assert primary["no_artifact_turns"] == 0              # the deferred turn ran no after_turn: the streak is untouched


def test_two_consecutive_done_replies_stop_research(tmp_path):
    client = PhaseClient([fetch("a", EU), DONE, DONE, fetch("never", tail.CARTUBE)])
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False, max_steps=6)
    assert result["stop_reason"] == "model_finished" and result["research_steps"] == 3
    assert result["primary_research"]["done_deferred_count"] == 1
    assert len(research_requests(client)) == 3
    from src import diagnostics as D
    diag = D.vehicle_diagnostics(events, result=result)
    assert diag["acquisition"]["summary"]["done_deferred_count"] == 1 and D.vehicle_row(diag)["acq_done_deferred"] == 1


def test_a_done_with_the_base_met_stops_at_once(tmp_path):
    client = PhaseClient([fetch("a", EU), fetch("b", tail.FORUM), fetch("c", tail.CARTUBE), DONE])
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False, max_steps=6)
    assert result["stop_reason"] == "model_finished" and result["research_steps"] == 4
    assert deferred(events) == [] and result["primary_research"]["done_deferred_count"] == 0


def test_a_done_on_an_extension_turn_stops_at_once(tmp_path):
    client = PhaseClient([fetch("a", EU), search("s2", "corolla q2"), DONE, fetch("never", tail.CARTUBE)])
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False, max_steps=2)
    assert result["stop_reason"] == "model_finished" and result["research_steps"] == 3
    assert [e["wanted_stop"] for e in deferred(events)] == ["max_turns"]          # only the ceiling extension
    assert result["primary_research"]["done_deferred_count"] == 0


def test_legacy_done_with_the_base_unmet_stops_exactly_as_before(tmp_path):
    client = PhaseClient([fetch("a", EU), say({"summary": "primary", "fields": {}}), fetch("never", tail.CARTUBE)])
    result, events = run(tmp_path, client, acquisition_mode="legacy", field_recovery_enabled=False, max_steps=6)
    assert result["stop_reason"] == "model_finished" and result["research_steps"] == 2
    assert deferred(events) == [] and result["status"] == "completed"
    assert result["primary_research"]["done_deferred_count"] == 0
    assert not any(m["role"] == "user" and "[operational note]" in (m["content"] or "")
                   for r in client.requests for m in r["messages"][2:])


# --- Part C: the document card (flag, default off) ------------------------------------------------------------

IL = "https://www.toyota.co.il/models/corolla-touring-sports/equipment"
IL_HTML = """<html><head><title>טויוטה קורולה טורינג ספורט 2024 1.8 היברידי Business - אבזור</title></head><body>
<h1>טויוטה קורולה טורינג ספורט 2024 1.8 היברידי Business</h1>
<table><tr><td>חימום מושבים</td><td>כן</td></tr><tr><td>גג פנורמי</td><td>יש</td></tr>
<tr><td>בסיס גלגלים</td><td>2,700 מ"מ</td></tr></table></body></html>"""
SPECS = resolve_requested_fields(None, propulsion="hybrid")


def card_session(tmp_path, *, mode="contract", card=True):
    ctx = ToolContext(cache=DocumentCache(tmp_path / "c"), evidence=EvidenceStore(), config=ToolConfig(),
                      session=FakeSession({IL: FakeResponse(IL_HTML.encode("utf-8"), url=IL),
                                           EU: FakeResponse(tail.EU_SPEC_HTML.encode("utf-8"), url=EU)}),
                      vehicle=dict(VEHICLE))
    ctx.admission = AdmissionContext.for_run(PAYLOAD, VEHICLE, SPECS, "IL")
    log = RunLog(tmp_path / "runs", "b", "1")
    config = AgentConfig(acquisition_mode=mode, acquisition_document_card=card)
    return ToolSession(ctx, log, config, specs=SPECS), log


def test_card_off_by_default_no_card_and_no_prompt_paragraph(tmp_path):
    assert AgentConfig().acquisition_document_card is False
    session, _ = card_session(tmp_path, card=False)
    messages = []
    session.execute([_call("a", "fetch_url", {"url": IL})], messages, phase="research")
    assert "document_card" not in session.turn_results[0]["result"] and "document_card" not in messages[0]["content"]
    assert research_system_prompt("contract") == research_system_prompt("contract", False)
    assert "document_card" not in research_system_prompt("contract")
    client = PhaseClient([fetch("a", EU), say({"done": True, "reason": "x"}), say({"done": True, "reason": "x"})])
    result, events = run(tmp_path / "run", client, acquisition_mode="contract", field_recovery_enabled=False)
    assert not any("document_card" in m["content"] for r in client.requests for m in r["messages"] if m["role"] == "tool")
    assert not any(e["kind"] in ("document_card", "document_card_failed") for e in events)
    started = next(e for e in events if e["kind"] == "run_started")
    import hashlib
    assert started["research_prompt_hash"] == hashlib.sha256(
        research_system_prompt("contract").encode("utf-8")).hexdigest()[:12]


def test_card_on_marks_target_market_documents_and_counts_in_scope_clusters(tmp_path):
    session, log = card_session(tmp_path)
    messages = []
    session.execute([_call("a", "fetch_url", {"url": IL}), _call("b", "fetch_url", {"url": EU})], messages,
                    phase="research")
    il, eu = (r["result"]["document_card"] for r in session.turn_results)
    assert il["note"] == A.DOCUMENT_CARD_NOTE and il["target_market_document"] is True and il["usable"] is True
    assert il["market"] == "IL" and il["source_authority"] == "official_importer"
    assert il["clusters"]["equipment"] >= 1 and il["candidate_fields_in_scope"] == sum(il["clusters"].values())
    # a foreign manufacturer page: only market-insensitive ("low") fields count
    assert eu["target_market_document"] is False and eu["candidate_fields_in_scope"] > 0
    low = {s["name"] for s in SPECS if s.get("market_sensitivity") == "low"}
    clusters = A.recovery_clusters(SPECS)
    assert "equipment" not in eu["clusters"]
    from src.candidate_harvest import harvest_document
    fields = {c["field"] for c in harvest_document(session.ctx.cache, eu_doc(session), SPECS)[0]}
    for cluster, count in eu["clusters"].items():
        assert count == sum(1 for s in clusters[cluster] if s["name"] in fields and s["name"] in low)
    assert all(f"document_card" in m["content"] for m in messages)                  # shown to the model
    assert sum(1 for line in log.events_path.read_text().splitlines() if '"kind": "document_card"' in line) == 2
    # a replay outside research never carries the card
    session.execute([_call("a2", "fetch_url", {"url": IL})], [], phase="field_recovery")
    assert session.turn_results[0]["reused"] and "document_card" not in session.turn_results[0]["result"]
    prompt = research_system_prompt("contract", True)
    assert DOCUMENT_CARD_PARAGRAPH in prompt and prompt.replace("\n" + DOCUMENT_CARD_PARAGRAPH, "") == \
        research_system_prompt("contract")


def eu_doc(session):
    return next(r["result"]["document_id"] for r in session.turn_results if r["result"].get("final_url", "").startswith(
        EU) or r["result"].get("url") == EU)


def test_card_flag_in_legacy_mode_adds_nothing(tmp_path):
    session, _ = card_session(tmp_path, mode="legacy")
    session.execute([_call("a", "fetch_url", {"url": IL})], [], phase="research")
    assert "document_card" not in session.turn_results[0]["result"]
    assert research_system_prompt("legacy", True) == research_system_prompt("legacy")


def test_a_card_failure_is_logged_and_the_fetch_result_still_returned(tmp_path, monkeypatch):
    import src.agent as agent_mod

    def broken(**kwargs):
        raise RuntimeError("card broke")

    monkeypatch.setattr(agent_mod, "document_card", broken)
    session, log = card_session(tmp_path)
    messages = []
    session.execute([_call("a", "fetch_url", {"url": IL})], messages, phase="research")
    result = session.turn_results[0]["result"]
    assert result.get("document_id") and "document_card" not in result and len(messages) == 1
    failed = [json.loads(l) for l in log.events_path.read_text().splitlines() if '"document_card_failed"' in l]
    assert failed and "card broke" in failed[0]["error"]


def test_a_card_run_records_its_prompt_and_flag(tmp_path):
    class CardClient(PhaseClient):
        def chat(self, messages, tools=None, **kwargs):
            if messages[0]["content"] == research_system_prompt("contract", True):
                messages = [{**messages[0], "content": research_system_prompt("contract")}] + messages[1:]
            return super().chat(messages, tools, **kwargs)

    client = CardClient([fetch("a", EU), say({"done": True, "reason": "x"}), say({"done": True, "reason": "x"})])
    result, events = run(tmp_path, client, acquisition_mode="contract", acquisition_document_card=True,
                         field_recovery_enabled=False)
    started = next(e for e in events if e["kind"] == "run_started")
    assert started["acquisition_document_card"] is True and result["acquisition_document_card"] is True
    import hashlib
    assert started["research_prompt_hash"] == result["research_prompt_hash"] == hashlib.sha256(
        research_system_prompt("contract", True).encode("utf-8")).hexdigest()[:12]
    assert any(e["kind"] == "document_card" for e in events)
    assert ACQUISITION_SYSTEM_PROMPT not in research_system_prompt("contract", True)
