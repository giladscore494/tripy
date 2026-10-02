"""DocumentCache under concurrent vehicle workers: single flight per key, atomic files, no global lock.
Fake HTTP only."""

import json
import threading
import time

from conftest import FakeResponse

from src.storage.cache import DocumentCache
from src.tools import ToolConfig, ToolContext, dispatch
from src.tools.evidence import EvidenceStore


def hammer(n, fn):
    barrier = threading.Barrier(n)
    out = [None] * n

    def run(i):
        barrier.wait(5)
        out[i] = fn(i)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return out


class SlowGLMSearch:
    settings = type("S", (), {"search_engine": "search-prime"})()

    def __init__(self):
        self.calls, self.lock = 0, threading.Lock()

    def web_search(self, query, count=8, domain=None):
        with self.lock:
            self.calls += 1
        time.sleep(0.2)
        return [{"title": query, "url": "https://result.example/1", "snippet": "", "site": "", "published": ""}]


class SlowSession:
    """Shared fake HTTP: counts GETs per URL and the peak of concurrent GETs."""

    def __init__(self, routes, delay=0.2):
        self.routes, self.delay = routes, delay
        self.calls: dict[str, int] = {}
        self.lock = threading.Lock()
        self.inflight = self.peak = 0

    def get(self, url, **kwargs):
        with self.lock:
            self.calls[url] = self.calls.get(url, 0) + 1
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        time.sleep(self.delay)
        with self.lock:
            self.inflight -= 1
        body = self.routes[url]
        return FakeResponse(body, url=url)


def worker_ctx(cache, session=None, glm=None):
    """One vehicle worker: its own counters / evidence / session, the SHARED cache."""
    return ToolContext(cache=cache, evidence=EvidenceStore(), config=ToolConfig(search_backend="glm"),
                       session=session, glm=glm)


def test_twenty_identical_searches_make_exactly_one_search_call(tmp_path):
    cache, glm = DocumentCache(tmp_path / "cache"), SlowGLMSearch()
    contexts = [worker_ctx(cache, glm=glm) for _ in range(20)]
    results = hammer(20, lambda i: dispatch(contexts[i], "search_web", {"query": "cadillac lyriq 2025 specs"}))
    assert glm.calls == 1                                                   # 1 Search-Prime HTTP call
    assert all(r["results"] == results[0]["results"] for r in results)      # identical result to every caller
    assert sum(1 for r in results if r["cache_hit"]) == 19
    assert sum(c.counters["search_api_calls"] for c in contexts) == 1      # billable calls counted once
    stats = cache.stats_snapshot()
    assert stats["cross_vehicle_search_singleflight_reuses"] >= 1 and stats["cross_vehicle_cache_waits"] >= 1


def test_fifteen_workers_fetching_the_same_pdf_download_it_once(tmp_path):
    url = "https://www.cadillac.co.il/lyriq/spec.html"
    session = SlowSession({url: b"<html><body><h1>LYRIQ</h1><p>Torque 610 Nm</p></body></html>"})
    cache = DocumentCache(tmp_path / "cache")
    contexts = [worker_ctx(cache, session=session) for _ in range(15)]
    results = hammer(15, lambda i: dispatch(contexts[i], "fetch_url", {"url": url}))
    assert session.calls == {url: 1}                                        # 1 network GET
    assert len({r["document_id"] for r in results}) == 1                    # same document_id for everyone
    assert sum(1 for r in results if not r["cache_hit"]) == 1
    folders = list((tmp_path / "cache" / "documents").iterdir())
    assert len(folders) == 1
    meta = json.loads((folders[0] / "meta.json").read_text("utf-8"))        # valid, complete JSON
    assert meta["document_id"] == results[0]["document_id"] and (folders[0] / "body.bin").stat().st_size == meta["bytes"]
    assert not list((tmp_path / "cache").rglob("*.tmp"))                    # no half-written files left behind
    assert cache.stats_snapshot()["cross_vehicle_document_singleflight_reuses"] >= 1


def test_different_urls_are_fetched_concurrently(tmp_path):
    urls = [f"https://site{i}.example/page" for i in range(8)]
    session = SlowSession({u: f"<html><body>page {i}</body></html>".encode() for i, u in enumerate(urls)}, delay=0.3)
    cache = DocumentCache(tmp_path / "cache")
    contexts = [worker_ctx(cache, session=session) for _ in urls]
    started = time.monotonic()
    hammer(len(urls), lambda i: dispatch(contexts[i], "fetch_url", {"url": urls[i]}))
    assert session.peak > 1                                                 # not serialized behind one lock
    assert time.monotonic() - started < 0.3 * len(urls) * 0.6


def test_derived_extractions_are_computed_once_under_concurrency(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    record = cache.put("fetch", "https://a.example/", b"<html><table><tr><td>Length</td><td>4880 mm</td></tr></table>",
                       {"doc_type": "html", "content_type": "text/html"}, "Length 4880 mm")
    calls = {"n": 0}
    lock = threading.Lock()

    def compute():
        with lock:
            calls["n"] += 1
        time.sleep(0.1)
        return [{"rows": [["Length", "4880 mm"]]}]

    values = hammer(10, lambda i: cache.derived(record["document_id"], "tables", compute))
    assert calls["n"] == 1 and all(v[0] == values[0][0] for v in values)
