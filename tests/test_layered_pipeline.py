"""Layered field harvesting: research -> deterministic harvest -> model document sweep -> current_evaluation ->
breadth-first web recovery -> durable checkpoint -> finalizer. Scripted GLM / fake HTTP only."""

import json

import pytest

from conftest import FakeResponse
from fixtures.cadillac_lyriq import IL_SPEC, PAYLOAD, US_PAGE, VEHICLE, put_documents
from test_tools_smoke import ScriptedGLM, _call

from src.agent import (DOCUMENT_SWEEP_SYSTEM_PROMPT, FIELD_RECOVERY_SYSTEM_PROMPT, AgentConfig, research_system_prompt,
                       run_vehicle)
from src.benchmark import compute_metrics
from src.document_sweep import DOCUMENT_SWEEP_TOOLS
from src.glm_client import ChatResponse
from src.storage.run_log import RunLog, read_events
from src.tools import ToolConfig

SMALL = ["torque_nm", "battery_gross_kwh", "wheelbase_mm", "ventilated_seats", "local_trim_name"]


def turn(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def say(obj):
    return {"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)}


class PhaseGLM:
    """A deterministic stand-in for the model, answering by the step that is asking (system prompt):
    research turns from a script, the document sweep from `sweep(packet)`, field recovery from
    `recover(packet)`, and the finalizer with a fixed JSON. Counts calls per step."""

    model = "glm-5.3-flash"

    def __init__(self, research, sweep=None, recover=None, final=None):
        self.research = list(research)
        self.sweep_fn = sweep or (lambda packet, turn_no: say({"reviewed": []}))
        self.recover_fn = recover or (lambda packet, turn_no: say({"field": packet["requested_field"]["name"],
                                                                  "status": "unresolved"}))
        self.final = final or {"summary": "final", "fields": {}}
        self.requests = []
        self.calls = {"research": 0, "document_sweep": 0, "field_recovery": 0, "finalization": 0}
        self.on_finalizer = None

    def chat(self, messages, tools=None, **kwargs):
        system = messages[0]["content"]
        self.requests.append({"messages": messages, "tools": tools, **kwargs})
        packet = None
        if system in (DOCUMENT_SWEEP_SYSTEM_PROMPT, FIELD_RECOVERY_SYSTEM_PROMPT):
            packet = json.loads(messages[1]["content"].split("\n", 1)[1])
        turn_no = sum(1 for m in messages if m["role"] == "assistant") + 1
        self.last_messages = messages          # what the model actually sees this turn (tool results included)
        if system == research_system_prompt():
            self.calls["research"] += 1
            message = self.research.pop(0)
        elif system == DOCUMENT_SWEEP_SYSTEM_PROMPT:
            self.calls["document_sweep"] += 1
            message = self.sweep_fn(packet, turn_no)
        elif system == FIELD_RECOVERY_SYSTEM_PROMPT:
            self.calls["field_recovery"] += 1
            message = self.recover_fn(packet, turn_no)
        else:
            self.calls["finalization"] += 1
            if self.on_finalizer:
                self.on_finalizer()
            message = say(self.final)
        return ChatResponse(message=message, finish_reason="stop",
                            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})


def run(tmp_path, ctx, client, **cfg):
    config = AgentConfig(**{"max_steps": 4, "no_new_research_turns": 0, "recovery_mode": "legacy", **cfg})
    log = RunLog(tmp_path / "runs", "b", "85095")
    result = run_vehicle({"upstream_record_id": "85095"}, PAYLOAD, client=client, cache=ctx.cache, run_log=log,
                         config=config, tool_config=ToolConfig(), vehicle_meta=VEHICLE, session=ctx.session)
    return result, read_events(log.events_path), log


def read_docs(ids):
    return turn(*[_call(f"g{i}", "get_cached_document", {"key": d}) for i, d in enumerate(ids)])


# --- the document sweep ------------------------------------------------------------------------------

def test_document_sweep_uses_only_cached_documents_and_never_searches_or_fetches(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)
    seen = {}

    def sweep(packet, turn_no):
        if turn_no == 2:                                   # follow-up granted by the cached inspection below
            return say({"reviewed": [], "notes": "read the wheelbase hits; nothing else to store"})
        seen["packet"] = packet
        torque = next(c for c in packet["deterministic_candidates"]["torque_nm"] if c.get("market_hint") == "IL")
        return turn(_call("s1", "store_evidence", {"field": "torque_nm", "value": torque["value"], "unit": "Nm",
                                                   "document_id": torque["document_id"], "quote": torque["quote"],
                                                   "market": "IL", "variant_match": "exact"}),
                    _call("s2", "search_web", {"query": "cadillac lyriq torque"}),
                    _call("s3", "fetch_url", {"url": "https://www.cadillac.com/lyriq/specs"}),
                    _call("s4", "render_page", {"url": "https://www.cadillac.com/lyriq/specs"}),
                    _call("s5", "find_in_document", {"document_id": ids[0], "query": "בסיס גלגלים"}))

    client = PhaseGLM([read_docs(ids[:4]), say({"summary": "primary", "fields": {}})], sweep=sweep)
    result, events, _ = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_attempts=0)
    sweep_request = next(r for r in client.requests if r["messages"][0]["content"] == DOCUMENT_SWEEP_SYSTEM_PROMPT)
    assert {t["function"]["name"] for t in sweep_request["tools"]} == set(DOCUMENT_SWEEP_TOOLS)
    assert ctx.session.calls == [] and result["search_api_calls"] == 0          # zero searches, zero fetches
    blocked = [e["name"] for e in events if e["kind"] == "tool_blocked"]
    assert blocked == ["search_web", "fetch_url", "render_page"]                # refused BEFORE execution
    assert not [e for e in events if e["kind"] == "tool_call" and e["name"] in ("search_web", "fetch_url",
                                                                                "render_page")]
    sweep_summary = result["document_sweep"]
    assert sweep_summary["model_calls"] == 2 and sweep_summary["external_calls"] == 0   # bounded: 2 at most
    assert sweep_summary["follow_up_turn"]["tools"] == ["find_in_document"]
    assert sweep_summary["tool_calls_blocked"] == 3 and sweep_summary["evidence_promoted_from_candidates"] == 1
    assert "torque_nm" in sweep_summary["fields_resolved"]
    # the packet carries every applicable field together, compactly (no whole documents)
    packet = seen["packet"]
    assert set(packet["requested_fields"]) == set(SMALL) and packet["turn_budget"] == 2
    assert len(json.dumps(packet, ensure_ascii=False)) < 40000 and "cached_documents" in packet
    assert result["usage_document_sweep"]["model_calls"] == 2
    m = compute_metrics(result)
    assert m["document_sweep_model_calls"] == 2 and m["tool_calls_blocked"] == 3
    assert m["tool_calls"] == len([c for c in result["tool_calls"] if not c.get("blocked")])


# The parser misses it (the label stands alone on its line, the availability is on the next line, not a bare value
# cell); the label + stated availability make it admissible evidence once the model has read it.
INTERIOR = "LYRIQ Luxury interior: climate and cooling overview.\nSeat ventilation\nFront row: standard"
INTERIOR_URL = "https://www.cadillac.co.il/lyriq/interior"


def tool_messages(messages):
    return [m for m in messages if m.get("role") == "tool"]


def test_sweep_finds_a_parser_miss_only_after_reading_the_inspection_result(tmp_path, make_ctx):
    """Real conversational ordering: turn 1 does not know the value and inspects the cached document; only the
    second turn, which sees the tool result in the conversation, can store the evidence."""
    ctx = make_ctx({})
    doc = ctx.cache.put("fetch", INTERIOR_URL, INTERIOR.encode(), {"doc_type": "text", "final_url": INTERIOR_URL},
                        INTERIOR)["document_id"]
    seen = {}

    def sweep(packet, turn_no):
        messages = client.last_messages
        if turn_no == 1:
            seen["packet"] = packet
            assert "ventilated_seats" in packet["fields_without_candidates"]   # the parser found nothing
            assert not tool_messages(messages)                                  # nothing read yet: no answer known
            return turn(_call("i1", "find_in_document", {"document_id": doc, "query": "cooling"}))
        results = tool_messages(messages)
        assert len(results) == 1 and results[0]["tool_call_id"] == "i1"
        body, note = results[0]["content"].split("\n[operational note] ", 1)
        assert note.startswith("Final document-sweep turn")
        hits = json.loads(body)["hits"]
        snippet = next(h["snippet"] for h in hits if "Seat ventilation" in h["snippet"])
        # the quote names the feature AND states its availability ("standard"); a label alone would be rejected
        quote = snippet[snippet.index("Seat ventilation"):snippet.index("standard") + len("standard")]
        return turn(_call("s1", "store_evidence", {"field": "ventilated_seats", "value": True, "document_id": doc,
                                                   "quote": quote, "market": "IL", "variant_match": "exact"}))

    client = PhaseGLM([read_docs([doc]), say({"summary": "primary", "fields": {}})], sweep=sweep)
    result, events, _ = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_total_steps=4)
    harvested = [e for e in events if e["kind"] == "candidates_harvested"]
    assert harvested and "ventilated_seats" not in harvested[0]["fields"]
    sweep_calls = [e for e in events if e.get("phase") == "document_sweep" and e["kind"] in ("tool_call",
                                                                                              "model_response")]
    assert [(e["kind"], e.get("name")) for e in sweep_calls] == [
        ("model_response", None), ("tool_call", "find_in_document"),           # turn 1: inspect
        ("model_response", None), ("tool_call", "store_evidence")]             # turn 2: store what it read
    assert client.calls["document_sweep"] == 2 and result["document_sweep"]["model_calls"] == 2
    assert [e["kind"] for e in events].count("document_sweep_follow_up") == 1
    assert ctx.session.calls == [] and result["search_api_calls"] == 0          # no Search-Prime, no fetch
    missed = [e for e in events if e["kind"] == "candidate_missed_by_deterministic_harvest"]
    assert [(e["field"], e["document_id"]) for e in missed] == [("ventilated_seats", doc)]
    started = next(e for e in events if e["kind"] == "document_sweep_started")
    assert "ventilated_seats" in started["fields_to_review"]                    # missing before the sweep
    assert "ventilated_seats" in result["document_sweep"]["fields_resolved"]
    assert result["research_bundle"]["field_states"]["ventilated_seats"]["state"] == "ok"
    rec = result["field_recovery"]
    assert "ventilated_seats" not in rec["queue"]                               # never researched again
    assert not [e for e in events if e["kind"] == "field_recovery_started" and e["field"] == "ventilated_seats"]


def test_sweep_fast_path_promotes_a_candidate_in_one_turn(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)

    def sweep(packet, turn_no):
        assert turn_no == 1, "no pointless second sweep turn"
        wheel = packet["deterministic_candidates"]["wheelbase_mm"][0]
        return turn(_call("s", "store_evidence", {"field": "wheelbase_mm", "value": wheel["value"], "unit": "mm",
                                                  "document_id": wheel["document_id"], "quote": wheel["quote"],
                                                  "market": "IL", "variant_match": "exact"}),
                    _call("r", "report_field_status", {"field": "local_trim_name", "status": "unresolved"}))

    client = PhaseGLM([read_docs(ids[:1]), say({"summary": "primary", "fields": {}})], sweep=sweep)
    result, events, _ = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_attempts=0)
    assert client.calls["document_sweep"] == 1 and result["document_sweep"]["follow_up_turn"] is None
    assert "document_sweep_follow_up" not in [e["kind"] for e in events]
    assert "wheelbase_mm" in result["document_sweep"]["fields_resolved"]


def test_empty_inspections_and_a_one_turn_budget_never_buy_a_second_turn(tmp_path, make_ctx):
    ctx = make_ctx({})
    doc = ctx.cache.put("fetch", INTERIOR_URL, INTERIOR.encode(), {"doc_type": "text", "final_url": INTERIOR_URL},
                        INTERIOR)["document_id"]

    def zero_hits(packet, turn_no):
        assert turn_no == 1
        return turn(_call("i", "find_in_document", {"document_id": doc, "query": "panoramic roof"}),
                    _call("j", "extract_tables", {"document_id": doc}),           # plain text: no tables
                    _call("k", "find_in_document", {"document_id": "d_unknown", "query": "x"}))   # error

    client = PhaseGLM([read_docs([doc]), say({"summary": "primary", "fields": {}})], sweep=zero_hits)
    result, _, _ = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_attempts=0)
    assert client.calls["document_sweep"] == 1 and result["document_sweep"]["follow_up_turn"] is None

    ctx2 = make_ctx({})
    doc2 = ctx2.cache.put("fetch", INTERIOR_URL, INTERIOR.encode(), {"doc_type": "text", "final_url": INTERIOR_URL},
                          INTERIOR)["document_id"]

    def inspect(packet, turn_no):
        assert turn_no == 1
        return turn(_call("i", "find_in_document", {"document_id": doc2, "query": "cooling"}))

    client2 = PhaseGLM([read_docs([doc2]), say({"summary": "primary", "fields": {}})], sweep=inspect)
    result2, _, _ = run(tmp_path / "one", ctx2, client2, requested_fields=SMALL, field_recovery_max_attempts=0,
                        document_sweep_max_turns=1)
    assert client2.calls["document_sweep"] == 1                                  # DOCUMENT_SWEEP_MAX_TURNS=1


def test_the_sweep_never_exceeds_two_turns(tmp_path, make_ctx):
    ctx = make_ctx({})
    doc = ctx.cache.put("fetch", INTERIOR_URL, INTERIOR.encode(), {"doc_type": "text", "final_url": INTERIOR_URL},
                        INTERIOR)["document_id"]

    def always_inspect(packet, turn_no):
        assert turn_no <= 2
        return turn(_call(f"i{turn_no}", "find_in_document", {"document_id": doc, "query": f"cooling {turn_no}"}))

    client = PhaseGLM([read_docs([doc]), say({"summary": "primary", "fields": {}})], sweep=always_inspect)
    result, _, _ = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_attempts=0,
                       document_sweep_max_turns=5)                                # clamped to the absolute max 2
    assert client.calls["document_sweep"] == 2 and result["document_sweep"]["model_calls"] == 2


def test_recovery_starts_only_after_the_sweep_and_harvest(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)
    client = PhaseGLM([read_docs(ids[:2]), say({"summary": "primary", "fields": {}})])
    result, events, _ = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_attempts=1)
    kinds = [e["kind"] for e in events]
    order = ["research_stopped", "deterministic_harvest_summary", "document_sweep_started", "document_sweep_finished",
             "field_evaluation", "field_recovery_started"]
    positions = [kinds.index(k) for k in order]
    assert positions == sorted(positions)
    assert kinds.index("field_recovery_started") > kinds.index("document_sweep_finished")
    assert kinds.index("finalization_checkpoint_written") < kinds.index("finalization_started")


def test_breadth_first_recovery_after_the_sweep_and_candidate_context(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)
    order = []

    def recover(packet, turn_no):
        name = packet["requested_field"]["name"]
        order.append((name, packet["attempt"], turn_no))
        if turn_no < 4:   # every attempt uses its whole 4-turn budget without resolving the field
            return turn(_call(f"r{len(order)}", "find_in_document", {"document_id": ids[len(order) % 3],
                                                                      "query": f"{name} {len(order)}"}))
        return say({"field": name, "status": "unresolved"})

    def sweep(packet, turn_no):   # the sweep promotes only the wheelbase
        wheel = packet["deterministic_candidates"]["wheelbase_mm"][0]
        return turn(_call("s", "store_evidence", {"field": "wheelbase_mm", "value": wheel["value"], "unit": "mm",
                                                  "document_id": wheel["document_id"], "quote": wheel["quote"],
                                                  "market": "IL", "variant_match": "exact"}))

    client = PhaseGLM([read_docs(ids[:3]), say({"summary": "primary", "fields": {}})], sweep=sweep, recover=recover)
    result, events, _ = run(tmp_path, ctx, client, requested_fields=SMALL, field_recovery_max_total_steps=0)
    rec = result["field_recovery"]
    assert "wheelbase_mm" not in rec["queue"]                                      # resolved by the sweep
    assert rec["queue"] == ["torque_nm", "battery_gross_kwh", "ventilated_seats", "local_trim_name"]
    assert rec["attempt_order"][:4] == ["torque_nm#1", "battery_gross_kwh#1", "ventilated_seats#1",
                                        "local_trim_name#1"]                        # A1 -> B1, never A1 -> A2
    assert all(a["turns"] == 4 for a in rec["attempts"])
    assert rec["recovery_fields_given_first_attempt"] == 4 and rec["recovery_second_attempts_started"] == 4
    first = next(r for r in client.requests if r["messages"][0]["content"] == FIELD_RECOVERY_SYSTEM_PROMPT)
    packet = json.loads(first["messages"][1]["content"].split("\n", 1)[1])
    assert packet["requested_field"]["name"] == "torque_nm"
    assert {c["value"] for c in packet["deterministic_candidates"]} >= {610}       # recovery sees the candidates
    assert "aliases_he" not in packet["requested_field"]                           # dictionary never sent


def test_documents_fetched_during_recovery_are_harvested_for_every_field(tmp_path, make_ctx):
    page = "https://www.cadillac.co.il/lyriq/warranty"
    html = ("<html><body><table><tr><td>אחריות לרכב</td><td>5 שנים או 100,000 ק\"מ</td></tr>"
            "<tr><td>בסיס גלגלים</td><td>3,094 מ\"מ</td></tr><tr><td>מסך מרכזי</td><td>33 אינץ'</td></tr>"
            "</table></body></html>")
    ctx = make_ctx({page: FakeResponse(html.encode())})

    def recover(packet, turn_no):
        if turn_no == 1:
            return turn(_call("f", "fetch_url", {"url": page}))
        return say({"field": packet["requested_field"]["name"], "status": "unresolved"})

    client = PhaseGLM([say({"summary": "primary", "fields": {}})], recover=recover)
    result, events, _ = run(tmp_path, ctx, client, requested_fields=["vehicle_warranty", "wheelbase_mm",
                                                                       "screen_size_in"],
                            field_recovery_max_attempts=1, field_recovery_max_total_steps=2)
    harvested = [e for e in events if e["kind"] == "candidates_harvested"]
    assert len(harvested) == 1 and harvested[0]["phase"] == "field_recovery"
    assert {"vehicle_warranty", "wheelbase_mm", "screen_size_in"} <= set(harvested[0]["fields"])
    # still candidates only: nothing became ok without evidence
    assert result["research_bundle"]["field_states"]["wheelbase_mm"]["state"] == "missing"


# --- Cadillac acceptance ----------------------------------------------------------------------------------

def test_cadillac_acceptance_harvest_all_43_fields_before_paying_for_web_recovery(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)

    def sweep(packet, turn_no):
        """A careful reviewer: promote Israeli-market candidates with a confident label/value pairing; keep the
        US torque as a US value; never pick a winner."""
        calls = []
        for field, cands in packet["deterministic_candidates"].items():
            if field not in packet["fields_to_review"]:
                continue
            for c in cands:
                if c.get("market_hint") == "IL" and c["parser_confidence"] >= 0.8 and not c.get("ambiguity"):
                    calls.append(_call(f"s{len(calls)}", "store_evidence", {
                        "field": field, "value": c["value"], "unit": c.get("unit"), "document_id": c["document_id"],
                        "quote": c["quote"], "market": "IL", "variant_match": "exact"}))
                    break
        us = next(c for c in packet["deterministic_candidates"]["torque_nm"] if c.get("source") == "cadillac.com")
        calls.append(_call("us", "store_evidence", {"field": "torque_nm", "value": us["value"], "unit": "Nm",
                                                    "document_id": us["document_id"], "quote": us["quote"],
                                                    "market": "US", "variant_match": "unclear"}))
        return turn(*calls)

    client = PhaseGLM([read_docs(ids), say({"summary": "primary", "fields": {}})], sweep=sweep)
    result, events, _ = run(tmp_path, ctx, client, field_recovery_max_total_steps=24)
    harvest = next(e for e in events if e["kind"] == "deterministic_harvest_summary")
    assert harvest["applicable_fields"] == 43 and harvest["documents_harvested"] == 10
    assert harvest["candidate_fields_total"] >= 35 and harvest["fields_unresolved_before_harvest"] == 43
    sweep = result["document_sweep"]
    layered = result["candidate_summary"]
    assert sweep["model_calls"] == 1 and sweep["external_calls"] == 0
    assert sweep["fields_unresolved_before"] == 43 and sweep["fields_unresolved_after"] <= 18
    assert layered["fields_entering_web_recovery"] == sweep["fields_unresolved_after"]
    rec = result["field_recovery"]
    assert len(rec["queue"]) == sweep["fields_unresolved_after"]
    assert set(rec["queue"]).isdisjoint(sweep["fields_resolved"])                 # resolved fields never recovered
    first_round = [a for a in rec["attempts"] if a["attempt"] == 1]
    assert len(first_round) == rec["recovery_fields_given_first_attempt"]
    assert [a["field"] for a in rec["attempts"]][:len(first_round)] == [a["field"] for a in first_round]
    # torque: IL 610 + US 650 both kept as evidence (different markets: no same-scope conflict, nothing dropped)
    torque_values = {(e["value"], e["market"]) for e in result["evidence"] if e["field"] == "torque_nm"}
    assert torque_values == {(610, "IL"), (650, "US")}
    with_evidence = {e["field"] for e in result["evidence"]}
    assert len(with_evidence) >= 25            # the old trace reached 12 unique fields after ~23 recovery turns
    assert result["usage_research"]["model_calls"] == 2 and result["usage_field_recovery"]["model_calls"] <= 24
    m = compute_metrics(result)
    assert (m["fields_unresolved_before_harvest"], m["fields_entering_web_recovery"]) == \
        (43, sweep["fields_unresolved_after"])
    assert m["candidate_fields_total"] >= 35 and m["documents_harvested"] == 10
    print(json.dumps({"candidates": m["candidate_count_total"], "candidate_fields": m["candidate_fields_total"],
                      "unresolved_before": 43, "unresolved_after_sweep": sweep["fields_unresolved_after"],
                      "fields_with_evidence": len(with_evidence), "recovery_turns": m["field_recovery_turns_used"],
                      "attempt_order": rec["attempt_order"][:8]}, ensure_ascii=False))
