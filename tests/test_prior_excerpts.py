"""prior_relevant_excerpts: bounded, deduplicated, schema-driven retry working memory.
Scripted GLM / fake HTTP only: no network, no paid calls."""

import json

from test_tools_smoke import ScriptedGLM, _call

from src.agent import FIELD_RECOVERY_SYSTEM_PROMPT, AgentConfig, run_vehicle
from src.benchmark import compute_metrics
from src.excerpts import select_prior_excerpts
from src.storage.run_log import RunLog, read_events
from src.tools import ToolConfig

AC = {"name": "ac_charging_time", "description": "AC charging time (with % window)", "group": "electric_hybrid"}
LEGROOM = {"name": "rear_legroom_mm", "description": "rear_legroom_mm"}


def pair(step, name, args, result, field=None, attempt=None, phase=None):
    tags = {"phase": phase or ("field_recovery" if field else "research")}
    if field:
        tags.update(field=field, attempt=attempt or 1)
    cid = f"c{step}-{name}-{json.dumps(args, sort_keys=True)}"
    return [{"kind": "tool_call", "step": step, "call_id": cid, "name": name, "arguments": json.dumps(args), **tags},
            {"kind": "tool_result", "step": step, "call_id": cid, "name": name, "result": result, **tags}]


def find(step, doc, query, hits, **tags):
    return pair(step, "find_in_document", {"document_id": doc, "query": query},
                {"document_id": doc, "query": query, "scope": "text", "hit_count": len(hits),
                 "hits": [{"offset": o, "snippet": s, "match_score": 1.0} for o, s in hits]}, **tags)


def html(step, doc, offset, text, **tags):
    return pair(step, "extract_html", {"document_id": doc, "offset": offset},
                {"document_id": doc, "offset": offset, "text": text, "total_chars": 99999}, **tags)


def select(events, spec=AC, evidence=(), **limits):
    return select_prior_excerpts(events, spec, list(evidence), [{"document_id": "dA", "url": "https://a.example"}],
                                 **limits)


# --- packet construction ---------------------------------------------------------------------------

def test_hits_and_html_slices_are_reused_but_zero_hits_are_not():
    events = (find(1, "dA", "charging", [(500, "…AC charging 19.2 kW onboard charger, 10.5 hours…")])
              + find(2, "dA", "טעינה", [])
              + html(3, "dB", 1700, "Charging: an 11 kW AC charging unit is standard in some markets."))
    excerpts, stats = select(events)
    assert [e["origin_tool"] for e in excerpts] == ["find_in_document", "extract_html"]          # 1, 3
    first = excerpts[0]
    assert first["text"].startswith("…AC charging 19.2 kW") and first["query"] == "charging"
    assert (first["document_id"], first["source_url"], first["offset"], first["step"]) == \
        ("dA", "https://a.example", 500, 1)
    assert not any(e.get("query") == "טעינה" for e in excerpts)                                   # 2
    assert stats["items"] == 2 and stats["chars"] == sum(len(e["text"]) for e in excerpts)


def test_tables_and_structured_data_are_compacted():
    rows = [["Spec", "Value"]] + [[f"filler row {i}", "x" * 200] for i in range(40)] + \
           [["AC charging", "19.2 kW, 10.5 h"], ["DC charging", "350 kW"]]
    table = pair(1, "extract_tables", {"document_id": "dA"},
                 {"document_id": "dA", "tables_total": 1, "tables": [{"index": 0, "rows": rows}]})
    structured = pair(2, "get_structured_data", {"document_id": "dB"},
                      {"document_id": "dB", "found": {"json_ld": True},
                       "data": {"json_ld": [{"name": "Escalade IQ", "brand": {"name": "Cadillac"},
                                             "offers": [{"price": i} for i in range(30)],
                                             "specs": {"acCharging": "19.2 kW AC charging"}}]}})
    excerpts, _ = select(table + structured)
    t = next(e for e in excerpts if e["origin_tool"] == "extract_tables")                         # 4
    lines = t["text"].split("\n")
    assert lines[0] == "[table 0; 2 of 42 rows]" and lines[1] == "Spec | Value"
    assert lines[2:] == ["AC charging | 19.2 kW, 10.5 h", "DC charging | 350 kW"]
    assert "filler" not in t["text"]
    s = next(e for e in excerpts if e["origin_tool"] == "get_structured_data")                    # 5
    assert s["text"] == "json_ld[0].specs.acCharging: 19.2 kW AC charging" and "price" not in s["text"]


def test_same_field_ranks_first_and_unrelated_material_is_excluded():
    events = (find(1, "dA", "legroom", [(10, "Rear legroom 1,020 mm")], field="rear_legroom_mm")
              + html(2, "dB", 0, "Interior trim colours and seat materials.", field="ac_charging_time")
              + find(3, "dC", "charging", [(10, "AC charging takes 10.5 hours")], field="battery_gross_kwh")
              + html(4, "dD", 0, "Dealer opening hours and showroom address."))
    excerpts, stats = select(events)
    assert [e["document_id"] for e in excerpts] == ["dB", "dC"]                                   # 6
    assert excerpts[0]["field"] == "ac_charging_time" and excerpts[0]["relevance"] >= 100
    assert stats["below_relevance"] == 2                                                          # 7
    legroom, _ = select(events, LEGROOM)
    assert [e["document_id"] for e in legroom] == ["dA"]                                          # fresh field


def test_item_and_character_caps_are_enforced():
    events = []
    for i in range(12):
        events += find(i + 1, f"d{i}", "charging", [(0, f"AC charging detail {i}: " + "kW " * 400)])
    excerpts, stats = select(events, max_items=3, max_chars=2500, max_excerpt_chars=1000)           # 8
    assert len(excerpts) == 3 and all(len(e["text"]) <= 1000 for e in excerpts)
    assert sum(len(e["text"]) for e in excerpts) <= 2500 and stats["cut_by_caps"] >= 9
    default, _ = select(events)
    # 6 full ~1.2k excerpts + a 7th truncated to the remaining room: the 8,000-char budget binds first.
    assert len(default) == 7 and sum(len(e["text"]) for e in default) == 8000


def test_overlapping_excerpts_are_deduplicated_preferring_the_focused_one():
    doc_text = "x" * 1700 + "טעינה AC: 19.2 kW onboard charger, 10.5 hours" + "y" * 3000
    hit_at = 1700
    snippet = doc_text[hit_at - 300: hit_at + 5 + 300]
    events = (find(1, "dA", "טעינה", [(hit_at, snippet)], field="ac_charging_time")
              + html(2, "dA", 1400, doc_text[1400:5400], field="ac_charging_time"))
    excerpts, stats = select(events)                                                              # 9
    assert [e["origin_tool"] for e in excerpts] == ["find_in_document"] and stats["deduplicated"] == 1
    # Identical text from two different calls is also kept once.
    twice = find(1, "dA", "AC", [(5, "AC charging 19.2 kW")]) + find(2, "dA", "charging", [(5, "AC charging 19.2 kW")])
    assert len(select(twice)[0]) == 1


def test_no_full_document_is_inserted():
    long_text = "Overview. " * 200 + "AC charging at 19.2 kW takes about 10.5 hours. " + "Warranty. " * 2000
    events = pair(1, "get_cached_document", {"key": "dA"},
                  {"found": True, "document_id": "dA", "offset": 0, "text": long_text[:4000]})
    events += html(2, "dB", 0, long_text[:4000])
    excerpts, _ = select(events)                                                                  # 10
    assert excerpts and all(len(e["text"]) <= 1500 for e in excerpts)
    assert all("AC charging at 19.2 kW" in e["text"] for e in excerpts)
    assert sum(e["text"].count("Warranty") for e in excerpts) < 300   # a window, never the 20k-char tail


# --- cross-attempt Cadillac regression -------------------------------------------------------------------

def put(cache, url, text, html_body=None):
    meta = {"status": 200, "final_url": url, "doc_type": "html" if html_body else "text",
            "content_type": "text/html" if html_body else "text/plain"}
    return cache.put("fetch", url, (html_body or text).encode("utf-8"), meta, text)["document_id"]


def turn(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def say(obj):
    return {"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)}


def test_cadillac_ac_charging_attempt_two_gets_prior_excerpts(tmp_path, make_ctx):
    ctx = make_ctx()
    rows = "".join(f"<tr><td>שורה {i}</td><td>ערך {i}</td></tr>" for i in range(30))
    # the importer page names the exact technical variant (model, propulsion, power): its facts bind exactly
    title = 'קאדילאק אסקלייד IQ רכב חשמלי 750 כ"ס - טעינה ביתית'
    cm_html = (f"<html><body><p>{title}</p><table><tr><th>מפרט</th><th>ערך</th></tr>{rows}"
               "<tr><td>טעינה AC</td><td>19.2 קילוואט, כ-10.5 שעות</td></tr></table></body></html>")
    CM = put(ctx.cache, "https://www.cmotors.co.il/escalade-iq", title + "\nטעינה AC\n19.2 קילוואט, כ-10.5 שעות",
             cm_html)
    gadgety_text = "פתיח " * 340 + "טעינה AC במטען המובנה של 19.2 קילוואט נמשכת כ-10.5 שעות. " + "GADGETY-TAIL " * 600
    GD = put(ctx.cache, "https://www.gadgety.co.il/escalade-iq-review", gadgety_text)
    us_text = "Intro " * 250 + "The Escalade IQ supports 19.2 kW Level 2 AC charging. " + "US-TAIL " * 800
    US = put(ctx.cache, "https://www.cadillac.com/escalade-iq", us_text)
    script = [
        say({"summary": "primary", "fields": {}}),
        # ac_charging_time attempt 1 (as observed): three tool turns, then unresolved
        turn(_call("a1", "find_in_document", {"document_id": CM, "query": "טעינה"}),
             _call("a2", "find_in_document", {"document_id": GD, "query": "טעינה"})),
        turn(_call("a3", "extract_html", {"document_id": GD, "offset": 1700}),
             _call("a4", "extract_html", {"document_id": US, "offset": 1500})),
        turn(_call("a5", "extract_tables", {"document_id": CM})),
        say({"field": "ac_charging_time", "status": "unresolved", "notes": "need the Israeli figure confirmed"}),
        # breadth-first: rear_legroom_mm (a different field) gets its first attempt before any second attempt
        say({"field": "rear_legroom_mm", "status": "unresolved"}),
        # ac_charging_time attempt 2 answers from the excerpts it was given: no broad get_cached_document re-read
        turn(_call("b1", "store_evidence", {"field": "ac_charging_time", "value": "10.5 h", "document_id": CM,
                                            "market": "IL", "quote": "19.2 קילוואט, כ-10.5 שעות"})),
        say({"field": "rear_legroom_mm", "status": "unresolved"}),
        say({"summary": "final", "fields": {}}),
    ]
    client = ScriptedGLM(script)
    log = RunLog(tmp_path / "runs", "b", "85095")
    payload = {"identity": {"manufacturer": "קאדילאק", "commercial_name": "ESCALADE IQ", "government_record_id": "85095"},
               "engine_drivetrain": {"propulsion_normalized": "battery_electric", "power_hp": 750}}
    result = run_vehicle({"upstream_record_id": "85095"}, payload, client=client, cache=ctx.cache, run_log=log,
                         config=AgentConfig(max_steps=3, no_new_research_turns=0, recovery_mode="legacy",
                                            requested_fields=["ac_charging_time", "rear_legroom_mm"]),
                         tool_config=ToolConfig(), session=ctx.session)
    packets = [json.loads(r["messages"][1]["content"].split("\n", 1)[1]) for r in client.requests
               if r["messages"][0]["content"] == FIELD_RECOVERY_SYSTEM_PROMPT and len(r["messages"]) == 2]
    first, legroom, second = packets[0], packets[1], packets[2]
    assert (first["requested_field"]["name"], first["attempt"]) == ("ac_charging_time", 1)
    assert first["prior_relevant_excerpts"] == []                    # nothing exposed yet
    assert (second["requested_field"]["name"], second["attempt"]) == ("ac_charging_time", 2)

    excerpts = second["prior_relevant_excerpts"]
    text = json.dumps(excerpts, ensure_ascii=False)
    assert "19.2 קילוואט" in text and "19.2 kW Level 2 AC charging" in text
    assert {e["document_id"] for e in excerpts} == {CM, GD, US}
    assert all(e["field"] == "ac_charging_time" and e["attempt"] == 1 and e["phase"] == "field_recovery"
               for e in excerpts)
    assert any(e["origin_tool"] == "extract_tables" and "19.2" in e["text"] for e in excerpts)
    # bounded: no full documents, no previous conversation, no automatic evidence
    assert len(excerpts) <= 8 and sum(len(e["text"]) for e in excerpts) <= 8000
    assert all(len(e["text"]) <= 1500 for e in excerpts)
    assert text.count("GADGETY-TAIL") < 150 and text.count("US-TAIL") < 200   # windows, not the 600/800-token tails
    assert "שורה 29" not in text                                     # table filler rows dropped
    assert second["existing_evidence"] == [] and second["prior_excerpt_stats"]["items"] == len(excerpts)
    assert {o["tool"] for o in second["already_attempted_operations"]} >= {"find_in_document", "extract_html",
                                                                            "extract_tables"}
    assert all(m["role"] in ("system", "user") for m in client.requests[6]["messages"][:2])

    # attempt 2 resolved from the excerpts, without reopening any document
    rec = result["field_recovery"]
    assert rec["attempt_order"] == ["ac_charging_time#1", "rear_legroom_mm#1", "ac_charging_time#2",
                                    "rear_legroom_mm#2"]
    attempt2 = rec["attempts"][2]
    assert attempt2["early_resolved"] and attempt2["turns"] == 1
    assert attempt2["document_rereads_after_prior_excerpt"] == 0 and attempt2["prior_excerpt_items"] == len(excerpts)
    assert attempt2["prior_excerpt_chars"] == sum(len(e["text"]) for e in excerpts)
    assert attempt2["packet_chars"] < len(gadgety_text) + len(us_text)
    assert not [c for c in result["tool_calls"] if c["name"] == "get_cached_document"]
    assert [e["field"] for e in result["evidence"]] == ["ac_charging_time"]   # only the model's own store

    # a different field gets only excerpts relevant to it (none here)
    assert legroom["requested_field"]["name"] == "rear_legroom_mm" and legroom["prior_relevant_excerpts"] == []

    m = compute_metrics(result)
    assert m["recovery_attempts_with_prior_excerpts"] == 1 and m["recovery_prior_excerpt_items"] == len(excerpts)
    assert m["recovery_document_rereads_after_prior_excerpt"] == 0
    started = [e for e in read_events(log.events_path) if e["kind"] == "field_recovery_started"]
    assert started[2]["prior_excerpt_items"] == len(excerpts) and started[2]["prior_excerpts"] == excerpts
    assert result["output"]["summary"] == "final"


def test_a_stale_unresolved_statement_does_not_outlive_newer_evidence():
    """Exposed by the regression above: attempt 1 said "unresolved", attempt 2 then stored evidence."""
    from src.field_recovery import evaluate_field

    spec = {"name": "ac_charging_time", "applicable": True}
    ev = [{"evidence_id": "e1", "field": "ac_charging_time", "value": "10.5 h", "market": "IL",
           "document_id": "dA", "quote": "10.5 שעות"}]
    stale = {"status": "unresolved", "seq": 5}
    assert evaluate_field(spec, ev, stale, None, "IL", last_evidence_seq=9)["state"] == "ok"
    assert evaluate_field(spec, ev, {"status": "unresolved", "seq": 12}, None, "IL", last_evidence_seq=9)["state"] \
        == "unresolved"                                            # a statement after the evidence still holds
    assert evaluate_field(spec, ev, stale, None, "IL")["state"] == "unresolved"   # unknown order: it holds
    out = {"value": None, "provenance": "unresolved"}
    assert evaluate_field(spec, ev, None, out, "IL", last_evidence_seq=9, output_seq=4)["state"] == "ok"
    assert evaluate_field(spec, [], None, out, "IL", last_evidence_seq=None, output_seq=4)["state"] == "unresolved"
