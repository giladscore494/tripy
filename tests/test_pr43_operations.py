"""PR #43 operations (no network, no browser):

    H7  an empty 2xx HTML shell of an official / importer domain is re-fetched once with render_page (25 s budget,
        cached like a fetch, event rendered_fallback); a rendered page that is still empty or a challenge marks the
        domain unreadable for the run (acq_unreadable_domains) and no further fetch is spent on it
    H8  a shared, process-wide adaptive limiter on GLM calls: a 429 honours Retry-After (else backoff + jitter), lowers
        the pool's concurrency, never spends an attempt while the rate-limit budget remains; recovery is slow
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from conftest import FakeResponse
from src.concurrency import (RATE_LIMIT_RECOVER_AFTER_S, RATE_LIMIT_RECOVER_STEP_S, ConcurrencyController)
from src.diagnostics import operations_summary, vehicle_row, vehicle_diagnostics
from src.glm_client import GLMClient, GLMError, GLMSettings
from src.storage import trace
from src.tools import dispatch
from src.tools import fetch as fetch_module
from src.tools import render as render_module

SHELL = (b'<html><head><title>Audi</title><script src="/etc.clientlibs/app.js"></script></head>'
         b'<body><div id="root"></div><noscript>JavaScript</noscript></body></html>')
SPEC_HTML = ("<html><head><title>Audi Q8 TFSI e | Audi Israel</title></head><body><h1>Audi Q8 55 TFSI e quattro</h1>"
             + "".join(f"<p>נתון טכני מספר {i}: מנוע 3.0 TFSI V6 בהספק 340 כ\"ס, הנעה quattro.</p>" for i in range(20))
             + "</body></html>")
CHALLENGE_HTML = ("<html><head><title>Just a moment...</title></head><body><h1>Checking your browser before accessing "
                  "audi.co.il</h1><div id='cf-challenge'>Please enable JavaScript and cookies to continue</div>"
                  "</body></html>")
AUDI = "https://www.audi.co.il/il/web/he/models/q8/q8-tfsi-e.html"


@pytest.fixture()
def audi_ctx(make_ctx, monkeypatch):
    if "playwright.sync_api" not in sys.modules:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:               # the fallback only needs render_page to believe Playwright exists
            monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
            monkeypatch.setitem(sys.modules, "playwright.sync_api", types.ModuleType("playwright.sync_api"))
    events: list[tuple[str, dict]] = []

    def factory(routes, rendered_html: str | None):
        ctx = make_ctx(routes)
        ctx.vehicle = {"manufacturer": "אאודי"}
        ctx.log = lambda kind, **data: events.append((kind, data))
        calls: list[tuple] = []

        def fake_render(url, wait_ms, timeout_s):
            calls.append((url, timeout_s))
            if rendered_html is None:
                raise TimeoutError("render timed out")
            return {"status": 200, "final_url": url, "html": rendered_html, "links": []}

        monkeypatch.setattr(render_module, "_render", fake_render)
        return ctx, calls, events

    return factory


def _kinds(events, kind):
    return [data for k, data in events if k == kind]


# --- H7 -----------------------------------------------------------------------------------------------------------------

def test_empty_importer_shell_is_rendered_once_and_cached(audi_ctx):
    ctx, calls, events = audi_ctx({AUDI: FakeResponse(SHELL)}, SPEC_HTML)
    first = dispatch(ctx, "fetch_url", {"url": AUDI})
    assert first["rendered_fallback"] is True and first["text_chars"] >= fetch_module.RENDER_FALLBACK_MIN_TEXT
    assert first["fetch_document_id"] != first["document_id"]
    assert ctx.cache.get(first["document_id"])["extraction_path"] == "playwright.chromium"
    assert calls == [(AUDI, fetch_module.RENDER_FALLBACK_TIMEOUT_S)]                      # 25 s budget
    fallback = _kinds(events, "rendered_fallback")
    assert len(fallback) == 1 and fallback[0]["readable"] and fallback[0]["fetch_text_chars"] < 500
    # a second fetch reuses the cached fetch and the cached rendering: no new browser run
    second = dispatch(ctx, "fetch_url", {"url": AUDI})
    assert second["document_id"] == first["document_id"] and second["cache_hit"] is True and len(calls) == 1
    assert ctx.counters["acq_render_fallbacks"] == 2 and not ctx.unreadable_domains


def test_a_challenge_page_marks_the_domain_unreadable_for_the_run(audi_ctx):
    other = "https://www.audi.co.il/il/web/he/models/q7.html"
    ctx, calls, events = audi_ctx({AUDI: FakeResponse(SHELL), other: FakeResponse(SHELL)}, CHALLENGE_HTML)
    result = dispatch(ctx, "fetch_url", {"url": AUDI})
    assert result["unreadable"] == "challenge_page" and result["rendered_fallback"] is True
    assert ctx.unreadable_domains == {"audi.co.il": "challenge_page"}
    assert ctx.counters["acq_unreadable_domains"] == 1
    assert _kinds(events, "domain_unreadable") == [{"domain": "audi.co.il", "url": AUDI, "reason": "challenge_page"}]
    requests_before = len(ctx.session.calls)
    skipped = dispatch(ctx, "fetch_url", {"url": other})
    assert skipped["error"] == "domain_unreadable" and len(ctx.session.calls) == requests_before   # no fetch spent
    assert dispatch(ctx, "render_page", {"url": other})["error"] == "domain_unreadable" and len(calls) == 1
    assert ctx.counters["acq_unreadable_skips"] == 2
    diag = operations_summary([{"kind": k, **d} for k, d in events])
    assert diag["unreadable_domains"] == ["audi.co.il"] and diag["rendered_fallbacks"] == 1


def test_a_render_that_fails_or_stays_empty_is_unreadable_too(audi_ctx):
    ctx, _, _ = audi_ctx({AUDI: FakeResponse(SHELL)}, None)                                 # the render timed out
    assert dispatch(ctx, "fetch_url", {"url": AUDI})["unreadable"] == "render_failed"
    ctx2, _, _ = audi_ctx({AUDI: FakeResponse(SHELL)}, "<html><body><div id='root'></div></body></html>")
    ctx2.cache = ctx2.cache.__class__(ctx2.cache.root.parent / "cache2")
    assert dispatch(ctx2, "fetch_url", {"url": AUDI})["unreadable"] == "empty_after_render"


def test_no_fallback_for_other_domains_full_pages_or_errors(audi_ctx):
    media = "https://www.cartube.co.il/audi-q8"
    full = "https://www.audi.co.il/il/web/he/full.html"
    ctx, calls, _ = audi_ctx({media: FakeResponse(SHELL), full: FakeResponse(SPEC_HTML.encode())}, SPEC_HTML)
    assert "rendered_fallback" not in dispatch(ctx, "fetch_url", {"url": media})       # not an official domain
    assert "rendered_fallback" not in dispatch(ctx, "fetch_url", {"url": full})        # readable as fetched
    assert "rendered_fallback" not in dispatch(ctx, "fetch_url", {"url": "https://www.audi.co.il/missing"})  # 404
    assert calls == [] and not ctx.unreadable_domains


def test_a_host_without_a_browser_keeps_the_fetch_and_marks_nothing(make_ctx):
    ctx = make_ctx({AUDI: FakeResponse(SHELL)})
    ctx.vehicle = {"manufacturer": "אאודי"}
    result = dispatch(ctx, "fetch_url", {"url": AUDI})
    assert "rendered_fallback" not in result and not ctx.unreadable_domains
    assert fetch_module._RENDER_UNAVAILABLE is True                     # no later fetch of this process tries again


def test_dockerfile_installs_only_chromium_with_the_official_installer():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    docker = (root / "Dockerfile").read_text("utf-8")
    assert "python -m playwright install --with-deps --only-shell chromium" in docker
    assert "stealth" not in docker.lower().replace("no stealth", "")
    assert any(line.startswith("playwright==") for line in (root / "requirements.txt").read_text().splitlines())


# --- H8 -----------------------------------------------------------------------------------------------------------------

class PostResponse:
    def __init__(self, status: int, retry_after: str | None = None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json", **({"Retry-After": retry_after} if retry_after else {})}
        self.text = "{}" if status == 200 else '{"error":{"code":"1302","message":"rate limit"}}'

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class ScriptedSession:
    """POST replies: the first `limited` requests (across threads) get 429, then 200."""

    def __init__(self, limited: int, retry_after: str | None = None):
        self.limited, self.retry_after = limited, retry_after
        self.calls = 0
        self.lock = threading.Lock()

    def post(self, url, **kwargs):
        with self.lock:
            self.calls += 1
            limited = self.calls <= self.limited
        return PostResponse(429, self.retry_after) if limited else PostResponse(200)


def _client(session, controller, sleeps, events=None):
    settings = GLMSettings(api_key="k", model="glm-5.3-flash", chat_max_attempts=2)
    client = GLMClient(settings, session=session, sleeper=sleeps.append, concurrency=controller,
                       hook=(lambda kind, **d: events.append({"kind": kind, **d})) if events is not None else None)
    client.jitter = lambda: 0.0
    return client


def test_a_429_burst_is_retried_without_spending_attempts_and_lowers_concurrency():
    controller = ConcurrencyController()
    now = [1000.0]
    controller.clock = lambda: now[0]
    sleeps, events = [], []
    client = _client(ScriptedSession(limited=5, retry_after="3"), controller, sleeps, events)
    reply = client.chat([{"role": "user", "content": "hi"}])          # chat_max_attempts=2, five 429s in a row
    assert reply.message["content"] == "ok"
    assert sleeps == [3.0] * 5                                          # Retry-After honoured
    assert controller.limit_for("glm-5.3-flash") == 24                  # 48 halved once for the burst
    assert controller.rate_limit_stats() == {"rate_limited_retries": 5, "rate_limited_failures": 0}
    stats = trace.api_stats(events)
    assert stats["rate_limited_retries"] == 5 and stats["rate_limited_failures"] == 0 and stats["api_successes"] == 1
    assert all(e["rate_limited"] and e["will_retry"] for e in events if e["kind"] == "api_error")


class PerVehicleSession(ScriptedSession):
    """The production burst: 22 x 429 spread over 7 parallel vehicles (4 + 3 x 6), each vehicle's first requests."""

    def __init__(self, quotas):
        super().__init__(0)
        self.quotas, self.left = list(quotas), {}

    def post(self, url, **kwargs):
        me = threading.current_thread()          # kept alive by the test: never reused
        with self.lock:
            self.calls += 1
            if me not in self.left:
                self.left[me] = self.quotas.pop()
            limited = self.left[me] > 0
            self.left[me] -= 1 if limited else 0
        return PostResponse(429) if limited else PostResponse(200)


def test_parallel_vehicles_share_the_limiter_and_no_chunk_fails():
    controller = ConcurrencyController()
    session = PerVehicleSession([4, 3, 3, 3, 3, 3, 3])                  # 22 x 429 across 7 parallel vehicles
    sleeps, errors, replies = [], [], []

    def vehicle():
        client = _client(session, controller, sleeps)
        try:
            for _ in range(3):                                          # three sweep chunks each
                replies.append(client.chat([{"role": "user", "content": "chunk"}]))
        except GLMError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=vehicle) for _ in range(7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(replies) == 21
    stats = controller.rate_limit_stats()
    assert stats == {"rate_limited_retries": 22, "rate_limited_failures": 0}
    assert controller.limit_for("glm-5.3-flash") < 48
    assert sleeps and all(s >= 2.0 for s in sleeps)                     # exponential backoff without Retry-After


def test_a_429_fails_only_when_the_rate_limit_budget_is_spent():
    controller = ConcurrencyController()
    sleeps, events = [], []
    client = _client(ScriptedSession(limited=10 ** 6), controller, sleeps, events)
    with pytest.raises(GLMError) as caught:
        client.chat([{"role": "user", "content": "hi"}])
    assert caught.value.rate_limited and caught.value.status == 429
    assert sum(sleeps) <= client.rate_limit_budget_s
    assert controller.rate_limit_stats()["rate_limited_failures"] == 1
    assert trace.api_stats(events)["rate_limited_failures"] == 1
    assert controller.limit_for("glm-5.3-flash") >= 1


def test_concurrency_recovers_slowly_after_the_last_429():
    controller = ConcurrencyController()
    now = [0.0]
    controller.clock = lambda: now[0]
    controller.note_rate_limited("chat", "glm-5.3-flash", retried=True)
    assert controller.limit_for("glm-5.3-flash") == 24
    controller.note_success("chat", "glm-5.3-flash")
    assert controller.limit_for("glm-5.3-flash") == 24                  # too soon after the 429
    now[0] = RATE_LIMIT_RECOVER_AFTER_S
    assert controller.note_success("chat", "glm-5.3-flash") == 25
    assert controller.note_success("chat", "glm-5.3-flash") == 25       # one slot per step
    now[0] += RATE_LIMIT_RECOVER_STEP_S
    assert controller.note_success("chat", "glm-5.3-flash") == 26
    now[0] += 10_000
    for _ in range(100):
        now[0] += RATE_LIMIT_RECOVER_STEP_S
        controller.note_success("chat", "glm-5.3-flash")
    assert controller.limit_for("glm-5.3-flash") == 48                  # never above the operational limit


def test_retry_after_http_date_and_backoff_with_jitter():
    controller = ConcurrencyController()
    client = _client(ScriptedSession(0), controller, [])
    client.jitter = lambda: 1.0
    assert client._rate_limit_wait(PostResponse(429), 1) == 2.5         # 2 s + 25 % jitter
    assert client._rate_limit_wait(PostResponse(429), 3) == 10.0
    assert client._rate_limit_wait(PostResponse(429, "Wed, 21 Oct 2015 07:28:00 GMT"), 1) == 0.0   # in the past
    assert client._rate_limit_wait(PostResponse(429, "900"), 1) == 120.0                           # clamped


def test_vehicle_row_reports_the_counters():
    events = [{"kind": "run_started", "seq": 1},
              {"kind": "api_error", "status": 429, "will_retry": True, "endpoint": "chat/completions"},
              {"kind": "api_error", "status": 429, "will_retry": False, "endpoint": "chat/completions"},
              {"kind": "rendered_fallback", "readable": False},
              {"kind": "domain_unreadable", "domain": "audi.co.il"}]
    row = vehicle_row(vehicle_diagnostics(events))
    assert (row["rate_limited_retries"], row["rate_limited_failures"]) == (1, 1)
    assert (row["acq_rendered_fallbacks"], row["acq_unreadable_domains"]) == (1, 1)
