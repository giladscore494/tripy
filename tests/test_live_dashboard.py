"""Live Hebrew operations dashboard: schema-driven Hebrew labels, applicable-field denominators, live progress
from the shared evaluator, operational "why", provider reasoning (never fabricated), queue / in-flight states,
and the zero-extra-API-calls invariant. No network, no paid calls."""

import json
import threading
import time

import pytest
import requests

from fixtures.cadillac_lyriq import put_documents
from test_concurrency import InflightSession, client as glm_client
from test_layered_pipeline import PhaseGLM, read_docs, run, say

from src.concurrency import ConcurrencyController
from src.fields import resolve_requested_fields
from src.glm_client import GLMClient
from src.presentation import labels_he as he
from src.runstate.live_state import BatchLiveState, VehicleLive, candidate_table_rows

US = "https://www.cadillac.com/lyriq"
IL = "https://www.cadillac.co.il/lyriq"


def started(propulsion="battery_electric", fields=None, budget=24):
    specs = resolve_requested_fields(fields, propulsion=propulsion)
    return {"kind": "run_started", "requested_field_specs": specs, "target_market": "IL", "research_model":
            "glm-5.3-flash", "finalizer_model": "glm-5.3", "agent_config": {"field_recovery_max_total_steps": budget,
                                                                           "field_recovery_enabled": True}}


def evidence(seq, eid, field, value, market, url=IL):
    return {"kind": "evidence", "seq": seq, "evidence": {"evidence_id": eid, "field": field, "value": value,
                                                         "market": market, "source_url": url}}


def feed(vehicle: VehicleLive, events):
    for e in events:
        vehicle.apply(e["kind"], e)


def test_bev_denominator_is_45_of_47():
    v = VehicleLive("85095", "קאדילאק LYRIQ · 2025 · Luxury")
    feed(v, [started()])
    assert len(v.specs) == 47 and v.progress()["total"] == 45
    assert v.card()["progress_text"] == "הושלמו 0 מתוך 45 שדות"


def test_field_table_is_hebrew_for_every_default_field():
    v = VehicleLive("1", "t")
    feed(v, [started(propulsion="plug_in"),             # plug-in hybrid: all 45 default fields apply
             evidence(2, "e1", "battery_gross_kwh", 102, "US", US),
             evidence(3, "e2", "torque_nm", 610, "IL"), evidence(4, "e3", "torque_nm", 650, "IL"),
             {"kind": "field_status", "seq": 5, "field": "fuel_tank_l", "status": "not_applicable"}])
    rows = v.field_rows()
    assert len(rows) == 47 and list(rows[0]) == he.FIELD_TABLE_COLUMNS_HE
    text = json.dumps(rows, ensure_ascii=False)
    assert not he.has_raw_identifier(text)                             # no snake_case in the user-facing table
    for raw in ("battery_gross_kwh", "energy_consumption_kwh_100km", "foreign_market_only", "not_applicable"):
        assert raw not in text
    by_name = {r["שדה"]: r for r in rows}
    assert by_name["קיבולת סוללה ברוטו"]["מצב"] == "נמצא רק לשוק זר"
    assert by_name["צריכת חשמל"]["מצב"] == "לא נמצא"
    assert by_name["מומנט מרבי"]["מצב"] == "מידע סותר" and by_name["מומנט מרבי"]["אופן ההשלמה"] == "דורש הכרעה"
    assert by_name["נפח מיכל דלק"]["מצב"] == "לא רלוונטי"
    assert by_name["מומנט מרבי"]["קבוצה"] == "ביצועים וצריכה"
    assert by_name["מומנט מרבי"]["שווקים"] == "ישראל" and by_name["קיבולת סוללה ברוטו"]["מקור אחרון"] == "cadillac.com"
    assert he.op_label("waiting_search") == "ממתין לתור לחיפוש" and he.phase_label("document_sweep")
    # a regular hybrid does not plug in: its table (and coverage denominator) has no charging / electric-range rows
    hev = VehicleLive("2", "t")
    feed(hev, [started(propulsion="hybrid")])
    hev_names = {r["שדה"] for r in hev.field_rows()}
    assert len(hev_names) == 39 and "צריכת חשמל" not in hev_names and "קיבולת סוללה ברוטו" in hev_names


def test_unresolved_with_evidence_needs_followup_but_is_not_without_evidence():
    v = VehicleLive("1", "t")
    feed(v, [started(fields=["battery_gross_kwh", "energy_consumption_kwh_100km"]),
             evidence(2, "e1", "battery_gross_kwh", 102, "US", US)])
    p = v.progress()
    assert (p["total"], p["completed"], p["with_evidence"], p["needs_followup"], p["without_evidence"]) == \
        (2, 0, 1, 2, 1)                                     # foreign evidence: needs follow-up, NOT "no evidence"


def test_indirect_resolution_updates_progress_immediately_by_two():
    v = VehicleLive("1", "t")
    feed(v, [started(fields=["battery_gross_kwh", "energy_consumption_kwh_100km", "torque_nm"]),
             evidence(2, "e1", "battery_gross_kwh", 102, "US", US),
             {"kind": "field_recovery_started", "field": "battery_gross_kwh", "attempt": 1, "max_attempts": 2,
              "failure_reason": "foreign_market_only", "phase": "field_recovery"}])
    assert v.progress()["completed"] == 0
    card = v.card()
    assert card["field"] == "קיבולת סוללה ברוטו" and card["phase"] == "השלמת שדות שעדיין חסרים"
    assert card["why"].startswith("נמצא מידע על קיבולת סוללה ברוטו, אבל הוא עדיין לא אומת לשוק הישראלי.")
    feed(v, [evidence(5, "e2", "battery_gross_kwh", 102, "IL"),
             evidence(6, "e3", "energy_consumption_kwh_100km", 23.6, "IL"),
             {"kind": "field_recovery_queue_resolved_indirectly", "field": "energy_consumption_kwh_100km",
              "resolved_during_field": "battery_gross_kwh", "attempt": 1, "state": "ok"}])
    assert v.progress()["completed"] == 2                                   # +2 before any finalization
    rows = {r["שדה"]: r for r in v.field_rows()}
    assert rows["צריכת חשמל"]["אופן ההשלמה"] == "נפתר תוך טיפול בשדה אחר"
    assert rows["קיבולת סוללה ברוטו"]["אופן ההשלמה"] == "Recovery ישיר"
    assert rows["מומנט מרבי"]["אופן ההשלמה"] == "עדיין פתוח"


def test_recovery_budget_and_operational_why_per_phase():
    v = VehicleLive("1", "t")
    feed(v, [started(fields=["torque_nm"], budget=24)])
    for i in range(18):
        v.apply("model_response", {"phase": "field_recovery", "model": "glm-5.3-flash", "usage": {}})
    card = v.card()
    assert card["recovery"] == {"text": "18 / 24", "remaining": 6}
    v.apply("finalization_started", {"phase": "finalization", "model": "glm-5.3"})
    card = v.card()
    assert card["why"] == "שלב המחקר הסתיים. המודל מרכיב כעת תוצאה מובנית מהראיות שכבר נאספו."
    assert card["model"] == "GLM-5.3" and card["op"] == "מרכיב תוצאה סופית"
    unlimited = VehicleLive("2", "t")
    feed(unlimited, [started(fields=["torque_nm"], budget=0)])
    assert unlimited.card()["recovery"]["text"] == he.UNLIMITED_RECOVERY_HE


def test_provider_reasoning_is_shown_only_when_returned():
    v = VehicleLive("1", "t")
    feed(v, [started(fields=["torque_nm"])])
    v.apply("model_request_started", {"model": "glm-5.3-flash", "phase": "research"})
    assert v.card()["reasoning"] == "המודל חושב…" and v.card()["op"] == "המודל חושב…"
    v.apply("model_response", {"phase": "research", "model": "glm-5.3-flash", "tool_calls": [{"id": "x"}],
                               "reasoning_content": "Need the Israeli spec sheet first."})
    card = v.card()
    assert card["reasoning_title"] == "חשיבת המודל כפי שהוחזרה מהספק"
    assert card["reasoning"] == "Need the Israeli spec sheet first." and card["reasoning_available"]
    v.apply("model_request_started", {"model": "glm-5.3-flash", "phase": "research"})
    v.apply("model_response", {"phase": "research", "model": "glm-5.3-flash", "tool_calls": [{"id": "y"}]})
    card = v.card()
    assert card["reasoning"] == "הספק לא החזיר תוכן חשיבה עבור התור הזה." and not card["reasoning_available"]
    assert card["why"] != card["reasoning"]                    # orchestration "why" is never model reasoning


def test_live_states_from_real_request_lifecycle():
    """While one chat request blocks and holds the only glm-5.3 slot, the second vehicle shows
    'waiting for the model'; the first shows 'the model is thinking'. Same for Search-Prime."""
    controller = ConcurrencyController(model_limits={"glm-5.3": 1}, search_limit=1)
    gate = threading.Event()

    class GateSession(InflightSession):
        def post(self, url, headers=None, data=None, timeout=None):
            gate.wait(5)
            return super().post(url, headers=headers, data=data, timeout=timeout)

    session = GateSession(delay=0.0)
    a, b = VehicleLive("A", "a"), VehicleLive("B", "b")

    def run_chat(vehicle):
        c = glm_client(session, controller, model="glm-5.3")
        c.activity_hook = lambda kind, **d: vehicle.apply(kind, d)
        c.chat([{"role": "user", "content": "x"}])

    t1 = threading.Thread(target=run_chat, args=(a,))
    t1.start()
    time.sleep(0.2)
    t2 = threading.Thread(target=run_chat, args=(b,))
    t2.start()
    time.sleep(0.3)
    assert a.card()["op"] == "המודל חושב…" and b.card()["op"] == "ממתין לתור למודל"
    gate.set()
    t1.join(5), t2.join(5)

    gate.clear()
    sa, sb = VehicleLive("A", "a"), VehicleLive("B", "b")

    def run_search(vehicle):
        c = glm_client(session, controller)
        c.activity_hook = lambda kind, **d: vehicle.apply(kind, d)
        c.web_search("q")

    t1 = threading.Thread(target=run_search, args=(sa,))
    t1.start()
    time.sleep(0.2)
    t2 = threading.Thread(target=run_search, args=(sb,))
    t2.start()
    time.sleep(0.3)
    assert sa.card()["op"] == "מחפש ברשת" and sb.card()["op"] == "ממתין לתור לחיפוש"
    gate.set()
    t1.join(5), t2.join(5)


def test_dashboard_never_calls_the_api(tmp_path, make_ctx, monkeypatch):
    """Build the whole dashboard (header, cards, field tables, candidate matrix) from a finished run's events
    with every network entry point booby-trapped: zero chat, search or fetch calls."""
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)
    client = PhaseGLM([read_docs(ids[:3]), say({"summary": "primary", "fields": {}})])
    result, events, _ = run(tmp_path, ctx, client, requested_fields=["torque_nm", "wheelbase_mm"], primary_research_min_base_documents=0, primary_research_min_base_scoped_coverage=0,
                            field_recovery_max_attempts=1)
    calls_before = dict(client.calls)
    session_before = list(ctx.session.calls)        # the run's own requests (e.g. the importer site map)

    def forbidden(*args, **kwargs):
        raise AssertionError("the dashboard must not make API or network calls")

    for target, name in ((GLMClient, "chat"), (GLMClient, "web_search"), (GLMClient, "_post"),
                         (requests.Session, "post"), (requests.Session, "get"), (PhaseGLM, "chat")):
        monkeypatch.setattr(target, name, forbidden)
    state = BatchLiveState([("85095", "קאדילאק LYRIQ")], controller=ConcurrencyController())
    listener = state.listener_for("85095")
    for event in events:
        listener(event["kind"], event)
    state.drain()
    header = state.header()
    card = state.vehicles["85095"].card()
    rows = state.vehicles["85095"].field_rows()
    started_event = next(e for e in events if e["kind"] == "run_started")
    table = candidate_table_rows(events, started_event["requested_field_specs"])
    assert header["done"] == 1 and header["model_calls"] == sum(calls_before.values())
    assert card["status"] and rows and table
    assert client.calls == calls_before and ctx.session.calls == session_before


def test_a_finished_tool_action_is_not_shown_during_the_next_turn_or_phase():
    v = VehicleLive("1", "t")
    feed(v, [started(fields=["torque_nm"])])
    v.apply("tool_call", {"name": "store_evidence", "phase": "document_sweep",
                          "arguments": json.dumps({"field": "torque_nm", "value": 610})})
    assert v.card()["action"] == {"label": "שומר ראיה לשדה", "detail": "מומנט מרבי = 610"}
    v.apply("model_request_started", {"model": "glm-5.3-flash", "phase": "field_recovery"})
    assert v.card()["action"] is None
    v.apply("tool_call", {"name": "search_web", "arguments": json.dumps({"query": "lyriq torque"})})
    v.apply("finalization_started", {"phase": "finalization", "model": "glm-5.3"})
    assert v.card()["action"] is None
