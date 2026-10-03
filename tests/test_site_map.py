"""Importer site map (PR #31 Part D): robots.txt -> sitemap index -> gzipped sitemap with the standard library,
caps and depth, robots Disallow, the per-domain cache, ranking, and the contract acquisition message. Fake HTTP only."""

import gzip
import json

from conftest import FakeResponse, FakeSession
from fixtures import corolla_tail as tail
from test_phase_contracts import EU, PhaseClient, fetch, run, say

from src import site_map as S
from src.storage.cache import DocumentCache
from src.tools import ToolConfig, ToolContext
from src.tools.evidence import EvidenceStore

DOMAIN = "toyota.co.il"
BASE = f"https://{DOMAIN}"
SPEC_PDF = f"{BASE}/new-cars/corolla/specifications.pdf"
PRICES = f"{BASE}/new-cars/corolla/pricelist"
RAV4 = f"{BASE}/new-cars/rav4"
CROSS = f"{BASE}/new-cars/corolla-cross/specifications"
ABOUT = f"{BASE}/about"
PRIVATE = f"{BASE}/private/corolla-specifications"


def urlset(urls):
    body = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{body}</urlset>").encode()


def index(urls):
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{body}</sitemapindex>").encode()


def routes(robots=None):
    robots = robots if robots is not None else (f"User-agent: *\nDisallow: /private/\nSitemap: {BASE}/sitemap_index.xml\n")
    return {
        f"{BASE}/robots.txt": FakeResponse(robots.encode(), content_type="text/plain"),
        f"{BASE}/sitemap_index.xml": FakeResponse(index([f"{BASE}/models.xml.gz", f"{BASE}/nested.xml"]),
                                                   content_type="application/xml"),
        f"{BASE}/models.xml.gz": FakeResponse(gzip.compress(urlset([SPEC_PDF, PRICES, RAV4, CROSS, ABOUT, PRIVATE])),
                                              content_type="application/gzip"),
        f"{BASE}/nested.xml": FakeResponse(index([f"{BASE}/deep1.xml"]), content_type="application/xml"),
        f"{BASE}/deep1.xml": FakeResponse(index([f"{BASE}/deep2.xml"]), content_type="application/xml"),
        f"{BASE}/deep2.xml": FakeResponse(index([f"{BASE}/deep3.xml"]), content_type="application/xml"),
        f"{BASE}/deep3.xml": FakeResponse(urlset([f"{BASE}/too-deep/corolla"]), content_type="application/xml"),
    }


def ctx_for(tmp_path, session):
    return ToolContext(cache=DocumentCache(tmp_path / "cache"), evidence=EvidenceStore(), config=ToolConfig(),
                       vehicle={"manufacturer": "טויוטה"}, session=session)


def test_robots_index_and_gzipped_sitemap_with_depth_cap_and_cache_reuse(tmp_path):
    session = FakeSession(routes())
    ctx = ctx_for(tmp_path, session)
    site = S.build_site_map(ctx, [DOMAIN])
    entry = site["domains"][0]
    urls = [u for u, _ in site["urls"]]
    assert entry["cache_hit"] is False and SPEC_PDF in urls and RAV4 in urls
    # nested indexes followed to depth 3 only: deep3.xml (depth 4) is never read
    fetched = [c[1] for c in session.calls]
    assert f"{BASE}/deep2.xml" in fetched and f"{BASE}/deep3.xml" not in fetched
    assert f"{BASE}/too-deep/corolla" not in urls
    # robots Disallow: never offered
    offered = S.offer(site, model_names=["corolla", "קורולה"], other_families=["rav4", "corolla cross"], year=2024)
    assert PRIVATE not in [o["url"] for o in offered] and SPEC_PDF in [o["url"] for o in offered]
    # a second vehicle of the same importer: the cached URL list, no HTTP at all
    calls = len(session.calls)
    again = S.build_site_map(ctx_for(tmp_path, session), [DOMAIN])
    assert again["domains"][0]["cache_hit"] is True and len(session.calls) == calls
    assert [u for u, _ in again["urls"]] == urls


def test_caps_and_expiry(tmp_path):
    many = [f"{BASE}/p/{i}" for i in range(50)]
    site_routes = {f"{BASE}/robots.txt": FakeResponse(b"", status=404),
                   f"{BASE}/sitemap.xml": FakeResponse(urlset(many), content_type="application/xml")}
    session = FakeSession(site_routes)
    record = S.crawl_domain(DOMAIN, lambda u: (lambda r: (r.status_code, r.content))(session.get(u)),
                            deadline=__import__("time").monotonic() + 5, max_urls=10)
    assert len(record["urls"]) == 10 and record["truncated"]
    # fallback paths when robots.txt names no sitemap
    assert f"{BASE}/sitemap.xml" in record["sitemaps"]
    # the per-domain cache expires after its TTL
    cache = DocumentCache(tmp_path / "c")
    clock = [1000.0]
    S.cached_domain(cache, DOMAIN, lambda: {"urls": ["a"], "sitemaps": ["s"]}, now=lambda: clock[0])
    assert S.cached_domain(cache, DOMAIN, lambda: {"urls": ["b"]}, now=lambda: clock[0])[0]["urls"] == ["a"]
    clock[0] += S.TTL_S + 1
    assert S.cached_domain(cache, DOMAIN, lambda: {"urls": ["b"]}, now=lambda: clock[0])[0]["urls"] == ["b"]


def test_entity_declarations_are_refused():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset><url><loc>&a;</loc></url></urlset>'
    assert S.parse_sitemap(evil) == ("unknown", [])


def test_ranking_puts_the_model_spec_pdf_first_and_drops_other_families():
    ranked = S.rank_urls([ABOUT, RAV4, CROSS, PRICES, SPEC_PDF], model_names=["corolla", "קורולה"], year=2024,
                         other_families=["rav4", "corolla cross", "yaris"])
    urls = [r["url"] for r in ranked]
    assert urls[0] == SPEC_PDF
    assert RAV4 not in urls and CROSS not in urls and ABOUT not in urls
    assert "pdf" in ranked[0]["reasons"] and any(r.startswith("model:") for r in ranked[0]["reasons"])
    # a cluster's own intent words: the price list first for the commercial cluster
    commercial = S.rank_urls([SPEC_PDF, PRICES], model_names=["corolla"], cluster="commercial")
    assert commercial[0]["url"] == PRICES
    # already fetched URLs are not offered again; at most 15
    assert S.rank_urls([SPEC_PDF], model_names=["corolla"], exclude=[SPEC_PDF]) == []
    assert len(S.rank_urls([f"{BASE}/corolla/{i}" for i in range(40)], model_names=["corolla"])) == S.TOP_N


def test_contract_acquisition_message_lists_offered_urls(tmp_path):
    client = PhaseClient([fetch("a", EU), say({"done": True, "reason": "enough"})])
    result, events = run_with_site(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False,
                         primary_research_min_base_documents=0, primary_research_min_base_scoped_coverage=0)
    first = client.requests[0]["messages"][1]["content"]
    assert "Known pages on the official site (from its sitemap; fetch these instead of guessing URLs):" in first
    assert SPEC_PDF in first and RAV4 not in first
    event = next(e for e in events if e["kind"] == "site_map")
    assert event["url_count"] >= 6 and SPEC_PDF in [o["url"] for o in event["offered_urls"]]
    assert {"domains", "sitemap_count", "duration_ms", "cache_hit", "errors"} <= set(event)
    usage = result["primary_research"]["site_map"]
    assert usage["offered"] >= 1 and usage["fetched"] == 0
    from src import diagnostics as D
    row = D.vehicle_row(D.vehicle_diagnostics(events, result=result))
    assert row["acq_site_map_offered"] == usage["offered"] and row["acq_site_map_fetched"] == 0
    assert result["site_map"] is True and next(e for e in events if e["kind"] == "run_started")["site_map"] is True


def run_with_site(tmp_path, client, **cfg):
    """test_phase_contracts.run with the importer's robots.txt and sitemaps served next to the document routes."""
    from test_phase_contracts import ROUTES

    from src.agent import AgentConfig, run_vehicle
    from src.storage.run_log import RunLog, read_events
    from fixtures.corolla_touring import PAYLOAD, VEHICLE

    session = FakeSession({**{url: FakeResponse(body.encode("utf-8"), content_type=ctype, url=url)
                              for url, (body, ctype) in ROUTES.items()}, **routes()})
    log = RunLog(tmp_path / "runs", "b", "38626")
    config = AgentConfig(**{"research_memory_enabled": False, "requested_fields": tail.FIELDS,
                            "no_new_research_turns": 0, **cfg})
    result = run_vehicle({"upstream_record_id": "38626"}, PAYLOAD, client=client, cache=DocumentCache(tmp_path / "c"),
                         run_log=log, vehicle_meta=VEHICLE, config=config, tool_config=ToolConfig(), session=session)
    return result, read_events(log.events_path)


def test_site_map_failure_is_logged_and_the_run_continues(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("sitemap host unreachable")

    monkeypatch.setattr(S, "build_site_map", boom)
    client = PhaseClient([fetch("a", EU), say({"done": True, "reason": "enough"})])
    result, events = run(tmp_path, client, acquisition_mode="contract", field_recovery_enabled=False,
                         primary_research_min_base_documents=0, primary_research_min_base_scoped_coverage=0)
    failed = [e for e in events if e["kind"] == "site_map_failed"]
    assert failed and "sitemap host unreachable" in failed[0]["error"]
    assert "Known pages on the official site" not in client.requests[0]["messages"][1]["content"]
    assert result["status"] == "completed" and result["research_steps"] == 2


def test_site_map_off_sends_nothing(tmp_path):
    client = PhaseClient([fetch("a", EU), say({"done": True, "reason": "enough"})])
    result, events = run_with_site(tmp_path, client, acquisition_mode="contract", site_map=False,
                         field_recovery_enabled=False, primary_research_min_base_documents=0,
                         primary_research_min_base_scoped_coverage=0)
    assert not [e for e in events if e["kind"] in ("site_map", "site_map_failed")]
    assert "Known pages" not in client.requests[0]["messages"][1]["content"]
    assert "site_map" not in result["primary_research"]
