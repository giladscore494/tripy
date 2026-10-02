"""Concurrent vehicle execution: per-model chat pools, Search-Prime pool, per-attempt slots, hook isolation,
failure isolation, cancellation and batch observability. Fake HTTP sessions only: no network, no paid calls."""

import json
import threading
import time

import pytest

from test_run_config import PostResponse, ScriptedPostSession, chat_reply, tool_call
from test_tools_smoke import ScriptedGLM, _call

from src.agent import AgentConfig, run_vehicle
from src.benchmark import batch_observability, run_batch, start_batch, vehicle_client_factory
from src.concurrency import (DEFAULT_MODEL_LIMITS, PROVIDER_MODEL_LIMITS, SEARCH_PRIME_PROVIDER_LIMIT,
                             BatchCancelled, ConcurrencyController)
from src.glm_client import GLMClient, GLMError, GLMSettings
from src.storage.cache import DocumentCache
from src.storage.run_log import RunLog, load_batch, read_events, update_batch
from src.tools import ToolConfig

KEY = "test-key-not-real"  # pragma: allowlist secret


class InflightSession:
    """Counts concurrent POSTs per model id (or 'search') at the HTTP layer; each call takes `delay` s."""

    def __init__(self, delay=0.15, fail_first=0):
        self.delay, self.lock = delay, threading.Lock()
        self.inflight: dict[str, int] = {}
        self.peak: dict[str, int] = {}
        self.calls = 0
        self.fail_first = fail_first

    def post(self, url, headers=None, data=None, timeout=None):
        payload = json.loads(data)
        key = payload.get("model") or "search"
        with self.lock:
            self.calls += 1
            n = self.calls
            self.inflight[key] = self.inflight.get(key, 0) + 1
            self.peak[key] = max(self.peak.get(key, 0), self.inflight[key])
        try:
            time.sleep(self.delay)
        finally:
            with self.lock:
                self.inflight[key] -= 1
        if n <= self.fail_first:
            return PostResponse(429, text="rate limited")
        if key == "search":
            return PostResponse(200, {"search_result": [{"title": "t", "link": "https://x.example/", "content": "c"}]})
        return chat_reply({"role": "assistant", "content": "ok"})


def client(session, controller, model="glm-5.3-flash", **kw):
    settings = GLMSettings(api_key=KEY, model=model, chat_max_attempts=kw.pop("attempts", 1), search_max_attempts=1)
    return GLMClient(settings, session=session, concurrency=controller, sleeper=lambda s: None, **kw)


def hammer(n, fn):
    threads = [threading.Thread(target=fn, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not any(t.is_alive() for t in threads)


# --- limits ------------------------------------------------------------------------------------------

def test_known_limits_defaults_and_conservative_unknown_model(monkeypatch):
    assert PROVIDER_MODEL_LIMITS == {"glm-5.3-flash": 50, "glm-5.3-flashx": 20, "glm-5.3": 5}
    assert SEARCH_PRIME_PROVIDER_LIMIT == 5 and DEFAULT_MODEL_LIMITS["glm-5.3-flash"] == 48
    c = ConcurrencyController()
    assert (c.limit_for("GLM-5.3-Flash"), c.provider_limit_for("glm-5.3-flash")) == (48, 50)
    assert c.limit_for("glm-5.3") == 5 and c.search.limit == 5
    assert c.limit_for("some-new-model") == 1                          # never assumed to share Flash's 50
    assert c.set_model_limit("glm-5.3-flash", 80) == 50                # never above the provider limit
    assert c.set_search_limit(9) == 5
    monkeypatch.setenv("GLM_CHAT_MAX_INFLIGHT", "60")
    monkeypatch.setenv("GLM_UNKNOWN_MODEL_MAX_INFLIGHT", "3")
    env = ConcurrencyController.from_env()
    assert env.limit_for("glm-5.3-flash") == 50 and env.limit_for("glm-5.3") == 5 and env.limit_for("x") == 3
    monkeypatch.setenv("GLM_CHAT_MAX_INFLIGHT_BY_MODEL", '{"glm-5.3-flash": 30}')
    assert ConcurrencyController.from_env().limit_for("glm-5.3-flash") == 30
    config = c.config(["glm-5.3-flash"])
    assert config["model_limits"]["glm-5.3-flash"] == 50 and config["provider_model_limits"]["glm-5.3-flash"] == 50
    assert config["search_max_inflight"] == 5


def test_flash_never_exceeds_its_configured_48_in_flight():
    controller, session = ConcurrencyController(), InflightSession(delay=0.2)
    errors = []

    def call(i):
        try:
            client(session, controller).chat([{"role": "user", "content": str(i)}])
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    with controller.observe() as obs:
        hammer(120, call)
    assert not errors and session.calls == 120
    assert session.peak["glm-5.3-flash"] == 48                       # reached, never exceeded
    assert obs.peak_chat_inflight_by_model["glm-5.3-flash"] == 48
    assert obs.chat_queue_wait_count > 0 and obs.chat_queue_wait_ms > 0
    assert controller.chat_pool("glm-5.3-flash").active == 0         # every slot released


def test_search_prime_never_exceeds_five(make_ctx):
    controller, session = ConcurrencyController(), InflightSession(delay=0.1)

    def search(i):
        client(session, controller).web_search(f"query {i}")

    with controller.observe() as obs:
        hammer(20, search)
    assert session.calls == 20 and session.peak["search"] <= 5 and obs.peak_search_inflight <= 5
    assert session.peak["search"] == 5 and obs.search_queue_wait_count > 0


def test_research_and_finalizer_models_use_independent_pools():
    controller, session = ConcurrencyController(), InflightSession(delay=0.15)

    def call(i):
        model = "glm-5.3" if i % 2 else "glm-5.3-flash"
        c = client(session, controller, model="glm-5.3-flash")
        c.chat([{"role": "user", "content": "x"}], model=model)       # the finalizer passes its own model id

    with controller.observe() as obs:
        hammer(140, call)
    assert session.peak["glm-5.3-flash"] <= 48 and session.peak["glm-5.3"] <= 5
    assert session.peak["glm-5.3"] == 5 and session.peak["glm-5.3-flash"] > 5   # Flash is not throttled to 5
    assert set(obs.peak_chat_inflight_by_model) == {"glm-5.3-flash", "glm-5.3"}


def test_slot_is_per_http_attempt_and_released_before_the_retry_sleep():
    controller = ConcurrencyController(model_limits={"glm-5.3": 1})
    session = InflightSession(delay=0.01, fail_first=1)
    seen = []
    c = client(session, controller, model="glm-5.3", attempts=2)
    c.sleeper = lambda s: seen.append(controller.chat_pool("glm-5.3").active)
    activity = []
    c.activity_hook = lambda kind, **d: activity.append((kind, d.get("attempt")))
    c.chat([{"role": "user", "content": "x"}])
    assert session.calls == 2 and seen == [0]          # nothing held while sleeping between attempts
    assert [k for k, _ in activity] == ["model_request_started", "model_request_finished"] * 2
    assert [a for _, a in activity] == [1, 1, 2, 2]     # a retry reacquires its own slot


def test_queue_wait_events_name_the_model_and_the_wait():
    controller = ConcurrencyController(model_limits={"glm-5.3": 1})
    gate, session = threading.Event(), InflightSession(delay=0.2)
    seen = []
    lock = threading.Lock()

    def call(i):
        c = client(session, controller, model="glm-5.3")
        c.activity_hook = lambda kind, **d: (lock.acquire(), seen.append((i, kind, d)), lock.release())
        c.chat([{"role": "user", "content": "x"}])

    hammer(2, call)
    kinds = [k for _, k, _ in seen]
    assert kinds.count("model_queue_wait_started") == 1 and kinds.count("model_slot_acquired") == 1
    waited = next(d for _, k, d in seen if k == "model_slot_acquired")
    assert waited["model"] == "glm-5.3" and waited["limit"] == 1 and waited["wait_ms"] >= 100
    assert all("Bearer" not in json.dumps(d) and KEY not in json.dumps(d) for _, _, d in seen)


# --- run_batch ---------------------------------------------------------------------------------------

def vehicles(n):
    return [{"upstream_record_id": str(1000 + i), "ordinal": i + 1, "manufacturer": "m", "model": "x",
             "year": 2025, "trim": "t"} for i in range(n)]


def test_ten_vehicles_run_concurrently_and_return_in_benchmark_order():
    vs = vehicles(10)
    rows = {v["upstream_record_id"]: {"upstream_record_id": v["upstream_record_id"]} for v in vs}
    lock, state = threading.Lock(), {"now": 0, "peak": 0}

    def run_one(vehicle, row):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.25 - 0.02 * vehicle["ordinal"])   # later vehicles finish first
        with lock:
            state["now"] -= 1
        return {"record_id": vehicle["upstream_record_id"], "status": "completed"}

    stats = {}
    results = run_batch(vs, rows, run_one, max_workers=4, stats=stats)
    assert [r["record_id"] for r in results] == [v["upstream_record_id"] for v in vs]
    assert 1 < state["peak"] <= 4 and stats["peak_vehicle_workers"] == state["peak"]
    sequential = run_batch(vs[:3], rows, run_one)                   # max_workers=1: the historical loop
    assert [r["record_id"] for r in sequential] == ["1000", "1001", "1002"]


def test_a_failing_worker_never_cancels_the_rest():
    vs = vehicles(6)
    rows = {v["upstream_record_id"]: {} for v in vs}

    def run_one(vehicle, row):
        if vehicle["ordinal"] == 3:
            raise GLMError("HTTP 500 from chat/completions", status=500)
        time.sleep(0.05)
        return {"record_id": vehicle["upstream_record_id"], "status": "completed"}

    stats = {}
    results = run_batch(vs, rows, run_one, max_workers=3, stats=stats)
    assert [r["status"] for r in results] == ["completed", "completed", "error", "completed", "completed", "completed"]
    assert "HTTP 500" in results[2]["error"] and stats["worker_errors"] == 1


def test_cancelling_a_batch_stops_scheduling_and_interrupts_running_workers():
    vs = vehicles(8)
    rows = {v["upstream_record_id"]: {} for v in vs}
    started, cancel = [], threading.Event()

    def run_one(vehicle, row):
        started.append(vehicle["upstream_record_id"])
        while not cancel.is_set():          # a running vehicle notices cancellation at its next safe point
            time.sleep(0.01)
        raise BatchCancelled()

    polls = {"n": 0}

    def on_poll():
        polls["n"] += 1
        if polls["n"] == 3:
            raise KeyboardInterrupt          # e.g. the Streamlit stop reaching the script thread

    stats = {}
    with pytest.raises(KeyboardInterrupt):
        run_batch(vs, rows, run_one, max_workers=2, cancel_event=cancel, on_poll=on_poll, poll_interval_s=0.02,
                  stats=stats)
    time.sleep(0.2)
    assert cancel.is_set() and stats["cancelled"]
    assert len(started) == 2                # the six queued vehicles never began


# --- per-vehicle clients: hook isolation ----------------------------------------------------------

def scripted_vehicle_session(tag):
    page = f"https://{tag}.example/spec"
    return ScriptedPostSession({
        "chat/completions": [
            PostResponse(200, {"id": f"resp-{tag}-1", "model": "glm-5.3-flash", "choices": [{"message": tool_call(
                "c1", "store_evidence", {"field": "torque_nm", "value": 600, "market": "IL", "source_url": page}),
                "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}),
            PostResponse(200, {"id": f"resp-{tag}-2", "model": "glm-5.3-flash", "choices": [{"message": {
                "role": "assistant", "content": json.dumps({"summary": tag, "fields": {}})}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}),
        ]})


class SlowScripted(ScriptedPostSession):
    def post(self, url, headers=None, data=None, timeout=None):
        time.sleep(0.05)
        return super().post(url, headers=headers, data=data, timeout=timeout)


def test_concurrent_vehicles_never_mix_api_events_between_traces(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    controller = ConcurrencyController()
    sessions = {}
    for tag in ("A", "B", "C", "D"):
        s = scripted_vehicle_session(tag)
        sessions[tag] = SlowScripted(s.routes)
    vs = [{"upstream_record_id": tag, "ordinal": i} for i, tag in enumerate(sessions)]
    rows = {tag: {"upstream_record_id": tag} for tag in sessions}
    settings = GLMSettings(api_key=KEY, model="glm-5.3-flash")

    def run_one(vehicle, row):
        tag = vehicle["upstream_record_id"]
        c = GLMClient(settings, session=sessions[tag], concurrency=controller)
        return run_vehicle(row, {"identity": {"government_record_id": tag}}, client=c, cache=cache,
                           run_log=RunLog(tmp_path / "runs", "b", tag),
                           config=AgentConfig(max_steps=3, field_recovery_enabled=False, requested_fields=["torque_nm"]),
                           tool_config=ToolConfig())

    results = run_batch(vs, rows, run_one, max_workers=4)
    assert [r["output"]["summary"] for r in results] == ["A", "B", "C", "D"]
    for tag in sessions:
        events = read_events(tmp_path / "runs" / "b" / tag / "events.jsonl")
        ids = {e["response_meta"]["id"] for e in events if e["kind"] == "model_response"}
        assert ids == {f"resp-{tag}-1", f"resp-{tag}-2"}            # A events only in A/events.jsonl, etc.
        lifecycle = [e for e in events if e["kind"] in ("model_request_started", "model_request_finished")]
        assert len(lifecycle) == 4 and {e["record_id"] for e in lifecycle} == {tag}
        assert all(e["phase"] in ("research", "finalization") for e in lifecycle)


def test_one_client_cannot_serve_two_running_vehicles(tmp_path, make_ctx):
    ctx = make_ctx()
    gate, inside = threading.Event(), threading.Event()

    class Blocking(ScriptedGLM):
        def chat(self, messages, tools=None, **kwargs):
            inside.set()
            gate.wait(5)
            return super().chat(messages, tools=tools, **kwargs)

    shared = Blocking([{"role": "assistant", "content": json.dumps({"summary": "a", "fields": {}})}])
    cfg = AgentConfig(max_steps=1, field_recovery_enabled=False, requested_fields=["torque_nm"])
    first = threading.Thread(target=run_vehicle, args=({"upstream_record_id": "A"}, {}),
                             kwargs=dict(client=shared, cache=ctx.cache, run_log=RunLog(tmp_path, "b", "A"), config=cfg,
                                         tool_config=ToolConfig(), session=ctx.session))
    first.start()
    inside.wait(5)
    with pytest.raises(RuntimeError, match="one GLMClient per vehicle"):
        run_vehicle({"upstream_record_id": "B"}, {}, client=shared, cache=ctx.cache,
                    run_log=RunLog(tmp_path, "b", "B"), config=cfg, tool_config=ToolConfig(), session=ctx.session)
    gate.set()
    first.join(5)
    assert shared._tripy_active_run is None


def test_vehicle_client_factory_builds_independent_clients_on_one_controller():
    controller = ConcurrencyController()
    make = vehicle_client_factory(GLMSettings(api_key=KEY, model="glm-5.3-flash"), controller)
    a, b = make(), make()
    assert a is not b and a.session is not b.session and a.concurrency is b.concurrency is controller


def test_cancel_event_interrupts_run_vehicle_and_persists_partial_state(tmp_path, make_ctx):
    ctx = make_ctx()
    cancel = threading.Event()

    class CancelsAfterFirstTurn(ScriptedGLM):
        def chat(self, messages, tools=None, **kwargs):
            reply = super().chat(messages, tools=tools, **kwargs)
            cancel.set()                         # another thread cancels the batch while this turn runs
            return reply

    c = CancelsAfterFirstTurn([{"role": "assistant", "content": "", "tool_calls": [_call(
        "c1", "store_evidence", {"field": "torque_nm", "value": 600, "market": "IL", "source_url": "https://a"})]}])
    log = RunLog(tmp_path / "runs", "b", "1")
    with pytest.raises(BatchCancelled):
        run_vehicle({"upstream_record_id": "1"}, {}, client=c, cache=ctx.cache, run_log=log,
                    config=AgentConfig(max_steps=5, requested_fields=["torque_nm"]), tool_config=ToolConfig(),
                    session=ctx.session, cancel_event=cancel)
    saved = json.loads((log.dir / "result.json").read_text("utf-8"))
    assert saved["status"] == "interrupted" and saved["interruption_type"] == "BatchCancelled"
    assert saved["finalization"] is None and len(c.requests) == 1     # no finalizer, no further model call
    assert saved["evidence"] == []                                     # the cancelled tool call never ran
    assert not any(e["kind"] == "evidence" for e in read_events(log.events_path))


def test_batch_json_records_concurrency_config_and_observed_peaks(tmp_path):
    controller = ConcurrencyController()
    c = client(InflightSession(delay=0.01), controller)
    agent_cfg, tool_cfg = AgentConfig(), ToolConfig()
    start_batch(tmp_path, "b1", client=c, agent_cfg=agent_cfg, tool_cfg=tool_cfg, pricing={}, vehicles=vehicles(2),
                level15_source="snapshot", level15_note="", selection="All 50", prompt_version="p",
                concurrency={"batch_max_workers": 50, **controller.config(["glm-5.3-flash"])})
    info = load_batch(tmp_path, "b1")
    assert info["concurrency"]["batch_max_workers"] == 50
    assert info["concurrency"]["model_limits"] == {"glm-5.3-flash": 48}
    assert info["concurrency"]["provider_model_limits"] == {"glm-5.3-flash": 50}
    assert info["concurrency"]["search_max_inflight"] == 5
    with controller.observe() as obs:
        c.chat([{"role": "user", "content": "x"}])
    observed = batch_observability({"peak_vehicle_workers": 7}, obs, {"cross_vehicle_cache_waits": 1},
                                   {"cross_vehicle_cache_waits": 4, "cross_vehicle_search_singleflight_reuses": 2,
                                    "cross_vehicle_document_singleflight_reuses": 3})
    update_batch(tmp_path, "b1", {"concurrency_observed": observed})
    stored = load_batch(tmp_path, "b1")["concurrency_observed"]
    assert stored["peak_vehicle_workers"] == 7 and stored["peak_chat_inflight_by_model"] == {"glm-5.3-flash": 1}
    assert (stored["cross_vehicle_cache_waits"], stored["cross_vehicle_singleflight_reuses"]) == (3, 5)
    for key in ("peak_search_inflight", "chat_queue_wait_count", "chat_queue_wait_ms", "search_queue_wait_count",
                "search_queue_wait_ms"):
        assert key in stored
    assert load_batch(tmp_path, "b1")["concurrency"]["batch_max_workers"] == 50   # config kept
