"""Negative route identity is never looser than the operation it stands for. A search route is the provider route
(backend, engine, normalized query, normalized domain, effective result count): the search cache's own key. A fetch
route is its mechanism (fetch_url, fetch_pdf, render_page) and URL, plus a render's effective wait. Old or
incomplete records refuse nothing. Negative memory stays scheduling only and never suppresses research that can
return new material: another provider, a larger result window, another domain or another fetch mechanism runs."""

import json

from test_reuse_feedback import SPECS
from test_tools_smoke import _call

from src.agent import AgentConfig, ToolSession, _route_guard, _routes_of
from src.field_recovery import current_evaluation
from src.research_memory import (ROUTE_VERSION, ResearchMemory, provider_routes, route_applies, route_identity,
                                 route_label, route_signature)
from src.storage.run_log import RunLog
from src.tools.search import planned_searches, search_key, search_provider

QUERY = "Corolla ground clearance"
FIELD = "ground_clearance_mm"
URL = "https://www.toyota.co.uk/new-cars/corolla/specs"
GLM = ("glm", "fake")


class _Provider:
    model = "glm-test"
    settings = type("S", (), {"search_engine": "fake"})()

    def __init__(self):
        self.calls = []

    def web_search(self, query, count=8, domain=None):
        self.calls.append((query, count, domain))
        return []


def routes(tool, args, provider=GLM, **kw):
    return provider_routes(tool, args, provider=provider, **kw)


def sig(tool, args, provider=GLM, **kw):
    [route] = routes(tool, args, provider, **kw)
    return route["signature"]


def _dead(*calls, field=FIELD, provider=GLM):
    """Negative memory for `field` exactly as recovery records it (the provider routes of the failed calls)."""
    return {field: {"routes": [r for tool, args in calls for r in routes(tool, args, provider)], "attempts": 1}}


def _session(tmp_path, make_ctx, negative, backend="glm", default_domains=None):
    ctx = make_ctx()
    ctx.glm = _Provider()
    ctx.config.search_backend = backend
    session = ToolSession(ctx, RunLog(tmp_path, "b", "1"), AgentConfig())
    session.route_guard = _route_guard(negative, [FIELD], SPECS, list(SPECS.values()), default_domains,
                                       search_provider(ctx))
    return session, ctx


def _run(session, name, args):
    messages = []
    session.execute([_call("c", name, args)], messages, phase="field_recovery")
    return json.loads(messages[-1]["content"].split("\n[operational note]")[0])


def refused(result):
    return result.get("error") == "known_unproductive_route"


# --- search route identity --------------------------------------------------------------------------------------

def test_same_query_domain_count_and_provider_is_equivalent(tmp_path, make_ctx):
    uk = {"query": QUERY, "domain": "toyota.co.uk"}
    assert sig("search_web", uk) == sig("search_web", {"query": "  corolla GROUND clearance ",
                                                       "domain": "https://www.Toyota.co.uk/", "max_results": 8})
    session, ctx = _session(tmp_path, make_ctx, _dead(("search_web", uk)))
    assert refused(_run(session, "search_web", {**uk, "max_results": 8}))
    assert ctx.glm.calls == [] and session.route_blocks == 1


def test_omitted_max_results_is_the_effective_default_8():
    omitted = sig("search_web", {"query": QUERY, "domain": "toyota.co.uk"})
    assert omitted == sig("search_web", {"query": QUERY, "domain": "toyota.co.uk", "max_results": 8})
    assert omitted == sig("search_web", {"query": QUERY, "domain": "toyota.co.uk", "max_results": "8"})
    # clamped like the tool: 50 executes as 20, 0 as the default 8
    assert sig("search_web", {"query": QUERY, "max_results": 50}) == sig("search_web", {"query": QUERY,
                                                                                         "max_results": 20})
    assert sig("search_web", {"query": QUERY, "max_results": 0}) == sig("search_web", {"query": QUERY})


def test_a_larger_result_window_is_not_equivalent_and_runs(tmp_path, make_ctx):
    eight = {"query": QUERY, "domain": "toyota.co.uk", "max_results": 8}
    assert sig("search_web", eight) != sig("search_web", {**eight, "max_results": 20})
    session, ctx = _session(tmp_path, make_ctx, _dead(("search_web", eight)))
    assert not refused(_run(session, "search_web", {**eight, "max_results": 20}))
    assert ctx.glm.calls == [(QUERY, 20, "toyota.co.uk")] and session.route_blocks == 0


def test_another_search_provider_is_not_equivalent(tmp_path, make_ctx):
    uk = {"query": QUERY, "domain": "toyota.co.uk"}
    assert sig("search_web", uk, ("duckduckgo", "fake")) != sig("search_web", uk, ("glm", "fake"))
    assert sig("search_web", uk, ("glm", "search_pro")) != sig("search_web", uk, ("glm", "search_std"))
    # a GLM failure never refuses the same search on DuckDuckGo: the call goes to the provider
    session, ctx = _session(tmp_path, make_ctx, _dead(("search_web", uk), provider=("glm", "fake")),
                            backend="duckduckgo")
    misses = ctx.counters["search_cache_misses"]
    assert not refused(_run(session, "search_web", uk))
    assert ctx.counters["search_cache_misses"] == misses + 1 and session.route_blocks == 0


def test_another_domain_is_not_equivalent(tmp_path, make_ctx):
    assert sig("search_web", {"query": QUERY, "domain": "toyota.co.uk"}) != \
        sig("search_web", {"query": QUERY, "domain": "toyota.co.il"})
    session, ctx = _session(tmp_path, make_ctx, _dead(("search_web", {"query": QUERY, "domain": "toyota.co.uk"})))
    assert not refused(_run(session, "search_web", {"query": QUERY, "domain": "toyota.co.il"}))
    assert ctx.glm.calls == [(QUERY, 8, "toyota.co.il")]


def test_open_web_and_domain_specific_searches_are_not_equivalent(tmp_path, make_ctx):
    session, ctx = _session(tmp_path, make_ctx, _dead(("search_web", {"query": QUERY, "domain": "toyota.co.uk"})))
    assert not refused(_run(session, "search_web", {"query": QUERY}))
    session, ctx = _session(tmp_path / "2", make_ctx, _dead(("search_web", {"query": QUERY})))
    assert not refused(_run(session, "search_web", {"query": QUERY, "domain": "toyota.co.il"}))
    assert refused(_run(session, "search_web", {"query": QUERY}))
    assert ctx.glm.calls == [(QUERY, 8, "toyota.co.il")]


def test_a_different_query_is_allowed(tmp_path, make_ctx):
    session, ctx = _session(tmp_path, make_ctx, _dead(("search_web", {"query": QUERY, "domain": "toyota.co.uk"})))
    assert not refused(_run(session, "search_web", {"query": "Corolla minimum ground clearance mm",
                                                    "domain": "toyota.co.uk"}))


def test_route_identity_is_the_search_cache_identity(tmp_path, make_ctx):
    """Every search the tool executes is one planned route with the same (provider, query, count, domain) as its
    search cache key, so the two identities cannot drift apart."""
    ctx = make_ctx()
    ctx.glm = _Provider()
    session = ToolSession(ctx, RunLog(tmp_path, "b", "1"), AgentConfig())
    for name, args in (("search_web", {"query": QUERY, "max_results": 20, "domain": "toyota.co.uk"}),
                       ("search_web", {"query": "corolla boot"}),
                       ("search_official_domains", {"query": QUERY, "domains": ["toyota.co.uk", "toyota.co.il"]})):
        ctx.glm.calls.clear()
        _run(session, name, args)
        planned = planned_searches(name, args)
        assert ctx.glm.calls == planned                                         # what actually ran
        assert all(ctx.cache.has_search(search_key(ctx, q, c, d)) for q, c, d in planned)
        recorded = session.tool_calls[-1]["routes"]
        assert [(r["route"], r["count"], r["domain"] or None, r["backend"], r["engine"]) for r in recorded] == \
            [(q, c, d, *search_provider(ctx)) for q, c, d in planned]


# --- search_official_domains: one provider route per domain ------------------------------------------------------

def test_official_domains_failed_uk_route_plus_new_il_route_runs(tmp_path, make_ctx):
    dead = _dead(("search_official_domains", {"query": QUERY, "domains": ["toyota.co.uk"]}))
    session, ctx = _session(tmp_path, make_ctx, dead)
    result = _run(session, "search_official_domains", {"query": QUERY, "domains": ["toyota.co.uk", "toyota.co.il"]})
    assert not refused(result) and (QUERY, 5, "toyota.co.il") in ctx.glm.calls and session.route_blocks == 0


def test_official_domains_all_planned_routes_failed_may_be_refused(tmp_path, make_ctx):
    both = {"query": QUERY, "domains": ["toyota.co.uk", "toyota.co.il"]}
    session, ctx = _session(tmp_path, make_ctx, _dead(("search_official_domains", both)))
    assert refused(_run(session, "search_official_domains", both))
    assert refused(_run(session, "search_official_domains", {"query": QUERY, "domains": ["toyota.co.il"]}))
    # the same domain through search_web is a different result window (8, not 5): it runs
    assert not refused(_run(session, "search_web", {"query": QUERY, "domain": "toyota.co.il"}))
    assert ctx.glm.calls == [(QUERY, 8, "toyota.co.il")] and session.route_blocks == 2


def test_official_domains_use_the_default_domains_for_their_identity(tmp_path, make_ctx):
    dead = _dead(("search_official_domains", {"query": QUERY, "domains": ["toyota.co.uk"]}))
    session, _ = _session(tmp_path, make_ctx, dead, default_domains=["toyota.co.uk"])
    assert refused(_run(session, "search_official_domains", {"query": QUERY}))
    session, _ = _session(tmp_path / "2", make_ctx, dead, default_domains=["toyota.co.il"])
    assert not refused(_run(session, "search_official_domains", {"query": QUERY}))


def test_a_provider_error_for_one_domain_is_never_recorded():
    args = {"query": QUERY, "domains": ["toyota.co.uk", "toyota.co.il"]}
    result = {"per_domain": {"toyota.co.uk": {"error": "HTTP 429"}, "toyota.co.il": {"result_count": 0}}}
    recorded = routes("search_official_domains", args, result=result)
    assert [(r["domain"], r["count"]) for r in recorded] == [("toyota.co.il", 5)]
    calls = [{"name": "search_official_domains", "arguments": json.dumps(args), "route_failed": False,
              "routes": recorded}]
    assert [r["domain"] for r in _routes_of(calls)] == ["toyota.co.il"]


# --- fetch route identity: the mechanism is part of the route ---------------------------------------------------

def test_fetch_mechanisms_are_distinct_routes():
    fetch, pdf, render = (sig(t, {"url": URL}) for t in ("fetch_url", "fetch_pdf", "render_page"))
    assert len({fetch, pdf, render}) == 3
    assert sig("fetch_url", {"url": "http://toyota.co.uk/new-cars/corolla/specs/#top"}) == fetch     # same page


def test_a_failed_fetch_never_suppresses_a_render_and_vice_versa(tmp_path, make_ctx):
    session, ctx = _session(tmp_path, make_ctx, _dead(("fetch_url", {"url": URL})))
    assert not refused(_run(session, "render_page", {"url": URL}))
    assert not refused(_run(session, "fetch_pdf", {"url": URL}))
    assert refused(_run(session, "fetch_url", {"url": URL}))
    session, ctx = _session(tmp_path / "2", make_ctx, _dead(("render_page", {"url": URL})))
    assert not refused(_run(session, "fetch_url", {"url": URL}))
    assert refused(_run(session, "render_page", {"url": URL, "wait_ms": 2500}))


def test_render_wait_is_part_of_the_route():
    default = sig("render_page", {"url": URL})
    assert default == sig("render_page", {"url": URL, "wait_ms": 2500})                  # effective default
    assert sig("render_page", {"url": URL, "wait_ms": 20000}) == sig("render_page", {"url": URL, "wait_ms": 15000})
    assert sig("render_page", {"url": URL, "wait_ms": 8000}) != default                  # a longer wait: new route
    assert routes("render_page", {"url": URL, "wait_ms": "soon"}) == []                  # unidentifiable: no route


def test_a_longer_render_wait_runs(tmp_path, make_ctx):
    session, _ = _session(tmp_path, make_ctx, _dead(("render_page", {"url": URL})))
    assert not refused(_run(session, "render_page", {"url": URL, "wait_ms": 8000}))
    assert refused(_run(session, "render_page", {"url": URL}))


# --- old and incomplete records fail safe -----------------------------------------------------------------------

def test_old_or_incomplete_route_records_refuse_nothing(tmp_path, make_ctx):
    memory = ResearchMemory(tmp_path / "memory")
    current = routes("search_web", {"query": QUERY, "domain": "toyota.co.uk"})
    v2_search = {"tool": "search_web", "route": "corolla wheelbase", "domain": "toyota.co.uk", "signature": "s2",
                 "route_version": "route-v2"}                                          # no count, no provider
    v2_fetch = {"tool": "fetch_url", "route": URL, "signature": "s3", "route_version": "route-v2"}
    v1 = {"tool": "search_web", "route": "corolla height", "signature": "s1"}          # no version, no domain
    no_provider = {"tool": "search_web", "route": "corolla width", "domain": "", "count": 8,
                   "route_version": ROUTE_VERSION}                                     # current version, incomplete
    forged = {**current[0], "signature": "forged"}                                    # signature is recomputed
    memory.record_routes([{"scope_key": "k", "field": FIELD, "outcome": "no_new_material",
                           "routes": [v1, v2_search, v2_fetch, no_provider, forged]}], "earlier-run")
    negative = memory.negative_routes({FIELD: "k"})
    assert [(r["route"], r["signature"]) for r in negative[FIELD]["routes"]] == [(QUERY, current[0]["signature"])]
    assert route_identity(v2_search) is None and route_identity(no_provider) is None
    session, ctx = _session(tmp_path, make_ctx, negative)
    for name, args in (("search_web", {"query": "corolla height"}),
                       ("search_web", {"query": "corolla wheelbase", "domain": "toyota.co.uk"}),
                       ("search_web", {"query": "corolla width"}), ("fetch_url", {"url": URL})):
        assert not refused(_run(session, name, args)), args
    assert refused(_run(session, "search_web", {"query": QUERY, "domain": "toyota.co.uk"}))
    assert session.route_blocks == 1


def test_known_routes_shown_to_the_model_match_what_the_guard_refuses():
    uk20 = routes("search_web", {"query": QUERY, "domain": "toyota.co.uk", "max_results": 20})[0]
    official = routes("search_official_domains", {"query": QUERY, "domains": ["toyota.co.il"]})[0]
    render = routes("render_page", {"url": URL, "wait_ms": 8000})[0]
    assert route_label(uk20) == f"{QUERY} (domain: toyota.co.uk, results: 20)"
    assert route_label(official) == f"{QUERY} (domain: toyota.co.il)"
    assert route_label(routes("search_web", {"query": QUERY})[0]) == QUERY
    assert route_label(render) == f"{URL} (render_page, wait_ms: 8000)"
    assert route_label(routes("fetch_url", {"url": URL})[0]) == URL
    # a search recorded with another provider is not shown as this session's dead route; fetches always apply
    assert route_applies(uk20, GLM) and not route_applies(uk20, ("duckduckgo", ""))
    assert route_applies(render, ("duckduckgo", ""))
    assert route_signature({"tool": "search_web", "route": QUERY}) is None


# --- scheduling only ------------------------------------------------------------------------------------------

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
    assert refused(_run(session, "search_web", {"query": QUERY, "domain": "toyota.co.uk"}))
    assert ctx.evidence.items == [] and session.ctx.counters["evidence_rejected"] == 0
    after = current_evaluation(events, [SPECS[FIELD]])[0]
    assert after == before and after["state"] == "conflicting"
    assert "research_memory" not in json.dumps(after)
