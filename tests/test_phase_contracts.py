"""Phase Contracts v1: the acquisition contract (ACQUISITION_MODE=contract), sweep failure isolation and per-phase
max attempts, and observational end-to-end telemetry. Scripted GLM / fake HTTP only: no network, no paid calls.

Every contract-mode test passes acquisition_mode="contract" explicitly, so it holds under ACQUISITION_MODE=legacy too;
the legacy counterparts pass acquisition_mode="legacy"."""

import copy
import json

import pytest
import requests

from conftest import FakeResponse, FakeSession
from fixtures import corolla_tail as tail
from fixtures.corolla_touring import PAYLOAD, VEHICLE
from test_tools_smoke import _call

from src import acquisition as A
from src import diagnostics as D
from src.agent import (ACQUISITION_SYSTEM_PROMPT, CLUSTER_RECOVERY_SYSTEM_PROMPT, DOCUMENT_SWEEP_SYSTEM_PROMPT,
                       FIELD_RECOVERY_SYSTEM_PROMPT, OUTPUT_SHAPE, AgentConfig, ModelCaller, ToolSession,
                       agent_config_from_env, build_acquisition_message, effective_glm_config, research_system_prompt,
                       run_vehicle)
from src.fields import resolve_requested_fields
from src.glm_client import ChatResponse, GLMClient, GLMError, GLMSettings
from src.phase_settings import describe, for_phase, phase_settings_from_env
from src.storage.cache import DocumentCache
from src.storage.run_log import RunLog, read_events
from src.tools import ToolConfig, tool_specs

EU = tail.EU_SPEC
BLOG = "https://www.example-blog.net/corolla-touring-sports-review"
BLOG_TEXT = "A long-term review of the Toyota Corolla Touring Sports. Comfortable, quiet, economical. No figures."
ROUTES = {EU: (tail.EU_SPEC_HTML, "text/html; charset=utf-8"), tail.FORUM: (tail.FORUM_TEXT, "text/plain"),
          BLOG: (BLOG_TEXT, "text/plain"), tail.CARTUBE: (tail.CARTUBE_TEXT, "text/plain"),
          tail.LAUNCH: (tail.LAUNCH_TEXT, "text/plain")}
UK_OFFICIAL = "https://www.toyota.co.uk/new-cars/corolla-touring-sports"
SPECS = resolve_requested_fields(tail.FIELDS, propulsion="hybrid")
# tests about later stages script an early {"done": true}: the minimum acquisition base (which would defer it) is off
GATE_OFF = {"primary_research_min_base_documents": 0, "primary_research_min_base_scoped_coverage": 0}


def say(obj):
    return {"role": "assistant", "content": json.dumps(obj)}


def turn(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def search(cid, query):
    return turn(_call(cid, "search_web", {"query": query}))


def fetch(cid, *urls):
    return turn(*[_call(f"{cid}{i}", "fetch_url", {"url": u}) for i, u in enumerate(urls)])


class PhaseClient:
    """Answers by the step asking (system prompt): research turns from a script, the sweep / recovery with
    'nothing found', the finalizer with a fixed JSON. A search returns one official URL per query."""

    model = "glm-5.3-flash"

    def __init__(self, research, sweep=None, search_url=EU):
        self.research = list(research)
        self.sweep = sweep
        self.search_url = search_url
        self.requests, self.kinds = [], []

    def web_search(self, query, count=8, domain=None):
        return [{"url": self.search_url, "title": "Toyota Corolla Touring Sports specifications", "snippet": "s"}]

    def chat(self, messages, tools=None, **kwargs):
        system = messages[0]["content"]
        self.requests.append({"messages": messages, "tools": tools, **kwargs})
        usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
        if system in (research_system_prompt(), research_system_prompt("contract")):
            kind, message = "research", self.research.pop(0)
        elif system == DOCUMENT_SWEEP_SYSTEM_PROMPT:
            kind = "sweep"
            self.sweep_calls = getattr(self, "sweep_calls", 0) + 1
            message = self.sweep(self.sweep_calls) if self.sweep else say({"reviewed": []})
        elif system == CLUSTER_RECOVERY_SYSTEM_PROMPT:
            packet = json.loads(messages[1]["content"].split("\n", 1)[1])
            kind, message = "recovery", say({"cluster": packet["cluster"], "fields": [
                {"field": f["field"], "status": "unresolved"} for f in packet["fields"]]})
        elif system == FIELD_RECOVERY_SYSTEM_PROMPT:
            packet = json.loads(messages[1]["content"].split("\n", 1)[1])
            kind, message = "recovery", say({"field": packet["requested_field"]["name"], "status": "unresolved"})
        else:
            kind, message = "final", say({"summary": "final", "fields": {}})
        self.kinds.append(kind)
        return ChatResponse(message=message, finish_reason="stop", usage=usage)


def run(tmp_path, client, *, routes=ROUTES, **cfg):
    session = FakeSession({url: FakeResponse(body.encode("utf-8"), content_type=ctype, url=url)
                           for url, (body, ctype) in routes.items()})
    log = RunLog(tmp_path / "runs", "b", "38626")
    config = AgentConfig(**{"research_memory_enabled": False, "requested_fields": tail.FIELDS,
                            "no_new_research_turns": 0, **cfg})
    result = run_vehicle({"upstream_record_id": "38626"}, PAYLOAD, client=client, cache=DocumentCache(tmp_path / "c"),
                         run_log=log, vehicle_meta=VEHICLE, config=config, tool_config=ToolConfig(), session=session)
    return result, read_events(log.events_path)


def research_tools(client) -> list[set]:
    return [{t["function"]["name"] for t in r["tools"] or []} for r in client.requests
            if r["messages"][0]["content"] == research_system_prompt("contract")]


# --- A1: the tool surface, enforced in code -------------------------------------------------------------------

def test_contract_research_offers_and_executes_only_the_acquisition_tools(tmp_path):
    script = [turn(_call("a", "fetch_url", {"url": EU}),
                   _call("b", "store_evidence", {"field": "torque_nm", "value": 142, "source_url": EU,
                                                 "quote": "Max torque 142 Nm"}),
                   _call("c", "find_in_document", {"document_id": "x", "query": "torque"})),
              say({"done": True, "reason": "enough"})]
    client = PhaseClient(script)
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False)
    expected = {s["function"]["name"] for s in tool_specs()} & set(A.ACQUISITION_TOOLS)
    assert research_tools(client) and all(names == expected for names in research_tools(client))
    blocked = [e for e in events if e["kind"] == "tool_blocked" and e["phase"] == "research"]
    assert [e["name"] for e in blocked] == ["store_evidence", "find_in_document"]
    sent = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
    assert sum("tool_not_allowed_in_phase" in m["content"] for m in sent) == 2
    assert result["evidence"] == [] and not any(e["kind"] == "evidence" for e in events)
    assert result["acquisition_mode"] == "contract"
    started = next(e for e in events if e["kind"] == "run_started")
    assert started["acquisition_mode"] == "contract" and result["effective_config"]["agent"]["acquisition_mode"] \
        == "contract"


def test_legacy_research_keeps_every_tool(tmp_path):
    script = [fetch("a", EU), say({"summary": "primary", "fields": {}})]
    client = PhaseClient(script)
    result, events = run(tmp_path, client, acquisition_mode="legacy", field_recovery_enabled=False)
    tools = [{t["function"]["name"] for t in r["tools"] or []} for r in client.requests
             if r["messages"][0]["content"] == research_system_prompt()]
    assert tools and all(names == {s["function"]["name"] for s in tool_specs()} for names in tools)
    assert result["status"] == "completed" and "final" not in client.kinds      # legacy: the reply is the output


def test_acquisition_mode_comes_from_the_environment():
    assert agent_config_from_env({"ACQUISITION_MODE": "legacy"}.get).acquisition_mode == "legacy"
    assert agent_config_from_env({"ACQUISITION_MODE": "Contract"}.get).acquisition_mode == "contract"
    assert agent_config_from_env({"ACQUISITION_MODE": "bogus"}.get,
                                 acquisition_mode="contract").acquisition_mode == "contract"
    import dataclasses
    assert next(f.default for f in dataclasses.fields(AgentConfig) if f.name == "acquisition_mode") == "contract"


# --- A3 / A4: the acquisition prompt and task ------------------------------------------------------------------

def test_the_acquisition_prompt_never_invites_field_work(monkeypatch):
    prompt = research_system_prompt("contract")
    for word in ("store_evidence", "report_field_status", "evidence_id", "quote", "semantic"):
        assert word not in prompt
    for key in json.loads(json.dumps(["variant_identity", "provenance_summary", "research_trace",
                                      "additional_findings", "evidence_ids"])):
        assert key in OUTPUT_SHAPE and key not in prompt
    for tool in ("inspect_document_for_fields", "find_in_document", "extract_html", "get_cached_document"):
        assert tool not in prompt
    assert '{"done": true, "reason": "<short>"}' in prompt and "`links`" in prompt
    assert "acquisition_priority" in prompt and "priority_commercial" in prompt
    import src.agent as agent_mod

    monkeypatch.setattr(agent_mod, "unavailable_tools", lambda: {"render_page": "no Playwright"})
    stripped = agent_mod.research_system_prompt("contract")
    assert "render_page" not in stripped and "fetch_url / fetch_pdf store" in stripped
    assert "render_page" in ACQUISITION_SYSTEM_PROMPT


def test_the_acquisition_task_lists_clusters_and_names_only():
    specs = resolve_requested_fields(None, propulsion="hybrid")
    message = build_acquisition_message(PAYLOAD, False, 6, {"note": "x"}, specs, 2, "IL")
    clusters = {s.get("recovery_cluster") for s in specs if s.get("applicable", True)}
    assert all(f"- {c} [" in message for c in clusters)
    assert "torque_nm" in message and "Target market: IL" in message and "Operator notes" in message
    for spec in specs:
        if spec.get("semantic_definition"):
            assert spec["semantic_definition"] not in message
    assert "Maximum torque (Nm)" not in message                 # no descriptions either
    assert "Research budget: about 6 model turns" in message and "Level 3" not in message
    # every cluster of the shipped schema holds at least one market-sensitive field: target-market sources
    assert "target-market (IL) source: importer model page, brochure, price list, warranty page" in message
    low = [{"name": "torque_nm", "recovery_cluster": "performance", "market_sensitivity": "low"},
           {"name": "list_price", "recovery_cluster": "commercial", "market_sensitivity": "high",
            "display_name_he": "מחיר", "description": "List price"}]
    small = build_acquisition_message(PAYLOAD, False, None, None, low)
    assert "- performance [official technical source, any market]: torque_nm" in small
    assert "- commercial [target-market (IL) source: importer model page, brochure, price list, warranty page]: " \
           "list_price (מחיר)" in small and "List price" not in small
    assert "Level 3 open research" in build_acquisition_message(PAYLOAD, True, 6, None, specs)


def test_cluster_source_type_follows_market_sensitivity():
    low = [{"name": "a", "market_sensitivity": "low"}, {"name": "b", "market_sensitivity": "low"}]
    mixed = low + [{"name": "c", "market_sensitivity": "medium"}]
    assert A.cluster_source_type(low) == A.ANY_MARKET_SOURCE
    assert A.cluster_source_type(mixed, "IL") == A.target_market_source("IL")
    assert A.cluster_source_type([{"name": "d"}]) == A.target_market_source("IL")      # unknown = market-bound


# --- A2: navigation links ---------------------------------------------------------------------------------------

IMPORTER = "https://www.toyota.co.il/models/corolla-touring-sports"
IMPORTER_HTML = """<html><head><title>קורולה טורינג ספורט</title></head><body>
<a href="/about">אודות</a>
<a href="https://news.example.com/corolla">Corolla news</a>
<a href="mailto:info@toyota.co.il">mail</a><a href="tel:*1234">call</a><a href="javascript:void(0)">x</a>
<a href="#top">top</a>
<a href="/models/corolla-touring-sports/pricelist">מחירון</a>
<a href="/files/corolla-ts-spec-2024.pdf">מפרט טכני</a>
<a href="/files/corolla-ts-spec-2024.pdf#page=2">מפרט טכני עמוד 2</a>
</body></html>"""


def _session(make_ctx, mode="contract"):
    ctx = make_ctx({IMPORTER: FakeResponse(IMPORTER_HTML.encode("utf-8"), url=IMPORTER),
                    EU: FakeResponse(tail.EU_SPEC_HTML.encode("utf-8"), url=EU)})
    return ToolSession(ctx, RunLog(ctx.cache.root.parent / "runs", "b", "1"), AgentConfig(acquisition_mode=mode))


def test_a_research_fetch_carries_ranked_navigation_links(make_ctx):
    session = _session(make_ctx)
    messages = []
    session.execute([_call("a", "fetch_url", {"url": IMPORTER})], messages, phase="research")
    links = session.turn_results[0]["result"]["links"]
    assert links[0]["url"] == "https://www.toyota.co.il/files/corolla-ts-spec-2024.pdf"
    assert links[1]["url"] == "https://www.toyota.co.il/models/corolla-touring-sports/pricelist"
    urls = [l["url"] for l in links]
    assert len(urls) == len(set(urls)) and len(urls) <= A.MAX_NAVIGATION_LINKS
    assert not any(u.startswith(("mailto:", "tel:", "javascript:")) or "#" in u for u in urls)
    assert IMPORTER not in urls                                                   # same-page fragment dropped
    assert '"links"' in messages[0]["content"] and "corolla-ts-spec-2024.pdf" in messages[0]["content"]


def test_links_only_in_contract_research_fetches(make_ctx):
    session = _session(make_ctx)
    session.execute([_call("a", "fetch_url", {"url": EU})], [], phase="field_recovery")
    assert "links" not in session.turn_results[0]["result"]
    session.execute([_call("b", "fetch_url", {"url": IMPORTER})], [], phase="document_sweep")
    assert "links" not in session.turn_results[0]["result"]
    legacy = _session(make_ctx, "legacy")
    legacy.execute([_call("c", "fetch_url", {"url": IMPORTER})], [], phase="research")
    assert "links" not in legacy.turn_results[0]["result"]


def test_a_research_fetch_replayed_in_recovery_has_no_links(make_ctx):
    session = _session(make_ctx)
    session.execute([_call("a", "fetch_url", {"url": IMPORTER})], [], phase="research")
    session.execute([_call("a2", "fetch_url", {"url": IMPORTER})], [], phase="field_recovery")
    assert session.turn_results[0]["reused"] and "links" not in session.turn_results[0]["result"]


def test_rank_links_orders_by_site_pdf_keywords_and_model():
    page = "https://www.toyota.co.il/models/corolla"
    links = [{"url": "https://other.example/about", "text": "about"},
             {"url": "https://other.example/corolla", "text": "Corolla club"},
             {"url": "https://www.toyota.co.il/contact", "text": "צור קשר"},
             {"url": "https://www.toyota.co.il/%D7%90%D7%97%D7%A8%D7%99%D7%95%D7%AA", "text": ""},   # /אחריות
             {"url": "https://cdn.other.example/brochure.pdf", "text": "Brochure"},
             {"url": "https://www.toyota.co.il/catalog.pdf", "text": "קטלוג"},
             {"url": "mailto:a@b.c", "text": "mail"}, {"url": "https://www.toyota.co.il/models/corolla#specs",
                                                        "text": "specs"},
             {"url": "https://www.toyota.co.il/contact#x", "text": "dup"}]
    ranked = [l["url"] for l in A.rank_links(links, page, A.model_tokens("Corolla"))]
    assert ranked == ["https://www.toyota.co.il/catalog.pdf",                       # site + pdf + keyword
                      "https://www.toyota.co.il/%D7%90%D7%97%D7%A8%D7%99%D7%95%D7%AA",   # site + Hebrew keyword
                      "https://cdn.other.example/brochure.pdf",                     # pdf + keyword
                      "https://www.toyota.co.il/contact",                           # site
                      "https://other.example/corolla",                              # model token
                      "https://other.example/about"]
    assert A.rank_links(links, page, limit=2) == [{"url": ranked[0], "text": "קטלוג"},
                                                  {"url": ranked[1], "text": ""}]
    assert A.model_tokens("Corolla Touring-Sports", None) == ["corolla", "touring", "sports"]


# --- A7: acquisition progress ------------------------------------------------------------------------------------

def test_a_search_only_official_url_is_discovered_not_acquired(tmp_path):
    script = [fetch("a", EU), search("s", "corolla touring sports uk"), say({"done": True, "reason": "x"})]
    client = PhaseClient(script, search_url=UK_OFFICIAL)
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False,
                         max_steps=6, primary_research_min_base_documents=0,
                         primary_research_min_base_scoped_coverage=0)
    second = [e for e in events if e["kind"] == "primary_research_turn"][1]
    assert second["artifacts"] == []                                              # not progress
    assert second["state_after"]["official_urls_discovered"] == second["state_before"]["official_urls_discovered"] + 1
    assert second["state_after"]["official_documents"] == second["state_before"]["official_documents"] == 1
    assert result["primary_research"]["official_sources"] == 1
    assert result["primary_research"]["official_urls_discovered"] == 2
    # legacy keeps the historical definition: a search-only official URL is an artifact
    client = PhaseClient([fetch("a", EU), search("s", "corolla touring sports uk"),
                          say({"summary": "p", "fields": {}})], search_url=UK_OFFICIAL)
    _, events = run(tmp_path / "legacy", client, acquisition_mode="legacy", field_recovery_enabled=False,
                    max_steps=6, primary_research_min_base_documents=0, primary_research_min_base_scoped_coverage=0)
    second = [e for e in events if e["kind"] == "primary_research_turn"][1]
    assert second["artifacts"] == ["new_official_source:1"]


def test_contract_artifacts_ignore_evidence_binding_and_search_only_official_sources():
    base = {"useful_urls": set(), "target_market_urls": set(), "official_sources": set(), "candidates": set(),
            "evidence": set(), "best_binding": {"torque_nm": 1}}
    after = {**base, "official_sources": {"u"}, "evidence": {"e1"}, "best_binding": {"torque_nm": 3}}
    assert A.artifacts(base, after, mode="contract") == []
    assert A.artifacts(base, after, mode="legacy") == ["new_official_source:1", "new_admitted_evidence:1",
                                                       "binding_improvement:1"]
    grown = {**after, "useful_urls": {"d"}, "target_market_urls": {"d"}, "candidates": {"c"}}
    assert A.artifacts(base, grown, mode="contract") == ["new_usable_document:1", "new_target_market_document:1",
                                                         "new_candidate:1"]


# --- A6: the research reply is never the run output in contract mode -------------------------------------------

def test_a_finished_contract_research_always_reaches_the_finalizer(tmp_path):
    client = PhaseClient([fetch("a", EU), say({"done": True, "reason": "official spec page fetched"})])
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False, **GATE_OFF)
    assert result["stop_reason"] == "model_finished" and result["status"] == "completed"
    assert client.kinds[-1] == "final" and any(e["kind"] == "finalization_started" for e in events)
    assert result["output"]["summary"] == "final" and result["parse_note"].startswith("finalizer:")
    assert "official spec page" not in json.dumps(result["output"])


# --- A8: extension futility --------------------------------------------------------------------------------------

def test_the_real_run_sequence_still_reaches_its_productive_turn(tmp_path):
    """Turns 1-3 acquire, 4-7 acquire nothing (coverage flat < 50%), turn 8 fetches two target-market documents
    that meet the base: research ends by meeting the base after turn 8, never with extension_exhausted."""
    script = ([fetch("a", EU), fetch("b", tail.FORUM), fetch("c", BLOG)]
              + [search(f"s{i}", f"corolla touring sports query {i}") for i in range(4, 8)]
              + [fetch("d", tail.CARTUBE, tail.LAUNCH), say({"done": True, "reason": "never reached"})])
    client = PhaseClient(script)
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False, max_steps=6)
    turns = [e for e in events if e["kind"] == "primary_research_turn"]
    assert [bool(t["artifacts"]) for t in turns] == [True, True, True, False, False, False, False, True]
    assert {t["scoped_coverage_pct"] for t in turns[2:7]} == {turns[2]["scoped_coverage_pct"]} \
        and turns[2]["scoped_coverage_pct"] < 50
    assert result["research_steps"] == 8 and result["stop_reason"] == "max_steps"
    primary = result["primary_research"]
    assert primary["stop_reason"] == "max_turns" and primary["minimum_acquisition_met"]
    assert primary["extension_exhausted"] is None
    assert not any(e["kind"] == "primary_research_extension_exhausted" for e in events)


@pytest.mark.sweep_mode("legacy")   # encodes the legacy tool-loop sweep
def test_two_extension_turns_without_a_new_document_end_research(tmp_path):
    script = [fetch("a", EU), search("s2", "corolla q2"), search("s3", "corolla q3"), search("s4", "corolla q4"),
              search("s5", "never reached")]
    client = PhaseClient(script)
    result, events = run(tmp_path, client, acquisition_mode="contract", max_steps=2)
    assert result["research_steps"] == 4 and result["stop_reason"] == "extension_exhausted"
    assert result["primary_research"]["stop_reason"] == "under_acquired_exhausted"
    assert result["status"] == "under_acquired_finalized"
    exhausted = next(e for e in events if e["kind"] == "primary_research_extension_exhausted")
    assert (exhausted["turn"], exhausted["reason"], exhausted["reason_code"]) == (4, "no_new_usable_document", 2)
    assert exhausted["min_documents"] == 3 and exhausted["useful_documents"] == 1
    # the run continues exactly as after any other stop: harvest, sweep, recovery, finalization
    kinds = [e["kind"] for e in events]
    assert kinds.index("primary_research_extension_exhausted") < kinds.index("deterministic_harvest_summary")
    assert "sweep" in client.kinds and "recovery" in client.kinds and client.kinds[-1] == "final"
    assert result["field_recovery"]["attempt_count"] >= 1


def test_an_extension_turn_with_only_blocked_calls_ends_research_at_once(tmp_path):
    script = [fetch("a", EU), search("s2", "corolla q2"),
              turn(_call("x", "store_evidence", {"field": "torque_nm", "value": 1, "source_url": EU, "quote": "q"})),
              search("s4", "never reached")]
    client = PhaseClient(script)
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False, max_steps=2)
    assert result["research_steps"] == 3 and result["stop_reason"] == "extension_exhausted"
    exhausted = next(e for e in events if e["kind"] == "primary_research_extension_exhausted")
    assert (exhausted["reason"], exhausted["reason_code"], exhausted["discovery_calls"]) == ("no_discovery_calls", 1, 0)


def test_stalls_before_the_ceiling_never_count_toward_extension_futility(tmp_path):
    """Pre-ceiling stall turns and the ceiling turn itself are not extension turns: the first extension turn
    without a new document leaves research running."""
    script = [fetch("a", EU)] + [search(f"s{i}", f"corolla q{i}") for i in range(2, 6)] + [
        fetch("d", tail.CARTUBE, tail.LAUNCH), say({"done": True, "reason": "never reached"})]
    result, events = run(tmp_path, PhaseClient(script), acquisition_mode="contract", field_recovery_enabled=False,
                         max_steps=4)
    assert result["research_steps"] == 6 and result["stop_reason"] == "max_steps"


def test_legacy_mode_keeps_extending_to_the_hard_ceiling(tmp_path):
    script = [fetch("a", EU)] + [search(f"s{i}", f"corolla q{i}") for i in range(2, 7)] + [
        say({"summary": "never reached", "fields": {}})]
    result, events = run(tmp_path, PhaseClient(script), acquisition_mode="legacy", field_recovery_enabled=False,
                         max_steps=2, primary_research_hard_max_turns=5)
    assert result["research_steps"] == 5 and result["stop_reason"] == "max_steps"
    assert result["primary_research"]["stop_reason"] == "hard_max_turns_under_acquired"


# --- A5: missing source categories (a hint only) ---------------------------------------------------------------

def test_missing_source_categories_lists_uncovered_clusters_with_their_source_type():
    specs = [{"name": "torque_nm", "recovery_cluster": "performance", "market_sensitivity": "low"},
             {"name": "power_hp", "recovery_cluster": "performance", "market_sensitivity": "low"},
             {"name": "list_price", "recovery_cluster": "commercial", "market_sensitivity": "high"},
             {"name": "warranty_years", "recovery_cluster": "warranty", "market_sensitivity": "high"},
             {"name": "fuel_tank_l", "recovery_cluster": "technical_spec", "market_sensitivity": "low",
              "applicable": False}]
    snap = {"open_fields": {"torque_nm", "list_price", "warranty_years", "fuel_tank_l"},
            "scoped_candidate_fields": {"power_hp"}}
    missing = A.missing_source_categories(snap, specs, "IL")
    assert [(m["cluster"], m["source_type"]) for m in missing] == [
        ("commercial", A.target_market_source("IL")), ("warranty", A.target_market_source("IL"))]
    assert A.missing_categories_note(missing) == (
        "Source categories still missing: commercial: " + A.target_market_source("IL") + "; warranty: "
        + A.target_market_source("IL") + ".")
    assert A.missing_categories_note([]) == ""
    assert A.missing_source_categories({"open_fields": set(), "scoped_candidate_fields": set()}, specs) == []


def test_the_missing_categories_note_reaches_the_model_and_never_changes_a_stop(tmp_path, monkeypatch):
    script = [fetch("a", EU), search("s2", "corolla q2"), search("s3", "corolla q3"), search("s4", "corolla q4"),
              search("s5", "x")]
    client = PhaseClient(list(script))
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False, max_steps=2)
    notes = [m["content"] for r in client.requests for m in r["messages"][-1:] if m["role"] == "tool"]
    assert any("Source categories still missing:" in n and A.target_market_source("IL") in n for n in notes)
    import src.acquisition as acq

    monkeypatch.setattr(acq, "missing_source_categories", lambda *a, **k: [])
    quiet = PhaseClient(list(script))
    silent, _ = run(tmp_path / "quiet", quiet, acquisition_mode="contract", field_recovery_enabled=False, max_steps=2)
    assert not any("Source categories still missing" in m["content"] for r in quiet.requests
                   for m in r["messages"] if m["role"] == "tool")
    assert (silent["research_steps"], silent["stop_reason"]) == (result["research_steps"], result["stop_reason"])


# --- B1: sweep failure isolation ----------------------------------------------------------------------------------

def _timeout() -> GLMError:
    exc = GLMError("ReadTimeout: read timed out (240 s)")
    exc.timeout, exc.attempts = True, 1
    return exc


@pytest.mark.sweep_mode("legacy")   # encodes the legacy tool-loop sweep
def test_a_failed_sweep_chunk_never_ends_the_sweep(tmp_path):
    def sweep(call_number):
        if call_number == 1:
            raise _timeout()
        return say({"reviewed": []})

    client = PhaseClient([fetch("a", EU, tail.CARTUBE), say({"done": True, "reason": "x"})], sweep=sweep)
    result, events = run(tmp_path, client, acquisition_mode="contract", document_sweep_max_fields=4, **GATE_OFF)
    sweep_summary = result["document_sweep"]
    chunks = sweep_summary["document_sweep_chunk_details"]
    assert len(chunks) >= 2 and chunks[0]["failed"] and chunks[0]["error"].startswith("GLMError")
    assert chunks[1]["model_calls"] == 1 and not chunks[1]["failed"]
    assert sweep_summary["failed_chunk_fields"] == chunks[0]["fields"]
    assert sweep_summary["error"] == chunks[0]["error"] and sweep_summary["chunk_errors"] == [chunks[0]["error"]]
    failed = next(e for e in events if e["kind"] == "document_sweep_chunk_failed")
    assert failed["fields"] == chunks[0]["fields"] and failed["timeout"] is True
    # the failed chunk's fields flow to recovery as any open field
    queue = next(e for e in events if e["kind"] == "field_retry_queue")
    assert set(chunks[0]["fields"]) <= set(queue["fields"])
    assert result["field_recovery"]["attempt_count"] >= 1 and client.kinds[-1] == "final"


def test_the_pipeline_marks_a_sweep_failed_only_when_every_chunk_failed():
    from src.runstate.pipeline import pipeline_from_events

    def ev(seq, kind, **data):
        return {"seq": seq, "kind": kind, "ts": f"2026-01-01T00:00:{seq:02d}+00:00", **data}

    base = [ev(1, "run_started", agent_config={}), ev(2, "research_stopped", reason="max_steps"),
            ev(3, "deterministic_harvest_summary"), ev(4, "document_sweep_started", chunk={"index": 1, "of": 2}),
            ev(5, "document_sweep_failed", error="GLMError: timeout", chunk={"index": 1, "of": 2}),
            ev(6, "document_sweep_started", chunk={"index": 2, "of": 2})]
    ok = pipeline_from_events(base + [ev(7, "document_sweep_finished", document_sweep_calls=1)])
    assert ok.stages["sweep"] == "done" and "chunk 1/2 failed" in ok.stage_notes["sweep"]
    failed = pipeline_from_events(base[:5] + [ev(7, "document_sweep_finished", document_sweep_calls=0)])
    assert failed.stages["sweep"] == "failed"


# --- B2: per-phase max attempts ----------------------------------------------------------------------------------

class TimeoutSession:
    def __init__(self):
        self.posts = 0

    def post(self, url, headers=None, data=None, timeout=None):
        self.posts += 1
        raise requests.ReadTimeout("slow")


def test_a_timed_out_sweep_request_is_attempted_once_and_research_keeps_the_global_value(tmp_path):
    session = TimeoutSession()
    client = GLMClient(GLMSettings(api_key="k", model="m", chat_max_attempts=2), session=session,
                       sleeper=lambda s: None)
    caller = ModelCaller(client, RunLog(tmp_path, "b", "1"), AgentConfig())
    with pytest.raises(GLMError) as sweep:
        caller([{"role": "user", "content": "x"}], phase="document_sweep")
    assert session.posts == 1 and sweep.value.attempts == 1 and sweep.value.timeout
    with pytest.raises(GLMError) as research:
        caller([{"role": "user", "content": "x"}], phase="research")
    assert session.posts == 3 and research.value.attempts == 2
    overridden = ModelCaller(client, RunLog(tmp_path, "b", "2"),
                             AgentConfig(phase_settings={"document_sweep": {"max_attempts": 3}}))
    with pytest.raises(GLMError):
        overridden([{"role": "user", "content": "x"}], phase="document_sweep")
    assert session.posts == 6


def test_phase_max_attempts_parse_and_describe():
    env = {"GLM_DOCUMENT_SWEEP_MAX_ATTEMPTS": "2", "GLM_RECOVERY_MAX_ATTEMPTS": "0", "GLM_RESEARCH_MAX_ATTEMPTS": "x"}
    assert phase_settings_from_env(env.get) == {"document_sweep": {"max_attempts": 2}}
    defaults = {"research_model": "m", "finalizer_model": "m", "timeout_s": 240.0, "chat_max_attempts": 2}
    plain = describe(AgentConfig(), defaults)
    assert plain["document_sweep"]["max_attempts"] == 1                    # the built-in sweep default
    assert plain["research"]["max_attempts"] == plain["recovery"]["max_attempts"] == 2      # inherit the global
    assert plain["document_sweep"]["overridden"] == []
    configured = AgentConfig(phase_settings=phase_settings_from_env(env.get))
    assert describe(configured, defaults)["document_sweep"]["max_attempts"] == 2
    assert for_phase(configured, "document_sweep")["max_attempts"] == 2
    assert for_phase(AgentConfig(), "research")["max_attempts"] is None


def test_effective_config_records_the_sweep_attempts():
    class Client:
        model = "glm-5.3-flash"
        settings = GLMSettings(api_key="k", model="glm-5.3-flash", chat_max_attempts=2)

    phases = effective_glm_config(Client(), AgentConfig(), ToolConfig())["phase_settings"]
    assert phases["document_sweep"]["max_attempts"] == 1 and phases["research"]["max_attempts"] == 2


def test_new_sweep_packet_defaults():
    config = AgentConfig()
    assert (config.document_sweep_max_fields, config.document_sweep_max_candidates) == (12, 16)
    env = agent_config_from_env({"DOCUMENT_SWEEP_MAX_FIELDS": "30", "DOCUMENT_SWEEP_MAX_CANDIDATES": "48"}.get)
    assert (env.document_sweep_max_fields, env.document_sweep_max_candidates) == (30, 48)


# --- C1 / C2: telemetry without fake zeros ------------------------------------------------------------------------

def test_the_status_panel_shows_unknown_counts_as_a_dash():
    from src.runstate.model import RunRecord
    from src.ui.dashboard import status_panel_rows

    record = RunRecord(run_id="r1", status="COMPLETED", target={"record_ids": ["1"]})
    unknown = {"counters": {"sources": 3, "candidates": None, "model_calls": 4, "searches": None}}
    rows = dict(status_panel_rows(record, [unknown]))
    assert rows["Candidates"] == "—" and rows["Search calls"] == "—" and rows["Sources"] == 3
    known = {"counters": {"sources": 1, "candidates": 85, "model_calls": 1, "searches": 2}}
    rows = dict(status_panel_rows(record, [unknown, known]))
    assert rows["Candidates"] == 85 and rows["Search calls"] == 2


def test_pipeline_candidates_fall_back_to_the_live_acquisition_count():
    from src.runstate.pipeline import pipeline_from_events

    turn_event = {"seq": 2, "kind": "primary_research_turn", "turn": 1,
                  "state_after": {"official_documents": 1, "official_urls_discovered": 4, "candidates": 85}}
    live = pipeline_from_events([{"seq": 1, "kind": "run_started"}, turn_event]).counters()
    assert live["candidates"] == 85 and live["official_sources"] == 1 and live["official_urls_discovered"] == 4
    harvested = pipeline_from_events([{"seq": 1, "kind": "run_started"}, turn_event,
                                      {"seq": 3, "kind": "deterministic_harvest_summary",
                                       "candidate_count_total": 90}]).counters()
    assert harvested["candidates"] == 90
    assert pipeline_from_events([{"seq": 1, "kind": "run_started"}]).counters()["candidates"] is None
    old = pipeline_from_events([{"seq": 1, "kind": "run_started"},
                                {"seq": 2, "kind": "primary_research_turn", "turn": 1,
                                 "state_after": {"official_documents": 16}}]).counters()
    assert old["official_sources"] == 16 and old["official_urls_discovered"] is None


def test_official_labels_never_relabel_an_old_run():
    from src.ui.diagnostics_view import _official_pairs

    assert _official_pairs({"official_sources": 16}, {}) == [("Official", 16)]
    assert _official_pairs({"official_sources": 6, "official_urls_discovered": 16}, {}) == [
        ("Official docs (fetched)", 6), ("Official URLs (discovered)", 16)]
    assert "Official documents: 16" in D.summary_text({"official_documents": 16}, {})
    text = D.summary_text({"official_documents": 6, "official_urls_discovered": 16}, {})
    assert "Official docs (fetched): 6" in text and "Official URLs (discovered): 16" in text


# --- C3 / C4: end-to-end diagnostics ------------------------------------------------------------------------------

@pytest.fixture
def finished_run(tmp_path):
    script = [fetch("a", EU), search("s2", "corolla q2"), search("s3", "corolla q3"), search("s4", "corolla q4")]
    client = PhaseClient(script)
    result, events = run(tmp_path, client, acquisition_mode="contract", max_steps=2)
    return result, events


def test_end_to_end_diagnostics_of_a_completed_run(finished_run):
    result, events = finished_run
    diag = D.vehicle_diagnostics(events, run_id="b", result=result)
    assert diag["complete"] and not diag["interrupted"] and diag["run_status"] == "under_acquired_finalized"
    final = diag["final_fields"]
    assert sum(final["counts"].values()) == final["fields"] == len(tail.FIELDS)
    assert final["ok_fields"] == sorted(f for f, s in result["research_bundle"]["field_states"].items()
                                        if s["state"] == "ok")
    rec = diag["recovery"]
    assert rec["ran"] and rec["attempts"] == result["field_recovery"]["attempt_count"] >= 1
    assert rec["model_calls"] == result["usage_field_recovery"]["model_calls"]
    assert rec["fields_open_before"] >= rec["fields_open_after"] and rec["fields_resolved"] is not None
    totals = diag["totals"]
    assert totals["model_calls"] == result["usage"]["model_calls"]
    assert totals["by_phase"]["research"]["input_tokens"] == result["usage_research"]["prompt_tokens"]
    assert totals["wall_time_s"] == result["duration_s"] and totals["cost_source"] == "result.json"
    acq = diag["acquisition"]["summary"]
    assert acq["turn_reached_min_base"] is None and acq["tokens_until_min_base"] is None
    assert acq["tool_blocked"] == 0 and acq["extension_exhausted"] and acq["official_urls_discovered"] == 1
    assert diag["configuration"]["acquisition_mode"] == "contract"
    assert diag["configuration"]["sweep_max_attempts"] == 1 and diag["configuration"]["sweep_max_fields"] == 12
    row = D.vehicle_row(diag)
    assert row["final_ok"] == final["counts"]["ok"] and row["cfg_acquisition_mode"] == "contract"
    assert row["rec_model_calls"] == rec["model_calls"] and row["interrupted"] is False


def test_turn_reached_min_base_and_tokens_until_then(tmp_path):
    client = PhaseClient([fetch("a", EU), fetch("b", tail.FORUM), fetch("c", tail.CARTUBE),
                          say({"done": True, "reason": "x"})])
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False, max_steps=6)
    acq = D.vehicle_diagnostics(events, result=result)["acquisition"]["summary"]
    assert acq["turn_reached_min_base"] == 3 and acq["tokens_until_min_base"] == 3 * 110


def test_interrupted_runs_are_flagged_and_excluded_from_end_to_end_means(finished_run):
    result, events = finished_run
    complete = D.vehicle_diagnostics(events, run_id="b", result=result)
    cut = [e for e in events if e["kind"] != "run_finished"]
    interrupted = D.vehicle_diagnostics(cut, run_id="b2")
    assert interrupted["interrupted"] and not interrupted["complete"]
    agg = D.aggregate([complete, interrupted])
    assert agg["end_to_end"]["runs"] == 1 and agg["interrupted"] == {"runs": 1, "record_ids": ["38626"]}
    assert agg["end_to_end"]["final_ok"]["n"] == 1
    assert agg["end_to_end"]["total_model_calls"]["mean"] == complete["totals"]["model_calls"]
    assert len(agg["per_vehicle"]) == 2


def test_by_config_groups_runs_by_acquisition_mode(finished_run):
    result, events = finished_run
    contract = D.vehicle_diagnostics(events, run_id="b", result=result)
    legacy = copy.deepcopy(contract)
    legacy["configuration"]["acquisition_mode"] = "legacy"
    legacy["totals"]["model_calls"] = 99
    agg = D.aggregate([contract, legacy, copy.deepcopy(contract)])
    groups = agg["by_config"]
    assert len(groups) == 2
    by_mode = {g["configuration"]["acquisition_mode"]: g for g in groups.values()}
    assert by_mode["contract"]["vehicles"] == 2 and by_mode["legacy"]["vehicles"] == 1
    assert by_mode["legacy"]["end_to_end"]["total_model_calls"]["mean"] == 99
    assert "per_vehicle" not in by_mode["contract"] and "end_to_end" in by_mode["contract"]


def test_old_runs_still_aggregate(finished_run):
    """diagnostics.json written before the end-to-end sections existed (no configuration / totals keys)."""
    result, events = finished_run
    diag = D.vehicle_diagnostics(events, result=result)
    for key in ("configuration", "recovery", "final_fields", "totals", "interrupted"):
        diag.pop(key)
    agg = D.aggregate([diag])
    assert list(agg["by_config"]) == ["unknown"] and agg["per_vehicle"][0]["final_ok"] is None
    assert D.run_configuration([{"kind": "run_started", "agent_config": {}}])["acquisition_mode"] == "legacy"


def test_write_benchmark_includes_by_config(tmp_path, finished_run):
    result, _ = finished_run
    run_dir = tmp_path / "runs" / "b" / "38626"
    assert (run_dir / "events.jsonl").is_file()
    (run_dir / "result.json").write_text(json.dumps(result, default=str), "utf-8")
    out = D.write_benchmark(tmp_path / "runs", out_dir=tmp_path / "bench", rebuild=True)
    assert out["by_config"] and out["end_to_end"]["runs"] == 1
    written = json.loads((tmp_path / "bench" / "benchmark.json").read_text("utf-8"))
    assert written["by_config"] and (tmp_path / "bench" / "per_vehicle.csv").is_file()
