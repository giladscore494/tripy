"""PR #32 hardening before the benchmark: truncated model output, narration validation, grounded candidates as class A,
thinking / reasoning_effort sanitizing, site map bounds, profile isolation and the small fixes (a-f). Scripted GLM /
fake HTTP only: no network."""

import dataclasses
import gzip
import json
import threading
import time
from types import SimpleNamespace

import pytest

from conftest import FakeResponse, FakeSession, cache_source
from fixtures import corolla_tail as tail
from fixtures.corolla_touring import PAYLOAD, VEHICLE
from test_adjudication import TOYOTA_IL, AdjClient, _two_class_docs, evidence_of, packet_of, respond_all, sweep_run
from test_grounded import WARRANTY_QUOTE, pointer
from test_phase_contracts import EU, GATE_OFF, fetch, run, say
from test_reacquire import ReacquireClient
from test_reasoning_and_assembly import (PAYLOAD_C, NarrationClient, admitted_items, deterministic_run, events_of,
                                         every_phase, http_caller, specs_c)
from test_run_profiles import (base_request, profiled_research, series_manager, series_request, wait_series)
from test_railway_runstate import wait_terminal
from test_site_map import BASE, DOMAIN, ctx_for, routes as site_routes, urlset
from test_tools_smoke import _call

from src import adjudication as J
from src import diagnostics as D
from src import final_assembly as FA
from src import grounded as G
from src import run_profiles as R
from src import site_map as S
from src.agent import (REPAIR_PROMPT, AgentConfig, ModelCaller, OutputTruncated, ToolSession, agent_config_from_env,
                       current_evaluation, run_adjudication_sweep, run_grounded_candidates)
from src.candidate_harvest import RunHarvester, candidate_matrix, collect_harvest_caps
from src.evidence_admission import AdmissionContext
from src.fields import resolve_requested_fields
from src.glm_client import ChatResponse
from src.recovery import finalize_existing_run, plan_recovery
from src.storage.cache import DocumentCache
from src.storage.run_log import RunLog, read_events
from src.structure_harvest import clean_soup, html_pairs
from src.tail_planner import triage
from src.tools import ToolConfig, ToolContext
from src.tools import extract as X
from src.tools.evidence import EvidenceStore

pytestmark = [pytest.mark.sweep_mode("adjudication"), pytest.mark.grounded_candidates(False)]
CUT = '{"decisions": [{"id": "c1", "acc'           # a JSON reply cut at max_tokens
TRUNCATED_USAGE = {"prompt_tokens": 40, "completion_tokens": 4000, "total_tokens": 4040,
                   "completion_tokens_details": {"reasoning_tokens": 3900}}


class TruncatingClient(AdjClient):
    """AdjClient whose first `truncate[task]` replies to a packet task end with finish_reason "length"."""

    def __init__(self, truncate, respond=None):
        super().__init__(respond)
        self.truncate = dict(truncate)

    def chat(self, messages, tools=None, **kwargs):
        task = packet_of(messages)["task"]
        if self.truncate.get(task, 0) > 0:
            self.truncate[task] -= 1
            self.requests.append({"messages": messages, "tools": tools, **kwargs})
            return ChatResponse(message={"role": "assistant", "content": CUT}, finish_reason="length",
                                usage=dict(TRUNCATED_USAGE))
        return super().chat(messages, tools, **kwargs)


def tasks(client, task):
    return [r for r in client.requests if packet_of(r["messages"])["task"] == task]


def no_repair(client):
    return not any(m.get("content") == REPAIR_PROMPT for r in client.requests for m in r["messages"])


# --- 1. truncated output (finish_reason "length") ---------------------------------------------------------------------

def test_a_length_finish_reason_raises_output_truncated_only_with_an_explicit_max_tokens(tmp_path):
    class Cut:
        model = "glm-5.3"

        def chat(self, messages, tools=None, **kwargs):
            return ChatResponse(message={"role": "assistant", "content": "{"}, finish_reason="length",
                                usage=dict(TRUNCATED_USAGE))

    log = RunLog(tmp_path, "b", "1")
    caller = ModelCaller(Cut(), log, AgentConfig())
    with pytest.raises(OutputTruncated) as err:
        caller([{"role": "user", "content": "x"}], phase="document_sweep", max_tokens=4000, meta={"chunk": 1})
    assert err.value.max_tokens == 4000 and err.value.response.finish_reason == "length"
    # research / tool turns (no explicit max_tokens): logged only, the response is returned as before
    assert caller([{"role": "user", "content": "x"}], phase="research")["content"] == "{"
    events = read_events(log.events_path)
    cut = [e for e in events if e["kind"] == "model_output_truncated"]
    assert [(e["phase"], e["max_tokens"], e["raised"]) for e in cut] == [("document_sweep", 4000, True),
                                                                       ("research", None, False)]
    assert cut[0]["usage"]["completion_tokens"] == 4000 and cut[0]["reasoning_tokens"] == 3900
    assert cut[0]["chunk"] == 1
    assert caller.truncation["document_sweep"]["truncated_calls"] == 1
    assert caller.truncation["research"]["truncated_calls"] == 1


def test_a_truncated_adjudication_packet_is_retried_once_with_double_max_tokens(tmp_path):
    client = TruncatingClient({"adjudicate_unambiguous": 1})
    summary, events, *_ = sweep_run(tmp_path, client, _two_class_docs(), fields=["length_mm"],
                                    candidates={TOYOTA_IL: []})
    assert [r["max_tokens"] for r in client.requests] == [4000, 8000]
    assert no_repair(client) and summary["adjudication"]["repair_turns"] == 0
    assert summary["chunk_errors"] == [] and [e["field"] for e in evidence_of(events)] == ["length_mm"]
    assert (summary["truncated_calls"], summary["truncation_retries"]) == (1, 1)
    retry = next(e for e in events if e["kind"] == "truncation_retry")
    assert (retry["max_tokens"], retry["retry_max_tokens"]) == (4000, 8000)
    assert summary["document_sweep_chunk_details"][0]["model_calls"] == 2


def test_two_truncations_fail_only_that_packet_and_never_use_the_repair_prompt(tmp_path):
    client = TruncatingClient({"adjudicate_unambiguous": 2})
    summary, events, *_ = sweep_run(tmp_path, client, _two_class_docs(), fields=["length_mm", "height_mm"],
                                    candidates={TOYOTA_IL: []})
    assert [r["max_tokens"] for r in tasks(client, "adjudicate_unambiguous")] == [4000, 8000]
    assert no_repair(client)
    assert summary["chunk_errors"] == ["output_truncated"] and summary["failed_chunk_fields"] == ["length_mm"]
    assert {e["field"] for e in evidence_of(events)} == {"height_mm"}          # the A packet still ran
    assert (summary["truncated_calls"], summary["truncation_retries"]) == (2, 1)


def test_the_retry_is_capped_at_12000_tokens(tmp_path):
    client = TruncatingClient({"adjudicate_unambiguous": 1})
    sweep_run(tmp_path, client, _two_class_docs(), fields=["length_mm"], candidates={TOYOTA_IL: []},
              adjudication_u_max_tokens=7000)
    assert [r["max_tokens"] for r in client.requests] == [7000, 12000]


@pytest.mark.grounded_candidates(True)
@pytest.mark.parametrize("cuts", [1, 2])
def test_a_truncated_grounded_call_is_retried_once_and_a_second_cut_costs_only_its_document(tmp_path, cuts):
    def respond(packet, n):
        if packet["task"] == "locate_values":
            return {"items": [pointer(packet, "warranty_years", WARRANTY_QUOTE, 3, "years")]}
        return respond_all(packet)

    client = TruncatingClient({"locate_values": cuts}, respond)
    summary, events, *_ = sweep_run(tmp_path, client, {tail.CARTUBE: (tail.CARTUBE_TEXT, "text")},
                                    fields=["length_mm", "warranty_years"], grounded_candidates=True)
    assert [r["max_tokens"] for r in tasks(client, "locate_values")] == [4000, 8000]
    assert no_repair(client)
    grounded = summary["grounded_candidates"]
    assert (grounded["truncated_calls"], grounded["truncation_retries"]) == (cuts, 1)
    stored = {e["field"] for e in evidence_of(events)}
    if cuts == 1:
        assert stored == {"length_mm", "warranty_years"} and grounded["failed_documents"] == 0
    else:
        failed = next(e for e in events if e["kind"] == "grounded_candidates_document_failed")
        assert failed["error"] == "output_truncated" and grounded["failed_documents"] == 1
        assert stored == {"length_mm"}                         # adjudication of the harvested field still ran


class NarrClient:
    """Narration replies in order: (reply dict, finish_reason)."""
    model = "glm-5.3"

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def chat(self, messages, tools=None, **kwargs):
        self.requests.append({"messages": messages, **kwargs})
        reply, reason = self.replies.pop(0)
        return ChatResponse(message={"role": "assistant", "content": json.dumps(reply)}, finish_reason=reason,
                            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


def narrate(tmp_path, replies, config=None):
    client = NarrClient(replies)
    log = RunLog(tmp_path, "b", "1")
    fin = FA.run_deterministic_finalization(ModelCaller(client, log, config or AgentConfig()), run_log=log,
                                            events=events_of(admitted_items()), payload=PAYLOAD_C, specs=specs_c(),
                                            target_market="IL", model="m")
    return fin, client, read_events(log.events_path)


CLEAN = {"summary": "The 2024 COROLLA: 2 fields resolved from 5 admitted evidence items.",
         "research_trace": ["fetched the official price list", "checked the EU spec page"]}


def test_a_truncated_narration_is_retried_once_with_double_max_tokens(tmp_path):
    fin, client, events = narrate(tmp_path, [({"summary": "The 20"}, "length"), (CLEAN, "stop")])
    assert [r["max_tokens"] for r in client.requests] == [2000, 4000] and no_repair(client)
    assert fin["output"]["summary"] == CLEAN["summary"]
    narration = fin["info"]["narration"]
    assert narration["status"] == "ok" and (narration["truncated_calls"], narration["truncation_retries"]) == (1, 1)

    fin, client, events = narrate(tmp_path / "twice", [({"summary": "The"}, "length"), ({"summary": "The"}, "length")])
    assert len(client.requests) == 2 and fin["info"]["narration"]["status"] == "failed"
    assert fin["output"]["summary"].startswith("Assembled in code from")        # the code summary is kept
    assert fin["error"] is None and any(e["kind"] == "narration_failed" for e in events)


# --- 2. max_tokens defaults ------------------------------------------------------------------------------------------

def test_new_max_tokens_defaults_are_applied_and_overridable(tmp_path):
    cfg = AgentConfig()
    assert (cfg.adjudication_u_max_tokens, cfg.adjudication_a_max_tokens, cfg.adjudication_m_max_tokens) == \
        (4000, 6000, 4000)
    assert J.DEFAULT_MAX_TOKENS == {"U": 4000, "A": 6000, "M": 4000}
    assert (G.MAX_TOKENS, FA.NARRATION_MAX_TOKENS) == (4000, 2000)
    env = agent_config_from_env({"ADJUDICATION_U_MAX_TOKENS": "5000", "ADJUDICATION_A_MAX_TOKENS": "7000",
                                 "ADJUDICATION_M_MAX_TOKENS": "4500"}.get)
    assert (env.adjudication_u_max_tokens, env.adjudication_a_max_tokens, env.adjudication_m_max_tokens) == \
        (5000, 7000, 4500)
    client = AdjClient()
    sweep_run(tmp_path, client, _two_class_docs(), fields=["length_mm", "height_mm"], candidates={TOYOTA_IL: []},
              adjudication_u_max_tokens=5000, adjudication_a_max_tokens=7000)
    assert sorted(r["max_tokens"] for r in client.requests) == [5000, 7000]
    # the narration keeps its env override: the finalizer phase's max_tokens caps it
    _, client, _ = narrate(tmp_path / "n", [(CLEAN, "stop")],
                           AgentConfig(phase_settings={"finalizer": {"max_tokens": 1200}}))
    assert client.requests[0]["max_tokens"] == 1200


# --- 3. narration validation ----------------------------------------------------------------------------------------

def test_a_narration_stating_a_number_outside_its_digest_is_rejected(tmp_path):
    bad = {"summary": "Resolved 2 fields; the fuel tank holds 43 l.", "research_trace": ["read 9 pages"]}
    fin, _, events = narrate(tmp_path, [(bad, "stop")])
    assert fin["output"]["summary"].startswith("Assembled in code from")
    assert fin["output"]["research_trace"] != bad["research_trace"]
    narration = fin["info"]["narration"]
    assert narration["status"] == "rejected" and narration["unsupported_numbers"] == ["43 l", "9 pages"]
    rejected = next(e for e in events if e["kind"] == "narration_rejected")
    assert rejected["unsupported_numbers"] == ["43 l", "9 pages"]
    # fields never come from the narration, rejected or not
    assert fin["output"]["fields"]["fuel_tank_l"]["value"] == 43


def test_a_clean_narration_is_kept(tmp_path):
    fin, client, events = narrate(tmp_path, [(CLEAN, "stop")])
    digest = json.loads(client.requests[0]["messages"][1]["content"].split("\n", 1)[1])
    assert {"2024", "2", "5"} <= FA.digest_numbers(digest)
    assert fin["output"]["summary"] == CLEAN["summary"] and fin["output"]["research_trace"] == CLEAN["research_trace"]
    assert fin["info"]["narration"]["status"] == "ok" and not any(e["kind"] == "narration_rejected" for e in events)
    assert FA.unsupported_numbers({"summary": "4,650 mm or 4650"}, {"x": 4650}) == []
    assert FA.unsupported_numbers({"summary": "1.80 m"}, {"x": "1.8 Hybrid"}) == []


# --- 4. grounded candidates are class A, also in re-acquisition -------------------------------------------------------

GROUNDED_LENGTH = {"field": "length_mm", "value": 4650, "unit": "mm", "quote": 'אורך: 4,650 מ"מ',
                   "extraction_method": "grounded_llm", "parser_confidence": 0.5}


def test_one_admissible_grounded_candidate_makes_its_field_class_a():
    item = {"candidate": {"value": 4650, "extraction_method": "grounded_llm"}, "flags": {"variant_match": "exact"}}
    assert J.field_class([item], [item["candidate"]], False) == "A"
    plain = {"candidate": {"value": 4650}, "flags": {"variant_match": "exact"}}
    assert J.field_class([plain], [plain["candidate"]], False) == "U"
    assert J.field_class([plain, {**plain, "grounded": True}], [plain["candidate"]], False) == "A"


def test_a_grounded_candidate_from_the_matrix_is_judged_as_class_a_in_the_sweep(tmp_path):
    client = AdjClient()
    summary, events, ids, ctx, specs = sweep_run(tmp_path, client, {tail.CARTUBE: (tail.CARTUBE_TEXT, "text")},
                                                 fields=["length_mm"], candidates={tail.CARTUBE: [GROUNDED_LENGTH]})
    plan = next(e for e in events if e["kind"] == "adjudication_plan")
    assert plan["classes"] == {"length_mm": "A"}
    assert [packet_of(r["messages"])["task"] for r in client.requests] == ["adjudicate_ambiguous"]
    # extraction_method survives into the matrix; counts report it apart from the deterministic harvest
    matrix = candidate_matrix(events, specs, VEHICLE)
    assert [c["extraction_method"] for c in matrix["fields"]["length_mm"]] == ["grounded_llm"]
    assert (matrix["candidate_count"], matrix["grounded_candidate_count"]) == (0, 1)
    assert matrix["fields_with_candidates"] == [] and matrix["fields_with_grounded_candidates"] == ["length_mm"]
    evaluation = [{"field": "length_mm", "state": "missing", "retry_eligible": True, "info": []}]
    assert triage(evaluation, specs, matrix, [], 2)["length_mm"]["grounded_candidates"] == 1
    assert triage(evaluation, specs, matrix, [], 2)["length_mm"]["candidates"] == 0


def test_a_grounded_candidate_is_class_a_in_reacquire_adjudication(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    log = RunLog(tmp_path / "runs", "b", "38626")
    specs = resolve_requested_fields(["length_mm"], propulsion="hybrid")
    config = AgentConfig(research_memory_enabled=False, requested_fields=["length_mm"], grounded_candidates=False)
    log.event("run_started", requested_field_specs=specs, target_market="IL", agent_config={})
    ctx = ToolContext(cache=cache, evidence=EvidenceStore(), config=ToolConfig(), vehicle=dict(VEHICLE),
                      log=lambda kind, **data: log.event(kind, **data))
    ctx.admission = AdmissionContext.for_run(PAYLOAD, VEHICLE, specs, "IL")
    doc = cache_source(cache, tail.CARTUBE, tail.CARTUBE_TEXT)
    ctx.note_document(doc)
    cand = {**GROUNDED_LENGTH, "document_id": doc, "source_url": tail.CARTUBE}
    log.event("candidates_harvested", document_id=doc, phase="field_recovery", source="grounded_llm",
              stage="reacquire", candidates=[cand])
    client = AdjClient()
    caller = ModelCaller(client, log, config)
    before = current_evaluation(read_events(log.events_path), specs, "IL")
    run_adjudication_sweep(session=ToolSession(ctx, log, config), caller=caller, specs=specs, payload=PAYLOAD,
                           config=config, run_log=log, cache=cache, before=before, routed={"length_mm": [cand]},
                           snippets={}, profiles={}, open_fields=["length_mm"], doc_metas=[], pre={},
                           start_seq=log.seq, usage_before=dict(caller.usage["field_recovery"]),
                           unusable_candidates=0, vehicle=VEHICLE, phase="field_recovery",
                           settings_phase="document_sweep", event_prefix="reacquire_adjudication", stage="reacquire")
    events = read_events(log.events_path)
    plan = next(e for e in events if e["kind"] == "adjudication_plan")
    assert plan["stage"] == "reacquire" and plan["classes"] == {"length_mm": "A"}
    assert [packet_of(r["messages"])["task"] for r in client.requests] == ["adjudicate_ambiguous"]


# --- 5. thinking / reasoning_effort sanitizing -----------------------------------------------------------------------

def test_invalid_extra_body_thinking_and_effort_are_never_sent(tmp_path):
    env = {"GLM_EXTRA_BODY": '{"thinking": "disabled", "reasoning_effort": "none", "top_p": 0.9}'}.get
    caller, session, log = http_caller(tmp_path, agent_config_from_env(env))
    every_phase(caller)
    assert not any("thinking" in p for p in session.payloads)
    assert [p["reasoning_effort"] for p in session.payloads] == ["high", "low", "low", "low"]   # the phase defaults
    assert all(p["top_p"] == 0.9 for p in session.payloads)                                     # other keys stay
    events = read_events(log.events_path)
    assert [(e["kind"], e["value"]) for e in events if e["kind"] in ("thinking_dropped", "reasoning_effort_dropped")] \
        == [("thinking_dropped", "disabled"), ("reasoning_effort_dropped", "none")]           # once per run each
    # an enabled thinking object is still sent
    caller, session, _ = http_caller(tmp_path / "on", AgentConfig(extra_body={"thinking": {"type": "enabled"}}))
    every_phase(caller)
    assert all(p["thinking"] == {"type": "enabled"} for p in session.payloads)


def test_thinking_disabled_maps_each_phase_to_its_default_effort(tmp_path):
    caller, session, _ = http_caller(tmp_path, agent_config_from_env({"GLM_THINKING": "disabled"}.get))
    every_phase(caller)
    assert [p["reasoning_effort"] for p in session.payloads] == ["high", "low", "low", "low"]
    assert not any("thinking" in p for p in session.payloads)


def test_the_1210_retry_uses_the_phase_default_effort(tmp_path):
    rejected = {"error": {"code": "1210", "message": "This model always engages in thinking and cannot be disabled"}}
    for phase, effort in (("research", "high"), ("document_sweep", "low")):
        caller, session, log = http_caller(tmp_path / phase, AgentConfig(thinking="enabled"), replies=[(400, rejected)])
        caller([{"role": "user", "content": "x"}], phase=phase)
        first, retry = session.payloads
        assert "thinking" not in retry and retry["reasoning_effort"] == effort
        event = next(e for e in read_events(log.events_path) if e["kind"] == "reasoning_retry")
        assert event["retry_reasoning_effort"] == effort
    # an explicitly set effort is kept on the retry
    caller, session, _ = http_caller(tmp_path / "x", AgentConfig(thinking="enabled", reasoning_effort="max"),
                                     replies=[(400, rejected)])
    caller([{"role": "user", "content": "x"}], phase="document_sweep")
    assert session.payloads[1]["reasoning_effort"] == "max"


# --- 6. site map bounds -------------------------------------------------------------------------------------------------

class SlowResponse:
    status_code = 200

    def __init__(self, chunks=20, pause=0.05):
        self.chunks, self.pause = chunks, pause

    def iter_content(self, size):
        for _ in range(self.chunks):
            time.sleep(self.pause)
            yield b"<url><loc>https://x/y</loc></url>"

    def close(self):
        pass


class SlowSession(FakeSession):
    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return SlowResponse()


def test_a_slow_server_is_cut_at_the_deadline_and_the_crawl_is_partial(tmp_path):
    ctx = ctx_for(tmp_path, SlowSession())
    t0 = time.monotonic()
    with pytest.raises(S.SiteMapDeadline):
        S.http_fetch(ctx, f"{BASE}/sitemap.xml", t0 + 0.2)
    assert time.monotonic() - t0 < 0.6                     # 20 chunks x 50 ms were never all read
    record = S.crawl_domain(DOMAIN, lambda u: S.http_fetch(ctx, u, t0 + 0.2), deadline=t0 + 0.2)
    assert record["truncated"] and record["partial"]


def test_a_gzip_bomb_is_stopped_at_the_decompressed_cap(monkeypatch):
    monkeypatch.setattr(S, "MAX_SITEMAP_BYTES", 1024 * 1024)
    bomb = gzip.compress(b"<urlset>" + b" " * (8 * 1024 * 1024))
    assert len(bomb) < 64 * 1024
    with pytest.raises(S.SitemapTooLarge):
        S.parse_sitemap(bomb)
    session = FakeSession({f"{BASE}/robots.txt": FakeResponse(b"", status=404),
                           f"{BASE}/sitemap.xml": FakeResponse(bomb, content_type="application/gzip")})
    record = S.crawl_domain(DOMAIN, lambda u: (lambda r: (r.status_code, r.content))(session.get(u)),
                            deadline=time.monotonic() + 5)
    assert record["urls"] == [] and any("SitemapTooLarge" in e for e in record["errors"])
    assert S.MAX_FILE_BYTES == 15 * 1024 * 1024


def test_a_deadline_truncated_crawl_is_cached_for_one_hour_only(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    clock = [1000.0]
    partial = {"urls": ["a"], "sitemaps": ["s"], "truncated": True, "partial": True}
    record, hit = S.cached_domain(cache, DOMAIN, lambda: partial, now=lambda: clock[0])
    assert record["partial"] is True and not hit
    clock[0] += S.PARTIAL_TTL_S - 1
    assert S.cached_domain(cache, DOMAIN, lambda: {"urls": ["b"]}, now=lambda: clock[0])[1] is True
    clock[0] += 2
    fresh, hit = S.cached_domain(cache, DOMAIN, lambda: {"urls": ["b"], "sitemaps": ["s"]}, now=lambda: clock[0])
    assert not hit and fresh["urls"] == ["b"]
    # a complete crawl (a URL cap is not "partial") keeps the 7-day TTL
    capped = S.cached_domain(DocumentCache(tmp_path / "d"), DOMAIN,
                             lambda: {"urls": ["c"], "sitemaps": ["s"], "truncated": True, "partial": False},
                             now=lambda: 0.0)[0]
    assert capped["partial"] is False
    assert S.cached_domain(DocumentCache(tmp_path / "d"), DOMAIN, lambda: {"urls": ["z"]},
                           now=lambda: S.PARTIAL_TTL_S * 5)[1] is True
    # build_site_map reports a partial domain
    many = urlset([f"{BASE}/p/{i}" for i in range(3)])
    site_routes_ = {f"{BASE}/robots.txt": FakeResponse(b"", status=404),
                    f"{BASE}/sitemap.xml": FakeResponse(many, content_type="application/xml")}
    site = S.build_site_map(ctx_for(tmp_path / "e", FakeSession(site_routes_)), [DOMAIN])
    assert site["domains"][0]["partial"] is False


def test_a_held_single_flight_lock_times_out_and_the_run_continues(tmp_path):
    session = FakeSession(site_routes())
    ctx = ctx_for(tmp_path, session)
    with ctx.cache.hold(f"sitemap:{DOMAIN}"):           # another vehicle is crawling this domain (and hangs)
        t0 = time.monotonic()
        site = S.build_site_map(ctx, [DOMAIN], deadline_s=1.0)
        assert time.monotonic() - t0 < 3
    entry = site["domains"][0]
    assert "TimeoutError" in entry["error"] and site["urls"] == [] and session.calls == []
    log = RunLog(tmp_path / "runs", "b", "1")
    with ctx.cache.hold(f"sitemap:{DOMAIN}"):
        result = S.run_site_map(ctx, log, payload={}, vehicle={}, deadline_s=1.0)
    assert result["offered"] == [] and result["error"] is None
    # the key was released: the next call builds the site map
    assert S.build_site_map(ctx, [DOMAIN])["urls"]


# --- 7. profiles: comparable arms, isolated single runs, pinned settings --------------------------------------------

def test_single_ab_arm_runs_have_memory_and_negative_routes_off(tmp_path):
    seen = []
    scripted = profiled_research([])

    def research(*args, agent_cfg, **kwargs):
        seen.append((agent_cfg.run_profile, agent_cfg.research_memory_enabled, agent_cfg.negative_route_blocking))
        return scripted(*args, agent_cfg=agent_cfg, **kwargs)

    manager = series_manager(tmp_path, research)
    for i, profile in enumerate((R.BASELINE, R.TREATMENT, R.TREATMENT_CARD, R.PRODUCTION, R.CUSTOM)):
        request = dataclasses.replace(base_request(ids=(str(10 + i),), key=profile),
                                      agent_cfg=R.build_agent_config({}.get, {}, profile))
        wait_terminal(manager, manager.start(request).run_id)
    assert seen == [(R.BASELINE, False, False), (R.TREATMENT, False, False), (R.TREATMENT_CARD, False, False),
                    (R.PRODUCTION, True, True), (R.CUSTOM, True, True)]
    # a series run is unchanged (its private cache already isolates memory)
    seen.clear()
    series = wait_series(manager, manager.start_series(series_request(repeats=1, ids=("1",))).run_id)
    assert series["status"] == "COMPLETED"
    assert seen == [(R.BASELINE, True, True), (R.TREATMENT, True, True)]
    # recorded: run_configuration shows the flags of the single run
    single = R.isolate_single_run(R.build_agent_config({}.get, {}, R.TREATMENT), in_series=False)
    config = D.run_configuration([{"kind": "run_started", "agent_config": dataclasses.asdict(single),
                                   "acquisition_mode": single.acquisition_mode, "site_map": single.site_map,
                                   "recovery_mode": single.recovery_mode}])
    assert (config["research_memory"], config["negative_route_blocking"]) == (False, False)
    assert "research_memory=False" in D.config_key(config)


def test_the_site_map_is_part_of_the_treatment_and_run_configuration_records_its_use():
    def configured(profile):
        cfg = R.build_agent_config({}.get, {}, profile)
        return D.run_configuration([{"kind": "run_started", "agent_config": dataclasses.asdict(cfg),
                                     "acquisition_mode": cfg.acquisition_mode, "site_map": cfg.site_map,
                                     "recovery_mode": cfg.recovery_mode, "run_profile": profile}])

    baseline, treatment = configured(R.BASELINE), configured(R.TREATMENT)
    assert (baseline["site_map"], baseline["site_map_used"]) == (False, False)
    assert (treatment["site_map"], treatment["site_map_used"]) == (True, True)
    assert "site_map_used=True" in D.config_key(treatment)
    # a site map switched on where no stage uses it (legacy acquisition + cluster recovery) is not "used"
    legacy = D.run_configuration([{"kind": "run_started", "agent_config": {}, "acquisition_mode": "legacy",
                                   "recovery_mode": "cluster", "site_map": True}])
    assert (legacy["site_map"], legacy["site_map_used"]) == (True, False)
    assert "site map off" in R.__doc__ and "PART" in R.__doc__


NEWLY_PINNED = {"CLUSTER_MAX_ATTEMPTS": "5", "CLUSTER_BASE_TURNS": "3", "CLUSTER_MAX_TURNS": "3",
                "CLUSTER_SEARCH_BUDGET": "9", "FIELD_RECOVERY_MAX_TOTAL_STEPS": "50", "GLM_RESEARCH_MAX_ATTEMPTS": "4",
                "GLM_RECOVERY_MAX_ATTEMPTS": "3", "GLM_EXTRA_BODY": '{"top_p": 0.5}'}


@pytest.mark.parametrize("profile", sorted(R.NAMED_PROFILES))
def test_newly_pinned_settings_ignore_env_in_named_profiles(profile):
    from src.phase_settings import for_phase

    cfg = R.build_agent_config(NEWLY_PINNED.get, {"extra_body": {"top_k": 3}}, profile)
    default = AgentConfig()
    assert (cfg.cluster_max_attempts, cfg.cluster_base_turns, cfg.cluster_max_turns, cfg.cluster_search_budget,
            cfg.field_recovery_max_total_steps) == (default.cluster_max_attempts, default.cluster_base_turns,
                                                    default.cluster_max_turns, default.cluster_search_budget,
                                                    default.field_recovery_max_total_steps)
    assert for_phase(cfg, "research")["max_attempts"] is None              # the client's GLM_CHAT_MAX_ATTEMPTS
    assert for_phase(cfg, "field_recovery")["max_attempts"] == 1
    assert cfg.extra_body == {}                                             # extra_body is ignored entirely


def test_custom_uses_the_newly_listed_env_values_and_lists_them():
    from src.phase_settings import for_phase

    cfg = R.build_agent_config(NEWLY_PINNED.get, {}, R.CUSTOM)
    assert (cfg.cluster_max_attempts, cfg.cluster_search_budget, cfg.field_recovery_max_total_steps) == (5, 9, 50)
    assert for_phase(cfg, "research")["max_attempts"] == 4 and cfg.extra_body == {"top_p": 0.5}
    listed = [o["var"] for o in R.env_overrides(NEWLY_PINNED.get)]
    assert set(NEWLY_PINNED) <= set(listed)


# --- 8. LOW fixes ------------------------------------------------------------------------------------------------------

@pytest.mark.grounded_candidates(True)
def test_a_grounded_failure_never_costs_the_sweep(tmp_path, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("grounded setup exploded")

    monkeypatch.setattr("src.agent.run_grounded_candidates", broken)
    summary, events, *_ = sweep_run(tmp_path, AdjClient(), {tail.CARTUBE: (tail.CARTUBE_TEXT, "text")},
                                    fields=["length_mm", "warranty_years"], grounded_candidates=True)
    failed = next(e for e in events if e["kind"] == "grounded_candidates_failed")
    assert "grounded setup exploded" in failed["error"]
    assert [e["field"] for e in evidence_of(events)] == ["length_mm"]          # adjudication still ran
    assert summary["grounded_candidates"]["error"]


def test_a_grounded_setup_failure_returns_an_error_summary(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    log = RunLog(tmp_path / "runs", "b", "1")
    ctx = ToolContext(cache=cache, evidence=EvidenceStore(), config=ToolConfig(), vehicle=dict(VEHICLE))
    specs = resolve_requested_fields(["warranty_years"], propulsion="hybrid")
    ctx.admission = AdmissionContext.for_run(PAYLOAD, VEHICLE, specs, "IL")
    config = AgentConfig()
    out = run_grounded_candidates(session=ToolSession(ctx, log, config), caller=ModelCaller(AdjClient(), log, config),
                                  specs=specs, payload=None, config=config, run_log=log, cache=cache,
                                  fields=["warranty_years"], documents=[{"document_id": "d1"}])
    assert out["admissible"] == {} and "AttributeError" in out["summary"]["error"]
    assert any(e["kind"] == "grounded_candidates_failed" for e in read_events(log.events_path))


@pytest.mark.recovery_mode("reacquire")
def test_reacquire_turns_use_the_recovery_attempts_and_the_research_effort(tmp_path):
    def episodes(packet, turn_no):
        return fetch("source", tail.CARTUBE) if turn_no == 1 else say({"done": True})

    client = ReacquireClient([fetch("a", EU), say({"done": True})], episodes)
    run(tmp_path, client, acquisition_mode="contract", requested_fields=["fuel_tank_l", "length_mm"], **GATE_OFF)
    assert client.reacquire_requests
    for request in client.reacquire_requests:
        assert request["max_attempts"] == 1 and request["extra"]["reasoning_effort"] == "high"


def test_an_ok_field_without_a_carrier_in_scope_is_conflicting_never_foreign_direct(tmp_path, monkeypatch):
    monkeypatch.setattr(FA, "_carriers", lambda *a, **k: [])
    log = RunLog(tmp_path, "b", "1")
    fin = FA.run_deterministic_finalization(None, run_log=log, events=events_of(admitted_items()), payload=PAYLOAD_C,
                                            specs=specs_c(), target_market="IL", model="m", narrate=False)
    entry = fin["output"]["fields"]["fuel_tank_l"]                   # ok in the evaluator (a portable DE item)
    assert entry["state"] == "conflicting" and entry["value"] is None and entry["provenance"] == "unresolved"
    assert entry["alternatives"] == [{"value": 43, "market": "DE", "variant": None, "evidence_ids": ["e2"]}]
    assert "fuel_tank_l" not in fin["output"]["provenance_summary"]["foreign_market_values"]
    bad = {e["field"]: e for e in read_events(log.events_path) if e["kind"] == "final_assembly_inconsistent"}
    assert bad["fuel_tank_l"]["reason"] == "no_carrier_in_scope" and bad["fuel_tank_l"]["evidence_ids"] == []


def test_structure_harvest_skips_oversized_pages_and_stops_at_its_time_budget(tmp_path):
    rows = "".join(f"<li><span>Length {i}</span><span>{4000 + i} mm</span></li>" for i in range(30))
    html = f"<html><body><ul>{rows}</ul></body></html>"
    text = "\n".join(f"Length {i} | {4000 + i} mm" for i in range(30))
    assert html_pairs(html, text)                                         # normal page: pairs
    with collect_harvest_caps() as caps:
        assert html_pairs(html, text, max_html_bytes=100) == []
        assert len(html_pairs(html, text, time_budget_s=1e-9)) < 30
    assert [(c["stage"], c["reason"]) for c in caps] == [("structure_harvest", "html_too_large"),
                                                         ("structure_harvest", "time_budget")]
    # RunHarvester logs the cap of the document it harvests (a > 3 MB page)
    cache = DocumentCache(tmp_path / "c")
    big = "<html><body>" + "<p>filler text</p>" * 200000 + "</body></html>"
    assert len(big.encode()) > 3 * 1024 * 1024
    doc = cache_source(cache, "https://example.com/big", big, doc_type="html")
    log = RunLog(tmp_path / "runs", "b", "1")
    RunHarvester(cache, resolve_requested_fields(["length_mm"], propulsion="hybrid"), log).observe([doc], "research")
    capped = [e for e in read_events(log.events_path) if e["kind"] == "harvest_capped"]
    assert [(e["document_id"], e["reason"]) for e in capped] == [(doc, "html_too_large")]


class FakePage:
    width = height = 100

    def extract_tables(self):
        return []

    def extract_text(self):
        return "Length Width Height"


class FakePDF:
    def __init__(self, pages):
        self.pages = [FakePage() for _ in range(pages)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("pages,limit_s,reason,passes", [(70, 5.0, "pages", 60), (10, 0.005, "time_budget", 1)])
def test_the_pdf_text_strategy_pass_stops_after_60_pages_or_5_seconds(monkeypatch, pages, limit_s, reason, passes):
    import pdfplumber

    calls = []

    def text_tables(page, vocabulary):
        calls.append(page)
        time.sleep(0.01 if limit_s < 1 else 0)
        return []

    monkeypatch.setattr(pdfplumber, "open", lambda stream: FakePDF(pages))
    monkeypatch.setattr(X, "wants_text_pass", lambda *a: True)
    monkeypatch.setattr(X, "_text_strategy_tables", text_tables)
    with collect_harvest_caps() as caps:
        X._pdf_tables(b"%PDF", vocabulary=X.TableVocabulary(), text_max_s=limit_s)
    assert len(calls) == passes
    assert [(c["stage"], c["reason"]) for c in caps] == [("pdf_text_tables", reason)]
    assert (X.TEXT_PASS_MAX_PAGES, X.TEXT_PASS_MAX_S) == (60, 5.0)


def test_navigation_menus_breadcrumbs_and_consent_blocks_are_not_harvested():
    html = """<html><body class="has-header"><main>
      <div role="navigation"><dl><dt>Length</dt><dd>1 mm</dd></dl></div>
      <div id="mainNav"><dl><dt>Width</dt><dd>2 mm</dd></dl></div>
      <ul class="breadcrumbs"><li><span>Height</span><span>3 mm</span></li></ul>
      <div class="site-footer-links"><dl><dt>Wheelbase</dt><dd>4 mm</dd></dl></div>
      <div class="top-menu"><dl><dt>Torque</dt><dd>5 Nm</dd></dl></div>
      <div class="cookie-banner"><dl><dt>Power</dt><dd>6 hp</dd></dl></div>
      <div role="contentinfo"><dl><dt>Trunk</dt><dd>7 l</dd></dl></div>
      <div class="option-unavailable spec"><dl><dt>Fuel tank</dt><dd>43 l</dd></dl></div>
      <table><thead class="table-header"><tr><th>Version</th></tr></thead></table>
    </main></body></html>"""
    soup = clean_soup(html)
    kept = soup.get_text(" ", strip=True)
    for gone in ("1 mm", "2 mm", "3 mm", "4 mm", "5 Nm", "6 hp", "7 l"):
        assert gone not in kept
    assert "43 l" in kept and "Version" in kept                 # "unavailable" is not "nav"; table parts stay
    pairs = html_pairs(html, "Fuel tank | 43 l", alias_pattern=None)
    assert [(p["label"], p["value"]) for p in pairs] == [("Fuel tank", "43 l")]


@pytest.mark.final_assembly("deterministic")
def test_finalize_existing_uses_the_final_assembly_mode_the_run_recorded(tmp_path):
    # a deterministic run finalized under an "llm" environment stays deterministic ...
    deterministic_run(tmp_path / "d", {"summary": "Narrated."})
    finisher = NarrationClient({"summary": "Again."}, primary=[])
    again = finalize_existing_run(tmp_path / "d" / "runs", "b", "38626", client=finisher,
                                  cache=DocumentCache(tmp_path / "d" / "cache"),
                                  config=AgentConfig(final_assembly="llm"), force=True)
    assert again["final_assembly"] == "deterministic" and len(finisher.narrations) == 1
    assert plan_recovery(tmp_path / "d" / "runs", "b", "38626",
                         config=AgentConfig(final_assembly="llm"))["final_assembly"] == "deterministic"
    # ... and an llm run finalized under a deterministic environment keeps its finalizer
    deterministic_run(tmp_path / "l", {"summary": "unused"}, final_assembly="llm")
    finisher = NarrationClient({"summary": "unused"}, primary=[])
    again = finalize_existing_run(tmp_path / "l" / "runs", "b", "38626", client=finisher,
                                  cache=DocumentCache(tmp_path / "l" / "cache"),
                                  config=AgentConfig(final_assembly="deterministic"), force=True)
    assert again["final_assembly"] == "llm" and finisher.narrations == []


# --- telemetry: per-phase truncation counts reach diagnostics -------------------------------------------------------

def test_truncation_counts_reach_run_totals_and_the_vehicle_row():
    events = [{"kind": "run_started"},
              {"kind": "model_response", "phase": "document_sweep", "usage": {"total_tokens": 1}},
              {"kind": "model_output_truncated", "phase": "document_sweep"},
              {"kind": "truncation_retry", "phase": "document_sweep"},
              {"kind": "model_output_truncated", "phase": "finalization"}]
    totals = D.run_totals(events)
    assert (totals["by_phase"]["document_sweep"]["truncated_calls"],
            totals["by_phase"]["document_sweep"]["truncation_retries"]) == (1, 1)
    assert totals["by_phase"]["finalization"]["truncated_calls"] == 1
    row = D.vehicle_row({"acquisition": {"summary": {}, "turns": []}, "document_sweep": {"summary": {}},
                         "totals": totals})
    assert (row["sweep_truncated_calls"], row["sweep_truncation_retries"], row["finalizer_truncated_calls"]) == \
        (1, 1, 1)
