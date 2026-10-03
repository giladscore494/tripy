"""Candidate Adjudication sweep (SWEEP_MODE=adjudication) and the usable-document candidate filter.

The model only judges pre-checked candidates in small no-tool JSON packets; code groups candidates, dry-runs
admission, widens mechanical quotes, builds the store_evidence requests and runs them through the normal tool path.
Scripted GLM / fake HTTP only: no network."""

import json

import pytest

from conftest import FakeResponse, cache_source
from fixtures import corolla_tail as tail
from fixtures.corolla_touring import PAYLOAD, VEHICLE
from test_phase_contracts import EU, GATE_OFF, ROUTES, PhaseClient, _timeout, fetch, run, say

from src import adjudication as J
from src import diagnostics as D
from src.agent import (ADJUDICATION_SYSTEM_PROMPT, DOCUMENT_SWEEP_SYSTEM_PROMPT, REPAIR_PROMPT, AgentConfig,
                       ModelCaller, ToolSession, agent_config_from_env, run_document_sweep)
from src.candidate_harvest import candidate_matrix, harvest_document
from src.evidence_admission import AdmissionContext
from src.fields import resolve_requested_fields
from src.glm_client import ChatResponse
from src.storage.cache import DocumentCache
from src.storage.run_log import RunLog, read_events
from src.tail_planner import (candidate_key, candidates_fresh_first, fresh_candidates, rejected_keys, triage,
                              usable_candidate_matrix)
from src.tools import ToolConfig, ToolContext
from src.tools.evidence import EvidenceStore

TOYOTA_IL = "https://www.toyota.co.il/corolla-touring-sports/specifications"
TOYOTA_IL_HTML = ("<html><head><title>Toyota Corolla Touring Sports 2024 1.8 Hybrid Business</title></head><body>"
                  "<h1>Toyota Corolla Touring Sports 2024 1.8 Hybrid Business</h1>"
                  "<table><tr><th>Version</th><th>Max torque</th></tr>"
                  "<tr><td>1.8 Hybrid Business</td><td>142 Nm</td></tr></table>"
                  "<p>Ground clearance: 160 mm</p></body></html>")
# a table-row candidate whose own quote lacks the field label: the table's header line supplies it
TABLE_TORQUE = {"field": "torque_nm", "value": 142, "unit": "Nm", "quote": "1.8 Hybrid Business | 142 Nm",
                "table_index": 0, "row_index": 1, "extraction_method": "table_row", "parser_confidence": 0.6}
# a candidate whose quote occurs nowhere in its document: no widening can cure it
GHOST_LENGTH = {"field": "length_mm", "value": 4650, "unit": "mm", "quote": "Overall length 4,650 mm",
                "extraction_method": "alias_proximity", "parser_confidence": 0.9}
# these tests encode the adjudication sweep (they hold under `SWEEP_MODE=legacy pytest` too); the model call order is
# scripted, so the grounded-candidate step (its own tests: test_grounded.py) is off
pytestmark = [pytest.mark.sweep_mode("adjudication"), pytest.mark.grounded_candidates(False)]
WARRANTY_404 = "https://www.toyota.co.il/warranty"
WARRANTY_404_TEXT = "\n".join(["Page not found", "Length: 4,650 mm", "Width: 1,790 mm", "Wheelbase: 2,700 mm",
                               "Top speed: 180 km/h", "Fuel tank capacity: 43 l"])


def packet_of(messages) -> dict:
    return json.loads(messages[1]["content"].split("\n", 1)[1])


def respond_all(packet: dict) -> dict:
    """Accept everything the packet offers; locate nothing."""
    if packet["task"] == "adjudicate_unambiguous":
        return {"decisions": [{"id": i["id"], "accept": True, "reason": "ok"} for i in packet["items"]]}
    if packet["task"] == "adjudicate_ambiguous":
        return {"fields": [{"field": f["field"], "accept": [c["id"] for c in f["candidates"]]}
                           for f in packet["fields"]]}
    return {"fields": []}


class AdjClient:
    """Answers adjudication packets with `respond(packet, call_number)` (a dict, a raw string, or an exception to
    raise); records every request."""

    model = "glm-5.3-flash"

    def __init__(self, respond=None):
        self.respond = respond or (lambda packet, n: respond_all(packet))
        self.requests: list[dict] = []

    def chat(self, messages, tools=None, **kwargs):
        self.requests.append({"messages": messages, "tools": tools, **kwargs})
        reply = self.respond(packet_of(messages), len(self.requests))
        if isinstance(reply, BaseException):
            raise reply
        content = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return ChatResponse(message={"role": "assistant", "content": content}, finish_reason="stop",
                            usage={"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48})


class AdjPhaseClient(PhaseClient):
    """test_phase_contracts.PhaseClient (research / recovery / finalizer) plus adjudication packets."""

    def __init__(self, research, respond=None, **kw):
        super().__init__(research, **kw)
        self.adj = AdjClient(respond)

    def chat(self, messages, tools=None, **kwargs):
        if messages[0]["content"] == ADJUDICATION_SYSTEM_PROMPT:
            self.kinds.append("adjudication")
            return self.adj.chat(messages, tools, **kwargs)
        return super().chat(messages, tools, **kwargs)


def sweep_run(tmp_path, client, docs, *, fields, candidates=None, statuses=None, **cfg):
    """run_document_sweep alone over a hand-built run: `docs` = {url: (body, doc_type)} cached as fetched in research,
    harvested for real unless `candidates[url]` gives that document's candidates."""
    cache = DocumentCache(tmp_path / "c")
    log = RunLog(tmp_path / "runs", "b", "38626")
    specs = resolve_requested_fields(fields, propulsion="hybrid")
    config = AgentConfig(**{"research_memory_enabled": False, "requested_fields": fields, **cfg})
    log.event("run_started", requested_field_specs=specs, target_market="IL", agent_config={},
              sweep_mode=config.sweep_mode)
    ctx = ToolContext(cache=cache, evidence=EvidenceStore(), config=ToolConfig(), vehicle=dict(VEHICLE),
                      log=lambda kind, **data: log.event(kind, **data))
    ctx.admission = AdmissionContext.for_run(PAYLOAD, VEHICLE, specs, "IL")
    ids = {}
    for url, (body, doc_type) in docs.items():
        ids[url] = doc = cache_source(cache, url, body, doc_type=doc_type)
        if (statuses or {}).get(url):
            meta = cache.get(doc)
            meta["status"] = statuses[url]
            cache.put("fetch", url, body.encode("utf-8"), meta, cache.read_text(doc))
        ctx.note_document(doc)
        log.event("tool_result", step=1, phase="research", name="fetch_url", result={"document_id": doc, "url": url})
        given = (candidates or {}).get(url)
        cands = harvest_document(cache, doc, specs)[0] if given is None else \
            [{**c, "document_id": doc, "source_url": url} for c in given]
        log.event("candidates_harvested", document_id=doc, phase="research", url=url, candidates=cands)
    session = ToolSession(ctx, log, config)
    caller = ModelCaller(client, log, config)
    summary = run_document_sweep(session=session, caller=caller, specs=specs, payload=PAYLOAD, config=config,
                                 run_log=log, cache=cache, documents_dir=tmp_path / "docs", phase_ref={"name": "x"},
                                 vehicle={**VEHICLE})
    return summary, read_events(log.events_path), ids, ctx, specs


def evidence_of(events):
    return [e["evidence"] for e in events if e["kind"] == "evidence"]


# --- 1. Part A: candidates of unusable documents --------------------------------------------------------------------

def test_candidates_of_a_404_document_are_filtered_but_totals_stay(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    good = cache_source(cache, tail.CARTUBE, tail.CARTUBE_TEXT)
    bad = cache_source(cache, WARRANTY_404, WARRANTY_404_TEXT)
    meta = cache.get(bad)
    meta["status"] = 404
    cache.put("fetch", WARRANTY_404, WARRANTY_404_TEXT.encode(), meta, WARRANTY_404_TEXT)
    specs = resolve_requested_fields(["length_mm", "width_mm"], propulsion="hybrid")
    events = [{"kind": "candidates_harvested", "document_id": d, "candidates": harvest_document(cache, d, specs)[0]}
              for d in (good, bad)]
    matrix = candidate_matrix(events, specs, VEHICLE)
    filtered = usable_candidate_matrix(matrix, cache)
    from_bad = [c for n in matrix["fields"] for c in matrix["fields"][n] if c["document_id"] == bad]
    assert from_bad and filtered["candidates_from_unusable_documents"] == len(from_bad)
    assert filtered["unusable_documents"] == [bad]
    assert all(c["document_id"] == good for n in filtered["fields"] for c in filtered["fields"][n])
    assert filtered["candidate_count"] == matrix["candidate_count"]          # telemetry totals unchanged
    names = ["length_mm", "width_mm"]
    for out in (fresh_candidates(filtered, [], names), candidates_fresh_first(filtered, [], names)):
        assert all(c["document_id"] == good for v in out.values() for c in v)
    # a field whose only candidates come from the error page is no local material
    only_bad = {"fields": {"length_mm": [c for c in matrix["fields"]["length_mm"] if c["document_id"] == bad]}}
    entry = [{"field": "length_mm", "state": "missing", "retry_eligible": True}]
    spec = [s for s in specs if s["name"] == "length_mm"]
    assert triage(entry, spec, only_bad, [], 2)["length_mm"]["triage"] == "candidate_rich_local"
    assert triage(entry, spec, usable_candidate_matrix(only_bad, cache), [], 2)["length_mm"]["triage"] \
        == "true_missing"


def test_a_404_page_never_reaches_sweep_packets_recovery_or_triage(tmp_path):
    import test_phase_contracts as P
    from conftest import FakeSession

    routes = {**ROUTES, WARRANTY_404: (WARRANTY_404_TEXT, "text/plain")}

    session = FakeSession({url: FakeResponse(body.encode("utf-8"), content_type=ctype, url=url,
                                             status=404 if url == WARRANTY_404 else 200)
                           for url, (body, ctype) in routes.items()})
    client = AdjPhaseClient([fetch("a", EU, WARRANTY_404), say({"done": True, "reason": "x"})])
    log = RunLog(tmp_path / "runs", "b", "38626")
    config = AgentConfig(research_memory_enabled=False, requested_fields=tail.FIELDS, no_new_research_turns=0,
                         acquisition_mode="contract", **GATE_OFF)
    cache = DocumentCache(tmp_path / "c")
    result = P.run_vehicle({"upstream_record_id": "38626"}, PAYLOAD, client=client, cache=cache, run_log=log,
                           vehicle_meta=VEHICLE, config=config, tool_config=ToolConfig(), session=session)
    events = read_events(log.events_path)
    harvested = {e["document_id"]: e for e in events if e["kind"] == "candidates_harvested"}
    bad = next(d for d, e in harvested.items() if e["url"] == WARRANTY_404)
    assert harvested[bad]["candidates"], "the error page must yield candidates for this test to mean anything"
    summary = next(e for e in events if e["kind"] == "deterministic_harvest_summary")
    assert summary["candidates_from_unusable_documents"] > 0
    assert summary["candidate_count_total"] == sum(len(e["candidates"]) for e in harvested.values())
    offered = [k for e in events for k in (e.get("offered_candidate_keys") or []) + (e.get("presented_candidate_keys")
                                                                                       or [])]
    assert offered and not [k for k in offered if f"|{bad}|" in k]
    sent = [r for r in client.adj.requests]
    assert sent and not any(bad in r["messages"][1]["content"] for r in sent)
    queue = next(e for e in events if e["kind"] == "field_retry_queue")
    specs = resolve_requested_fields(tail.FIELDS, propulsion="hybrid")
    matrix = candidate_matrix([e for e in events if e["seq"] < queue["seq"]], specs, VEHICLE)
    usable = usable_candidate_matrix(matrix, cache)["fields"]
    assert queue["queue"]
    assert all(item["candidates"] == len(usable[item["field"]]) for item in queue["queue"])
    assert any(len(matrix["fields"][item["field"]]) > item["candidates"] for item in queue["queue"])
    assert result["document_sweep"]["candidates_from_unusable_documents"] == summary["candidates_from_unusable_documents"]


# --- 2. B0 grouping ---------------------------------------------------------------------------------------------------

def test_grouping_keeps_two_candidates_per_distinct_value_with_stable_ids():
    cands = [{"value": 4650, "document_id": "d1"}, {"value": "4,650", "document_id": "d2"},
             {"value": 4655, "document_id": "d1"}, {"value": 4650.0, "document_id": "d3"}]
    kept = J.group_by_value(cands)
    assert [c["document_id"] for c in kept] == ["d1", "d2", "d1"]
    assert len({J.material_key(c["value"]) for c in kept}) == 2
    ids = J.assign_ids(kept)
    assert list(ids) == ["c1", "c2", "c3"] and ids == J.assign_ids(J.group_by_value(cands))
    assert J.assign_ids(["x", "y"], prefix="s") == {"s1": "x", "s2": "y"}


# --- 3. B1 admission dry run --------------------------------------------------------------------------------------

def test_a_dry_run_admits_without_storing_anything(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    doc = cache_source(cache, tail.CARTUBE, tail.CARTUBE_TEXT)
    specs = resolve_requested_fields(["length_mm"], propulsion="hybrid")
    adm = AdmissionContext.for_run(PAYLOAD, VEHICLE, specs, "IL")
    cand = next(c for c in harvest_document(cache, doc, specs)[0] if c["field"] == "length_mm")
    decision = J.dry_run(adm, cache, J.candidate_request("length_mm", cand), [doc])
    assert decision["accepted"] and J.decision_flags(decision)["variant_match"] == "exact"
    assert decision["record"]["quote"] == cand["quote"]
    store = EvidenceStore()
    assert store.items == []      # admit() itself never stores; only the store_evidence tool does


def test_a_table_header_widens_a_label_less_quote(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    doc = cache_source(cache, TOYOTA_IL, TOYOTA_IL_HTML, doc_type="html")
    specs = resolve_requested_fields(["torque_nm"], propulsion="hybrid")
    adm = AdmissionContext.for_run(PAYLOAD, VEHICLE, specs, "IL")
    cand = {**TABLE_TORQUE, "document_id": doc}
    first = J.dry_run(adm, cache, J.candidate_request("torque_nm", cand), [doc])
    assert not first["accepted"] and first["reasons"] == ["field_label_not_in_quote"]
    wider = J.widen_quote(J.DocumentReader(cache), cand)
    assert wider == "Version Max torque 1.8 Hybrid Business 142 Nm"
    assert J.dry_run(adm, cache, J.candidate_request("torque_nm", cand, wider), [doc])["accepted"]


def test_the_sweep_widens_shows_admissible_and_hides_inadmissible_candidates(tmp_path):
    client = AdjClient()
    summary, events, ids, ctx, specs = sweep_run(
        tmp_path, client, {tail.CARTUBE: (tail.CARTUBE_TEXT, "text"), TOYOTA_IL: (TOYOTA_IL_HTML, "html")},
        fields=["length_mm", "torque_nm"], candidates={TOYOTA_IL: [TABLE_TORQUE, GHOST_LENGTH]})
    dry = summary["adjudication"]["dry_run"]
    assert dry["admissible_after_widen"] == 1 and dry["not_admissible"] == 1
    assert dry["not_admissible_by_reason"] == {"quote_not_in_source": 1}
    ghost = f"length_mm|{ids[TOYOTA_IL]}|num:4650.0"
    rows = next(e for e in events if e["kind"] == "adjudication_not_admissible")["rows"]
    assert [r["candidate_key"] for r in rows] == [ghost] and rows[0]["reasons"] == ["quote_not_in_source"]
    sent = " ".join(r["messages"][1]["content"] for r in client.requests)
    assert "Overall length" not in sent                                    # never shown to the model
    assert "Version Max torque 1.8 Hybrid Business 142 Nm" in sent         # shown with the widened quote
    torque = [e for e in evidence_of(events) if e["field"] == "torque_nm" and e["document_id"] == ids[TOYOTA_IL]]
    assert [e["quote"] for e in torque] == ["Version Max torque 1.8 Hybrid Business 142 Nm"]
    # recovery treats the inadmissible candidate like a rejected one: never fresh again
    assert ghost in rejected_keys(events)
    matrix = candidate_matrix(events, specs, VEHICLE)
    assert ghost not in {candidate_key(c) for v in fresh_candidates(matrix, events, ["length_mm"]).values()
                         for c in v}


# --- 4. B2 classes ---------------------------------------------------------------------------------------------------

def _adm(value, match="exact", **hints):
    cand = {"value": value, **hints}
    return {"candidate": cand, "flags": {"variant_match": match}}


def test_one_exact_value_without_hints_is_unambiguous():
    items = [_adm(4650), _adm("4,650")]
    assert J.field_class(items, [i["candidate"] for i in items], False) == "U"


def test_a_harvester_hint_moves_a_single_value_field_to_ambiguous():
    items = [_adm(4650)]
    assert J.field_class(items, [{"value": 4650}, {"value": 4650, "trim_mentioned": True}], False) == "A"
    assert J.field_class(items, [{"value": 4650, "year_hint_differs": True}], False) == "A"
    # a field's own dictionary ambiguity rule hint key counts too
    keys = J.hint_keys({"ambiguity_rules": [{"terms": ["folded"], "hint_key": "cargo_configuration"}]})
    assert "cargo_configuration" in keys
    assert J.field_class(items, [{"value": 596, "cargo_configuration": "folded"}], False, keys) == "A"


def test_several_values_or_a_non_exact_binding_are_ambiguous():
    assert J.field_class([_adm(1460), _adm(1435)], [{"value": 1460}, {"value": 1435}], False) == "A"
    assert J.field_class([_adm(43, match="unclear")], [{"value": 43}], False) == "A"


def test_nothing_admissible_with_snippets_is_missing_and_without_is_not_sent():
    assert J.field_class([], [{"value": 1}], True) == "M"
    assert J.field_class([], [], False) is None


def test_packets_follow_clusters_and_limits():
    fields = ["a", "b", "c", "d", "e"]
    classes = {"a": "U", "b": "A", "c": "U", "d": "M", "e": "U"}
    clusters = {"a": "x", "b": "x", "c": "y", "d": "x", "e": "x"}
    sizes = {"a": 30, "b": 3, "c": 2, "d": 1, "e": 20}
    plan = J.plan_packets(fields=fields, classes=classes, clusters=clusters, sizes=sizes, limits={"u_items": 40})
    assert [(p["class"], p["cluster"], p["fields"]) for p in plan] == [
        ("U", "x", ["a"]), ("U", "x", ["e"]), ("A", "x", ["b"]), ("M", "x", ["d"]), ("U", "y", ["c"])]


# --- 5. B3 model calls: no tools, JSON only ---------------------------------------------------------------------------

def _two_class_docs():
    return {tail.CARTUBE: (tail.CARTUBE_TEXT, "text"), TOYOTA_IL: (TOYOTA_IL_HTML, "html")}


def test_requests_carry_no_tools_and_replies_are_parsed(tmp_path):
    def respond(packet, n):
        if packet["task"] == "locate_missing":
            field = packet["fields"][0]
            snip = field["snippets"][0]
            start = snip["text"].index("Ground clearance")
            return {"fields": [{"field": field["field"], "value": 160, "unit": "mm", "snippet": snip["id"],
                                "span": [start, snip["text"].index("mm", start) + 2]}]}
        return respond_all(packet)

    client = AdjClient(respond)
    summary, events, ids, ctx, _ = sweep_run(tmp_path, client, _two_class_docs(),
                                             fields=["length_mm", "height_mm", "ground_clearance_mm"],
                                             candidates={TOYOTA_IL: []})
    assert client.requests and all(r["tools"] is None for r in client.requests)
    assert {packet_of(r["messages"])["task"] for r in client.requests} == {
        "adjudicate_unambiguous", "adjudicate_ambiguous", "locate_missing"}
    assert [r["max_tokens"] for r in client.requests] == [
        {"adjudicate_unambiguous": 4000, "adjudicate_ambiguous": 6000, "locate_missing": 4000}[
            packet_of(r["messages"])["task"]] for r in client.requests]
    assert summary["adjudication"]["class_counts"] == {"U": 1, "A": 1, "M": 1}
    stored = {(e["field"], e["value"]) for e in evidence_of(events)}
    assert stored == {("length_mm", 4650), ("height_mm", 1460), ("height_mm", 1435), ("ground_clearance_mm", 160)}
    assert not any(e["kind"] == "tool_blocked" for e in events)


def test_unknown_ids_and_out_of_range_spans_are_ignored_and_logged(tmp_path):
    def respond(packet, n):
        if packet["task"] == "adjudicate_unambiguous":
            return {"decisions": [{"id": "c99", "accept": True, "reason": "ok"}]}
        if packet["task"] == "locate_missing":
            field = packet["fields"][0]
            return {"fields": [{"field": field["field"], "value": 160, "unit": "mm",
                                "snippet": field["snippets"][0]["id"], "span": [0, 99999]},
                               {"field": "not_in_packet", "value": 1, "snippet": "s1", "span": [0, 2]}]}
        return respond_all(packet)

    summary, events, ids, ctx, _ = sweep_run(tmp_path, AdjClient(respond), _two_class_docs(),
                                             fields=["length_mm", "ground_clearance_mm"], candidates={TOYOTA_IL: []})
    invalid = [r for e in events if e["kind"] == "adjudication_invalid_decision" for r in e["rows"]]
    assert {r["problem"] for r in invalid} == {"unknown_id", "span_out_of_range", "field_not_in_packet"}
    assert evidence_of(events) == [] and summary["adjudication"]["invalid_decisions"] == 3
    assert summary["adjudication"]["failed_packets"] == {"U": 0, "A": 0, "M": 0}


def test_a_missing_field_cannot_use_another_fields_snippet():
    snippets = {"s1": {"field": "ground_clearance_mm", "document_id": "d1", "text": "Ground clearance: 160 mm"}}
    rows, invalid = J.parse_m_reply({"fields": [{"field": "height_mm", "value": 160, "unit": "mm",
                                               "snippet": "s1", "span": [0, 20]}]},
                                    snippets, ["height_mm", "ground_clearance_mm"])
    assert rows == [] and invalid[0]["problem"] == "snippet_of_another_field"


def test_an_unparseable_reply_gets_one_repair_turn(tmp_path):
    def respond(packet, n):
        return "Sure! Here is my judgement" if n == 1 else respond_all(packet)

    client = AdjClient(respond)
    summary, events, *_ = sweep_run(tmp_path, client, _two_class_docs(), fields=["length_mm"])
    assert len(client.requests) == 2
    assert client.requests[1]["messages"][-1]["content"] == REPAIR_PROMPT
    assert summary["adjudication"]["repair_turns"] == 1 and summary["chunk_errors"] == []
    assert [e["field"] for e in evidence_of(events)] == ["length_mm"]


def test_a_second_unparseable_reply_fails_only_that_packet(tmp_path):
    def respond(packet, n):
        return "no json" if packet["task"] == "adjudicate_unambiguous" else respond_all(packet)

    client = AdjClient(respond)
    summary, events, *_ = sweep_run(tmp_path, client, _two_class_docs(), fields=["length_mm", "height_mm"])
    assert summary["chunk_errors"] == ["unparseable_after_repair"]
    assert summary["failed_chunk_fields"] == ["length_mm"]
    assert summary["adjudication"]["failed_packets"]["U"] == 1
    assert {e["field"] for e in evidence_of(events)} == {"height_mm"}       # the A packet still ran
    presented = [e for e in events if e["kind"] == "candidates_presented"]
    assert [p["chunk"]["class"] for p in presented] == ["A"]


# --- 6. B4 storage through the normal tool path --------------------------------------------------------------------

def test_an_accepted_candidate_is_stored_through_the_tool_path_with_its_own_quote(tmp_path):
    summary, events, ids, ctx, specs = sweep_run(tmp_path, AdjClient(), _two_class_docs(), fields=["length_mm"],
                                                 candidates={TOYOTA_IL: []})
    cand = next(c for c in candidate_matrix(events, specs, VEHICLE)["fields"]["length_mm"])
    [item] = evidence_of(events)
    assert item["quote"] == cand["quote"] and item["value"] == 4650 and item["document_id"] == ids[tail.CARTUBE]
    assert ctx.evidence.items == [item]
    calls = [e for e in events if e["kind"] == "tool_call"]
    results = [e for e in events if e["kind"] == "tool_result" and e["phase"] == "document_sweep"]
    assert [c["name"] for c in calls] == ["store_evidence"] and calls[0]["phase"] == "document_sweep"
    args = json.loads(calls[0]["arguments"])
    assert args == {"field": "length_mm", "value": 4650, "unit": cand["unit"], "document_id": ids[tail.CARTUBE],
                    "quote": cand["quote"], "variant_match": "exact"}
    assert results[0]["result"]["stored"] is True

    # the same shape as a store_evidence the legacy sweep's model emits
    legacy_client = PhaseClient([], sweep=lambda n: {"role": "assistant", "content": "", "tool_calls": [{
        "id": "m1", "type": "function", "function": {"name": "store_evidence", "arguments": json.dumps(args)}}]}
        if n == 1 else say({"reviewed": []}))
    _, legacy, *_ = sweep_run(tmp_path / "legacy", legacy_client, _two_class_docs(), fields=["length_mm"],
                              candidates={TOYOTA_IL: []}, sweep_mode="legacy")
    legacy_call = next(e for e in legacy if e["kind"] == "tool_call" and e["name"] == "store_evidence")
    legacy_result = next(e for e in legacy if e["kind"] == "tool_result" and e["name"] == "store_evidence")
    assert set(legacy_call) == set(calls[0]) and set(legacy_result) == set(results[0])
    assert set(next(e for e in legacy if e["kind"] == "evidence")) == set(next(e for e in events
                                                                                if e["kind"] == "evidence"))


def test_a_located_statement_is_stored_with_the_exact_snippet_substring(tmp_path):
    seen = {}

    def respond(packet, n):
        field = packet["fields"][0]
        snip = field["snippets"][0]
        start = snip["text"].index("Ground clearance")
        span = [start, snip["text"].index("mm", start) + 2]
        seen["quote"] = snip["text"][span[0]:span[1]]
        return {"fields": [{"field": field["field"], "value": 160, "unit": "mm", "snippet": snip["id"],
                            "span": span}]}

    summary, events, ids, *_ = sweep_run(tmp_path, AdjClient(respond), {TOYOTA_IL: (TOYOTA_IL_HTML, "html")},
                                         fields=["ground_clearance_mm"], candidates={TOYOTA_IL: []})
    [item] = evidence_of(events)
    assert seen["quote"] == "Ground clearance: 160 mm" and item["quote"] == seen["quote"]
    assert item["document_id"] == ids[TOYOTA_IL] and summary["adjudication"]["located"] == 1
    assert "variant_match" not in json.loads(next(e for e in events if e["kind"] == "tool_call")["arguments"])


def test_a_rejected_decision_stores_nothing(tmp_path):
    def respond(packet, n):
        return {"decisions": [{"id": i["id"], "accept": False, "reason": "other_trim"} for i in packet["items"]]}

    summary, events, *_ = sweep_run(tmp_path, AdjClient(respond), _two_class_docs(), fields=["length_mm"],
                                    candidates={TOYOTA_IL: []})
    assert evidence_of(events) == [] and not any(e["kind"] == "tool_call" for e in events)
    assert summary["adjudication"]["rejected_by_model"] == 1
    # the model judged it: presented (not fresh for recovery), even though nothing was stored
    assert any(e["kind"] == "candidates_presented" and e["source"] == "adjudication" for e in events)


def test_an_accept_with_another_reason_omits_the_variant_claim():
    item = {"field": "length_mm", "candidate": {"value": 4650, "unit": "mm", "document_id": "d1"}, "quote": "Length"}
    assert "variant_match" not in J.store_arguments(item, {"reason": "unclear"})
    assert J.store_arguments(item, {"reason": "ok"})["variant_match"] == "exact"


# --- 7. B5 a failed packet presents nothing -----------------------------------------------------------------------

def test_a_timed_out_packet_presents_nothing_and_its_fields_stay_local_material(tmp_path):
    def respond(packet, n):
        return _timeout() if n == 1 else respond_all(packet)

    client = AdjPhaseClient([fetch("a", EU, tail.CARTUBE), say({"done": True, "reason": "x"})], respond=respond)
    result, events = run(tmp_path, client, acquisition_mode="contract", **GATE_OFF)
    starts = [e for e in events if e["kind"] == "document_sweep_started"]
    first, second = starts[0], starts[1]
    assert first["offered_candidate_keys"]
    failed = next(e for e in events if e["kind"] == "document_sweep_chunk_failed")
    assert failed["index"] == 1 and failed["timeout"] is True
    presented = [e for e in events if e["kind"] == "candidates_presented" and e["source"] == "adjudication"]
    assert presented and 1 not in [p["chunk"]["index"] for p in presented]
    assert presented[0]["presented_candidate_keys"] == second["offered_candidate_keys"]   # the next packet ran
    failed_fields = set(first["fields_to_review"])
    queue = next(e for e in events if e["kind"] == "field_retry_queue")
    triaged = {q["field"]: q["triage"] for q in queue["queue"]}
    still_open = [f for f in failed_fields if f in triaged]
    assert still_open and all(triaged[f] == "candidate_rich_local" for f in still_open)
    assert result["document_sweep"]["failed_chunk_candidates_kept_fresh"] == len(set(first["offered_candidate_keys"]))


def test_a_local_apply_failure_keeps_candidates_fresh_for_recovery(tmp_path, monkeypatch):
    def broken_execute(self, calls, messages, *, phase, **kwargs):
        raise RuntimeError("local storage failed")

    monkeypatch.setattr(ToolSession, "execute", broken_execute)
    summary, events, _, _, specs = sweep_run(tmp_path, AdjClient(), _two_class_docs(),
                                             fields=["length_mm"], candidates={TOYOTA_IL: []})
    assert summary["adjudication"]["failed_packets"]["U"] == 1
    assert summary["candidates_presented"] == 0
    assert not any(e["kind"] == "candidates_presented" for e in events)
    matrix = candidate_matrix(events, specs, VEHICLE)
    assert fresh_candidates(matrix, events, ["length_mm"])["length_mm"]


# --- 8. end to end on the Corolla fixtures -----------------------------------------------------------------------

@pytest.fixture
def adjudicated(tmp_path):
    client = AdjPhaseClient([fetch("a", EU, tail.CARTUBE), say({"done": True, "reason": "x"})])
    result, events = run(tmp_path, client, acquisition_mode="contract", **GATE_OFF)
    return client, result, events


def test_end_to_end_fields_resolve_with_zero_model_store_calls(adjudicated):
    client, result, events = adjudicated
    sweep = result["document_sweep"]
    assert sweep["sweep_mode"] == "adjudication" and sweep["fields_resolved"]
    assert {"length_mm", "width_mm", "wheelbase_mm", "torque_nm"} <= set(sweep["fields_resolved"])
    for key in ("fields_resolved", "unique_fields_resolved", "evidence_stored", "model_calls", "turns",
                "candidates_presented", "tool_calls_blocked", "external_calls", "error", "chunk_errors",
                "failed_chunk_fields", "document_sweep_calls", "document_sweep_timeouts",
                "document_sweep_chunk_details", "pre_sweep_inspection", "candidates_from_unusable_documents"):
        assert key in sweep, key
    responses = [e for e in events if e["kind"] == "model_response" and e["phase"] == "document_sweep"]
    assert responses and not any(e.get("tool_calls") for e in responses)          # no tool call from the model
    stores = [e for e in events if e["kind"] == "tool_call" and e["phase"] == "document_sweep"]
    assert stores and all(e["name"] == "store_evidence" and e["call_id"].startswith("adj") for e in stores)
    assert sweep["evidence_stored"] == sweep["adjudication"]["admitted"] > 0
    assert sweep["adjudication"]["rejected_by_admission"] == 0
    assert sweep["model_calls"] == sum(sweep["adjudication"]["packets"].values())
    assert all(r["tools"] is None for r in client.adj.requests)
    detail = sweep["document_sweep_chunk_details"][0]
    assert {"latency_ms", "prompt_tokens", "completion_tokens", "class"} <= set(detail)


def test_diagnostics_read_adjudication_runs(adjudicated):
    client, result, events = adjudicated
    calls = D.sweep_calls(events)
    assert len(calls) == sum(result["document_sweep"]["adjudication"]["packets"].values())
    assert all(c["sweep_mode"] == "adjudication" and c["packet_class"] in ("U", "A", "M") for c in calls)
    assert all(c["model_call_count"] == 1 and c["call_success"] and not c["malformed_final_reply"] for c in calls)
    assert sum(c["evidence_admitted"] for c in calls) == result["document_sweep"]["evidence_stored"]
    config = D.run_configuration(events)
    assert config["sweep_mode"] == "adjudication" and "sweep_mode=adjudication" in D.config_key(config)
    assert D.run_configuration([{"kind": "run_started", "agent_config": {}}])["sweep_mode"] == "legacy"
    assert result["sweep_mode"] == "adjudication"
    assert result["effective_config"]["agent"]["sweep_mode"] == "adjudication"
    assert next(e for e in events if e["kind"] == "run_started")["sweep_mode"] == "adjudication"


# --- 9. SWEEP_MODE=legacy ------------------------------------------------------------------------------------------

def test_sweep_mode_comes_from_the_environment():
    assert AgentConfig.__dataclass_fields__["sweep_mode"].default == "adjudication"
    assert agent_config_from_env(lambda name: None).sweep_mode == "adjudication"
    assert agent_config_from_env({"SWEEP_MODE": "legacy"}.get).sweep_mode == "legacy"
    assert agent_config_from_env({"SWEEP_MODE": "nonsense"}.get).sweep_mode == "adjudication"
    env = {"ADJUDICATION_MAX_U_ITEMS": "10", "ADJUDICATION_A_MAX_TOKENS": "900"}
    config = agent_config_from_env(env.get)
    assert config.adjudication_max_u_items == 10 and config.adjudication_a_max_tokens == 900


def test_legacy_mode_runs_the_tool_loop_sweep(tmp_path):
    client = AdjPhaseClient([fetch("a", EU, tail.CARTUBE), say({"done": True, "reason": "x"})])
    result, events = run(tmp_path, client, acquisition_mode="contract", sweep_mode="legacy", **GATE_OFF)
    sweeps = [r for r in client.requests if r["messages"][0]["content"] == DOCUMENT_SWEEP_SYSTEM_PROMPT]
    assert sweeps and all(r["tools"] for r in sweeps) and client.adj.requests == []
    assert not any(e["kind"].startswith("adjudication") for e in events)
    assert result["document_sweep"]["sweep_mode"] == "legacy" and "adjudication" not in result["document_sweep"]


# --- 10. stale DOCUMENT_SWEEP_* limits ----------------------------------------------------------------------------

def test_stale_document_sweep_limits_never_change_adjudication_packets(tmp_path):
    sent = []
    for name, cfg in (("default", {}), ("stale", {"document_sweep_max_fields": 30, "document_sweep_max_candidates": 48,
                                                    "document_sweep_packet_max_chars": 500})):
        client = AdjPhaseClient([fetch("a", EU, tail.CARTUBE), say({"done": True, "reason": "x"})])
        run(tmp_path / name, client, acquisition_mode="contract", **GATE_OFF, **cfg)
        sent.append([r["messages"][1]["content"] for r in client.adj.requests])
    assert sent[0] and sent[0] == sent[1]
    env = {"DOCUMENT_SWEEP_MAX_FIELDS": "30"}
    assert agent_config_from_env(env.get).document_sweep_max_fields == 30     # still read, only for legacy
