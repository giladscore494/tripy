"""PR 3: verified fact reuse, negative research memory, historical yield and the training feedback dataset.
Offline only: policy / scripted models, fake HTTP, a fixed search index."""

import copy
import csv
import json
import threading
from pathlib import Path

import pytest

from conftest import FakeSession
from fixtures import corolla_family as family
from fixtures.corolla_touring import CARTUBE, PAYLOAD, TOYOTA_UK, TWO_LITRE, VEHICLE, put_documents
from test_tools_smoke import ScriptedGLM, _call

from src.agent import AgentConfig, run_vehicle
from src.document_binding import target_identity
from src.fields import load_schema, resolve_requested_fields
from src.research_memory import ResearchMemory, provider_routes, reuse_level, scope_key, spec_identity
from src.tools.search import search_provider
from src.storage.cache import DocumentCache
from src.storage.run_log import RunLog, read_events
from src.tools import ToolConfig
from src.training_feedback import export_feedback, feedback_examples, improvement_report

ROOT = Path(__file__).resolve().parent.parent
SPECS = {s["name"]: s for s in load_schema()}
HEV = resolve_requested_fields(None, propulsion="hybrid")


# --- schema policy -----------------------------------------------------------------------------------------------

def test_reuse_scope_is_schema_driven_and_never_commercial():
    for spec in SPECS.values():
        assert spec["reuse_scope"] in ("none", "exact_market_trim", "exact_technical_variant", "body_powertrain")
        if spec["recovery_cluster"] in ("commercial", "warranty", "equipment", "multimedia", "tires_wheels") \
                or spec.get("time_sensitive"):
            assert spec["reuse_scope"] == "none" and reuse_level(spec) is None, spec["name"]
    assert SPECS["cargo_volume_l"]["reuse_scope"] == "exact_technical_variant"
    assert SPECS["curb_weight_kg"]["reuse_scope"] == "exact_market_trim"
    assert reuse_level({**SPECS["fuel_tank_l"], "time_sensitive": True}) is None
    for module in ("src/research_memory.py", "src/training_feedback.py"):
        code = (ROOT / module).read_text("utf-8")
        assert not [n for n in SPECS if f'"{n}"' in code], module          # no field lists in Python


def test_scope_keys_need_every_identity_part():
    identity = target_identity(PAYLOAD, VEHICLE)
    assert scope_key(identity, "exact_technical_variant").startswith("exact_technical_variant:")
    two_litre = copy.deepcopy(PAYLOAD)
    two_litre["engine_drivetrain"].update({"engine_cc": 1987, "power_hp": 152})
    other = target_identity(two_litre, VEHICLE)
    assert scope_key(other, "exact_technical_variant") != scope_key(identity, "exact_technical_variant")
    assert scope_key(other, "body_powertrain") == scope_key(identity, "body_powertrain")
    unknown = copy.deepcopy(PAYLOAD)
    unknown["engine_drivetrain"]["power_hp"] = None                       # a missing part would collide
    assert scope_key(target_identity(unknown, {**VEHICLE}), "exact_technical_variant") is None
    bev = copy.deepcopy(PAYLOAD)
    bev["engine_drivetrain"].update({"engine_cc": None, "propulsion_normalized": "battery_electric"})
    assert scope_key(target_identity(bev, {**VEHICLE, "propulsion": "battery_electric"}),
                     "exact_technical_variant") is not None


# --- verified facts ------------------------------------------------------------------------------------------------

def _evidence(**kw):
    base = {"evidence_id": "e1", "field": "fuel_tank_l", "value": 43, "unit": "l", "document_id": "d1",
            "quote": "Fuel tank capacity 43 l", "market": "UK", "variant_match": "exact",
            "binding_level": "exact_technical_variant", "source_authority": "official_manufacturer",
            "admission_status": "accepted"}
    return {**base, **kw}


def ok(*fields, ids=("e1", "e2", "e3", "e4", "e5", "e6", "e9")):
    """A final evaluation where these fields ended ok with these evidence ids."""
    return [{"field": f, "state": "ok", "evidence_ids": list(ids), "conflict_evidence_ids": []} for f in fields]


def test_only_admitted_exactly_bound_policy_facts_are_recorded_once(tmp_path):
    memory = ResearchMemory(tmp_path)
    identity = target_identity(PAYLOAD, VEHICLE)
    evidence = [_evidence(), _evidence(evidence_id="e2", variant_match="unclear"),
                _evidence(evidence_id="e3", field="list_price", value=179990, unit="ILS", quote="price 179,990"),
                _evidence(evidence_id="e4", admission_status=None), _evidence(evidence_id="e5", reused_from={"x": 1}),
                _evidence(evidence_id="e6", binding_level="body_powertrain")]
    written = memory.record_facts(evidence, HEV, identity, {"record_id": "38626"}, ok("fuel_tank_l", "list_price"))
    assert [r["origin"]["evidence_id"] for r in written] == ["e1"]
    # the same fact from the same source recorded again (another run / variant) is still ONE record = one source
    assert memory.record_facts([_evidence(evidence_id="e9")], HEV, identity, {"record_id": "99999"},
                               ok("fuel_tank_l")) == []
    records, skipped = memory.reusable_facts(HEV, identity)
    assert len(records) == 1 and records[0]["origin"]["record_id"] == "38626" and skipped == {}


def test_stale_expired_and_colliding_records_are_never_reused(tmp_path):
    memory = ResearchMemory(tmp_path)
    identity = target_identity(PAYLOAD, VEHICLE)
    memory.record_facts([_evidence()], HEV, identity, {}, ok("fuel_tank_l"))
    changed = [{**s, "semantic_definition": "a different meaning"} if s["name"] == "fuel_tank_l" else s for s in HEV]
    assert spec_identity(changed[[s["name"] for s in HEV].index("fuel_tank_l")]) != spec_identity(SPECS["fuel_tank_l"])
    assert memory.reusable_facts(changed, identity) == ([], {"stale_schema_or_gate_version": 1})
    path = next((tmp_path / "facts").rglob("*.json"))
    record = json.loads(path.read_text("utf-8"))
    path.write_text(json.dumps({**record, "recorded_at": "2020-01-01T00:00:00+00:00"}), "utf-8")
    assert memory.reusable_facts(HEV, identity) == ([], {"expired": 1})
    path.write_text(json.dumps({**record, "scope_key": "exact_technical_variant:[other]"}), "utf-8")
    assert memory.reusable_facts(HEV, identity) == ([], {"scope_key_mismatch": 1})


def test_a_colliding_record_from_another_powertrain_fails_readmission(tmp_path, make_ctx):
    """Even a record filed under the 1.8 key that cites the 2.0 review is re-admitted against the target: the source
    binds to another variant, so nothing crosses."""
    ctx = make_ctx()
    docs = put_documents(ctx.cache)
    memory = ResearchMemory(ctx.cache.root / "memory")
    identity = target_identity(PAYLOAD, VEHICLE)
    rogue = _evidence(field="cargo_volume_l", value=581, document_id=docs[TWO_LITRE], quote="Boot space: 581 litres",
                      market="unknown")
    memory.record_facts([rogue], HEV, identity, {"record_id": "forged"}, ok("cargo_volume_l"))
    client = ScriptedGLM([{"role": "assistant", "content": json.dumps({"summary": "p", "fields": {}})},
                          {"role": "assistant", "content": json.dumps({"summary": "f", "fields": {}})}])
    result = run_vehicle({"upstream_record_id": "38626"}, PAYLOAD, client=client, cache=ctx.cache,
                         run_log=RunLog(tmp_path / "runs", "b", "38626"), vehicle_meta=VEHICLE,
                         config=AgentConfig(max_steps=1, field_recovery_enabled=False, layered_harvest_enabled=False),
                         tool_config=ToolConfig(), session=FakeSession({}))
    assert result["fact_reuse"]["reused"] == 0 and result["evidence"] == []
    assert sum(result["fact_reuse"]["not_readmitted"].values()) == 1


# --- the cold / warm family benchmark --------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def corolla_family(tmp_path_factory):
    return family.benchmark(tmp_path_factory.mktemp("family"))


def test_warm_related_variant_reuses_only_what_its_scope_allows(corolla_family):
    a, a2, cold = corolla_family["A"], corolla_family["A2"], corolla_family["A2_no_memory"]
    assert a["verified_fact_cache_hits"] == 0                                    # cold
    assert {"fuel_tank_l", "battery_gross_kwh", "torque_nm", "top_speed_kmh"} <= set(a2["reused_fields"])
    assert not {"list_price", "warranty_years", "height_mm", "curb_weight_kg"} & set(a2["reused_fields"])   # trim/commercial
    assert "cargo_volume_l" not in a2["reused_fields"]          # A left it foreign_market_only: never handed on as settled
    assert a2["values"]["list_price"] == [189990]                              # never A's 179,990-183,990
    assert a2["model_calls"] < cold["model_calls"] and a2["tail_model_calls"] < cold["tail_model_calls"]
    assert a2["fields_ok"] >= cold["fields_ok"]


def test_a_different_powertrain_inherits_nothing(corolla_family):
    b = corolla_family["B"]
    assert b["reused_fields"] == [] and b["verified_fact_cache_hits"] == 0
    assert b["values"]["cargo_volume_l"] == [581] and 596 not in b["values"].get("cargo_volume_l", [])


def test_a_rerun_uses_negative_route_memory_without_changing_truth(corolla_family):
    a, rerun = corolla_family["A"], corolla_family["A_rerun"]
    assert rerun["negative_route_fields_shown"] >= 1 and rerun["search_calls"] < a["search_calls"]
    assert rerun["states"]["ground_clearance_mm"] == a["states"]["ground_clearance_mm"] != "not_applicable"
    # reuse is not corroboration: one source stays one evidence item
    assert rerun["values"]["fuel_tank_l"] == [43]


def test_reused_evidence_keeps_its_source_and_is_counted_once(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    family.run_variant("A", tmp_path / "A", cache)
    run = family.run_variant("A2", tmp_path / "A2", cache)
    reused = [e for e in run["result"]["evidence"] if e.get("reused_from")]
    assert reused and all(e["document_id"] and e["source_url"] and e["admission_status"] == "accepted"
                          for e in reused)
    tank = [e for e in run["result"]["evidence"] if e["field"] == "fuel_tank_l"]
    assert len(tank) == 1 and tank[0]["market"] == "EU"                     # source market kept, one item
    m = run["metrics"]
    assert m["verified_fact_cache_hits"] == len(reused)
    events = run["events"]
    assert all(e.get("phase") == "fact_reuse" for e in events if e.get("kind") == "evidence"
               and (e.get("evidence") or {}).get("reused_from"))


# --- negative memory and yield are scheduling only -------------------------------------------------------------

PROVIDER = ("glm", "fake")       # the search provider of _session (backend, engine): see src/tools/search.search_provider


def _dead(field, *routes):
    """Negative memory exactly as recovery records it: the provider routes of the failed calls."""
    recorded = [r for tool, route in routes
                for r in provider_routes(tool, {"query" if tool.startswith("search") else "url": route},
                                         provider=PROVIDER)]
    return {field: {"routes": recorded, "attempts": 1}}


def _session(tmp_path, make_ctx, negative, open_fields):
    from src.agent import ToolSession, _route_guard

    ctx = make_ctx()
    ctx.glm = _Searches()
    session = ToolSession(ctx, RunLog(tmp_path, "b", "1"), AgentConfig())
    session.route_guard = _route_guard(negative, open_fields, SPECS, list(SPECS.values()),
                                       provider=search_provider(ctx))
    assert search_provider(ctx) == PROVIDER
    return session, ctx


class _Searches:
    model = "glm-test"
    settings = type("S", (), {"search_engine": "fake"})()

    def __init__(self):
        self.calls = []

    def web_search(self, query, count=8, domain=None):
        self.calls.append(query)
        return []


def _run(session, name, args):
    messages = []
    session.execute([_call("c", name, args)], messages, phase="field_recovery")
    return json.loads(messages[-1]["content"].split("\n[operational note]")[0])


def test_an_equivalent_failed_query_is_refused_without_execution(tmp_path, make_ctx):
    query = "Toyota Corolla Touring Sports 2024 ground clearance"
    session, ctx = _session(tmp_path, make_ctx, _dead("ground_clearance_mm", ("search_web", query)),
                            ["ground_clearance_mm"])
    result = _run(session, "search_web", {"query": "  toyota corolla TOURING sports 2024   GROUND clearance "})
    assert result["error"] == "known_unproductive_route" and ctx.glm.calls == []      # not executed, not billed
    assert session.route_blocks == 1


def test_an_equivalent_failed_url_is_refused(tmp_path, make_ctx):
    url = "https://www.example-forum.net/threads/corolla-ground-clearance"
    session, ctx = _session(tmp_path, make_ctx, _dead("ground_clearance_mm", ("fetch_url", url)),
                            ["ground_clearance_mm"])
    for variant in ("http://example-forum.net/threads/corolla-ground-clearance/",
                    "https://www.example-forum.net/threads/corolla-ground-clearance#replies"):
        assert _run(session, "fetch_url", {"url": variant})["error"] == "known_unproductive_route"
    assert session.ctx.session.calls == []
    # a browser render is another mechanism (it can expose JavaScript content the fetch did not): never refused
    assert _run(session, "render_page", {"url": url}).get("error") != "known_unproductive_route"
    assert session.route_blocks == 2


def test_a_new_route_still_runs_after_other_routes_failed(tmp_path, make_ctx):
    dead = _dead("ground_clearance_mm", ("search_web", "corolla ground clearance mm"),
                 ("search_web", "corolla touring ground clearance"))
    session, ctx = _session(tmp_path, make_ctx, dead, ["ground_clearance_mm"])
    result = _run(session, "search_web", {"query": "corolla 2024 ground clearance specification"})   # query C
    assert "error" not in result and ctx.glm.calls == ["corolla 2024 ground clearance specification"]
    assert session.route_blocks == 0


def test_a_route_is_refused_only_for_the_fields_it_failed_for(tmp_path, make_ctx):
    url = "https://www.example-forum.net/threads/corolla"
    dead = _dead("ground_clearance_mm", ("search_web", "corolla ground clearance"), ("fetch_url", url))
    open_fields = ["ground_clearance_mm", "curb_weight_kg"]
    session, ctx = _session(tmp_path, make_ctx, dead, open_fields)
    # the search names ground clearance (the field it failed for): refused
    assert _run(session, "search_web", {"query": "corolla ground clearance"})["error"] == "known_unproductive_route"
    # the page was never tried for curb weight, which is also open: it runs
    assert _run(session, "fetch_url", {"url": url}).get("error") != "known_unproductive_route"
    assert session.route_blocks == 1


class _IgnoresHints(family.tail.PolicyGLM):
    """The benchmark's policy model, minus its habit of avoiding known unproductive routes: it repeats them, so the
    ENGINE has to refuse the repeats."""

    def recover(self, messages, tools):
        packet = json.loads(messages[1]["content"].split("\n", 1)[1])
        packet.pop("known_unproductive_routes", None)
        messages = [messages[0], {**messages[1], "content": "Cluster recovery task (JSON):\n" + json.dumps(packet)},
                    *messages[2:]]
        return super().recover(messages, tools)


def _family_run(workdir, cache, client_cls):
    v = family.variant("A")
    client = client_cls(primary=v["primary"], search_index=v["index"], engine=v["engine"])
    return family.tail.run_mode("cluster", workdir, client=client, payload=v["payload"], vehicle=v["vehicle"],
                                routes=family.ROUTES, record_id=v["record_id"], cache=cache, batch="family",
                                requested_fields=family.FIELDS, research_memory_enabled=True)


def test_negative_memory_blocks_routes_never_research_and_never_truth(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    memory = ResearchMemory(cache.root / "memory")
    v = family.variant("A")
    identity = target_identity(v["payload"], v["vehicle"])
    specs = {s["name"]: s for s in resolve_requested_fields(family.FIELDS, propulsion="hybrid")}

    def dead(field, query, run):     # an earlier run's failed search, exactly as recovery records it
        spec = specs[field]
        memory.record_routes([{"scope_key": scope_key(identity, reuse_level(spec) or "exact_market_trim"),
                               "spec_identity": spec_identity(spec), "cluster": "technical_spec", "field": field,
                               "outcome": "no_new_material",
                               "routes": provider_routes("search_web", {"query": query},
                                                         provider=("glm", "fake-index"))}], run)

    dead("ground_clearance_mm", "Toyota Corolla Touring Sports 2024 1.8 hybrid ground clearance", "earlier-1")
    dead("ground_clearance_mm", "corolla minimum ground clearance", "earlier-2")   # a second, different route
    run = _family_run(tmp_path / "run", cache, _IgnoresHints)
    events = run["events"]
    blocked = [e for e in events if e.get("kind") == "tool_blocked" and e.get("reason") == "known_unproductive_route"]
    assert [json.loads(e["arguments"])["query"] for e in blocked] == [
        "Toyota Corolla Touring Sports 2024 1.8 hybrid ground clearance"]
    assert run["result"]["field_recovery"]["negative_route_cache_hits"] == 1
    # research was NOT skipped: the same attempt went on with another route after the refusal
    later = [e for e in events if e.get("kind") == "tool_call" and e["seq"] > blocked[0]["seq"]
             and e.get("name") in ("search_web", "fetch_url")]
    assert later
    assert not [e for e in events if e.get("kind") == "cluster_recovery_skipped"
                and e.get("reason") == "negative_route_memory"]
    # truth is untouched: every evidence item came from an admitted store request or a re-admitted fact, and no field
    # was declared absent because of memory
    stores = {e["seq"] for e in events if e.get("kind") == "tool_call" and e.get("name") == "store_evidence"}
    for e in events:
        if e.get("kind") == "evidence":
            assert e.get("phase") == "fact_reuse" or any(s < e["seq"] for s in stores)
    states = {f: s["state"] for f, s in run["result"]["research_bundle"]["field_states"].items()}
    assert "not_applicable" not in {states["curb_weight_kg"], states["ground_clearance_mm"]}
    assert "research_memory" not in (ROOT / "src/field_recovery.py").read_text("utf-8")  # evaluator never reads it


def test_negative_memory_is_not_kept_for_time_sensitive_fields_and_expires(tmp_path):
    from src.agent import run_cluster_recovery  # noqa: F401  (the scope keys come from the same helpers)

    memory = ResearchMemory(tmp_path, route_max_age_days=30)
    entry = {"scope_key": "k", "field": "list_price", "outcome": "no_new_material",
             "routes": [{"tool": "search_web", "route": "corolla price", "signature": "s"}]}
    memory.record_routes([entry], "old-run")
    path = next((tmp_path / "routes").glob("*.json"))
    data = json.loads(path.read_text("utf-8"))
    path.write_text(json.dumps({**data, "recorded_at": "2020-01-01T00:00:00+00:00"}), "utf-8")
    assert memory.negative_routes({"list_price": "k"}) == {}                     # expired
    assert SPECS["list_price"].get("time_sensitive")                             # and never looked up (route_keys)
    agent_code = (ROOT / "src/agent.py").read_text("utf-8")
    assert 'if not by_name[f].get("time_sensitive")' in agent_code


def test_historical_yield_needs_enough_samples(tmp_path):
    memory = ResearchMemory(tmp_path)
    rows = [{"kind": "cluster", "manufacturer": "M", "propulsion": "hybrid", "cluster": "performance", "turns": 2,
             "resolutions": 2}]
    for i in range(4):
        memory.record_yield(rows, f"run{i}")
    assert memory.cluster_yield("M", "hybrid") == {}                          # 4 samples: not enough
    memory.record_yield(rows, "run4")
    assert memory.cluster_yield("M", "hybrid")["performance"]["resolutions_per_turn"] == 1.0
    assert memory.cluster_yield("M", "battery_electric") == {}


def test_concurrent_writers_never_corrupt_memory(tmp_path):
    memory = ResearchMemory(tmp_path)
    identity = target_identity(PAYLOAD, VEHICLE)
    errors = []

    def write(i):
        try:
            memory.record_facts([_evidence(evidence_id=f"e{i}")], HEV, identity, {"record_id": str(i)},
                                ok("fuel_tank_l", ids=[f"e{i}"]))
            memory.record_routes([{"scope_key": "k", "field": "f", "outcome": "no_new_material", "routes": []}], f"r{i}")
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    files = list((tmp_path / "facts").rglob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text("utf-8"))["field"] == "fuel_tank_l"
    assert len(list((tmp_path / "routes").glob("*.json"))) == 16
    assert not list(tmp_path.rglob("*.tmp"))


# --- training feedback -----------------------------------------------------------------------------------------

def corolla_feedback_run(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    docs = put_documents(cache)
    stores = [("cargo_volume_l", 581, docs[TWO_LITRE], "Boot space: 581 litres"),
              ("gearbox_type", "e-CVT", docs[CARTUBE], "תיבת הילוכים: e-CVT"),
              ("gear_count", 1, docs[CARTUBE], "תיבת הילוכים: e-CVT"),
              ("power_seats", True, docs[CARTUBE], "מושבים חשמליים: אין"),
              ("heated_seats", True, docs[CARTUBE], "חימום מושבים: אין"),
              ("sunroof_panoramic", True, docs[CARTUBE], "חלון גג: אין"),
              ("warranty_years", 3, docs[CARTUBE], "אחריות: שלוש שנים"),
              ("height_mm", 1460, docs[CARTUBE], 'גובה 146.0 ס"מ'),
              ("height_mm", 1435, docs[CARTUBE], 'גובה 143.5 ס"מ'),
              ("fuel_tank_l", 43, docs[TOYOTA_UK], "Fuel tank capacity 43 l"),
              ("length_mm", 4650, docs[CARTUBE], "Length 4650 mm")]        # quote not in the source
    calls = [_call(f"p{i}", "store_evidence", {"field": f, "value": v, "document_id": d, "quote": q, "market": "IL",
                                               "variant_match": "exact"}) for i, (f, v, d, q) in enumerate(stores)]
    client = ScriptedGLM([{"role": "assistant", "content": "", "tool_calls": calls},
                          {"role": "assistant", "content": json.dumps({"summary": "p", "fields": {}})},
                          {"role": "assistant", "content": json.dumps({"summary": "f", "fields": {}})}])
    log = RunLog(tmp_path / "runs", "b", "38626")
    result = run_vehicle({"upstream_record_id": "38626"}, PAYLOAD, client=client, cache=cache, run_log=log,
                         vehicle_meta=VEHICLE, config=AgentConfig(max_steps=2, no_new_research_turns=0,
                                                                  field_recovery_enabled=False),
                         tool_config=ToolConfig(), session=FakeSession({}))
    examples = [json.loads(line) for line in (log.dir / "training_feedback.jsonl").read_text("utf-8").splitlines()]
    return result, examples, log


def test_required_corolla_training_cases(tmp_path):
    result, examples, _ = corolla_feedback_run(tmp_path)
    by = {}
    for e in examples:
        by.setdefault(e["example_type"], []).append(e)
    fp = {(e["field"], e["quote"]) for e in by["candidate_false_positive"]}
    assert fp == {("power_seats", "מושבים חשמליים: אין"), ("heated_seats", "חימום מושבים: אין"),
                  ("sunroof_panoramic", "חלון גג: אין")}
    assert all(e["contradicting_value"] is False and e["candidate_value"] is True
               for e in by["candidate_false_positive"])
    assert [(e["field"], e["accepted_value"]) for e in by["deterministic_miss"]] == [("warranty_years", 3)]
    gear = next(e for e in by["evidence_admission_rejected"] if e["field"] == "gear_count")
    assert gear["reason_code"] == "unsupported_inference" and gear["rejected_value"] == 1
    cargo = by["variant_binding_rejected"][0]
    assert (cargo["field"], cargo["accepted_value"], cargo["variant_match"]) == ("cargo_volume_l", 581, "different")
    conflict = by["conflict_example"][0]
    assert conflict["field"] == "height_mm" and sorted(conflict["candidate_value"]) == [1435, 1460]
    assert conflict["reason_code"] == "internal_source_inconsistency"
    assert by["portability_accepted"][0]["field"] == "fuel_tank_l"
    keys = {"example_type", "record_id", "field", "quote", "document_id", "source_url", "schema_hash",
            "harvester_version", "timestamp", "feedback_version"}
    assert keys <= set(by["deterministic_miss"][0])
    assert result["training_feedback"]["examples"] == len(examples)


def test_feedback_never_mislabels(tmp_path):
    result, examples, log = corolla_feedback_run(tmp_path)
    # a quote problem is a rejection, never a false positive; candidates nobody tried to promote get no label
    length = [e for e in examples if e["field"] == "length_mm"]
    assert [e["example_type"] for e in length] == ["evidence_admission_rejected"]
    labelled = {(e["field"], e.get("document_id"), json.dumps(e.get("candidate_value")))
                for e in examples if e["example_type"].startswith("candidate_")}
    events = read_events(log.events_path)
    harvested = [c for e in events if e.get("kind") == "candidates_harvested" for c in e["candidates"]]
    unlabelled = [c for c in harvested if (c["field"], c["document_id"], json.dumps(c["value"])) not in labelled]
    assert unlabelled                                              # e.g. the climate / torque candidates
    for e in examples:                                             # every false positive has an explicit basis
        if e["example_type"] == "candidate_false_positive":
            assert "contradicting_value" in e or "other_field" in e["reason_code"] or "semantic" in e["reason_code"]


def test_feedback_export_is_deterministic_and_complete(tmp_path):
    corolla_feedback_run(tmp_path / "one")
    runs = tmp_path / "one" / "runs"
    # an older run without a feedback file is derived from its events
    old = runs / "b" / "38626"
    (old / "training_feedback.jsonl").rename(old / "kept.jsonl")
    first = export_feedback(runs, tmp_path / "out1")
    second = export_feedback(runs, tmp_path / "out2")
    a = (tmp_path / "out1" / "training_feedback.jsonl").read_text("utf-8")
    assert a == (tmp_path / "out2" / "training_feedback.jsonl").read_text("utf-8") and first["examples"] > 10
    assert a.splitlines() and len(a.splitlines()) == first["examples"] == second["examples"]
    rows = list(csv.DictReader((tmp_path / "out1" / "training_feedback.csv").open(encoding="utf-8")))
    assert len(rows) == first["examples"] and {"example_type", "field", "reason_code", "pattern"} <= set(rows[0])
    summary = json.loads((tmp_path / "out1" / "training_feedback_summary.json").read_text("utf-8"))
    misses = summary["metrics"]["deterministic_miss"]
    assert misses["by_field"] == {"warranty_years": 1} and misses["by_source_family"] == {"cartube": 1}
    assert summary["metrics"]["candidate_false_positive"]["total"] == 3


def test_concurrent_exports_never_expose_a_partial_file(tmp_path):
    corolla_feedback_run(tmp_path / "one")
    runs, out = tmp_path / "one" / "runs", tmp_path / "out"
    errors = []

    def export():
        try:
            export_feedback(runs, out)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=export) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and not list(out.glob(".*.tmp"))
    lines = (out / "training_feedback.jsonl").read_text("utf-8").splitlines()
    assert all(json.loads(line)["example_id"] for line in lines)


def test_improvement_report_groups_by_field_family_and_pattern():
    report = improvement_report([
        {"example_type": "deterministic_miss", "field": "warranty_years", "source_url": "https://www.cartube.co.il/x",
         "pattern": "אחריות: שלוש שנים"},
        {"example_type": "deterministic_miss", "field": "warranty_years", "source_domain": "cartube.co.il",
         "pattern": "אחריות: שלוש שנים"},
        {"example_type": "candidate_accepted", "field": "x"}])
    misses = report["metrics"]["deterministic_miss"]
    assert misses["total"] == 2 and misses["by_pattern"] == {"אחריות: שלוש שנים": 2}
    assert report["metrics"]["variant_binding_rejected"]["total"] == 0


def test_feedback_from_events_matches_the_live_file(tmp_path):
    result, examples, log = corolla_feedback_run(tmp_path)
    derived = feedback_examples(read_events(log.events_path), PAYLOAD)
    assert {e["example_id"] for e in derived} >= {e["example_id"] for e in examples
                                                 if not e["example_type"].startswith(("conflict", "portability"))}


# --- review regressions -----------------------------------------------------------------------------------------

def test_a_fact_its_own_run_left_unsettled_is_never_recorded(tmp_path):
    memory = ResearchMemory(tmp_path)
    identity = target_identity(PAYLOAD, VEHICLE)
    conflicting = [{"field": "fuel_tank_l", "state": "conflicting", "evidence_ids": ["e1", "e2"],
                    "conflict_evidence_ids": ["e1", "e2"]}]
    assert memory.record_facts([_evidence()], HEV, identity, {}, conflicting) == []
    unresolved = [{"field": "fuel_tank_l", "state": "unresolved", "evidence_ids": ["e1"]}]
    assert memory.record_facts([_evidence()], HEV, identity, {}, unresolved) == []
    assert memory.record_facts([_evidence()], HEV, identity, {}) == []          # no evaluation: nothing


def _identity(trim="BUSINESS EDI", code="ZWE211L DWXNBW", **engine):
    payload = copy.deepcopy(PAYLOAD)
    payload["identity"].update({"trim": trim, "model_code": code})
    payload["engine_drivetrain"].update(engine)
    return target_identity(payload, {**VEHICLE, "trim": trim, "model_code": code})


def test_trim_and_model_code_keep_scope_keys_apart():
    assert scope_key(_identity("GR SPORT"), "exact_market_trim") != scope_key(_identity("SPORT"), "exact_market_trim")
    assert scope_key(_identity("LIMITED 7 SEATS"), "exact_market_trim") != \
        scope_key(_identity("LIMITED 5 SEATS"), "exact_market_trim")
    assert scope_key(_identity("EDITION"), "exact_market_trim") is None         # only generic words: no scope
    # the model code separates variants the engine figures cannot (battery size, gearbox, driven axle)
    assert scope_key(_identity(code="AAA1"), "exact_technical_variant") != \
        scope_key(_identity(code="BBB2"), "exact_technical_variant")
    assert scope_key(_identity(code=""), "exact_technical_variant") is None


def test_miss_and_false_positive_labels_need_their_basis(tmp_path):
    from src.training_feedback import feedback_examples

    def ev(seq, kind, **data):
        return {"seq": seq, "kind": kind, "ts": "t", **data}

    item = {"evidence_id": "e1", "field": "warranty_years", "value": 3, "document_id": "d1", "quote": "אחריות: שלוש שנים",
            "admission_status": "accepted", "variant_match": "exact"}
    no_harvest = [ev(1, "evidence", evidence=item)]
    assert not [e for e in feedback_examples(no_harvest) if e["example_type"] == "deterministic_miss"]
    harvested = [ev(0, "candidates_harvested", document_id="d1", harvester_version="h", candidates=[])]
    other = {**item, "variant_match": "different", "model_variant_claim": "exact"}
    labels = [e["example_type"] for e in feedback_examples(harvested + [ev(1, "evidence", evidence=other)])]
    assert "deterministic_miss" not in labels and "variant_binding_rejected" in labels
    assert [e["example_type"] for e in feedback_examples(harvested + [ev(1, "evidence", evidence=item)])] == \
        ["deterministic_miss"]
    # a refusal of one quote is never pinned on a candidate read from another line
    cand = {"field": "heated_seats", "value": True, "document_id": "d2", "quote": "חימום מושבים: יש"}
    events = [ev(0, "candidates_harvested", document_id="d2", harvester_version="h", candidates=[cand]),
              ev(1, "evidence_rejected", field="heated_seats", value=True, document_id="d2",
                 reasons=["value_not_stated"], request={"quote": "Business: חימום מושבים: אין", "document_id": "d2"})]
    labels = [e["example_type"] for e in feedback_examples(events)]
    assert labels == ["evidence_admission_rejected"]


def _executed(name, args, **entry):
    """A session tool_calls entry as ToolSession.execute records it (with the provider routes it made)."""
    return {"name": name, "arguments": json.dumps(args), **entry,
            "routes": provider_routes(name, args, provider=PROVIDER)}


def test_failed_calls_and_other_fields_never_become_negative_routes():
    from src.agent import _route_names_field, _routes_of

    calls = [_executed("search_web", {"query": "corolla ground clearance"}, error="http_429"),
             _executed("fetch_url", {"url": "https://x"}, error="http_503"),
             _executed("search_web", {"query": "corolla ground clearance"}, error=None)]
    assert [r["route"] for r in _routes_of(calls)] == ["corolla ground clearance"]
    specs = list(SPECS.values())
    dc = {"tool": "search_web", "route": "lyriq dc charging time"}
    assert not _route_names_field(dc, SPECS["ac_charging_time"], specs)
    warranty = {"tool": "search_web", "route": "corolla battery warranty"}
    assert not _route_names_field(warranty, SPECS["battery_gross_kwh"], specs)


def test_malformed_memory_files_are_ignored(tmp_path):
    memory = ResearchMemory(tmp_path)
    (tmp_path / "routes").mkdir(parents=True)
    (tmp_path / "routes" / "a.json").write_text("null", "utf-8")
    (tmp_path / "routes" / "b.json").write_text(json.dumps({"recorded_at": "2999-01-01T00:00:00", "entries": []}))
    (tmp_path / "routes" / "c.json").write_text(json.dumps({"recorded_at": "2026-01-01T00:00:00",
                                                            "entries": [None, {"field": "f", "scope_key": "k",
                                                                               "outcome": "no_new_material",
                                                                               "routes": [{"route": "x"}]}]}))
    (tmp_path / "yield").mkdir()
    (tmp_path / "yield" / "y.json").write_text("[1, 2]", "utf-8")
    assert memory.negative_routes({"f": "k"}).get("f", {}).get("routes", []) == []
    assert memory.cluster_yield("M", "hybrid") == {}


def test_reused_facts_are_not_relabelled_as_accepted_candidates(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    family.run_variant("A", tmp_path / "A", cache)
    run = family.run_variant("A2", tmp_path / "A2", cache)
    reused = {e["field"] for e in run["result"]["evidence"] if e.get("reused_from")}
    examples = [json.loads(line) for line in (Path(run["result"]["documents_dir"]).parent
                                              / "training_feedback.jsonl").read_text("utf-8").splitlines()]
    assert reused and not [e for e in examples if e["example_type"] == "candidate_accepted" and e["field"] in reused]


# --- second-review regressions -------------------------------------------------------------------------------------

def test_a_later_run_that_disputes_a_reused_fact_stops_its_reuse(tmp_path):
    memory = ResearchMemory(tmp_path)
    identity = target_identity(PAYLOAD, VEHICLE)
    record = memory.record_facts([_evidence()], HEV, identity, {"record_id": "A"}, ok("fuel_tank_l"))[0]
    reused = _evidence(evidence_id="e1", reused_from={"record_id": record["record_id"],
                                                       "scope_type": "exact_technical_variant"})
    contradiction = _evidence(evidence_id="e2", value=40, document_id="d9", quote="Fuel tank capacity 40 l")
    disputed = [{"field": "fuel_tank_l", "state": "conflicting", "evidence_ids": ["e1", "e2"],
                 "conflict_evidence_ids": ["e1", "e2"]}]
    memory.record_facts([reused, contradiction], HEV, identity, {"record_id": "B"}, disputed)
    assert memory.reusable_facts(HEV, identity) == ([], {"disputed_by_a_later_run": 1})


def test_full_model_code_and_transmission_separate_variants():
    auto = _identity(code="ZWE211L-DWXNBW", automatic=1)
    manual = _identity(code="ZWE211L-DWMNBW", automatic=0)
    assert scope_key(auto, "exact_technical_variant") != scope_key(manual, "exact_technical_variant")
    assert scope_key(_identity(code="ZWE211L-DWXNBW", automatic=0), "exact_technical_variant") != \
        scope_key(auto, "exact_technical_variant")
    for short in ("GY", "205.040", "NSGHA"):                     # codes without a letter+digit token still key
        assert scope_key(_identity(code=short), "exact_technical_variant")


def test_failed_web_calls_are_never_unproductive_routes():
    from src.agent import _route_failed, _routes_of

    assert _route_failed({"status": 429, "document_id": "d"}) and _route_failed({"error": "x"})
    assert _route_failed({"results": [], "per_domain": {"a": {"error": "429"}, "b": {"error": "429"}}})
    assert not _route_failed({"results": [], "per_domain": {"a": {"result_count": 0}}})
    calls = [_executed("fetch_url", {"url": "https://x"}, route_failed=True),
             _executed("search_web", {"query": "q"}, route_failed=False)]
    assert [r["route"] for r in _routes_of(calls)] == ["q"]


def test_table_row_candidates_keep_their_labels():
    from src.training_feedback import _same_place

    assert _same_place("Fuel tank capacity | 43 l (1.8 Hybrid 140)", "Fuel tank capacity\n43 l")
    assert not _same_place("חימום מושבים: יש", "Business: חימום מושבים: אין")


def test_negative_route_attempts_count_independent_runs(tmp_path):
    memory = ResearchMemory(tmp_path)
    entry = {"scope_key": "k", "field": "f", "outcome": "no_new_material", "routes": [{"route": "q", "signature": "s"}]}
    memory.record_routes([entry, dict(entry)], "one-run")
    assert memory.negative_routes({"f": "k"})["f"]["attempts"] == 1
    memory.record_routes([entry], "another-run")
    assert memory.negative_routes({"f": "k"})["f"]["attempts"] == 2
