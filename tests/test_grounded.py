"""Grounded candidates (PR #31 Part E): a model points at a statement, code cuts and verifies the quote, dry-runs
admission, and the candidate reaches adjudication as class A. Nothing is stored without adjudication + admission."""

import json

from fixtures import corolla_tail as tail
from test_adjudication import AdjClient, evidence_of, respond_all, sweep_run

from src import grounded as G
from src.fields import resolve_requested_fields

WARRANTY_QUOTE = "אחריות: שלוש שנים"


def grounded_requests(client):
    return [r for r in client.requests if json.loads(r["messages"][1]["content"].split("\n", 1)[1])["task"]
            == "locate_values"]


def pointer(packet, field, needle, value, unit=None, **extra):
    for block in packet["blocks"]:
        start = block["text"].find(needle)
        if start >= 0:
            return {"field": field, "value": value, "unit": unit, "block": block["id"], "start": start,
                    "end": start + len(needle), **extra}
    raise AssertionError(f"{needle!r} not in any block")


def test_a_grounded_pointer_becomes_a_class_a_candidate_and_is_stored_only_through_adjudication(tmp_path):
    seen = {}

    def respond(packet, n):
        if packet["task"] == "locate_values":
            seen["grounded"] = packet
            items = [pointer(packet, "warranty_years", WARRANTY_QUOTE, 3, "years"),
                     {"field": "warranty_years", "value": 9, "block": packet["blocks"][0]["id"], "start": 5,
                      "end": 5000},                                                     # span out of range
                     {"field": "torque_nm", "value": 142, "block": packet["blocks"][0]["id"], "start": 0,
                      "end": 4},                                                        # not a requested field
                     {"field": "warranty_years", "value": 7, "block": "b999", "start": 0, "end": 3}]   # unknown block
            return {"items": items}
        if packet["task"] == "adjudicate_ambiguous":
            seen.setdefault("a_packets", []).append(packet)
        return respond_all(packet)

    client = AdjClient(respond)
    summary, events, ids, ctx, _ = sweep_run(tmp_path, client, {tail.CARTUBE: (tail.CARTUBE_TEXT, "text")},
                                             fields=["length_mm", "warranty_years"], grounded_candidates=True)
    # only the field without an admissible candidate was sent (length_mm has its harvested "אורך: 4,650 מ"מ")
    assert [f["field"] for f in seen["grounded"]["fields"]] == ["warranty_years"]
    assert all(r["tools"] is None for r in client.requests)
    # the grounded candidate is harvest-equivalent (candidate_matrix sees it) and was judged in an A packet
    grounded_events = [e for e in events if e["kind"] == "candidates_harvested" and e.get("source") == "grounded_llm"]
    assert len(grounded_events) == 1
    cand = grounded_events[0]["candidates"][0]
    assert (cand["field"], cand["value"], cand["extraction_method"], cand["parser_confidence"]) == \
        ("warranty_years", 3, "grounded_llm", 0.5)
    assert cand["quote"] == WARRANTY_QUOTE
    a_fields = [c for p in seen.get("a_packets") or [] for f in p["fields"] if f["field"] == "warranty_years"
                for c in f["candidates"]]
    assert a_fields and a_fields[0]["value"] == 3
    plan = next(e for e in events if e["kind"] == "adjudication_plan")
    assert plan["classes"]["warranty_years"] == "A" and plan["grounded_fields"] == ["warranty_years"]
    # invalid pointers are logged and dropped
    invalid = next(e for e in events if e["kind"] == "grounded_candidates_invalid")
    assert {r["problem"] for r in invalid["rows"]} == {"span_out_of_range", "field_not_requested", "unknown_block"}
    # the model accepted it in adjudication -> stored through store_evidence + admission
    stored = [e for e in evidence_of(events) if e["field"] == "warranty_years"]
    assert [e["value"] for e in stored] == [3]
    finished = next(e for e in events if e["kind"] == "grounded_candidates_finished")
    assert finished["admissible"] == 1 and finished["invalid"] == 3 and finished["model_calls"] == 1
    assert summary["grounded_candidates"]["admissible"] == 1


def test_a_grounded_candidate_rejected_in_adjudication_is_never_stored(tmp_path):
    def respond(packet, n):
        if packet["task"] == "locate_values":
            return {"items": [pointer(packet, "warranty_years", WARRANTY_QUOTE, 3, "years")]}
        if packet["task"] == "adjudicate_ambiguous":
            return {"fields": [{"field": f["field"], "reject": [{"id": c["id"], "reason": "unclear"}
                                                                for c in f["candidates"]]} for f in packet["fields"]]}
        return respond_all(packet)

    summary, events, *_ = sweep_run(tmp_path, AdjClient(respond), {tail.CARTUBE: (tail.CARTUBE_TEXT, "text")},
                                    fields=["length_mm", "warranty_years"], grounded_candidates=True)
    assert not [e for e in evidence_of(events) if e["field"] == "warranty_years"]
    # grounded never calls store_evidence itself: the only store requests are the adjudication's synthetic calls
    stores = [e for e in events if e["kind"] == "tool_call" and e["name"] == "store_evidence"]
    assert all(str(e["call_id"]).startswith("adj") for e in stores)


def test_quotes_not_in_the_document_and_inadmissible_values_are_dropped(tmp_path, monkeypatch):
    real = G.document_blocks

    def with_fake_block(*args, **kwargs):
        return real(*args, **kwargs) + [{"id": "b900", "text": "Warranty: 9 years everywhere"}]

    monkeypatch.setattr(G, "document_blocks", with_fake_block)

    def respond(packet, n):
        if packet["task"] == "locate_values":
            return {"items": [pointer(packet, "warranty_years", "Warranty: 9 years", 9, "years"),       # invented text
                              pointer(packet, "warranty_years", WARRANTY_QUOTE, 5, "years")]}          # wrong value
        return respond_all(packet)

    summary, events, *_ = sweep_run(tmp_path, AdjClient(respond), {tail.CARTUBE: (tail.CARTUBE_TEXT, "text")},
                                    fields=["length_mm", "warranty_years"], grounded_candidates=True)
    invalid = next(e for e in events if e["kind"] == "grounded_candidates_invalid")
    assert [r["problem"] for r in invalid["rows"]] == ["quote_not_in_document"]
    rejected = next(e for e in events if e["kind"] == "grounded_candidates_not_admissible")
    assert rejected["rows"][0]["value"] == 5 and rejected["rows"][0]["reasons"]
    assert not [e for e in events if e["kind"] == "candidates_harvested" and e.get("source") == "grounded_llm"]
    assert not [e for e in evidence_of(events) if e["field"] == "warranty_years"]


def test_a_failed_grounded_call_costs_only_its_document(tmp_path):
    from src.glm_client import GLMError

    docs = {f"https://www.toyota.co.il/corolla/page-{i}": (f"טויוטה קורולה 2024 עמוד {i}. אחריות: שלוש שנים", "text")
            for i in range(2)}
    calls = []

    def respond(packet, n):
        if packet["task"] == "locate_values":
            calls.append(n)
            if len(calls) == 1:
                error = GLMError("read timeout")
                error.timeout = True
                return error
            return {"items": [pointer(packet, "warranty_years", WARRANTY_QUOTE, 3, "years")]}
        return respond_all(packet)

    summary, events, *_ = sweep_run(tmp_path, AdjClient(respond), docs, fields=["warranty_years"],
                                    grounded_candidates=True)
    assert len(calls) == 2
    failed = [e for e in events if e["kind"] == "grounded_candidates_document_failed"]
    assert len(failed) == 1
    assert summary["grounded_candidates"]["admissible"] == 1


def test_selection_limits_three_documents_and_fifteen_fields(tmp_path):
    names = [s["name"] for s in resolve_requested_fields(None, propulsion="hybrid") if s.get("applicable", True)]
    fields = [n for n in names if n not in ("length_mm",)][:20]
    docs = {f"https://www.toyota.co.il/corolla/info-{i}": (f"טויוטה קורולה 2024 מידע כללי {i}", "text")
            for i in range(5)}
    docs["https://www.example-forum.net/corolla"] = ("Toyota Corolla 2024 owners talk", "text")   # not official / IL
    packets = []

    def respond(packet, n):
        if packet["task"] == "locate_values":
            packets.append(packet)
            return {"items": []}
        return respond_all(packet)

    sweep_run(tmp_path, AdjClient(respond), docs, fields=fields, grounded_candidates=True)
    assert len(packets) == 3 and all(len(p["fields"]) == 15 for p in packets)
    assert all("example-forum" not in str(p.get("source")) for p in packets)


def test_grounded_off_and_nothing_to_do_skip_the_stage(tmp_path):
    client = AdjClient()
    sweep_run(tmp_path, client, {tail.CARTUBE: (tail.CARTUBE_TEXT, "text")}, fields=["warranty_years"],
              grounded_candidates=False)
    assert not grounded_requests(client)
    client = AdjClient()
    _, events, *_ = sweep_run(tmp_path / "2", client, {tail.CARTUBE: (tail.CARTUBE_TEXT, "text")},
                              fields=["length_mm"], grounded_candidates=True)
    assert not grounded_requests(client)
    skipped = next(e for e in events if e["kind"] == "grounded_candidates_skipped")
    assert skipped["reason"] == "no_fields_without_admissible_candidate"


def test_blocks_parse_and_quote_cut_are_pure():
    blocks = G.document_blocks("[page 1]\nLength 4,650 mm\nWarranty three years\n[page 2]\nTop speed 180 km/h",
                               is_pdf=True, tables=[{"rows": [["Width", "1,790 mm"]]}])
    assert [(b["id"], b.get("page")) for b in blocks] == [("b1", 1), ("b2", 2), ("b3", None)]
    assert blocks[2]["text"] == "Width | 1,790 mm"
    by_id = {b["id"]: b for b in blocks}
    items, invalid = G.parse_reply({"items": [{"field": "top_speed_kmh", "value": 180, "block": "b2", "start": 2,
                                               "end": 12}]}, by_id, ["top_speed_kmh"])
    assert items[0]["quote"] == "Top speed 180" and not invalid       # widened to whole words
    long_text = "x " * 2000
    capped = G.document_blocks(long_text * 20, is_pdf=False)
    assert sum(len(b["text"]) for b in capped) <= G.MAX_PACKET_TEXT
