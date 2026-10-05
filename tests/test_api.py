"""The FastAPI backend (src/api): an interface over the SAME RunManager, run state, loaders and serializers as the
Streamlit dashboard. No network: runs execute through the real RunManager with a scripted research function (as in
tests/test_railway_runstate.py); reads use the PR #40 fixture run and a legacy run folder."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.deps import ApiContext, env_secret
from src.jobs.manager import ResearchRequest, RunManager
from src.research_targets import VehicleCatalog
from src.runstate import model as M
from src.runstate.model import RunRecord
from src.runstate.repository import FileRunRepository
from src.storage.paths import resolve_paths
from src.storage.run_log import RunLog

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
RUN, RECORD, LEGACY = "SYNTH-pr40", "101122", "20261001T185509Z-glm-5.3-one"
VEHICLE = "101122"                     # XPeng G6 (the dashboard's default vehicle); OTHER is a second benchmark vehicle
OTHER = "38626"
ACCESS = "api-access-token-0123456789abcdef"
SECRETS = {"GLM_API_KEY": "sk-seeded-GLMKEY-0123456789", "DATABASE_URL": "postgresql://tripy:pw-seeded-db-7788@db:5432/x",
           "TRIPY_MCP_TOKEN": "mcp-seeded-token-0123456789abcdef", "SENTRY_DSN": "https://seeded-dsn-9911@sentry.io/1"}
LEAKS = ["sk-seeded-GLMKEY-0123456789", "pw-seeded-db-7788", "mcp-seeded-token-0123456789abcdef", "seeded-dsn-9911",
         "bearer-seeded-xyz-12345", "zz-seeded-json-key-998", ACCESS]
ENV_CLEAR = ("TRIPY_ENV", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "TRIPY_ACCESS_TOKEN",
             "TRIPY_MCP_TOKEN", "MILO_RUNS_DIR", "MILO_CACHE_DIR", "SEARCH_BACKEND", "GLM_EXTRA_BODY",
             "SUPABASE_DB_URL", "TRIPY_MAX_ACTIVE_RUNS")


# --- scripted engine (the RunManager itself is real) -----------------------------------------------------------------

def ev(kind, **data):
    return {"kind": kind, **data}


EARLY = [ev("run_started", agent_config={"layered_harvest_enabled": True}),
         ev("model_response", phase="research", tool_calls=[{"id": "1"}]),
         ev("tool_result", result={"document_id": "d1", "url": "https://importer.example/spec"}),
         ev("research_stopped", reason="model_finished"),
         ev("deterministic_harvest_summary", candidate_count_total=3, candidate_fields_total=1),
         ev("document_sweep_started", phase="document_sweep")]
LATE = [ev("document_sweep_finished", unique_fields_resolved=1),
        ev("field_evaluation", stage="primary"), ev("field_evaluation", stage="after_recovery"),
        ev("finalization_checkpoint_written"), ev("finalization_started", phase="finalization")]


def scripted_research(gate: threading.Event | None = None, status: str = "completed"):
    def research(vehicle, row, *, client, cache, runs_dir, batch_id, listener, cancel_event, **kw):
        rid = vehicle["upstream_record_id"]
        log = RunLog(runs_dir, batch_id, rid, listener=listener)
        for event in EARLY:
            log.event(event["kind"], **{k: v for k, v in event.items() if k != "kind"})
        while gate is not None and not gate.wait(0.02):
            if cancel_event.is_set():
                from src.concurrency import BatchCancelled
                log.event("interrupted", phase="document_sweep")
                log.write_result({"record_id": rid, "status": "interrupted", "output": None,
                                  "interrupted_phase": "document_sweep", "documents": ["d1"]})
                log.event("run_finished", status="interrupted")
                raise BatchCancelled("stop")
        for event in LATE:
            log.event(event["kind"], **{k: v for k, v in event.items() if k != "kind"})
        ok = status == "completed"
        result = {"record_id": rid, "status": status, "documents": ["d1"],
                  "output": {"summary": "A summary.", "fields": {"torque_nm": {"value": 300, "unit": "Nm",
                                                                               "market": "IL", "evidence_ids": ["e1"]}},
                             "conflicts": [], "additional_findings": ["note"]} if ok else None,
                  "research_bundle": {"field_states": {"torque_nm": {"state": "ok"}}} if ok else {},
                  "error": None if ok else "GLM finalizer HTTP 500", "api_error": None if ok else {"status": 500}}
        log.write_result(result)
        log.event("run_finished", status=status)
        return result
    return research


def fake_factory(settings, controller, cancel):
    def make():
        return SimpleNamespace(model="glm-5.3-flash", finalizer_model="glm-5.3-flash",
                               settings=SimpleNamespace(model="glm-5.3-flash", public=lambda: {}))
    return make


def fake_loader(ids, source, dsn):
    return SimpleNamespace(rows=[{"upstream_record_id": i} for i in ids], missing=[], source="snapshot", note="")


# --- fixtures --------------------------------------------------------------------------------------------------------

@pytest.fixture
def data_root(tmp_path, monkeypatch):
    for name in ENV_CLEAR:
        monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    runs = data / "runs"
    runs.mkdir(parents=True)
    shutil.copytree(FIXTURES / "pr40_runs" / RUN, runs / RUN)
    shutil.copytree(FIXTURES / "pr40_runs" / "_cache" / "documents", data / "cache" / "documents")
    shutil.copytree(FIXTURES / "baseline_runs" / LEGACY, runs / LEGACY)
    with (runs / RUN / RECORD / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "api_error", "seq": 23, "error": f"HTTP 401 for key {SECRETS['GLM_API_KEY']}",
                             "headers": {"Authorization": "Bearer bearer-seeded-xyz-12345"},
                             "raw": '{"api_key": "zz-seeded-json-key-998"}', "dsn": SECRETS["DATABASE_URL"]}) + "\n")
        fh.write(json.dumps({"kind": "run_finished", "seq": 24, "status": "completed"}) + "\n")
    FileRunRepository(runs).create(RunRecord(
        run_id=RUN, status=M.COMPLETED, started_at="2026-10-03T23:49:00+00:00", finished_at="2026-10-03T23:59:00+00:00",
        target={"label": "XPeng G6 · 2026 · MAX", "scope": "One vehicle", "record_ids": [RECORD]},
        request={"run_profile": "production", "research_model": "glm-5.3-flash"}, owner={"boot_id": "elsewhere"},
        vehicles={RECORD: {"status": M.COMPLETED, "engine_status": "completed"}},
        jobs=[{"started_at": "2026-10-03T23:49:00+00:00", "finished_at": "2026-10-03T23:59:00+00:00"}]))
    monkeypatch.setenv("TRIPY_DATA_DIR", str(data))
    monkeypatch.setenv("GLM_MODEL", "glm-5.3-flash")
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    return data


def make_context(research, **kw) -> ApiContext:
    paths = resolve_paths()
    catalog = VehicleCatalog.load()
    manager = RunManager(paths, client_factory=fake_factory, level15_loader=fake_loader, research_fn=research,
                         register_atexit=False, vehicle_label=catalog.title, **kw)
    return ApiContext(paths=paths, manager=manager, catalog=catalog, secret=env_secret)


@pytest.fixture
def gate():
    event = threading.Event()
    yield event
    event.set()


@pytest.fixture
def ctx(data_root, gate):
    context = make_context(scripted_research(gate))
    yield context
    gate.set()
    context.manager.shutdown(grace_s=5)


@pytest.fixture
def client(ctx):
    with TestClient(create_app(context=ctx, mount_mcp=False)) as test_client:
        yield test_client


def wait_terminal(manager, run_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = manager.get(run_id)
        if record and record.terminal and not manager.is_executing(run_id):
            return record
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish")


def wait_status(manager, run_id, status, timeout=10.0):
    deadline = time.monotonic() + timeout
    while manager.get(run_id).status != status and time.monotonic() < deadline:
        time.sleep(0.02)
    assert manager.get(run_id).status == status


# --- health and authentication ---------------------------------------------------------------------------------------

def test_health_is_public_and_cheap(data_root, monkeypatch):
    monkeypatch.setenv("TRIPY_ENV", "production")
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", ACCESS)
    app = create_app(mount_mcp=False)           # no context and no lifespan: /health touches no TRIPY state
    response = TestClient(app).get("/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok", "service": "tripy"})
    assert app.state.tripy is None


def test_api_requires_the_bearer_token_in_production(client, monkeypatch):
    monkeypatch.setenv("TRIPY_ENV", "production")
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", ACCESS)
    for headers in ({}, {"Authorization": "Bearer wrong-token-0123456789"}, {"Authorization": f"Basic {ACCESS}"},
                    {"Authorization": f"Bearer {ACCESS.upper()}"}, {"Authorization": f"Bearer {ACCESS[:-1]}"},
                    {"Authorization": "Bearer"}):
        response = client.get("/api/runs", headers=headers)
        assert response.status_code == 401, headers
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["error"]["code"] == "unauthorized" and ACCESS not in response.text
    assert client.get(f"/api/runs?access_token={ACCESS}").status_code == 401        # never from the URL
    assert client.get("/api/runs", cookies={"access_token": ACCESS}).status_code == 401
    # authentication runs before body validation: an anonymous POST learns nothing about the contract
    assert client.post("/api/runs", json={"nonsense": 1}).status_code == 401
    assert client.post(f"/api/runs/{RUN}/cancel").status_code == 401
    ok = client.get("/api/runs", headers={"Authorization": f"Bearer {ACCESS}"})
    assert ok.status_code == 200 and ok.json()["total"] == 2
    assert client.get("/health").status_code == 200


def test_production_without_a_token_fails_closed(client, monkeypatch):
    monkeypatch.setenv("TRIPY_ENV", "production")
    response = client.get("/api/runs", headers={"Authorization": "Bearer anything-at-all-123"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "access_control_not_configured"
    assert client.get("/health").status_code == 200


def test_development_is_open_like_the_dashboard(client):
    assert client.get("/api/runs").status_code == 200


def test_token_comparison_is_the_dashboards_constant_time_check(client, monkeypatch):
    from src import access_control

    calls = []
    real = access_control.hmac.compare_digest
    monkeypatch.setattr(access_control.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    monkeypatch.setenv("TRIPY_ENV", "production")
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", ACCESS)
    assert client.get("/api/runs", headers={"Authorization": f"Bearer {ACCESS}"}).status_code == 200 and calls


# --- history, details, progress --------------------------------------------------------------------------------------

def test_run_history_lists_the_durable_runs(client):
    body = client.get("/api/runs").json()
    by_id = {r["run_id"]: r for r in body["runs"]}
    assert body["total"] == 2 and set(by_id) == {RUN, LEGACY}
    row = by_id[RUN]
    assert row["status"] == M.COMPLETED and row["status_label"] == "Completed" and row["record_ids"] == [RECORD]
    assert row["profile"] == "production" and row["scope"] == "One vehicle" and row["label"] == "XPeng G6 · 2026 · MAX"
    assert row["executing"] is False and row["active"] is False and row["elapsed_s"] == 600.0
    assert by_id[LEGACY]["legacy"] is True
    assert len(client.get("/api/runs?limit=1").json()["runs"]) == 1


def test_run_details(client):
    detail = client.get(f"/api/runs/{RUN}").json()
    assert detail["run_id"] == RUN and detail["terminal"] and detail["completed"]
    assert detail["progress"] == {"vehicles_total": 1, "vehicles_finished": 1, "vehicles_completed": 1}
    vehicle = detail["vehicles"][0]
    assert vehicle["record_id"] == RECORD and vehicle["status"] == M.COMPLETED
    assert [s["key"] for s in vehicle["pipeline"]["stages"]] == ["acquisition", "harvest", "sweep", "recovery",
                                                                 "finalization"]
    assert vehicle["pipeline"]["events_seen"] == 24 and "sources" in vehicle["pipeline"]["counters"]
    assert vehicle["result_available"] is False                # the fixture run wrote no result.json
    assert vehicle["report"]["record_id"] == RECORD
    legacy = client.get(f"/api/runs/{LEGACY}").json()
    assert legacy["legacy"] and legacy["vehicles"][0]["failure"]["actions"] == ["restart"]   # legacy: no finalize


@pytest.mark.parametrize("bad", ["nope", "_cache", ".hidden", "..", "%2E%2E", "a%2Fb"])
def test_unknown_runs_are_404(client, bad):
    response = client.get(f"/api/runs/{bad}")
    assert response.status_code == 404 and "Traceback" not in response.text
    assert response.json()["error"]["code"] in ("unknown_run", "not_found")


def test_progress_reads_the_live_pipeline(client, ctx, gate):
    started = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE})
    run_id = started.json()["run_id"]
    wait_status(ctx.manager, run_id, M.SWEEPING)
    progress = client.get(f"/api/runs/{run_id}/progress").json()
    assert progress["status"] == M.SWEEPING and progress["active"] and progress["executing"]
    assert (progress["completed"], progress["total"]) == (0, 1)
    vehicle = progress["vehicles"][0]
    assert vehicle["current_stage"] == "sweep" and vehicle["counters"]["candidates"] == 3
    assert {s["key"]: s["state"] for s in vehicle["stages"]}["harvest"] == "done"
    assert "percent" not in json.dumps(progress)                 # no invented percentage
    assert client.get(f"/api/runs/{run_id}/results").json() == {"run_id": run_id, "available": False,
                                                                  "reason": "The run is still active.", "vehicles": [],
                                                                  "output_source_caption": None}
    gate.set()
    wait_terminal(ctx.manager, run_id)
    done = client.get(f"/api/runs/{run_id}/progress").json()
    assert (done["status"], done["completed"], done["vehicles_completed"]) == (M.COMPLETED, 1, 1)


# --- start: the dashboard's path -------------------------------------------------------------------------------------

def test_start_uses_the_run_manager_with_the_dashboards_request(client, ctx, gate, monkeypatch):
    from src.run_settings import build_research_request, settings_from_env

    seen: list[ResearchRequest] = []
    real_start = ctx.manager.start
    monkeypatch.setattr(ctx.manager, "start", lambda request: seen.append(request) or real_start(request))
    response = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE, "profile": "benchmark_treatment",
                                              "idempotency_key": "submit-1"})
    assert response.status_code == 201
    body = response.json()
    run_id = body["run_id"]
    assert body["created"] and body["run"]["run_id"] == run_id and body["run"]["profile"] == "benchmark_treatment"
    request = seen[0]
    expected = build_research_request(settings_from_env(env_secret, ctx.manager.controller), env_secret,
                                      [ctx.catalog.by_id[VEHICLE]], ctx.catalog.title(VEHICLE), "One vehicle",
                                      "submit-1", "benchmark_treatment")
    assert dataclasses.asdict(request) == dataclasses.asdict(expected)
    assert request.settings.api_key == SECRETS["GLM_API_KEY"] and request.scope == "One vehicle"
    # durable artifacts of RunManager.start: run_state.json + batch.json, same target / request shape as the dashboard
    record = ctx.manager.get(run_id)
    assert record.target["scope"] == "One vehicle" and record.target["record_ids"] == [VEHICLE]
    assert record.label == ctx.catalog.title(VEHICLE) and record.request["run_profile"] == "benchmark_treatment"
    batch = json.loads((ctx.paths.runs_dir / run_id / "batch.json").read_text("utf-8"))
    assert batch["agent_config"]["run_profile"] == "benchmark_treatment"
    assert SECRETS["GLM_API_KEY"] not in json.dumps(batch) + json.dumps(record.to_dict())
    again = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE, "idempotency_key": "submit-1"})
    assert again.status_code == 200 and again.json()["run_id"] == run_id and again.json()["created"] is False
    gate.set()
    assert wait_terminal(ctx.manager, run_id).status == M.COMPLETED


def test_conflicting_runs_are_409(client, ctx, gate):
    first = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE}).json()["run_id"]
    same = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE})
    assert same.status_code == 409
    assert same.json()["error"] == {"code": "run_rejected", "existing_run_id": first,
                                    "message": f"A run for this target is already active ({first})."}
    limit = client.post("/api/runs", json={"scope": "one", "record_id": OTHER})      # TRIPY_MAX_ACTIVE_RUNS=1
    assert limit.status_code == 409 and limit.json()["error"]["existing_run_id"] == first
    gate.set()
    wait_terminal(ctx.manager, first)


def test_manufacturer_scope_matches_the_dashboard(client, ctx, gate):
    gate.set()
    maker = ctx.catalog.by_id[VEHICLE]["manufacturer"]
    response = client.post("/api/runs", json={"scope": "manufacturer", "manufacturer": maker})
    assert response.status_code == 201
    record = wait_terminal(ctx.manager, response.json()["run_id"])
    expected = [v["upstream_record_id"] for v in ctx.catalog.vehicles if v["manufacturer"] == maker]
    assert record.record_ids == expected and record.label == f"{maker} · {len(expected)} vehicles"
    assert record.target["scope"] == "Manufacturer"


@pytest.mark.parametrize("body,code", [
    ({"scope": "one"}, "unknown_vehicle"), ({"scope": "one", "record_id": "999999999"}, "unknown_vehicle"),
    ({"scope": "manufacturer", "manufacturer": "Nobody"}, "unknown_manufacturer"),
    ({"scope": "galaxy"}, "invalid_request"), ({"scope": "all", "profile": "turbo"}, "invalid_request"),
    ({"scope": "all", "model": "glm-other"}, "invalid_request"), ({"scope": "all", "api_key": "x"}, "invalid_request")])
def test_invalid_start_requests_are_4xx(client, ctx, body, code):
    response = client.post("/api/runs", json=body)
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    assert all("input" not in d for d in response.json()["error"].get("details", []))     # never echoed
    assert ctx.manager.list_runs() and not [r for r in ctx.manager.list_runs() if not r.legacy and r.run_id != RUN]


def test_missing_configuration_blocks_a_start_like_the_dashboard(client, ctx, monkeypatch):
    monkeypatch.delenv("GLM_API_KEY")
    response = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE})
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "configuration_incomplete" and {c["name"] for c in error["checks"]} == {"GLM", "Search"}
    monkeypatch.setenv("GLM_API_KEY", SECRETS["GLM_API_KEY"])
    monkeypatch.setenv("GLM_EXTRA_BODY", "[1]")                          # the dashboard's invalid-JSON check
    blocked = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE}).json()["error"]
    # validate_config and the settings' own JSON parse both report it, exactly as the dashboard's System panel does
    assert {c["name"] for c in blocked["checks"]} == {"Configuration"}
    assert not [r for r in ctx.manager.list_runs() if r.run_id not in (RUN, LEGACY)]


def test_provider_and_level15_errors_are_mapped_without_tracebacks(client, ctx, monkeypatch):
    from src.db import Level15Error
    from src.glm_client import GLMError

    for exc, status, code in ((GLMError(f"bad key {SECRETS['GLM_API_KEY']}"), 503, "provider_configuration"),
                              (Level15Error("DATABASE_URL is not set"), 503, "level15_unavailable"),
                              (ValueError("bad value"), 400, "invalid_request")):
        def boom(request, exc=exc):
            raise exc
        monkeypatch.setattr(ctx.manager, "start", boom)
        response = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE})
        assert (response.status_code, response.json()["error"]["code"]) == (status, code)
        assert SECRETS["GLM_API_KEY"] not in response.text and "Traceback" not in response.text


def test_unexpected_errors_are_a_generic_500(ctx, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError(f"internal detail {SECRETS['GLM_API_KEY']}")
    monkeypatch.setattr(ctx.manager, "list_runs", boom)
    with TestClient(create_app(context=ctx, mount_mcp=False), raise_server_exceptions=False) as test_client:
        response = test_client.get("/api/runs")
    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error", "message": "An unexpected error occurred."}}


# --- cancel, finalize, restart ---------------------------------------------------------------------------------------

def test_cancel_uses_the_run_managers_cancellation(client, ctx, gate):
    run_id = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE}).json()["run_id"]
    wait_status(ctx.manager, run_id, M.SWEEPING)
    response = client.post(f"/api/runs/{run_id}/cancel")
    assert response.status_code == 202 and response.json()["run_id"] == run_id
    record = wait_terminal(ctx.manager, run_id)
    assert record.status == M.CANCELLED and record.error["code"] == "cancelled"
    again = client.post(f"/api/runs/{run_id}/cancel")
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_executing"
    assert client.post("/api/runs/nope/cancel").status_code == 404


def test_finalize_and_restart_follow_the_failure_card(data_root):
    ctx = make_context(scripted_research(status="finalization_failed"))
    calls = []

    def finalize(runs_dir, batch_id, record_id, *, client, cache, config, tool_config, metrics_fn):
        calls.append((batch_id, record_id))
        log = RunLog(runs_dir, batch_id, record_id)
        log.event("recovery_started")
        log.write_result({"record_id": record_id, "status": "recovered_finalized", "output": {"fields": {}},
                          "recovered": True})
        log.event("recovery_finished", status="recovered_finalized")
        return {}

    ctx.manager._finalize_fn = finalize
    try:
        with TestClient(create_app(context=ctx, mount_mcp=False)) as client:
            run_id = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE,
                                                    "profile": "benchmark_baseline"}).json()["run_id"]
            assert wait_terminal(ctx.manager, run_id).status == M.FAILED
            failure = client.get(f"/api/runs/{run_id}").json()["vehicles"][0]["failure"]
            assert failure["code"] == "provider_error" and failure["actions"] == ["finalize", "restart"]
            assert client.post(f"/api/runs/{run_id}/vehicles/{OTHER}/finalize").status_code == 404
            restarted = client.post(f"/api/runs/{run_id}/vehicles/{VEHICLE}/restart",
                                    json={"idempotency_key": "restart-1"})
            assert restarted.status_code == 201
            new_id = restarted.json()["run_id"]
            new = wait_terminal(ctx.manager, new_id)
            assert new_id != run_id and new.target["scope"] == "One vehicle"
            assert new.request["run_profile"] == "benchmark_baseline"        # the failed run's own profile
            response = client.post(f"/api/runs/{run_id}/vehicles/{VEHICLE}/finalize")
            assert response.status_code == 202 and response.json()["status"] == M.FINALIZING
            record = wait_terminal(ctx.manager, run_id)
            assert calls == [(run_id, VEHICLE)] and record.status == M.COMPLETED
            assert [j["kind"] for j in record.jobs] == ["research", "finalization_retry"]
            assert client.get(f"/api/runs/{run_id}").json()["vehicles"][0]["failure"] is None
            done = client.post(f"/api/runs/{run_id}/vehicles/{VEHICLE}/finalize")
            assert done.status_code == 409 and done.json()["error"]["code"] == "finalize_not_available"
            assert client.post(f"/api/runs/{run_id}/vehicles/{VEHICLE}/restart").status_code == 409
    finally:
        ctx.manager.shutdown(grace_s=5)


# --- events, results, candidates, evidence ---------------------------------------------------------------------------

def test_events_are_read_incrementally_with_a_line_cursor(client, data_root):
    first = client.get(f"/api/runs/{RUN}/events?limit=10").json()
    assert [e["line"] for e in first["events"]] == list(range(1, 11))
    assert first["next_cursor"] == 10 and first["more"] and first["record_id"] == RECORD
    rest = client.get(f"/api/runs/{RUN}/events?after={first['next_cursor']}").json()
    assert [e["line"] for e in rest["events"]] == list(range(11, 25)) and rest["next_cursor"] == 24
    assert not rest["more"]
    assert client.get(f"/api/runs/{RUN}/events?after=24").json()["events"] == []
    with (data_root / "runs" / RUN / RECORD / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "note", "seq": 25}) + "\n" + '{"kind": "torn", "seq"')   # a line being written
    tail = client.get(f"/api/runs/{RUN}/events?after=24").json()
    assert [(e["line"], e["kind"]) for e in tail["events"]] == [(25, "note")] and tail["next_cursor"] == 25
    only = client.get(f"/api/runs/{RUN}/events?kind=candidates_harvested").json()
    assert {e["kind"] for e in only["events"]} == {"candidates_harvested"}
    assert client.get(f"/api/runs/{RUN}/events?record_id=999").status_code == 404
    assert client.get(f"/api/runs/{RUN}/events?after=-1").status_code == 422


def test_events_of_a_multi_vehicle_run_need_a_record_id(client, ctx, gate):
    gate.set()
    maker = ctx.catalog.by_id[OTHER]["manufacturer"]
    run_id = client.post("/api/runs", json={"scope": "manufacturer", "manufacturer": maker}).json()["run_id"]
    record = wait_terminal(ctx.manager, run_id)
    assert len(record.record_ids) > 1
    response = client.get(f"/api/runs/{run_id}/events")
    assert response.status_code == 422 and response.json()["error"]["code"] == "record_id_required"
    assert response.json()["error"]["record_ids"] == record.record_ids
    page = client.get(f"/api/runs/{run_id}/events?record_id={OTHER}").json()
    assert page["record_id"] == OTHER and page["events"][0]["kind"] == "run_started"
    assert client.get(f"/api/runs/{run_id}/candidates").status_code == 422
    assert client.get(f"/api/runs/{run_id}/export/candidates.csv").status_code == 422


def test_results_are_structured(client, ctx, gate):
    gate.set()
    run_id = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE}).json()["run_id"]
    wait_terminal(ctx.manager, run_id)
    results = client.get(f"/api/runs/{run_id}/results").json()
    assert results["available"] and len(results["vehicles"]) == 1
    vehicle = results["vehicles"][0]
    assert vehicle["engine_status"] == "completed" and vehicle["has_output"] and vehicle["summary"] == "A summary."
    assert vehicle["fields"] == [{"field": "torque_nm", "value": 300, "unit": "Nm", "market": "IL", "provenance": None,
                                  "notes": None, "evidence_ids": ["e1"], "state": "ok"}]
    assert vehicle["additional_findings"] == ["note"] and vehicle["result_source"] == "result.json"
    detail = client.get(f"/api/runs/{run_id}").json()["vehicles"][0]
    assert detail["result_available"] and detail["failure"] is None
    synthesized = client.get(f"/api/runs/{RUN}/results").json()["vehicles"][0]
    assert synthesized["synthesized"] and not synthesized["has_output"] and synthesized["fields"] == []


def test_candidates_and_evidence(client):
    candidates = client.get(f"/api/runs/{RUN}/candidates").json()
    assert len(candidates["fields"]) == 11 and candidates["summary"]["applicable_fields"] == 11
    with_values = [f for f in candidates["fields"] if f["candidates"]]
    assert with_values and {"value", "document_id"} <= set(with_values[0]["candidates"][0])
    one = client.get(f"/api/runs/{RUN}/candidates?field=height_mm").json()
    assert [f["field"] for f in one["fields"]] == ["height_mm"]
    evidence = client.get(f"/api/runs/{RUN}/evidence").json()
    assert evidence["summary"]["admitted"] == len(evidence["admitted"]) > 0
    item = evidence["admitted"][0]
    assert {"evidence_id", "field", "document_id"} <= set(item)
    assert evidence["summary"]["rejected"] == len(evidence["rejected"])


# --- exports ---------------------------------------------------------------------------------------------------------

def test_candidates_csv_is_byte_identical_to_the_dashboards_previous_download(client, data_root):
    """tests/fixtures/pr40_candidates_pandas_golden.csv is what the dashboard's download wrote for this run
    (pandas.DataFrame(candidate_table_rows(...)).to_csv(index=False)), frozen before pandas left the dependencies."""
    golden = (Path(__file__).parent / "fixtures" / "pr40_candidates_pandas_golden.csv").read_bytes()
    response = client.get(f"/api/runs/{RUN}/export/candidates.csv")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert 'filename="tripy_candidates.csv"' in response.headers["content-disposition"]
    assert response.content == golden
    from src.exports import rows_to_csv
    tricky = [{"a": 'x,"y"', "b": None, "c": 3}, {"a": "line\nbreak", "b": True, "c": 0}]
    assert rows_to_csv(tricky) == 'a,b,c\n"x,""y""",,3\n"line\nbreak",True,0\n'       # pandas' to_csv bytes


def test_benchmark_exports(client):
    per_vehicle = client.get(f"/api/runs/{RUN}/export/per_vehicle.csv")
    assert per_vehicle.status_code == 200 and per_vehicle.text.splitlines()[0].startswith(("run_id", "record_id"))
    bench = client.get(f"/api/runs/{RUN}/export/benchmark.json")
    assert bench.status_code == 200 and bench.json()["per_vehicle"][0]["run_id"] == RUN
    assert client.get("/api/runs/nope/export/benchmark.json").status_code == 404


# --- secrets ---------------------------------------------------------------------------------------------------------

def test_no_secret_reaches_any_response(client, monkeypatch):
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", ACCESS)
    responses = [client.get(path) for path in (
        "/api/runs", f"/api/runs/{RUN}", f"/api/runs/{RUN}/progress", f"/api/runs/{RUN}/events?limit=1000",
        f"/api/runs/{RUN}/results", f"/api/runs/{RUN}/candidates", f"/api/runs/{RUN}/evidence",
        f"/api/runs/{RUN}/export/candidates.csv", "/api/config/status", "/api/vehicles", "/openapi.json")]
    assert all(r.status_code == 200 for r in responses), [r.status_code for r in responses]
    blob = "\n".join(r.text for r in responses)
    for leak in LEAKS:
        assert leak not in blob, leak
    seeded = responses[3].json()["events"][-2]
    assert seeded["kind"] == "api_error" and "[redacted]" in json.dumps(seeded)


def test_config_status_reports_presence_only(client, monkeypatch):
    monkeypatch.setenv("TRIPY_ACCESS_TOKEN", ACCESS)
    status = client.get("/api/config/status").json()
    assert status["configured"] is True and status["blocking"] == []
    assert status["research_model"] == "glm-5.3-flash" and status["search_backend"] == "glm"
    assert status["level15_source"] == "database" and status["mcp_enabled"] is True
    assert status["access_control"] == {"required": False, "configured": True}
    assert status["environment"] == "development" and status["default_profile"] == "production"
    assert {c["name"] for c in status["checks"]} >= {"GLM", "Search", "Persistent storage", "Level 1.5 data"}
    assert "dsn" not in json.dumps(status).lower().replace("database_url is set", "")


# --- shared settings: the API starts runs with exactly what an untouched dashboard would use ------------------------

def sidebar_settings(secret, controller, **edits):
    """The former Streamlit "Advanced settings" sidebar without widgets: every widget's default exactly as it read it
    (environment defaults from run_settings, numbers within the widget ranges), the given edits applied, the model-
    dependent defaults (prices, in-flight limits) following the edited model, then the same assemble_settings call.
    The reference the HTTP API's settings path is held to now that the sidebar is gone."""
    from src.run_settings import (EFFORT_PHASES, agent_defaults, assemble_settings, chat_limits_for, clamp,
                                  dsn_default, effort_default, glm_defaults, pricing_defaults, pricing_from,
                                  search_limit_default, workers_default)
    from src.run_profiles import env_overrides

    g, a = glm_defaults(secret), agent_defaults(secret)
    v = {"model_id": g["model_id"], "finalizer_model_id": g["finalizer_model_id"],
         "search_backend": g["search_backend"], "search_engine": g["search_engine"],
         "chat_attempts": g["chat_attempts"], "search_attempts": g["search_attempts"],
         "chat_timeout": g["chat_timeout"], "workers": workers_default(secret),
         **{k: a[k] for k in ("max_steps", "no_artifact", "idle_turns", "tool_chars", "recovery_on",
                              "recovery_attempts", "recovery_steps", "recovery_total", "include_level3",
                              "acquisition_mode", "card_choice", "site_map_choice", "grounded_choice", "recovery_mode",
                              "sweep_attempts", "sweep_fields", "sweep_candidates")},
         "temperature": None, "max_tokens": 0, "data_source": "auto",
         **{f"effort_{phase}": effort_default(secret, phase) for phase, _ in EFFORT_PHASES}}
    v = {k: clamp(k, x) if isinstance(x, (int, float)) and not isinstance(x, bool) else x for k, x in v.items()}
    v.update(edits)
    prices = pricing_defaults(v["model_id"], secret)
    return assemble_settings(
        model_id=v["model_id"], finalizer_model_id=v["finalizer_model_id"], base_url=g["base_url"],
        chat_path=g["chat_path"], api_key=secret("GLM_API_KEY"), api_key_from_ui=False,
        search_backend=v["search_backend"], search_path=g["search_path"], search_engine=v["search_engine"],
        chat_attempts=v["chat_attempts"], search_attempts=v["search_attempts"], chat_timeout=v["chat_timeout"],
        workers=v["workers"], chat_limits=chat_limits_for(controller, v["model_id"], v["finalizer_model_id"]),
        search_limit=clamp("search_limit", search_limit_default(controller)), max_steps=v["max_steps"],
        no_artifact=v["no_artifact"], idle_turns=v["idle_turns"], tool_chars=v["tool_chars"],
        recovery_on=v["recovery_on"], recovery_attempts=v["recovery_attempts"], recovery_steps=v["recovery_steps"],
        recovery_total=v["recovery_total"], include_level3=v["include_level3"], temperature=v["temperature"],
        max_tokens=v["max_tokens"], efforts={phase: v[f"effort_{phase}"] for phase, _ in EFFORT_PHASES},
        extra_raw=a["extra_raw"], acquisition_mode=v["acquisition_mode"], card_choice=v["card_choice"],
        site_map_choice=v["site_map_choice"], grounded_choice=v["grounded_choice"], recovery_mode=v["recovery_mode"],
        sweep_attempts=v["sweep_attempts"], sweep_fields=v["sweep_fields"], sweep_candidates=v["sweep_candidates"],
        pricing=pricing_from(prices, edits.get("price_in", prices["input_per_mtok"]),
                             edits.get("price_out", prices["output_per_mtok"]),
                             edits.get("price_search", prices["web_search_per_call"])),
        data_source=v["data_source"], dsn=dsn_default(secret), env_overrides=env_overrides(secret))


def test_settings_from_env_equal_the_untouched_sidebar(data_root, monkeypatch):
    for name, value in {"GLM_FINALIZER_MODEL": "glm-5.3", "PRIMARY_RESEARCH_MAX_TURNS": "9",
                        "GLM_PRICE_INPUT_PER_MTOK": "0.7", "GLM_EXTRA_BODY": '{"x": 1}', "SEARCH_BACKEND": "glm",
                        "GLM_RECOVERY_REASONING_EFFORT": "medium", "DOCUMENT_SWEEP_MAX_FIELDS": "20",
                        "BATCH_MAX_WORKERS": "3", "GLM_CHAT_TIMEOUT_S": "300"}.items():
        monkeypatch.setenv(name, value)
    from src.concurrency import ConcurrencyController
    from src.run_settings import settings_from_env

    controller = ConcurrencyController.from_env(env_secret)
    api = dataclasses.asdict(settings_from_env(env_secret, controller))
    assert api == dataclasses.asdict(sidebar_settings(env_secret, controller))
    assert api["agent_overrides"]["max_steps"] == 9 and api["workers"] == 3 and api["chat_timeout"] == 300.0
    assert set(api["chat_limits"]) == {"glm-5.3-flash", "glm-5.3"} and api["pricing"]["input_per_mtok"] == 0.7
    # an env value outside a widget's range is clamped into it, as the sidebar's widgets required
    monkeypatch.setenv("BATCH_MAX_WORKERS", "500")
    assert settings_from_env(env_secret, controller).workers == 50


# --- framework independence and MCP ----------------------------------------------------------------------------------

def test_the_api_never_imports_streamlit(data_root):
    code = """
import sys
from fastapi.testclient import TestClient
from src.api.app import create_app
with TestClient(create_app(mount_mcp=False)) as client:
    for path in ["/health", "/api/runs", "/api/runs/SYNTH-pr40", "/api/runs/SYNTH-pr40/results",
                 "/api/runs/SYNTH-pr40/candidates", "/api/runs/SYNTH-pr40/evidence", "/api/runs/SYNTH-pr40/progress",
                 "/api/runs/SYNTH-pr40/export/candidates.csv", "/api/runs/SYNTH-pr40/export/benchmark.json",
                 "/api/config/status", "/api/vehicles"]:
        assert client.get(path).status_code == 200, path
ui = sorted(m for m in sys.modules if m.startswith("src.ui"))
assert "streamlit" not in sys.modules and "pandas" not in sys.modules, "framework imported"
assert ui == [], ui                     # framework-neutral state lives in src/runstate and src/presentation
print("ok")
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                            env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert result.returncode == 0 and result.stdout.strip().endswith("ok"), result.stderr[-3000:]
    source = "\n".join(p.read_text("utf-8") for p in (ROOT / "src" / "api").rglob("*.py"))
    assert "import streamlit" not in source and "ui.dashboard" not in source and "ui.run_view" not in source


def test_the_mcp_mounts_into_the_api_unchanged(ctx):
    token = SECRETS["TRIPY_MCP_TOKEN"]
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    with TestClient(create_app(context=ctx, mount_mcp=True)) as client:
        good = client.post(f"/mcp/{token}", json=body, headers=headers)
        assert good.status_code == 200 and good.json()["result"] == {}
        for path in ("/mcp", "/mcp/wrong", f"/mcp/{token[:-1]}", f"/MCP/{token}", f"/mcp/{token}/x"):
            response = client.post(path, json=body, headers=headers)
            assert (response.status_code, response.content) == (404, b""), path
        assert client.get("/api/runs").status_code == 200 and client.get("/health").status_code == 200


def test_no_mcp_route_without_the_token(ctx, monkeypatch):
    monkeypatch.delenv("TRIPY_MCP_TOKEN")
    with TestClient(create_app(context=ctx)) as client:
        assert client.post(f"/mcp/{SECRETS['TRIPY_MCP_TOKEN']}", json={}).json()["error"]["code"] == "not_found"


def test_startup_builds_the_dashboards_context(data_root):
    from src.jobs import manager as manager_mod

    with TestClient(create_app(mount_mcp=False)) as client:
        assert client.get("/api/runs").json()["total"] == 2
        ctx = client.app.state.tripy
        assert ctx.manager is manager_mod.existing_manager(resolve_paths())           # the process-wide manager
        assert ctx.manager.controller is manager_mod.shared_controller()
        assert ctx.paths == resolve_paths() and ctx.manager.repository.vehicle_label == ctx.catalog.title
    key = manager_mod._manager_key(resolve_paths())
    manager_mod._MANAGERS.pop(key, None)                 # this test's manager must not leak into other tests
