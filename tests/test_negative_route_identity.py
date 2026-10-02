"""Negative route identity includes every material routing constraint. A search route is the provider route
(normalized query, normalized domain): "Corolla ground clearance" on toyota.co.uk and on toyota.co.il are different
research routes, and so is the same query without a domain. A failed UK search never suppresses a new IL search;
negative memory stays scheduling only."""

import json

from test_reuse_feedback import SPECS
from test_tools_smoke import _call

from src.agent import AgentConfig, ToolSession, _route_guard, _routes_of
from src.field_recovery import current_evaluation
from src.research_memory import (ROUTE_VERSION, ResearchMemory, normalize_route, provider_routes, route_label,
                                 route_signature)
from src.storage.run_log import RunLog

QUERY = "Corolla ground clearance"
FIELD = "ground_clearance_mm"


class _Provider:
    model = "glm-test"
    settings = type("S", (), {"search_engine": "fake"})()

    def __init__(self):
        self.calls = []

    def web_search(self, query, count=8, domain=None):
        self.calls.append((query, domain))
        return []


def _dead(*routes, field=FIELD):
    """Negative memory for `field` exactly as recovery records it (provider routes of executed calls)."""
    recorded = [r for tool, args in routes for r in provider_routes(tool, args)]
    return {field: {"routes": recorded, "attempts": 1}}


def _session(tmp_path, make_ctx, negative, open_fields=(FIELD,), default_domains=None):
    ctx = make_ctx()
    ctx.glm = _Provider()
    session = ToolSession(ctx, RunLog(tmp_path, "b", "1"), AgentConfig())
    session.route_guard = _route_guard(negative, list(open_fields), SPECS, list(SPECS.values()), default_domains)
    return session, ctx


def _run(session, name, args):
    messages = []
    session.execute([_call("c", name, args)], messages, phase="field_recovery")
    return json.loads(messages[-1]["content"].split("\n[operational note]")[0])


def test_signatures_include_the_normalized_domain():
    uk, il = route_signature("search_web", QUERY, "toyota.co.uk"), route_signature("search_web", QUERY, "toyota.co.il")
    bare = route_signature("search_web", QUERY)
    assert len({uk, il, bare}) == 3
    # same query + same domain: equivalent, whatever the spelling of either
    assert route_signature("search_web", "  corolla GROUND clearance ", "https://www.Toyota.co.uk/") == uk
    assert normalize_route("search_web", QUERY, "toyota.co.uk") == ("search", "corolla ground clearance",
                                                                    "toyota.co.uk")
    # the provider route is the same whichever search tool made it
    [official] = provider_routes("search_official_domains", {"query": QUERY, "domains": ["toyota.co.uk"]})
    assert official["signature"] == uk and official["domain"] == "toyota.co.uk"
    assert official["route_version"] == ROUTE_VERSION
    assert route_label(official) == f"{QUERY} (domain: toyota.co.uk)"
    assert route_signature("search_web", "Corolla boot volume", "toyota.co.uk") != uk          # different query


def test_domain_search_is_one_provider_route_per_domain():
    routes = provider_routes("search_official_domains", {"query": QUERY, "domains": ["toyota.co.uk", "toyota.co.il"]})
    assert [r["domain"] for r in routes] == ["toyota.co.uk", "toyota.co.il"]
    defaults = provider_routes("search_official_domains", {"query": QUERY}, ["toyota.co.il"])
    assert [r["domain"] for r in defaults] == ["toyota.co.il"]                     # the manufacturer's defaults
    [plain] = provider_routes("search_official_domains", {"query": QUERY})        # no domains: a plain web search
    assert "domain" not in plain and plain["signature"] == route_signature("search_web", QUERY)
    # a domain whose provider search errored says nothing about its route: not recorded
    result = {"per_domain": {"toyota.co.uk": {"error": "HTTP 429"}, "toyota.co.il": {"result_count": 0}}}
    calls = [{"name": "search_official_domains", "arguments": json.dumps({"query": QUERY}), "route_failed": False,
              "routes": provider_routes("search_official_domains",
                                        {"query": QUERY, "domains": ["toyota.co.uk", "toyota.co.il"]}, None, result)}]
    assert [r["domain"] for r in _routes_of(calls)] == ["toyota.co.il"]


def test_same_query_same_domain_is_refused(tmp_path, make_ctx):
    session, ctx = _session(tmp_path, make_ctx, _dead(("search_web", {"query": QUERY, "domain": "toyota.co.uk"})))
    result = _run(session, "search_web", {"query": "corolla  ground CLEARANCE", "domain": "www.toyota.co.uk"})
    assert result["error"] == "known_unproductive_route" and result["domains"] == ["toyota.co.uk"]
    assert ctx.glm.calls == [] and session.route_blocks == 1


def test_a_failed_uk_search_never_suppresses_a_new_il_search(tmp_path, make_ctx):
    session, ctx = _session(tmp_path, make_ctx, _dead(("search_web", {"query": QUERY, "domain": "toyota.co.uk"})))
    result = _run(session, "search_web", {"query": QUERY, "domain": "toyota.co.il"})
    assert "error" not in result and ctx.glm.calls == [(QUERY, "toyota.co.il")] and session.route_blocks == 0


def test_no_domain_and_domain_specific_searches_are_not_equivalent(tmp_path, make_ctx):
    # a failed domain-specific search does not suppress the open web search ...
    session, ctx = _session(tmp_path, make_ctx, _dead(("search_web", {"query": QUERY, "domain": "toyota.co.uk"})))
    assert "error" not in _run(session, "search_web", {"query": QUERY})
    assert ctx.glm.calls == [(QUERY, None)]
    # ... and a failed open web search does not suppress a domain-specific one
    session, ctx = _session(tmp_path / "2", make_ctx, _dead(("search_web", {"query": QUERY})))
    assert "error" not in _run(session, "search_web", {"query": QUERY, "domain": "toyota.co.il"})
    assert ctx.glm.calls == [(QUERY, "toyota.co.il")]
    assert _run(session, "search_web", {"query": QUERY})["error"] == "known_unproductive_route"


def test_official_domain_search_failed_domain_a_never_suppresses_new_domain_b(tmp_path, make_ctx):
    dead = _dead(("search_official_domains", {"query": QUERY, "domains": ["toyota.co.uk"]}))
    session, ctx = _session(tmp_path, make_ctx, dead)
    # B was never tried: the call runs (and searches B)
    assert "error" not in _run(session, "search_official_domains", {"query": QUERY,
                                                                       "domains": ["toyota.co.uk", "toyota.co.il"]})
    assert (QUERY, "toyota.co.il") in ctx.glm.calls and session.route_blocks == 0
    assert "error" not in _run(session, "search_official_domains", {"query": QUERY, "domains": ["toyota.co.il"]})
    # only A, which failed: refused, as is the equivalent search_web provider route
    session, ctx = _session(tmp_path / "2", make_ctx, dead)
    assert _run(session, "search_official_domains", {"query": QUERY, "domains": ["toyota.co.uk"]})["error"] == \
        "known_unproductive_route"
    assert _run(session, "search_web", {"query": QUERY, "domain": "toyota.co.uk"})["error"] == \
        "known_unproductive_route"
    assert ctx.glm.calls == [] and session.route_blocks == 2


def test_official_domain_search_uses_the_default_domains_for_its_identity(tmp_path, make_ctx):
    dead = _dead(("search_official_domains", {"query": QUERY, "domains": ["toyota.co.uk"]}))
    session, ctx = _session(tmp_path, make_ctx, dead, default_domains=["toyota.co.uk"])
    assert _run(session, "search_official_domains", {"query": QUERY})["error"] == "known_unproductive_route"
    session, ctx = _session(tmp_path / "2", make_ctx, dead, default_domains=["toyota.co.il"])
    assert _run(session, "search_official_domains", {"query": QUERY}).get("error") != "known_unproductive_route"


def test_a_different_query_is_allowed(tmp_path, make_ctx):
    session, ctx = _session(tmp_path, make_ctx, _dead(("search_web", {"query": QUERY, "domain": "toyota.co.uk"})))
    assert "error" not in _run(session, "search_web", {"query": "Corolla minimum ground clearance mm",
                                                       "domain": "toyota.co.uk"})
    assert session.route_blocks == 0


def test_recorded_routes_round_trip_and_ambiguous_old_records_refuse_nothing(tmp_path, make_ctx):
    memory = ResearchMemory(tmp_path / "memory")
    uk = provider_routes("search_web", {"query": QUERY, "domain": "toyota.co.uk"})
    legacy = {"tool": "search_web", "route": QUERY, "signature": "legacy"}       # no domain, no route version
    memory.record_routes([{"scope_key": "k", "field": FIELD, "outcome": "no_new_material",
                           "routes": uk + [legacy]}], "earlier-run")
    negative = memory.negative_routes({FIELD: "k"})
    assert [(r["route"], r.get("domain")) for r in negative[FIELD]["routes"]] == [(QUERY, "toyota.co.uk")]
    session, ctx = _session(tmp_path, make_ctx, negative)
    assert "error" not in _run(session, "search_web", {"query": QUERY})                   # the legacy record: ignored
    assert "error" not in _run(session, "search_web", {"query": QUERY, "domain": "toyota.co.il"})
    assert _run(session, "search_web", {"query": QUERY, "domain": "toyota.co.uk"})["error"] == \
        "known_unproductive_route"
    assert ctx.glm.calls == [(QUERY, None), (QUERY, "toyota.co.il")]


def test_refusals_are_scheduling_only(tmp_path, make_ctx):
    """A refused route creates no evidence, changes no field state, marks nothing not_applicable and resolves no
    conflict: the evaluator over the session's events is the same with and without the refusal."""
    il = {"evidence_id": "e1", "field": FIELD, "value": 135, "market": "IL", "variant_match": "exact",
          "binding_level": "exact_technical_variant", "binding_requirement": "exact_technical_variant",
          "admission_status": "accepted"}
    other = {**il, "evidence_id": "e2", "value": 150}
    events = [{"kind": "run_started", "seq": 0, "target_market": "IL"},
              {"kind": "evidence", "seq": 1, "evidence": il}, {"kind": "evidence", "seq": 2, "evidence": other}]
    before = current_evaluation(events, [SPECS[FIELD]])[0]
    session, ctx = _session(tmp_path, make_ctx, _dead(("search_web", {"query": QUERY, "domain": "toyota.co.uk"})))
    assert _run(session, "search_web", {"query": QUERY, "domain": "toyota.co.uk"})["error"] == \
        "known_unproductive_route"
    assert ctx.evidence.items == [] and session.ctx.counters["evidence_rejected"] == 0
    after = current_evaluation(events, [SPECS[FIELD]])[0]
    assert after == before and after["state"] == "conflicting"
    assert "research_memory" not in json.dumps(after)
