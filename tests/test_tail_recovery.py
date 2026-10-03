"""PR 2: clustered tail recovery, conflict normalization, market portability and adaptive budgets.
Offline only: scripted / policy models, fake HTTP, a fixed search index."""

import json
from pathlib import Path

import pytest

from fixtures import corolla_tail as tail
from fixtures.corolla_tail import FIELDS, PolicyGLM, run_mode, summarize
from test_tools_smoke import _call

from src.agent import CLUSTER_RECOVERY_SYSTEM_PROMPT, AgentConfig, SearchBudget, ToolSession, run_vehicle
from src.benchmark import compute_metrics
from src.conflict_normalizer import classify_conflict, resolver_packet
from src.field_recovery import evaluate_field, is_target_market
from src.fields import load_schema
from src.market_portability import assess
from src.storage import trace
from src.storage.run_log import RunLog, read_events
from src.tail_planner import (cluster_of, novelty, plan_clusters, search_hints, snapshot, source_yield_score,
                              triage)
from src.tools import ToolConfig
from src.typed_values import typed_value

# these tests encode the cluster recovery agent (RECOVERY_MODE=cluster, the pre-#31 default) and its model call order
pytestmark = [pytest.mark.recovery_mode("cluster"), pytest.mark.grounded_candidates(False)]

SPECS = {s["name"]: s for s in load_schema()}
CLUSTERS = {"technical_spec", "performance", "charging_ev", "equipment", "multimedia", "tires_wheels", "commercial",
            "warranty"}


def item(eid, field, value, unit=None, **kw):
    spec = SPECS[field]
    unit = unit or spec.get("normalized_unit")
    return {"evidence_id": eid, "field": field, "value": value, "unit": unit, "market": kw.pop("market", "IL"),
            "variant_match": kw.pop("variant_match", "exact"),
            "binding_level": kw.pop("binding_level", "exact_technical_variant"),
            "typed_value": typed_value(value, unit=unit, value_type=spec.get("value_type"), matcher=spec.get("matcher")),
            **kw}


def evaluate(field, evidence, declared=None):
    spec = {**SPECS[field], "applicable": True}
    port = assess(spec, evidence, "IL", is_target_market)
    return evaluate_field(spec, evidence, declared, None, "IL", portability=port)


# --- schema --------------------------------------------------------------------------------------------------

def test_every_field_has_a_recovery_cluster_and_a_market_policy():
    clusters: dict[str, list[str]] = {}
    for spec in SPECS.values():
        assert spec["recovery_cluster"] in CLUSTERS
        assert spec["market_sensitivity"] in ("high", "medium", "low")
        assert spec["portability_scope"] in ("none", "exact_technical_variant")
        assert spec["portability_scope"] == "none" or spec["market_sensitivity"] == "low"
        clusters.setdefault(spec["recovery_cluster"], []).append(spec["name"])
    assert all(len(v) >= 3 for v in clusters.values())                 # no micro-clusters
    assert {"battery_gross_kwh", "height_mm", "ground_clearance_mm", "fuel_tank_l", "curb_weight_kg"} <= set(
        clusters["technical_spec"])
    for name in ("list_price", "registration_licence_fee", "vehicle_warranty", "warranty_km", "local_trim_name",
                 "heated_seats", "screen_size_in"):
        assert SPECS[name]["market_sensitivity"] == "high"               # price, fees, warranty, trim equipment
    for name in ("height_mm", "ground_clearance_mm", "curb_weight_kg", "cargo_volume_l", "width_mm"):
        assert SPECS[name]["portability_scope"] == "none"                # not every dimension is portable
    assert SPECS["fuel_tank_l"]["portability_scope"] == "exact_technical_variant"


# --- conflict normalizer ---------------------------------------------------------------------------------------

def test_price_scalar_inside_range_is_normalized_only_when_the_scalar_is_trim_bound():
    scalar = item("e1", "list_price", 179990, binding_level="exact_market_trim", document_id="d1")
    rng = item("e2", "list_price", "179990-183990", binding_level="exact_market_trim", document_id="d2")
    result = evaluate("list_price", [scalar, rng])
    assert result["state"] == "ok" and "conflict_normalized:scalar_inside_range" in result["info"]
    unclear = {**scalar, "binding_level": "exact_technical_variant"}
    result = evaluate("list_price", [unclear, rng])
    assert result["state"] == "conflicting" and result["conflict_class"]["class"] == "scalar_inside_range"
    # a scalar OUTSIDE the range is never "inside" (not the naive 179990 vs 183990 equivalence either)
    other = item("e3", "list_price", 183990, binding_level="exact_market_trim", document_id="d3")
    assert evaluate("list_price", [scalar, other])["state"] == "conflicting"


def test_height_case_is_an_internal_source_inconsistency_with_a_compact_resolver_packet():
    a = item("e1", "height_mm", 1460, document_id="d1", source_domain="cartube.co.il", quote='גובה 146.0 ס"מ',
             source_authority="aggregator")
    b = item("e2", "height_mm", 1435, document_id="d1", source_domain="cartube.co.il", quote='גובה 143.5 ס"מ',
             source_authority="aggregator", note="1435 is from European Auto-Data")
    result = evaluate("height_mm", [a, b])
    assert result["state"] == "conflicting"
    cls = result["conflict_class"]
    assert cls["class"] == "internal_source_inconsistency" and not cls["normalized"]
    packet = resolver_packet(spec=SPECS["height_mm"], items=[a, b], classification=cls,
                             identity={"model": "COROLLA"}, prior_queries=["corolla height"] * 10)
    text = json.dumps(packet, ensure_ascii=False)
    assert len(text) < 2500
    assert [e["quote"] for e in packet["competing_evidence"]] == ['גובה 146.0 ס"מ', 'גובה 143.5 ס"מ']
    assert {e["source_domain"] for e in packet["competing_evidence"]} == {"cartube.co.il"}
    assert "Auto-Data" not in text                                      # a model note is never provenance
    assert packet["field_semantics"] and len(packet["prior_queries"]) <= 6
    assert set(packet) == {"vehicle_identity", "field", "field_semantics", "conflict_class", "conflict_detail",
                           "competing_evidence", "prior_queries", "task"}


def test_unit_equivalent_values_are_not_a_conflict():
    mm = item("e1", "height_mm", 1460, document_id="d1")
    cm = item("e2", "height_mm", "146 cm", unit="cm", document_id="d2")
    cm["typed_value"] = typed_value("146", unit="cm", value_type="number", matcher="numeric")
    result = evaluate("height_mm", [mm, cm])
    assert result["state"] == "ok" and "conflict_normalized:unit_equivalent" in result["info"]


def test_no_majority_vote_and_the_remaining_classes():
    three = [item(f"e{i}", "height_mm", 1460, document_id=f"d{i}", source_domain=f"s{i}.example") for i in range(3)]
    one = item("e9", "height_mm", 1435, document_id="d9", source_domain="s9.example")
    result = evaluate("height_mm", three + [one])
    assert result["state"] == "conflicting" and result["conflict_class"]["class"] == "true_conflict"   # 3 vs 1: no vote
    spec = SPECS["height_mm"]
    scoped = [item("a", "height_mm", 1460, document_id="x", binding_level="exact_market_trim"),
              item("b", "height_mm", 1435, document_id="y", binding_level="body_powertrain")]
    assert classify_conflict(spec, scoped)["class"] == "variant_scope_difference"
    markets = [item("a", "height_mm", 1460, document_id="x", market="IL"),
               item("b", "height_mm", 1435, document_id="y", market="UK")]
    assert classify_conflict(spec, markets)["class"] == "market_difference"


# --- market portability ----------------------------------------------------------------------------------------

def uk_tank(eid="u1", value=43, **kw):
    return item(eid, "fuel_tank_l", value, market=kw.pop("market", "UK"), document_id=kw.pop("document_id", "uk"),
                source_authority=kw.pop("source_authority", "official_manufacturer"), **kw)


def test_official_foreign_fact_is_portable_and_keeps_its_market():
    evidence = [uk_tank("u1"), uk_tank("u2", document_id="uk2"),
                uk_tank("eu", market="EU", source_authority="aggregator", document_id="eu")]
    result = evaluate("fuel_tank_l", evidence)
    assert result["state"] == "ok" and set(result["portable_evidence_ids"]) == {"u1", "u2", "eu"}
    verdict = result["portability"]["u1"]
    assert verdict["portable_to_target_market"] is True and verdict["portability_policy"] == "exact_technical_variant/low"
    assert "official_manufacturer" in verdict["portability_basis"]
    assert [e["market"] for e in evidence] == ["UK", "UK", "EU"]          # the source market is never rewritten


def test_portability_veto_and_policy_limits():
    il = item("il", "fuel_tank_l", 45, market="IL", document_id="il", source_authority="official_importer")
    vetoed = evaluate("fuel_tank_l", [uk_tank(), il])
    assert vetoed["portability"]["u1"]["portable_to_target_market"] is False
    assert vetoed["portability"]["u1"]["portability_basis"] == "vetoed_by_target_market_evidence:il"
    assert vetoed["state"] == "ok" and vetoed["values"] == [43, 45] and vetoed["portable_evidence_ids"] == []
    # high market sensitivity (price), a non-portable dimension (height), no official source, foreign disagreement,
    # and a binding below the policy's level are all refused
    price = item("p", "list_price", 30000, market="UK", source_authority="official_manufacturer", document_id="p")
    assert evaluate("list_price", [price])["state"] == "foreign_market_only"
    height = item("h", "height_mm", 1460, market="UK", source_authority="official_manufacturer", document_id="h")
    assert evaluate("height_mm", [height])["state"] == "foreign_market_only"
    agg = uk_tank(source_authority="aggregator")
    assert evaluate("fuel_tank_l", [agg])["portability"]["u1"]["portability_basis"] == "no_official_source"
    disagree = evaluate("fuel_tank_l", [uk_tank("u1", 43), uk_tank("de", 45, market="DE", document_id="de")])
    assert disagree["state"] == "foreign_market_only" and disagree["portable_evidence_ids"] == []
    low = uk_tank(binding_level="body_powertrain")
    assert evaluate("fuel_tank_l", [low])["portability"]["u1"]["portability_basis"] == "binding_below_portability_scope"


# --- portability never reopens the "found" bypass (Reliability Foundation: server scope is authoritative) ---------

FOUND = {"status": "found", "seq": 99, "evidence_ids": ["u1"]}


def evaluate_declared(field, evidence, declared):
    spec = {**SPECS[field], "applicable": True}
    return evaluate_field(spec, evidence, declared, None, "IL", 1, portability=assess(spec, evidence, "IL",
                                                                                         is_target_market))


def test_found_cannot_make_non_portable_foreign_evidence_target_usable():
    height = item("u1", "height_mm", 1460, market="UK", source_authority="official_manufacturer", document_id="h")
    result = evaluate_declared("height_mm", [height], FOUND)          # exact UK binding, policy: not portable
    assert result["state"] == "foreign_market_only" and result["portable_evidence_ids"] == []
    assert "declaration_outside_server_scope:found" in result["info"]
    agg = uk_tank(source_authority="aggregator")                     # portable field, but no official source
    assert evaluate_declared("fuel_tank_l", [agg], FOUND)["state"] == "foreign_market_only"
    unclear = uk_tank(variant_match="unclear")                       # portable field, binding not exact
    assert evaluate_declared("fuel_tank_l", [unclear], FOUND)["state"] == "foreign_market_only"
    other = uk_tank(variant_match="different")
    assert evaluate_declared("fuel_tank_l", [other], FOUND)["state"] == "variant_not_exact"


def test_found_with_policy_accepted_foreign_fact_is_ok_and_keeps_its_market():
    evidence = [uk_tank("u1")]
    result = evaluate_declared("fuel_tank_l", evidence, FOUND)
    assert result["state"] == "ok" and result["portable_evidence_ids"] == ["u1"]
    assert evidence[0]["market"] == "UK" and result["markets"] == ["UK"]          # never rewritten as IL


def test_target_market_contradiction_vetoes_portability_without_any_vote():
    # three official foreign items agreeing on 43 do not outvote ONE target-market item stating 45
    foreign = [uk_tank("u1"), uk_tank("u2", document_id="uk2"), uk_tank("u3", market="DE", document_id="de")]
    il = item("il", "fuel_tank_l", 45, market="IL", document_id="il", source_authority="aggregator")
    result = evaluate_declared("fuel_tank_l", foreign + [il], FOUND)
    assert result["portable_evidence_ids"] == [] and result["state"] == "ok" and 45 in result["values"]
    assert all(v["portability_basis"] == "vetoed_by_target_market_evidence:il"
               for v in result["portability"].values())
    # and a conflict_resolved citing a vetoed foreign item resolves nothing between two IL values
    il2 = item("il2", "fuel_tank_l", 50, market="IL", document_id="il2")
    spec = {**SPECS["fuel_tank_l"], "applicable": True}
    ev = foreign + [il, il2]
    seq = {e["evidence_id"]: n for n, e in enumerate(ev, 1)}
    res = evaluate_field(spec, ev, {"status": "conflict_resolved", "seq": 99, "evidence_ids": ["u1"]}, None, "IL",
                         len(ev), evidence_seq=seq, portability=assess(spec, ev, "IL", is_target_market))
    assert res["state"] == "conflicting" and "conflict_resolution_not_evidence_backed" in res["info"]


def test_normalization_never_chooses_between_real_conflicts():
    a = item("a", "height_mm", 1460, document_id="x", source_domain="a.example", binding_level="exact_market_trim")
    b = item("b", "height_mm", 1435, document_id="y", source_domain="b.example", binding_level="exact_market_trim")
    many = [dict(a, evidence_id=f"a{i}", document_id=f"x{i}", source_domain=f"s{i}.example") for i in range(4)]
    assert evaluate("height_mm", many + [b], FOUND)["state"] == "conflicting"    # 4 vs 1 and a "found": no vote


# --- planner primitives ------------------------------------------------------------------------------------------

def test_triage_categories_are_scheduling_only():
    specs = [{**SPECS[n], "applicable": True} for n in ("length_mm", "height_mm", "fuel_tank_l", "wheelbase_mm",
                                                         "ground_clearance_mm")]
    specs[4]["recovery_attempts"] = 0
    evaluation = [{"field": "length_mm", "state": "missing", "retry_eligible": True},
                  {"field": "height_mm", "state": "conflicting", "retry_eligible": True},
                  {"field": "fuel_tank_l", "state": "foreign_market_only", "retry_eligible": True},
                  {"field": "wheelbase_mm", "state": "missing", "retry_eligible": True},
                  {"field": "ground_clearance_mm", "state": "missing", "retry_eligible": True}]
    matrix = {"fields": {"length_mm": [{"field": "length_mm", "value": 4650, "document_id": "d1"}]}}
    result = triage(evaluation, specs, matrix, [], 2, low_yield=["wheelbase_mm"])
    assert {k: v["triage"] for k, v in result.items()} == {
        "length_mm": "candidate_rich_local", "height_mm": "conflicting", "fuel_tank_l": "foreign_only",
        "wheelbase_mm": "low_yield", "ground_clearance_mm": "policy_blocked"}
    plan = plan_clusters(result, specs)
    assert plan == [{"cluster": "technical_spec", "fields": ["length_mm", "height_mm", "fuel_tank_l", "wheelbase_mm"],
                     "local_material": True}]
    # a candidate a model was already shown is not fresh local material
    shown = [{"kind": "document_sweep_started", "presented_candidate_keys": ["length_mm|d1|num:4650.0"]}]
    assert triage(evaluation, specs, matrix, shown, 2)["length_mm"]["triage"] == "true_missing"
    assert cluster_of({"name": "x"}) == "other" and cluster_of({"name": "x", "group": "g"}) == "g"


def test_source_yield_score_ranks_documents_never_values():
    official = source_yield_score(unresolved_with_candidates=4, binding_level="exact_technical_variant",
                                  source_authority="official_manufacturer", target_market=False, has_tables=True,
                                  evidence_yielded=0, identity_named=True, parser_quality=0.9)
    aggregator = source_yield_score(unresolved_with_candidates=1, binding_level="model_family",
                                    source_authority="aggregator", target_market=True, has_tables=False,
                                    evidence_yielded=3, identity_named=True, parser_quality=0.6)
    assert official > aggregator > 0


def test_search_hints_are_routing_metadata_for_unfetched_results():
    events = [{"kind": "tool_call", "seq": 1, "step": 1, "name": "search_web", "call_id": "s",
               "arguments": json.dumps({"query": "corolla"})},
              {"kind": "tool_result", "seq": 2, "step": 1, "name": "search_web", "call_id": "s",
               "result": {"results": [{"url": "https://random.example/blog", "title": "cars", "snippet": "x"},
                                      {"url": tail.EU_SPEC, "title": "Toyota Corolla 2024 fuel tank capacity",
                                       "snippet": "fuel tank capacity 43 l"},
                                      {"url": tail.CARTUBE, "title": "fetched already", "snippet": ""}]}}]
    hints = search_hints(events=events, specs=[SPECS["fuel_tank_l"]], identity={"manufacturer": "Toyota",
                                                                                  "model": "Corolla", "year": 2024},
                         manufacturer="טויוטה", opened_urls=[tail.CARTUBE])
    assert [h["url"] for h in hints] == [tail.EU_SPEC, "https://random.example/blog"]
    assert hints[0]["source_authority"] == "official_manufacturer" and hints[0]["from_query"] == "corolla"


def test_novelty_ignores_errors_repeats_and_commentary():
    base = {"documents": ["d1"], "candidates": {"a"}, "evidence": {"e1"}, "best_binding": {"x": 3},
            "states": {"x": "missing"}, "conflict_sizes": {"h": 2}}
    assert novelty(base, dict(base)) == []
    after = {**base, "evidence": {"e1", "e2"}, "states": {"x": "ok"}, "conflict_sizes": {"h": 2},
             "best_binding": {"x": 4}}
    assert novelty(base, after) == ["new_accepted_evidence:1", "binding_improvement:1", "field_state_improvement:1"]
    narrowed = {**base, "states": {"x": "missing", "h": "conflicting"}, "conflict_sizes": {"h": 1}}
    assert novelty({**base, "states": {"x": "missing", "h": "conflicting"}}, narrowed) == ["conflict_narrowing:1"]
    assert snapshot(events=[], evaluation=[], documents=[], open_fields=[], matrix={"fields": {}})["evidence"] == set()


# --- search budget (provider calls, not tool calls) -------------------------------------------------------------

class CountingSearch:
    model = "glm-test"

    def __init__(self):
        self.calls = []
        self.settings = type("S", (), {"search_engine": "fake"})()

    def web_search(self, query, count=8, domain=None):
        self.calls.append((query, domain))
        return [{"url": f"https://{domain or 'web'}/r", "title": query}]


def test_search_budget_counts_underlying_provider_calls(tmp_path, make_ctx):
    ctx = make_ctx()
    ctx.glm = CountingSearch()
    session = ToolSession(ctx, RunLog(tmp_path, "b", "1"), AgentConfig())
    session.search_budget = SearchBudget(4)
    messages = []
    three = {"query": "corolla specs", "domains": ["toyota.co.il", "toyota.co.uk", "toyota-europe.com"]}
    session.execute([_call("a", "search_official_domains", three)], messages, phase="field_recovery")
    assert session.search_budget.used == 3 and len(ctx.glm.calls) == 3            # 1 tool call = 3 provider calls
    session.execute([_call("b", "search_official_domains", {**three, "query": "corolla tank"})], messages,
                    phase="field_recovery")
    assert json.loads(messages[-1]["content"])["error"] == "search_budget_exhausted"  # 3 > 1 left: refused
    assert len(ctx.glm.calls) == 3 and session.search_budget.refused == 1
    session.execute([_call("c", "search_web", {"query": "corolla specs", "domain": "toyota.co.il",
                                               "max_results": 5})], messages, phase="field_recovery")
    assert session.search_budget.used == 3                                          # a cached query is free
    session.execute([_call("d", "search_web", {"query": "corolla boot"})], messages, phase="field_recovery")
    assert session.search_budget.used == 4 and session.search_budget.remaining == 0


# --- the offline Corolla benchmark ------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def corolla(tmp_path_factory):
    root = tmp_path_factory.mktemp("corolla-tail")
    return {mode: run_mode(mode, root / mode) for mode in ("legacy", "cluster")}


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_corolla_benchmark_cluster_mode_resolves_as_much_with_far_less_work(corolla):
    legacy, cluster = summarize(corolla["legacy"]), summarize(corolla["cluster"])
    assert legacy["tail_start_coverage"] == cluster["tail_start_coverage"] == "3/14"
    ok = lambda s: {f for f, v in s["final_states"].items() if v == "ok"}      # noqa: E731
    assert ok(cluster) >= ok(legacy) and len(ok(cluster)) == 10
    assert cluster["model_turns"] < legacy["model_turns"] / 2
    assert cluster["search_calls"] <= legacy["search_calls"]
    assert cluster["documents_fetched"] <= legacy["documents_fetched"]
    assert cluster["fields_resolved_per_turn"] > 2 * legacy["fields_resolved_per_turn"]
    states = cluster["final_states"]
    assert (states["height_mm"], states["ground_clearance_mm"], states["curb_weight_kg"]) == \
        ("conflicting", "unresolved", "foreign_market_only")                    # no truth was loosened


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_corolla_cluster_run_details(corolla):
    run = corolla["cluster"]
    result, events = run["result"], run["events"]
    rec = result["field_recovery"]
    order = [(a["cluster"], a["attempt"], a["mode"]) for a in rec["attempts"]]
    # the local pass does not use up the web attempts: technical_spec still gets its 2 web attempts
    assert order == [("technical_spec", 1, "local_only"), ("performance", 1, "web"), ("technical_spec", 2, "web"),
                     ("technical_spec", 3, "web")]
    assert rec["no_novelty_stops"] == 2 and rec["budget_extensions"] >= 1
    assert rec["conflicts_normalized_without_search"] == 1                       # the price range
    fields = result["research_bundle"]["field_states"]
    assert fields["height_mm"]["conflict_class"]["class"] == "internal_source_inconsistency"
    tank = next(e for e in result["research_bundle"]["evidence"] if e["field"] == "fuel_tank_l")
    assert tank["market"] == "EU" and tank["portable_to_target_market"] is True
    assert rec["fields_resolved_by_cluster"]["performance"] == ["acceleration_0_100_s", "top_speed_kmh"]
    # the official page found by the performance cluster fed the technical cluster without another search
    second = rec["attempts"][2]
    assert {"fuel_tank_l", "battery_gross_kwh"} <= set(second["fields_resolved"])
    m = compute_metrics(result)
    assert (m["recovery_mode"], m["tail_fields_at_start"], m["tail_fields_resolved"]) == ("cluster", 11, 7)
    assert m["cluster_attempts"] == 4 and m["portable_facts_accepted"] >= 3
    assert m["portable_facts_rejected"] == 0        # curb weight / boot volume are never portable: not "rejections"
    assert m["tail_model_calls"] == rec["turns"] and m["tail_search_calls"] == 2


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_local_first_exposes_only_cached_tools(corolla):
    client = corolla["cluster"]["client"]
    first = next(r for r in client.requests if r["messages"][0]["content"] == CLUSTER_RECOVERY_SYSTEM_PROMPT)
    packet = json.loads(first["messages"][1]["content"].split("\n", 1)[1])
    assert packet["mode"] == "local_only" and "search_hints" not in packet
    names = {t["function"]["name"] for t in first["tools"]}
    assert names.isdisjoint({"search_web", "search_official_domains", "fetch_url", "fetch_pdf", "render_page"})
    perf = [r for r in client.requests if r["messages"][0]["content"] == CLUSTER_RECOVERY_SYSTEM_PROMPT
            and '"cluster": "performance"' in r["messages"][1]["content"]][0]
    assert "search_web" in {t["function"]["name"] for t in perf["tools"]}      # no local material: web directly


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_every_new_document_is_harvested_for_all_fields_before_the_next_paid_turn(corolla):
    events = corolla["cluster"]["events"]
    fetch = next(e for e in events if e.get("kind") == "tool_result" and e.get("name") == "fetch_url"
                 and e.get("phase") == "field_recovery")
    doc = fetch["result"]["document_id"]
    harvested = next(e for e in events if e.get("kind") == "candidates_harvested" and e.get("document_id") == doc)
    next_turn = next(e for e in events if e.get("kind") == "model_response" and e["seq"] > fetch["seq"])
    assert harvested["seq"] < next_turn["seq"]
    assert {"fuel_tank_l", "battery_gross_kwh", "curb_weight_kg", "top_speed_kmh"} <= set(harvested["fields"])
    client = corolla["cluster"]["client"]
    noted = [m["content"] for r in client.requests for m in r["messages"] if m["role"] == "tool"
             and "New candidates harvested" in m["content"]]
    assert noted and "top_speed_kmh" in noted[0]


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_legacy_mode_reports_the_same_tail_metrics(corolla):
    m = compute_metrics(corolla["legacy"]["result"])
    assert m["recovery_mode"] == "legacy" and m["tail_fields_at_start"] == 11 and m["tail_model_calls"] == 24
    assert m["cluster_attempts"] == 0


# --- cluster-mode budgets and PR #18 semantics ------------------------------------------------------------------

class Scripted(PolicyGLM):
    """Primary research from the Corolla fixture, cluster turns from `recover(packet, turn, messages)`."""

    def __init__(self, recover):
        super().__init__()
        self.recover_fn = recover
        self.cluster_turns: list[tuple[str, int]] = []

    def recover(self, messages, tools):
        packet = json.loads(messages[1]["content"].split("\n", 1)[1])
        turn_no = sum(1 for m in messages if m["role"] == "assistant") + 1
        self.cluster_turns.append((packet["cluster"], turn_no))
        return self.recover_fn(packet, turn_no, messages)


# tests about recovery script an early primary {"done"}: the minimum acquisition base (which would defer it) is off
GATE_OFF = {"primary_research_min_base_documents": 0, "primary_research_min_base_scoped_coverage": 0}


def run_scripted(tmp_path, recover, **cfg):
    from conftest import FakeResponse, FakeSession
    from fixtures.corolla_touring import PAYLOAD, VEHICLE
    from src.storage.cache import DocumentCache

    routes = {u: FakeResponse(b.encode("utf-8"), content_type=c, url=u) for u, (b, c) in tail.ROUTES.items()}
    client = Scripted(recover)
    log = RunLog(tmp_path / "runs", "b", "38626")
    config = AgentConfig(**{"max_steps": 4, "no_new_research_turns": 0, "requested_fields": FIELDS,
                            "document_sweep_max_turns": 0, **cfg})
    result = run_vehicle({"upstream_record_id": "38626"}, PAYLOAD, client=client, cache=DocumentCache(tmp_path / "c"),
                         run_log=log, vehicle_meta=VEHICLE, config=config, tool_config=ToolConfig(),
                         session=FakeSession(routes))
    return result, read_events(log.events_path), client


def _say(obj):
    return {"role": "assistant", "content": json.dumps(obj)}


def _turn(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def test_turns_never_exceed_the_ceiling_and_stop_without_novelty(tmp_path):
    def endless_search(packet, turn_no, messages):          # never novel: every query returns the forum page
        return _turn(_call(f"s{turn_no}", "search_web", {"query": f"corolla ground clearance {turn_no}"}))

    result, events, client = run_scripted(tmp_path, endless_search, cluster_max_attempts=1, **GATE_OFF)
    rec = result["field_recovery"]
    assert all(a["turns"] == 2 and a["stop"] == "no_novelty" for a in rec["attempts"] if a["mode"] == "web")
    assert rec["budget_extensions"] == 0

    def always_novel(packet, turn_no, messages):            # stores a new admitted fact every turn
        doc = next(d["document_id"] for d in packet["ranked_documents"])
        line = ['אורך: 4,650 מ"מ', 'רוחב: 1,790 מ"מ', 'בסיס גלגלים: 2,700 מ"מ', "x", "y", "z"][turn_no - 1]
        field = ["length_mm", "width_mm", "wheelbase_mm", "length_mm", "length_mm", "length_mm"][turn_no - 1]
        value = {"length_mm": 4650, "width_mm": 1790, "wheelbase_mm": 2700}[field]
        return _turn(_call(f"v{turn_no}", "store_evidence", {"field": field, "value": value, "document_id": doc,
                                                             "quote": line}),
                     _call(f"q{turn_no}", "search_web", {"query": f"corolla {turn_no}"}))

    result, events, client = run_scripted(tmp_path / "b", always_novel, cluster_max_attempts=1, **GATE_OFF,
                                          cluster_max_turns=9)
    first = result["field_recovery"]["attempts"][0]
    assert first["turns"] == 4 and first["turn_ceiling"] == 4                  # CLUSTER_MAX_TURNS is capped at 4
    assert first["budget_extensions"] == 2 and all(first["novelty"][:3])        # turns 3-4 only after novelty


def test_cluster_search_budget_is_enforced_per_attempt(tmp_path):
    def official(packet, turn_no, messages):
        if packet["mode"] == "local_only":
            return _say({"cluster": packet["cluster"], "fields": []})
        return _turn(_call(f"o{turn_no}", "search_official_domains",
                           {"query": f"corolla {packet['cluster']} {turn_no}",
                            "domains": ["toyota.co.il", "toyota.co.uk", "toyota-europe.com"]}))

    result, events, client = run_scripted(tmp_path, official, cluster_search_budget=4, cluster_max_attempts=1, **GATE_OFF)
    for attempt in result["field_recovery"]["attempts"]:
        assert attempt["search_provider_calls"] <= 4
    refused = [e for e in events if e.get("kind") == "tool_blocked" and e.get("reason") == "search_budget_exhausted"]
    assert refused and all(e["planned"] > e["remaining"] for e in refused)


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_unbacked_conflict_resolution_keeps_the_conflict_and_admission_still_applies(tmp_path):
    def resolve(packet, turn_no, messages):
        if "height_mm" not in [f["field"] for f in packet["fields"]]:
            return _say({"cluster": packet["cluster"], "fields": []})
        if turn_no == 1:      # a value the cited document does not state is refused by the admission gate
            doc = packet["conflicts"]["height_mm"]["competing_evidence"][0]["document_id"]
            return _turn(_call("bad", "store_evidence", {"field": "height_mm", "value": 1450, "document_id": doc,
                                                         "quote": 'גובה 146.0 ס"מ'}))
        return _say({"cluster": packet["cluster"],
                     "fields": [{"field": "height_mm", "status": "conflict_resolved", "evidence_ids": []}]})

    result, events, client = run_scripted(tmp_path, resolve, cluster_max_attempts=1)
    assert result["research_bundle"]["field_states"]["height_mm"]["state"] == "conflicting"
    rejected = [e for e in events if e.get("kind") == "evidence_rejected" and e.get("field") == "height_mm"]
    assert rejected and not [e for e in trace.evidence_items(events) if e.get("value") == 1450]


def test_global_turn_cap_holds_across_clusters(tmp_path):
    def busy(packet, turn_no, messages):
        return _turn(_call(f"f{turn_no}", "find_in_document", {"document_id": packet["ranked_documents"][0]
                                                               ["document_id"], "query": f"x{turn_no}"}))

    result, events, client = run_scripted(tmp_path, busy, field_recovery_max_total_steps=3, **GATE_OFF)
    rec = result["field_recovery"]
    assert rec["turns"] == 3 and rec["stopped"] == "max_total_steps"
    summary = trace.field_recovery_summary(events)
    assert summary["mode"] == "cluster" and "length_mm" in summary["fields_retried"]
    assert not any(f.startswith("cluster:") for f in summary["fields_retried"])


def test_recovery_mode_from_env():
    from src.agent import agent_config_from_env

    import dataclasses

    # PR #31: targeted re-acquisition is the code default (this module pins "cluster" through its marker)
    assert {f.name: f.default for f in dataclasses.fields(AgentConfig)}["recovery_mode"] == "reacquire"
    assert agent_config_from_env({"RECOVERY_MODE": "cluster"}.get).recovery_mode == "cluster"
    env = {"RECOVERY_MODE": "legacy", "CLUSTER_SEARCH_BUDGET": "2", "CLUSTER_MAX_TURNS": "3"}
    config = agent_config_from_env(env.get)
    assert (config.recovery_mode, config.cluster_search_budget, config.cluster_max_turns) == ("legacy", 2, 3)
    assert Path(tail.__file__).name == "corolla_tail.py"


# --- review regressions ----------------------------------------------------------------------------------------

def test_error_pages_and_other_variant_evidence_are_not_novelty(tmp_path):
    from src.storage.cache import DocumentCache

    cache = DocumentCache(tmp_path)
    blocked = cache.put("fetch", "https://www.toyota.co.il/blocked", b"Forbidden",
                        {"status": 403, "final_url": "https://www.toyota.co.il/blocked", "doc_type": "text"},
                        "Forbidden")["document_id"]
    base = {"documents": [], "candidates": set(), "evidence": set(), "best_binding": {}, "states": {},
            "conflict_sizes": {}}
    assert novelty(base, {**base, "documents": [blocked]}, None, cache) == []
    events = [{"kind": "evidence", "seq": 1, "evidence": {"evidence_id": "e1", "field": "length_mm", "value": 1,
                                                          "variant_match": "different"}}]
    snap = snapshot(events=events, evaluation=[], documents=[], open_fields=[], matrix={"fields": {}})
    assert snap["evidence"] == set()


def test_a_local_pass_shows_the_candidates_that_justified_it():
    from src.tail_planner import candidate_key, candidates_fresh_first

    cands = [{"field": "length_mm", "value": 4600 + i, "document_id": "d", "parser_confidence": 1 - i / 100}
             for i in range(8)]
    presented = [candidate_key(c) for c in cands[:6]]
    events = [{"kind": "document_sweep_started", "presented_candidate_keys": presented}]
    ordered = candidates_fresh_first({"fields": {"length_mm": cands}}, events, ["length_mm"])
    assert [c["value"] for c in ordered["length_mm"][:2]] == [4606, 4607]
    announced = events + [{"kind": "cluster_candidates_announced", "presented_candidate_keys":
                           [candidate_key(c) for c in cands[6:]]}]
    spec = [{**SPECS["length_mm"], "applicable": True}]
    entry = [{"field": "length_mm", "state": "missing", "retry_eligible": True}]
    assert triage(entry, spec, {"fields": {"length_mm": cands}}, announced, 2)["length_mm"]["triage"] == "true_missing"


def test_one_web_attempt_still_follows_a_local_pass(tmp_path):
    result, events, client = run_scripted(tmp_path, lambda packet, turn_no, messages: _say(
        {"cluster": packet["cluster"], "fields": []}), cluster_max_attempts=1, **GATE_OFF)
    modes = [(a["cluster"], a["mode"]) for a in result["field_recovery"]["attempts"]]
    assert ("technical_spec", "local_only") in modes and ("technical_spec", "web") in modes


def test_old_runs_are_not_judged_by_the_new_portability_policy():
    from src.fields import public_spec, with_dictionary

    old = {k: v for k, v in public_spec(SPECS["fuel_tank_l"]).items()
           if k not in ("portability_scope", "market_sensitivity", "recovery_cluster")}
    completed = with_dictionary(old)
    assert completed.get("accepted_unit_variants") and "portability_scope" not in completed
    assert assess(old, [uk_tank()], "IL", is_target_market)["u1"]["portable_to_target_market"] is False


def test_target_market_spellings_are_one_market():
    spec = SPECS["height_mm"]
    items = [item("a", "height_mm", 1460, market="IL", document_id="x"),
             item("b", "height_mm", 1435, market="Israel", document_id="y")]
    assert classify_conflict(spec, items, "IL")["class"] != "market_difference"


def test_reconstructed_cluster_runs_keep_their_tail_metrics(corolla):
    summary = trace.field_recovery_summary(corolla["cluster"]["events"])
    live = corolla["cluster"]["result"]["field_recovery"]
    for key in ("tail_fields_at_start", "tail_fields_resolved", "cluster_attempts", "no_novelty_stops",
                "budget_extensions", "tail_search_calls", "tail_model_calls"):
        assert summary[key] == live[key], key
