"""PR #46 S1-S4: pluggable search backends, their sanity rules and cost, backend selection / fallback, and the search
bake-off (metrics, resolver dry run, the background job, its API and MCP tools). No network: every provider is a fake
over fixtures/pr46_search_responses.py."""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from fixtures import pr46_search_responses as R
from src.storage.cache import DocumentCache
from src.tools import ToolConfig, ToolContext, dispatch
from src.tools import search_backends as SB
from src.tools.evidence import EvidenceStore


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.closed = False

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


class Session:
    """Records every request; answers POST from `posts` (url substring -> body) and HEAD / GET redirects."""

    def __init__(self, posts=None, redirects=None, head_status=302):
        self.posts, self.redirects, self.head_status = posts or {}, redirects or {}, head_status
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json, headers))
        body = next((b for key, b in self.posts.items() if key in url), None)
        return Resp(200 if body is not None else 404, body)

    def head(self, url, allow_redirects=True, timeout=None):
        self.calls.append(("HEAD", url, allow_redirects, None))
        assert allow_redirects is False
        location = self.redirects.get(url)
        return Resp(self.head_status if location else 404, None, {"Location": location} if location else {})

    def get(self, url, allow_redirects=True, stream=False, timeout=None, **kw):
        self.calls.append(("GET", url, allow_redirects, stream))
        assert allow_redirects is False and stream is True
        location = self.redirects.get(url)
        return Resp(302 if location else 404, None, {"Location": location} if location else {})


def keys(**values):
    return lambda name: values.get(name, "")


# --- S1: each backend's response parsing ---------------------------------------------------------------------------------

def test_serper_request_and_parsing():
    session = Session(posts={"serper.dev": R.SERPER_SITE_RESPONSE})
    backend = SB.make_backend("serper", session=session, lookup=keys(SERPER_API_KEY="k-serper"))
    results, info = backend.query("אקספנג G6 2026", site="cartube.co.il", count=10)
    method, url, body, headers = session.calls[0]
    assert url == "https://google.serper.dev/search" and headers["X-API-KEY"] == "k-serper"
    assert body == {"q": "site:cartube.co.il אקספנג G6 2026", "gl": "il", "hl": "iw", "num": 10}
    assert [r.url for r in results][:1] == [R.CARTUBE_G6] and results[0].rank == 1 and results[0].site_native
    assert info.usd == SB.backend_settings("serper")["usd_per_query"] and info.billable
    # a query that already carries site: is sent as written
    backend.query("site:icar.co.il x", count=5)
    assert session.calls[1][2]["q"] == "site:icar.co.il x"


def test_sanity_drops_root_only_urls_and_post_filters_the_site():
    from src.tools.search_backends.serper import parse

    results = [r.as_dict() for r in parse(R.SERPER_SITE_RESPONSE, site_native=True)]
    usable, sanity = SB.sanitize(results, backend="serper", site="cartube.co.il")
    # the site root with an article's title is dropped; the home page with a generic title stays (resolver: home_page)
    assert [r["url"] for r in usable] == [R.CARTUBE_G6, "https://www.cartube.co.il/"]
    assert sanity["root_only"] == [{"url": "https://www.cartube.co.il", "title": "אקספנג G6 החדש נחשף"}]
    assert sanity["off_site"] == 1                                  # carsdirect.com: counted and dropped
    # glm keeps its current behaviour: off-site results counted, not dropped (root-only still dropped)
    glm, glm_sanity = SB.sanitize([dict(r) for r in R.GLM_RESULTS], backend="glm", site=None,
                                  query="site:auto.co.il אודי Q3 2024")
    assert [r["url"] for r in glm] == ["https://velocityjournal.com/audi-q3", R.AUTO_I30]
    assert len(glm_sanity["root_only"]) == 2 and glm_sanity["off_site"] == 1 and glm_sanity["site"] == "auto.co.il"


@pytest.mark.parametrize("url, title, dropped", [
    ("https://www.cartube.co.il", "מתיחת פנים: 2019 ב.מ.וו X1 החדש נחשף", True),
    ("https://www.auto.co.il/", "אודי Q3 החדש נחשף רשמית", True),
    ("https://www.toyota.co.il", "טויוטה קורולה החדשה", True),               # a catalog family name
    ("https://www.cartube.co.il/", "cartube - מחירון רכב חדש", False),          # a real home page title
    ("https://www.auto.co.il/cars/audi/q3/2024/1/", "אודי Q3 2024", False),     # a full path
])
def test_root_only_rule(url, title, dropped):
    assert SB.root_only(url, title) is dropped


def test_gemini_reads_only_grounding_chunks_and_resolves_redirects_without_the_body():
    from src.tools.search_backends.gemini import request_body

    session = Session(posts={"generateContent": R.GEMINI_RESPONSE}, redirects=R.GEMINI_REDIRECTS)
    backend = SB.make_backend("gemini", session=session, lookup=keys(GEMINI_API_KEY="k-gemini"))
    results, info = backend.query("site:cartube.co.il אקספנג G6 2026", count=10)
    post = session.calls[0]
    assert "models/gemini-3.8-flash:generateContent" in post[1] and post[3]["x-goog-api-key"] == "k-gemini"
    assert post[2]["tools"] == [{"google_search": {}}]
    assert "generationConfig" not in post[2] and "responseSchema" not in json.dumps(post[2])   # no structured output
    assert post[2]["contents"][0]["parts"][0]["text"] == "find the web pages for: site:cartube.co.il אקספנג G6 2026"
    # URLs from groundingChunks only (the text's "guessed-url" is never read); the failed redirect is dropped
    assert [r.url for r in results] == [R.CARTUBE_G6, R.ICAR_VERSION]
    assert results[0].raw_url.startswith("https://vertexaisearch.cloud.google.com/grounding-api-redirect/")
    assert info.redirects_resolved == 2 and info.redirects_failed == 1
    assert info.queries == R.GEMINI_RESPONSE["candidates"][0]["groundingMetadata"]["webSearchQueries"]
    assert all(c[0] in ("POST", "HEAD", "GET") for c in session.calls)
    # cost = tokens (output includes thoughts) + billed search queries, prices from data/search_backends.json
    cfg = SB.backend_settings("gemini")
    expected = 1000 * cfg["input_usd_per_mtok"] / 1e6 + 300 * cfg["output_usd_per_mtok"] / 1e6 \
        + 2 * cfg["usd_per_search_query"]
    assert info.usd == round(expected, 6)
    assert request_body("x", "icar.co.il")["contents"][0]["parts"][0]["text"].endswith("site:icar.co.il x")


def test_gemini_redirect_falls_back_to_a_get_without_the_body_when_head_is_refused():
    from src.tools.search_backends.gemini import resolve_redirect

    session = Session(redirects=R.GEMINI_REDIRECTS, head_status=405)
    session.head = lambda url, allow_redirects=True, timeout=None: Resp(405, None, {})
    uri = next(iter(R.GEMINI_REDIRECTS))
    assert resolve_redirect(session, uri, (1, 1)) == R.GEMINI_REDIRECTS[uri]
    assert session.calls[-1] == ("GET", uri, False, True)


# --- S1 / S3 through the search tools: cost, cache, unavailable backends, fallback ---------------------------------------

class FakeGlm:
    class settings:
        search_engine = "search-prime"

    def __init__(self, results=None):
        self.results, self.calls = results if results is not None else R.GLM_RESULTS, []

    def web_search(self, query, count=8, domain=None):
        self.calls.append((query, count, domain))
        return [dict(r) for r in self.results]


def _ctx(tmp_path, backend="glm", fallback="", glm=None, session=None, events=None):
    log = (lambda kind, **data: events.append((kind, data))) if events is not None else None
    return ToolContext(cache=DocumentCache(tmp_path / "cache"), evidence=EvidenceStore(),
                       config=ToolConfig(search_backend=backend, search_fallback_backend=fallback),
                       glm=glm or FakeGlm(), session=session or Session(), log=log)


def test_glm_search_counts_root_only_and_per_backend_cost(tmp_path):
    events = []
    ctx = _ctx(tmp_path, events=events)
    out = dispatch(ctx, "search_web", {"query": "site:auto.co.il i30 2018"})
    assert [r["url"] for r in out["results"]] == ["https://velocityjournal.com/audi-q3", R.AUTO_I30]
    assert ctx.counters["search_root_only_urls"] == 2 and ctx.counters["search_off_site"] == 1
    assert ctx.counters["search_api_calls"] == 1 and ctx.counters["search_calls:glm"] == 1
    assert ctx.counters["search_usd:glm"] == SB.backend_settings("glm")["usd_per_query"]
    kinds = [k for k, _ in events]
    assert "root_only_url" in kinds and "search_backend_call" in kinds
    search = next(d for k, d in events if k == "search")
    assert search["backend"] == "glm"
    # the cache: the same search again is free
    dispatch(ctx, "search_web", {"query": "site:auto.co.il i30 2018"})
    assert ctx.counters["search_api_calls"] == 1 and ctx.counters["search_cache_hits"] == 1


def test_the_glm_cache_key_is_unchanged(tmp_path):
    from src.tools.search import search_key

    ctx = _ctx(tmp_path)
    assert search_key(ctx, "q", 8, "x.co.il") == json.dumps(["glm", "search-prime", "q", 8, "x.co.il"])
    assert search_key(ctx, "q", 8, None, "serper") == json.dumps(["serper", "", "q", 8, ""])


def test_a_backend_without_its_key_is_unavailable_and_never_swapped(tmp_path, monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    status = SB.backend_status(keys())
    assert status["serper"] == {"available": False, "reason": "SERPER_API_KEY is not set", "key_env": "SERPER_API_KEY",
                                "label": "Serper (Google results)"}
    assert status["duckduckgo"]["available"] is True
    with pytest.raises(SB.BackendUnavailable):
        SB.make_backend("serper", lookup=keys())
    glm = FakeGlm()
    ctx = _ctx(tmp_path, backend="serper", glm=glm)
    out = dispatch(ctx, "search_web", {"query": "x"})
    assert out["error"] == "BackendUnavailable" and glm.calls == []          # glm never answered instead


def test_fallback_is_used_only_on_zero_usable_results(tmp_path, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "k")
    events = []
    session = Session(posts={"serper.dev": R.SERPER_SITE_RESPONSE})
    # the primary (glm) returns only root-only results: 0 usable -> serper answers
    ctx = _ctx(tmp_path, backend="glm", fallback="serper", glm=FakeGlm(R.GLM_RESULTS[:2]), session=session,
               events=events)
    out = dispatch(ctx, "search_web", {"query": "אקספנג G6 2026", "domain": "cartube.co.il"})
    assert out["backend"] == "serper" and out["fallback_used"] is True
    assert [r["url"] for r in out["results"]][0] == R.CARTUBE_G6
    assert ctx.counters["search_fallback_used"] == 1 and ctx.counters["search_calls:serper"] == 1
    assert any(k == "search_fallback_used" for k, _ in events)
    # a primary with usable results: the fallback is never called
    session2 = Session(posts={"serper.dev": R.SERPER_SITE_RESPONSE})
    ctx2 = _ctx(tmp_path / "b", backend="glm", fallback="serper", session=session2)
    out2 = dispatch(ctx2, "search_web", {"query": "i30 2018"})
    assert out2["backend"] == "glm" and "fallback_used" not in out2 and session2.calls == []


def test_fallback_uses_serper_when_glm_has_only_off_site_results_for_a_site_search(tmp_path, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "k")
    events = []
    session = Session(posts={"serper.dev": R.SERPER_SITE_RESPONSE})
    glm = FakeGlm([{"url": "https://velocityjournal.com/audi-q3", "title": "Audi Q3 specifications",
                    "snippet": "technical data"}])
    ctx = _ctx(tmp_path, backend="glm", fallback="serper", glm=glm, session=session, events=events)

    out = dispatch(ctx, "search_web", {"query": "אקספנג G6 2026", "domain": "cartube.co.il"})

    assert out["backend"] == "serper" and out["fallback_used"] is True
    assert [r["url"] for r in out["results"]][0] == R.CARTUBE_G6
    assert ctx.counters["search_off_site"] == 2  # one GLM off-site result + one Serper off-site result
    assert ctx.counters["search_fallback_used"] == 1 and ctx.counters["search_calls:serper"] == 1
    assert any(k == "search_fallback_used" for k, _ in events)


def test_run_cost_prices_each_backend():
    from src.pricing import phase_run_cost, search_by_backend, search_cost_inputs

    counters = Counter({"search_api_calls": 5, "search_calls:glm": 2, "search_calls:serper": 3,
                        "search_usd:glm": 0.02, "search_usd:serper": 0.003})
    assert search_cost_inputs(counters) == (2, 0.003)
    assert search_cost_inputs({"search_api_calls": 4}) == (4, 0.0)            # a run before PR #46
    assert search_by_backend(counters) == ({"glm": 2, "serper": 3}, {"glm": 0.02, "serper": 0.003})
    calls, extra = search_cost_inputs(counters)
    cost, _ = phase_run_cost(usage_research={}, usage_sweep={}, usage_recovery={}, usage_finalizer={},
                             search_api_calls=calls, pricing={"input_per_mtok": 1, "output_per_mtok": 1,
                                                              "web_search_per_call": 0.01},
                             extra_search_usd=extra)
    assert cost["web_search_usd"] == 0.023


# --- S3: run settings ------------------------------------------------------------------------------------------------

def test_run_settings_offer_the_backends_and_a_fallback_and_profiles_default_to_glm(monkeypatch):
    from src.concurrency import ConcurrencyController
    from src.run_settings import run_settings_contract, settings_checks, settings_for_run
    from src.storage.paths import resolve_paths

    env = {"GLM_API_KEY": "k", "GLM_MODEL": "glm-5.3", "SEARCH_BACKEND": "duckduckgo"}
    secret = lambda n: env.get(n, "")                                         # noqa: E731
    controller = ConcurrencyController()
    contract = run_settings_contract(secret, controller)
    spec = {s["name"]: s for s in contract["settings"]}
    assert spec["search_backend"]["options"] == ["glm", "serper", "gemini", "duckduckgo"]
    assert spec["search_fallback_backend"]["options"] == ["none", "glm", "serper", "gemini", "duckduckgo"]
    assert spec["search_fallback_backend"]["default"] == "none"
    assert spec["search_backend"]["unavailable_options"] == {"serper": "SERPER_API_KEY is not set",
                                                             "gemini": "GEMINI_API_KEY is not set"}
    assert spec["search_backend"]["profile_defaults"]["production"] == "glm"
    assert not spec["search_backend"]["pinned_by_named_profile"]
    # Production defaults to glm (never env); Custom takes the env default; a run's own choice wins
    assert settings_for_run(secret, controller, None, "production").search_backend == "glm"
    assert settings_for_run(secret, controller, None, "custom").search_backend == "duckduckgo"
    chosen = settings_for_run(secret, controller, {"search_backend": "serper", "search_fallback_backend": "glm"},
                              "production")
    assert chosen.search_backend == "serper" and chosen.tool_config(secret).search_fallback_backend == "glm"
    # selecting a backend without its key is a blocking configuration error (never a silent swap)
    blocking = [c for c in settings_checks(chosen, secret, resolve_paths(secret)) if c.level == "error"]
    assert any("serper is unavailable" in c.status for c in blocking)


# --- S4: the bake-off --------------------------------------------------------------------------------------------------

class FakeBackend:
    """A provider over a fixed {query substring: [(url, title)]} table."""

    def __init__(self, name, table, usd=0.001):
        self.name, self.table, self.usd = name, table, usd

    def query(self, query, *, site=None, country="il", lang="he", count=10):
        hits = next((v for k, v in self.table.items() if k in query), [])
        results = [SB.SearchResult(url=u, title=t, rank=n, backend=self.name, raw_url=u)
                   for n, (u, t) in enumerate(hits, start=1)]
        return results, SB.CallInfo(backend=self.name, usd=self.usd, latency_ms=100 + len(query))


def _g6_record():
    from src.search_bakeoff import benchmark_records

    (record,) = benchmark_records(["101122"])
    return record


def test_query_set_is_the_resolver_queries_plus_two_generic_ones():
    from src.search_bakeoff import record_queries

    queries = record_queries(_g6_record()["payload"])
    assert [q["kind"] for q in queries] == ["resolver"] * 3 + ["generic"] * 2
    assert [q["site"] for q in queries[:3]] == ["cartube.co.il", "icar.co.il", "auto.co.il"]
    assert all(q["query"].startswith(f"site:{q['site']} ") for q in queries[:3])
    assert queries[3]["query"].endswith("מפרט טכני") and queries[4]["query"].endswith("specifications")


def test_metrics_and_the_resolver_dry_run_on_a_fixture_result_set(tmp_path):
    from src.search_bakeoff import BakeoffConfig, markdown_table, metrics_csv, run_bakeoff

    record = _g6_record()
    good = FakeBackend("serper", {"site:cartube.co.il": [(R.CARTUBE_G6, "אקספנג G6 RWD CORE"),
                                                          ("https://www.cartube.co.il/news/1", "news")],
                                  "מפרט טכני": [("https://www.icar.co.il/x", "g6")]}, usd=0.001)
    bad = FakeBackend("glm", {"site:cartube.co.il": [("https://www.cartube.co.il", "אקספנג G6 החדש נחשף"),
                                                       ("https://www.drivearabia.com/g6", "XPeng G6")]}, usd=0.01)
    fetched = []

    def fetch(url):
        fetched.append(url)
        return {"error": "HTTPError", "status": 404}
    summary = run_bakeoff(BakeoffConfig(backends=["glm", "serper"], fetch_top=1), tmp_path / "b",
                          make_backend={"glm": bad, "serper": good}.get, fetch=fetch, cache=None, records=[record])
    m = {x["backend"]: x for x in summary["metrics"]}
    assert m["serper"]["version_url_hit"] == 1 and m["serper"]["first_version_rank"] == 1
    assert m["serper"]["full_path_ratio"] == 1.0 and m["serper"]["on_site_ratio"] == 1.0
    assert m["serper"]["israeli_domain_share"] == 1.0 and m["serper"]["unique_urls"] == 3
    assert m["serper"]["candidates"] == 1 and m["serper"]["fetched"] == 1 and m["serper"]["accepted"] == 0
    assert m["serper"]["rejected_by_reason"] == {"fetch_failed:HTTPError": 1}
    assert m["serper"]["usd_per_1000_queries"] == 1.0 and m["serper"]["usd_per_record"] == 0.005
    assert m["glm"]["version_url_hit"] == 0 and m["glm"]["full_path_ratio"] == 0.5 and m["glm"]["on_site_ratio"] == 0.5
    assert m["glm"]["candidates"] == 0 and m["glm"]["fetched"] == 0
    assert fetched == [R.CARTUBE_G6]                                    # only the ranked version candidate
    assert "| serper |" in markdown_table(summary) and metrics_csv(summary).startswith("backend,queries,")
    rows = [json.loads(line) for line in (tmp_path / "b" / "records.jsonl").read_text("utf-8").splitlines()]
    assert {(r["backend"], r["candidate_url"]) for r in rows} == {("glm", None), ("serper", R.CARTUBE_G6)}
    assert json.loads((tmp_path / "b" / "summary.json").read_text("utf-8"))["status"] == "completed"


def test_the_top_candidate_verdict_comes_from_the_normal_version_page_rules(tmp_path):
    from fixtures import pr43_audi_pages as A
    from fixtures import pr44_pages as P
    from fixtures.corolla_harvest import put
    from src.search_bakeoff import BakeoffConfig, run_bakeoff

    cache = DocumentCache(tmp_path / "cache")
    url = P.Q3_CARTUBE_URL                         # the PR #45 accepted cartube Q3 327 page
    html = P.q3_cartube_html(synthetic_row=False)
    record = {"record_id": "q3", "label": "Q3", "payload": A.Q3_PAYLOAD}
    backend = FakeBackend("serper", {"site:cartube.co.il": [(url, "אודי Q3 2.0 40TFSI 4X4 2024")]})
    summary = run_bakeoff(BakeoffConfig(backends=["serper"], fetch_top=1), tmp_path / "b",
                          make_backend=lambda name: backend,
                          fetch=lambda u: {"document_id": put(cache, u, html, "html"), "status": 200},
                          cache=cache, records=[record])
    (m,) = summary["metrics"]
    assert m["fetched"] == 1 and m["accepted"] == 1 and m["accepted_per_usd"] == 200.0
    row = json.loads((tmp_path / "b" / "records.jsonl").read_text("utf-8").splitlines()[0])
    assert row["verdict"] == "accepted" and row["candidate_url"] == url


def test_the_cli_entry_runs_the_same_implementation(tmp_path, capsys):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import search_bakeoff as cli

    record = _g6_record()
    backend = FakeBackend("serper", {"site:cartube.co.il": [(R.CARTUBE_G6, "g6")]})
    code = cli.main(["--backends", "serper", "--fetch-top", "0", "--out", str(tmp_path / "cli")],
                    make_backend=lambda name: backend, cache=DocumentCache(tmp_path / "c"), records=[record])
    assert code == 0 and "| serper | 5 |" in capsys.readouterr().out
    assert (tmp_path / "cli" / "summary.json").is_file()


def test_the_cli_refuses_an_unavailable_backend(monkeypatch, capsys):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import search_bakeoff as cli

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert cli.main(["--backends", "gemini"]) == 2
    assert "GEMINI_API_KEY is not set" in capsys.readouterr().err


# --- S4: the background job, its API and the MCP tools ----------------------------------------------------------------

def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_bakeoff_job_lifecycle_cancel_and_restart(tmp_path):
    from src.jobs.bakeoffs import BakeoffJobs, BakeoffRejected
    from src.search_bakeoff import BakeoffConfig

    record = _g6_record()
    gate, entered = threading.Event(), threading.Event()

    class Slow(FakeBackend):
        def query(self, *a, **k):
            entered.set()
            gate.wait(5)
            return super().query(*a, **k)
    backend = Slow("serper", {"site:cartube.co.il": [(R.CARTUBE_G6, "g6")]})
    jobs = BakeoffJobs(tmp_path / "bakeoffs", cache=None, owner={"boot_id": "boot-1"},
                       deps=lambda cache: (lambda name: backend, None),
                       records_loader=lambda ids: [record, {**record, "record_id": "second"}])
    bid = jobs.start(BakeoffConfig(backends=["serper"], fetch_top=0))
    assert jobs.get(bid)["status"] == "RUNNING" and jobs.get(bid)["executing"]
    with pytest.raises(BakeoffRejected):
        jobs.start(BakeoffConfig(backends=["serper"]))                     # one at a time
    assert entered.wait(5)                                                # the first record is being measured
    assert jobs.cancel(bid)
    gate.set()
    assert _wait(lambda: not jobs.executing(bid))
    state = jobs.get(bid)
    assert state["status"] == "CANCELLED" and jobs.summary(bid)["status"] == "cancelled"
    assert state["progress"]["done"] == 1                                 # it keeps what it measured
    # a RUNNING bake-off of another (gone) process is INTERRUPTED on restart
    jobs._update(bid, lambda s: s.update({"status": "RUNNING", "owner": {"boot_id": "gone"}}))
    assert BakeoffJobs(tmp_path / "bakeoffs", cache=None, owner={"boot_id": "boot-2"}).reconcile("boot-2") == [bid]
    assert jobs.get(bid)["status"] == "INTERRUPTED"


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.api.deps import ApiContext
    from src.jobs.manager import RunManager
    from src.research_targets import VehicleCatalog
    from src.storage.paths import resolve_paths

    for name in ("SERPER_API_KEY", "GEMINI_API_KEY", "TRIPY_ENV", "TRIPY_ACCESS_TOKEN", "RAILWAY_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRIPY_DATA_DIR", str(tmp_path / "data"))
    env = {"GLM_API_KEY": "k", "GLM_MODEL": "glm-5.3", "SERPER_API_KEY": "k-serper", "TRIPY_DATA_DIR": str(tmp_path / "data")}
    secret = lambda n: env.get(n, "")                                         # noqa: E731
    paths = resolve_paths(secret)
    catalog = VehicleCatalog.load()
    manager = RunManager(paths, register_atexit=False, vehicle_label=catalog.title)
    record = _g6_record()
    backend = FakeBackend("serper", {"site:cartube.co.il": [(R.CARTUBE_G6, "g6")]})
    manager.bakeoffs._deps = lambda cache: (lambda name: backend, None)
    manager.bakeoffs._records_loader = lambda ids: [record]
    ctx = ApiContext(paths=paths, manager=manager, catalog=catalog, secret=secret)
    with TestClient(create_app(context=ctx, mount_mcp=False)) as client:
        yield client, manager, paths
    manager.shutdown(grace_s=2)


def test_bakeoff_api_options_start_detail_and_csv(api_client):
    client, manager, paths = api_client
    options = client.get("/api/bakeoffs/options").json()
    by = {b["name"]: b for b in options["backends"]}
    assert list(by) == ["glm", "serper", "gemini"]
    assert by["gemini"]["available"] is False and by["gemini"]["reason"] == "GEMINI_API_KEY is not set"
    assert options["defaults"]["backends"] == ["glm", "serper"] and len(options["records"]) == 50
    refused = client.post("/api/bakeoffs", json={"backends": ["gemini"], "fetch_top": True})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "backend_unavailable"
    started = client.post("/api/bakeoffs", json={"backends": ["serper"], "fetch_top": False})
    assert started.status_code == 201
    bid = started.json()["bakeoff_id"]
    assert _wait(lambda: client.get(f"/api/bakeoffs/{bid}").json()["bakeoff"]["status"] == "COMPLETED")
    detail = client.get(f"/api/bakeoffs/{bid}").json()
    assert detail["summary"]["metrics"][0]["backend"] == "serper" and detail["records"][0]["version_url_hit"]
    assert (Path(paths.data_dir) / "bakeoffs" / bid / "summary.json").is_file()
    csv = client.get(f"/api/bakeoffs/{bid}/export/metrics.csv")
    assert csv.status_code == 200 and csv.text.startswith("backend,queries,") and "serper" in csv.text
    assert client.get("/api/bakeoffs").json()["bakeoffs"][0]["bakeoff_id"] == bid
    assert client.get("/api/bakeoffs/nope").status_code == 404

    from src.mcp_server.tools import Observer

    observer = Observer(paths)
    listed = observer.list_bakeoffs()
    assert listed["bakeoffs"][0]["bakeoff_id"] == bid and listed["bakeoffs"][0]["metrics"][0]["backend"] == "serper"
    result = observer.bakeoff_result(bid)
    assert result["summary"]["metrics"][0]["version_url_hit"] == 1 and result["records"][0]["record_id"] == "101122"
    from src.mcp_server.safety import ToolInputError

    with pytest.raises(ToolInputError):
        observer.bakeoff_result("../etc")


# --- P2: acquisition hygiene -----------------------------------------------------------------------------------------

def _session(tmp_path, pages=None, vehicle=None):
    from conftest import FakeResponse, FakeSession

    from src.agent import AgentConfig, ToolSession
    from src.storage.run_log import RunLog

    ctx = ToolContext(cache=DocumentCache(tmp_path / "c"), evidence=EvidenceStore(), config=ToolConfig(),
                      session=FakeSession({u: FakeResponse(b) for u, b in (pages or {}).items()}),
                      vehicle=vehicle or {"manufacturer": "אקספנג", "model": "G6"})
    return ctx, ToolSession(ctx, RunLog(tmp_path / "runs", "b", "1"), AgentConfig())


def test_a_guessed_url_on_an_il_version_site_is_refused(tmp_path):
    from test_tools_smoke import _call

    guessed = "https://www.cartube.co.il/מחירון-רכב-חדש/אקספנג/אקספנג-g6/9999-guessed"
    ctx, session = _session(tmp_path)
    messages = []
    session.execute([_call("a", "fetch_url", {"url": guessed})], messages, phase="research")
    assert ctx.counters["acq_guessed_url_refused"] == 1 and "fetch_url" not in {
        k.removeprefix("tool:") for k in ctx.counters if k.startswith("tool:")}   # nothing was fetched
    reply = json.loads(messages[-1]["content"])
    assert reply["error"] == "guessed_url_refused" and "cartube.co.il" in reply["message"]
    assert session.tool_calls[-1]["blocked"] is True
    # offered by a search result (even percent-encoded when fetched): allowed; another site's guess: allowed
    from urllib.parse import quote

    from src.acquisition import fetch_refusal, url_provenance

    url_provenance(ctx).observe_search({"results": [{"url": R.CARTUBE_G6, "title": "אקספנג G6"}]})
    encoded = "https://www.cartube.co.il/" + quote(R.CARTUBE_G6.split("cartube.co.il/", 1)[1])
    assert fetch_refusal(ctx, "fetch_url", encoded, "research") is None
    assert fetch_refusal(ctx, "fetch_url", "https://www.carnews.co.il/xpeng-g6", "recovery") is None
    # the resolver's own results and pages are offered
    from src.il_version_pages import _offer

    _offer(ctx, urls=[R.AUTO_I30], links=[{"url": "https://www.auto.co.il/cars/hyundai/i30/2018/530047/"}])
    assert fetch_refusal(ctx, "fetch_url", "https://www.auto.co.il/cars/hyundai/i30/2018/530047", "research") is None


def test_an_irrelevant_search_result_is_refused_in_primary_research_but_pdfs_and_official_domains_are_exempt(tmp_path):
    from src.acquisition import fetch_refusal, url_provenance

    ctx, _ = _session(tmp_path)
    provenance = url_provenance(ctx)
    tv = "https://www.zap.co.il/models.aspx?sog=e-tv"
    provenance.observe_search({"results": [
        {"url": tv, "title": "טלוויזיות - השוואת מחירים", "snippet": "LG, Samsung"},
        {"url": "https://www.carnews.co.il/review/123", "title": "מבחן דרכים: אקספנג G6", "snippet": ""},
        {"url": "https://www.example.co.il/brochure.pdf", "title": "brochure", "snippet": ""},
        {"url": "https://www.heyxpeng.co.il/news/1", "title": "news", "snippet": ""},
        {"url": "https://www.example.co.il/xpeng-g6-specs", "title": "specs", "snippet": ""},
    ]})
    refusal = fetch_refusal(ctx, "fetch_url", tv, "research")
    assert refusal["reason"] == "irrelevant_result"
    assert fetch_refusal(ctx, "fetch_url", tv, "recovery") is None                       # primary research only
    assert fetch_refusal(ctx, "fetch_url", "https://www.carnews.co.il/review/123", "research") is None   # names make
    assert fetch_refusal(ctx, "fetch_url", "https://www.example.co.il/xpeng-g6-specs", "research") is None  # URL
    assert fetch_refusal(ctx, "fetch_url", "https://www.example.co.il/brochure.pdf", "research") is None   # PDF
    assert fetch_refusal(ctx, "fetch_pdf", tv, "research") is None
    assert fetch_refusal(ctx, "fetch_url", "https://www.heyxpeng.co.il/news/1", "research") is None  # official
    # a URL a fetched page linked to is not "a search result" any more
    provenance.offer([tv])
    assert fetch_refusal(ctx, "fetch_url", tv, "research") is None


def test_hygiene_refusals_reach_the_diagnostics_row():
    from src.diagnostics import search_operations

    events = [{"kind": "tool_blocked", "reason": "guessed_url_refused"},
              {"kind": "tool_blocked", "reason": "irrelevant_result"},
              {"kind": "search_backend_call", "backend": "serper", "cache_hit": False, "usd": 0.001, "off_site": 2},
              {"kind": "search_backend_call", "backend": "glm", "cache_hit": False, "usd": 0.01},
              {"kind": "root_only_url", "results": [{"url": "https://x.co.il"}]},
              {"kind": "search_fallback_used"}]
    ops = search_operations(events)
    assert ops["guessed_url_refused"] == 1 and ops["irrelevant_result_refused"] == 1
    assert ops["search_calls_by_backend"] == {"glm": 1, "serper": 1}
    assert ops["search_usd_by_backend"] == {"glm": 0.01, "serper": 0.001}
    assert ops["search_off_site"] == 2 and ops["search_root_only_urls"] == 1 and ops["search_fallback_used"] == 1


def test_duckduckgo_backend_wraps_the_existing_parser(tmp_path):
    html = ('<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.icar.co.il%2Fx">'
            'iCar</a><a class="result__snippet">s</a></div>')

    class Get(Session):
        def get(self, url, params=None, headers=None, timeout=None, **kw):
            self.calls.append(("GET", url, params, headers))
            r = Resp(200)
            r.text = html
            return r
    session = Get()
    results, info = SB.make_backend("duckduckgo", session=session, lookup=keys()).query("g6", site="icar.co.il")
    assert session.calls[0][2] == {"q": "site:icar.co.il g6"}
    assert [r.url for r in results] == ["https://www.icar.co.il/x"] and info.billable is False and info.usd == 0.0
