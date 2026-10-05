"""Railway migration: persistent paths, durable run state, reconnect, duplicate prevention, failures, config."""

import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent import AgentConfig
from src.app_config import (blocking_errors, diagnostics, is_production, redact,
                            validate_config)
from src.jobs.manager import ResearchRequest, RunManager, RunRejected, _StageTracker
from src.runstate import model as M
from src.runstate.failures import classify_reason, explain
from src.runstate.model import RunRecord, aggregate_status, status_for_engine_result
from src.runstate.pipeline import EventTail, PipelineCache, pipeline_from_events
from src.runstate.repository import FileRunRepository, RunStateError
from src.storage.paths import resolve_paths, storage_status
from src.storage.run_log import RunLog, write_batch
from src.tools import ToolConfig

ROOT = Path(__file__).resolve().parent.parent


def env_of(mapping):
    return lambda name: mapping.get(name)


# --- persistent path configuration ----------------------------------------------------------------------

def test_data_root_derives_every_durable_path(tmp_path):
    paths = resolve_paths(env_of({"TRIPY_DATA_DIR": str(tmp_path / "data")}))
    assert paths.runs_dir == tmp_path / "data" / "runs"
    assert paths.cache_dir == tmp_path / "data" / "cache"
    assert paths.feedback_dir == tmp_path / "data" / "feedback"
    assert paths.source == "TRIPY_DATA_DIR"


def test_local_default_is_a_repo_folder_and_legacy_overrides_keep_their_layout(tmp_path):
    default = resolve_paths(env_of({}))
    assert default.data_dir == ROOT / ".tripy-data" and default.source == "default"
    legacy = resolve_paths(env_of({"MILO_RUNS_DIR": str(tmp_path / "runs")}))
    assert legacy.runs_dir == tmp_path / "runs" and legacy.cache_dir == tmp_path / "runs" / "_cache"
    both = resolve_paths(env_of({"TRIPY_DATA_DIR": str(tmp_path / "d"), "MILO_CACHE_DIR": str(tmp_path / "c")}))
    assert both.runs_dir == tmp_path / "d" / "runs" and both.cache_dir == tmp_path / "c"
    assert ".tripy-data/" in (ROOT / ".gitignore").read_text()


def test_storage_status_detects_ephemeral_railway_disk(tmp_path):
    paths = resolve_paths(env_of({"TRIPY_DATA_DIR": str(tmp_path / "data")}))
    assert storage_status(paths, env_of({}))["level"] == "ok"
    no_volume = storage_status(paths, env_of({"RAILWAY_ENVIRONMENT": "production"}))
    assert no_volume["level"] == "warning" and "ephemeral" in no_volume["message"]
    outside = storage_status(paths, env_of({"RAILWAY_ENVIRONMENT": "production",
                                            "RAILWAY_VOLUME_MOUNT_PATH": str(tmp_path / "elsewhere")}))
    assert outside["level"] == "warning" and outside["on_volume"] is False
    mounted = storage_status(paths, env_of({"RAILWAY_ENVIRONMENT": "production",
                                            "RAILWAY_VOLUME_MOUNT_PATH": str(tmp_path / "data")}))
    assert mounted["level"] == "ok" and mounted["on_volume"] is True


def test_unwritable_storage_is_an_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    paths = resolve_paths(env_of({"TRIPY_DATA_DIR": str(blocker / "data")}))   # a path under a regular file
    status = storage_status(paths, env_of({}))
    assert status["level"] == "error" and not status["writable"]


# --- run state serialization and repository ------------------------------------------------------------

def test_run_record_round_trip_and_tolerant_loading():
    record = RunRecord(run_id="r1", status=M.SWEEPING, stage="sweep", target={"label": "X", "record_ids": ["1"]},
                       vehicles={"1": {"status": M.SWEEPING}}, jobs=[{"kind": "research"}])
    data = json.loads(json.dumps(record.to_dict()))
    again = RunRecord.from_dict({**data, "future_key": 1})
    assert again == record and again.active and again.record_ids == ["1"] and again.schema == M.SCHEMA
    odd = RunRecord.from_dict({"run_id": "r2", "status": "WHATEVER", "vehicles": []})
    assert odd.status == M.FAILED and odd.vehicles == {} and "unknown status" in odd.notes[0]
    with pytest.raises(ValueError):
        RunRecord.from_dict({"status": M.QUEUED})


def test_file_repository_create_update_list(tmp_path):
    repo = FileRunRepository(tmp_path)
    repo.create(RunRecord(run_id="20260101T000000Z-a", target={"label": "A"}))
    with pytest.raises(RunStateError):
        repo.create(RunRecord(run_id="20260101T000000Z-a"))
    with pytest.raises(RunStateError):
        repo.path("../escape")
    updated = repo.update("20260101T000000Z-a", lambda r: setattr(r, "status", M.COMPLETED))
    assert updated.status == M.COMPLETED and repo.get("20260101T000000Z-a").status == M.COMPLETED
    assert not list(tmp_path.glob("*/.run_state.json.*.tmp"))           # atomic writes leave no temp files
    time.sleep(0.01)
    repo.create(RunRecord(run_id="20260102T000000Z-b"))
    assert [r.run_id for r in repo.list_runs()] == ["20260102T000000Z-b", "20260101T000000Z-a"]


def test_concurrent_updates_never_lose_writes(tmp_path):
    repo = FileRunRepository(tmp_path)
    repo.create(RunRecord(run_id="r"))

    def bump():
        for _ in range(25):
            repo.update("r", lambda r: r.notes.append("x"), durable=False)

    threads = [threading.Thread(target=bump) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(repo.get("r").notes) == 100


def test_legacy_run_folders_are_listed_read_only(tmp_path):
    write_batch(tmp_path, "20250101T000000Z-old", {"batch_id": "x", "record_ids": ["1", "2"], "selection": "Manufacturer"})
    (tmp_path / "20250101T000000Z-old" / "1").mkdir()
    (tmp_path / "20250101T000000Z-old" / "1" / "result.json").write_text(json.dumps(
        {"status": "completed", "started_at": "2025-01-01T00:00:00+00:00"}))
    log = RunLog(tmp_path, "20250101T000000Z-old", "2")
    log.event("run_started")
    repo = FileRunRepository(tmp_path, vehicle_label=lambda rid: f"car {rid}")
    record = repo.get("20250101T000000Z-old")
    assert record.legacy and record.status == M.COMPLETED
    assert record.vehicles["1"]["status"] == M.COMPLETED and record.vehicles["2"]["status"] == M.INTERRUPTED
    assert not (tmp_path / "20250101T000000Z-old" / "run_state.json").exists()


def test_engine_status_mapping():
    assert status_for_engine_result("max_steps_finalized") == M.COMPLETED
    assert status_for_engine_result("finalization_failed") == M.FAILED
    assert status_for_engine_result("completed_unparsed") == M.FAILED
    assert status_for_engine_result("finalization_pending") == M.INTERRUPTED
    assert status_for_engine_result("interrupted", cancel_requested=True) == M.CANCELLED
    assert aggregate_status([M.COMPLETED, M.FAILED]) == M.COMPLETED
    assert aggregate_status([M.FAILED, M.INTERRUPTED]) == M.INTERRUPTED
    assert aggregate_status([M.INTERRUPTED], cancel_requested=True) == M.CANCELLED


# --- phase-state transitions (pipeline reconstructed from events) -----------------------------------------

def ev(kind, **data):
    return {"kind": kind, "ts": data.pop("ts", "2026-10-01T10:00:00+00:00"), **data}


FULL_RUN = [
    ev("run_started", ts="2026-10-01T10:00:00+00:00", agent_config={"layered_harvest_enabled": True},
       requested_field_specs=[{"name": "torque_nm", "applicable": True}]),
    ev("model_response", phase="research", tool_calls=[{"id": "1"}]),
    ev("api_call", request_kind="search"),
    ev("tool_result", result={"document_id": "d1", "url": "https://x/spec"}),
    ev("primary_research_turn", turn=1, useful_documents=1, fields_with_candidates=1),
    ev("research_stopped", reason="model_finished", ts="2026-10-01T10:01:00+00:00"),
    ev("primary_research_summary", useful_documents=1, target_market_documents=1, official_sources=1,
       candidate_fields=1),
    ev("deterministic_harvest_summary", candidate_count_total=3, candidate_fields_total=1,
       ts="2026-10-01T10:01:02+00:00"),
    ev("document_sweep_started", phase="document_sweep"),
    ev("model_response", phase="document_sweep"),
    ev("document_sweep_finished", unique_fields_resolved=1, ts="2026-10-01T10:01:30+00:00"),
    ev("field_evaluation", stage="primary", ts="2026-10-01T10:01:31+00:00"),
    ev("field_retry_queue", fields=[]),
    ev("field_evaluation", stage="after_recovery", ts="2026-10-01T10:01:32+00:00"),
    ev("finalization_checkpoint_written", ts="2026-10-01T10:01:33+00:00"),
    ev("finalization_started", phase="finalization"),
    ev("model_response", phase="finalization"),
    ev("finalization_finished", status="parsed", ts="2026-10-01T10:01:40+00:00"),
    ev("run_finished", status="completed", ts="2026-10-01T10:01:41+00:00"),
]


def states(pipeline):
    return {s["key"]: s["state"] for s in pipeline.view()["stages"]}


def test_pipeline_walks_every_stage_in_order():
    p = pipeline_from_events(FULL_RUN[:3], "1")
    assert states(p)["acquisition"] == "running" and p.current_stage() == "acquisition"
    p = pipeline_from_events(FULL_RUN[:9], "1")
    assert states(p) == {"acquisition": "done", "harvest": "done", "sweep": "running", "recovery": "waiting",
                         "finalization": "waiting"}
    p = pipeline_from_events(FULL_RUN, "1")
    assert set(states(p).values()) == {"done"} and p.finished
    view = p.view()
    assert view["stages"][0]["duration_s"] == 60.0 and view["stages"][2]["duration_s"] == 28.0
    c = view["counters"]
    assert (c["sources"], c["target_market_sources"], c["candidates"], c["candidate_fields"]) == (1, 1, 3, 1)
    assert c["research_turns"] == 1 and c["searches"] == 1 and c["model_calls"] == 3
    assert c["model_calls_by_stage"] == {"acquisition": 1, "sweep": 1, "finalization": 1}


def test_research_failure_marks_later_stages_not_run():
    events = FULL_RUN[:4] + [ev("error", phase="research", message="GLM request timed out"),
                             ev("research_stopped", reason="api_failure"),
                             ev("run_finished", status="research_failed")]
    s = states(pipeline_from_events(events))
    assert s["acquisition"] == "failed" and {s[k] for k in ("harvest", "sweep", "recovery", "finalization")} == {
        "not_run"}


def test_interruption_and_finalization_retry_from_checkpoint():
    events = FULL_RUN[:16] + [ev("interrupted", phase="finalization"), ev("run_finished", status="interrupted")]
    p = pipeline_from_events(events)
    assert states(p)["finalization"] == "interrupted" and p.engine_status == "interrupted"
    p.apply(ev("recovery_started"))
    assert states(p)["finalization"] == "running" and not p.finished
    p.apply(ev("recovery_finished", status="recovered_finalized"))
    assert states(p)["finalization"] == "done" and p.engine_status == "recovered_finalized"


def test_model_finished_without_finalizer_skips_finalization():
    events = [ev("run_started", agent_config={"layered_harvest_enabled": False}),
              ev("research_stopped", reason="model_finished"),
              ev("field_evaluation", stage="primary"), ev("field_evaluation", stage="after_recovery"),
              ev("run_finished", status="completed")]
    s = states(pipeline_from_events(events))
    assert s == {"acquisition": "done", "harvest": "skipped", "sweep": "skipped", "recovery": "done",
                 "finalization": "skipped"}


def test_stage_tracker_only_advances(tmp_path):
    repo = FileRunRepository(tmp_path)
    repo.create(RunRecord(run_id="r", status=M.STARTING))
    tracker = _StageTracker(SimpleNamespace(repository=repo), "r")
    listen = tracker.listener_for("1")
    listen("run_started", {})
    assert repo.get("r").status == M.RESEARCHING
    listen("document_sweep_started", {"phase": "document_sweep"})
    assert (repo.get("r").status, repo.get("r").stage) == (M.SWEEPING, "sweep")
    listen("model_response", {"phase": "research"})                 # a stale phase never moves it back
    assert repo.get("r").stage == "sweep"
    listen("finalization_started", {"phase": "finalization"})
    assert repo.get("r").status == M.FINALIZING


# --- reconnect / restoration from durable state ---------------------------------------------------------------

def test_event_tail_reads_incrementally_and_waits_for_torn_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(FULL_RUN[0]) + "\n" + json.dumps(FULL_RUN[1])[:20])
    tail = EventTail(path)
    assert [e["kind"] for e in tail.read_new()] == ["run_started"]
    with path.open("a") as fh:
        fh.write(json.dumps(FULL_RUN[1])[20:] + "\n")
    assert [e["kind"] for e in tail.read_new()] == ["model_response"] and tail.read_new() == []


def test_a_new_session_rebuilds_the_same_pipeline_from_disk(tmp_path):
    path = tmp_path / "events.jsonl"
    with path.open("w") as fh:
        for event in FULL_RUN[:9]:
            fh.write(json.dumps(event) + "\n")
    cache = PipelineCache()
    assert cache.get(path, "1").current_stage() == "sweep"           # watched live, part-way through
    with path.open("a") as fh:
        for event in FULL_RUN[9:]:
            fh.write(json.dumps(event) + "\n")
    live = cache.get(path, "1")                                       # incremental: only the new lines
    reopened = PipelineCache().get(path, "1")                         # a refreshed browser / restarted process
    replayed = pipeline_from_events(FULL_RUN, "1")
    for view in (live.view(), reopened.view()):
        assert view["stages"] == replayed.view()["stages"] and view["counters"] == replayed.view()["counters"]


# --- the run manager: background execution, duplicates, restarts ------------------------------------------------

class FakeClient(SimpleNamespace):
    pass


def fake_factory(settings, controller, cancel):
    def make():
        return FakeClient(model="glm-5.3-flash", finalizer_model="glm-5.3-flash",
                          settings=SimpleNamespace(model="glm-5.3-flash", public=lambda: {}))
    return make


def fake_loader(ids, source, dsn):
    return SimpleNamespace(rows=[{"upstream_record_id": i} for i in ids], missing=[], source="snapshot", note="")


def scripted_research(gate: threading.Event | None = None, status="completed", fail_with=None):
    def research(vehicle, row, *, client, cache, runs_dir, batch_id, listener, cancel_event, **kw):
        rid = vehicle["upstream_record_id"]
        log = RunLog(runs_dir, batch_id, rid, listener=listener)
        for event in FULL_RUN[:9]:
            log.event(event["kind"], **{k: v for k, v in event.items() if k not in ("kind", "ts")})
        if gate is not None:
            while not gate.wait(0.02):
                if cancel_event.is_set():
                    from src.concurrency import BatchCancelled
                    log.event("interrupted", phase="document_sweep")
                    log.write_result({"record_id": rid, "status": "interrupted", "output": None,
                                      "interrupted_phase": "document_sweep", "documents": ["d1"]})
                    log.event("run_finished", status="interrupted")
                    raise BatchCancelled("stop")
        if fail_with:
            raise fail_with
        for event in FULL_RUN[9:-1]:
            log.event(event["kind"], **{k: v for k, v in event.items() if k not in ("kind", "ts")})
        result = {"record_id": rid, "status": status, "documents": ["d1"],
                  "output": {"summary": "ok", "fields": {}} if status == "completed" else None,
                  "error": "GLM finalizer HTTP 500" if status == "finalization_failed" else None,
                  "api_error": {"status": 500} if status == "finalization_failed" else None}
        log.write_result(result)
        log.event("run_finished", status=status)
        return result
    return research


def make_manager(tmp_path, research, **kw):
    paths = resolve_paths(env_of({"TRIPY_DATA_DIR": str(tmp_path / "data")}))
    return RunManager(paths, client_factory=fake_factory, level15_loader=fake_loader, research_fn=research,
                      register_atexit=False, **kw)


def request(ids=("1",), key=None):
    return ResearchRequest(vehicles=[{"upstream_record_id": i, "manufacturer": "M", "model": f"V{i}"} for i in ids],
                           label="M V", scope="One vehicle", settings=SimpleNamespace(model="glm-5.3-flash",
                                                                                      finalizer_model=""),
                           agent_cfg=AgentConfig(), tool_cfg=ToolConfig(), pricing={}, prompt_version="p",
                           idempotency_key=key)


def wait_terminal(manager, run_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = manager.get(run_id)
        if record and record.terminal and not manager.is_executing(run_id):
            return record
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish")


def test_run_executes_in_the_background_and_survives_a_new_reader(tmp_path):
    gate = threading.Event()
    manager = make_manager(tmp_path, scripted_research(gate))
    started = manager.start(request(key="k"))
    assert started.created
    deadline = time.monotonic() + 10
    while manager.get(started.run_id).status != M.SWEEPING and time.monotonic() < deadline:
        time.sleep(0.02)
    # a different "browser session" (or a fresh repository) sees the active run from disk only
    other = FileRunRepository(manager.runs_dir).get(started.run_id)
    assert other.active and other.status == M.SWEEPING and other.owner["boot_id"] == manager.boot_id
    gate.set()
    record = wait_terminal(manager, started.run_id)
    assert record.status == M.COMPLETED and record.vehicles["1"]["engine_status"] == "completed"
    report = record.report["vehicles"]["1"]
    assert set(report["durations_s"]) == {"acquisition", "harvest", "sweep", "recovery", "finalization"}
    assert report["candidate_count"] == 3 and report["target_market_documents"] == 1
    assert (manager.runs_dir / started.run_id / "batch.json").is_file()


def test_duplicate_runs_are_never_started(tmp_path):
    gate = threading.Event()
    manager = make_manager(tmp_path, scripted_research(gate))
    first = manager.start(request(key="click-1"))
    again = manager.start(request(key="click-1"))                  # the same submission rerendered
    assert again.run_id == first.run_id and not again.created
    with pytest.raises(RunRejected) as same_target:
        manager.start(request(key="click-2"))                      # a second click for the same vehicle
    assert same_target.value.existing_run_id == first.run_id
    with pytest.raises(RunRejected):
        manager.start(request(ids=("2",), key="click-3"))          # TRIPY_MAX_ACTIVE_RUNS=1
    with pytest.raises(RunRejected):
        manager.retry_finalization(first.run_id, "1", SimpleNamespace(model="m", finalizer_model=""))
    gate.set()
    wait_terminal(manager, first.run_id)
    assert len([r for r in manager.list_runs() if not r.legacy]) == 1


def test_cancel_persists_an_interrupted_partial_result(tmp_path):
    gate = threading.Event()
    manager = make_manager(tmp_path, scripted_research(gate))
    run_id = manager.start(request()).run_id
    time.sleep(0.2)
    assert manager.cancel(run_id)
    record = wait_terminal(manager, run_id)
    assert record.status == M.CANCELLED and record.error["code"] == "cancelled"
    assert record.vehicles["1"]["status"] == M.CANCELLED


def test_unexpected_worker_error_is_a_clean_failure(tmp_path):
    manager = make_manager(tmp_path, scripted_research(fail_with=RuntimeError("boom Bearer abcdefghijkl")))
    record = wait_terminal(manager, manager.start(request()).run_id)
    assert record.status == M.FAILED and record.vehicles["1"]["engine_status"] == "error"
    assert record.error["code"] == "unexpected"
    assert "abcdefghijkl" not in json.dumps(record.to_dict())


def test_restart_marks_orphaned_runs_interrupted_and_keeps_the_checkpoint(tmp_path):
    gate = threading.Event()
    manager = make_manager(tmp_path, scripted_research(gate))
    run_id = manager.start(request()).run_id
    deadline = time.monotonic() + 10
    while manager.get(run_id).status != M.SWEEPING and time.monotonic() < deadline:
        time.sleep(0.02)
    # the process dies: only its files remain (simulate the checkpoint the engine writes before the finalizer)
    run_dir = manager.runs_dir / run_id / "1"
    (run_dir / "result.json").write_text(json.dumps({"record_id": "1", "status": "finalization_pending",
                                                      "output": None, "documents": ["d1"]}))
    restarted = make_manager(tmp_path, scripted_research())          # a new process (new boot id), same volume
    record = restarted.get(run_id)
    assert record.status == M.INTERRUPTED and record.error["code"] == "server_stopped"
    assert record.vehicles["1"]["engine_status"] == "finalization_pending"
    gate.set()


def test_failed_run_restores_with_a_retry_from_the_checkpoint(tmp_path):
    manager = make_manager(tmp_path, scripted_research(status="finalization_failed"))
    run_id = manager.start(request()).run_id
    record = wait_terminal(manager, run_id)
    assert record.status == M.FAILED and record.error["stage"] == "finalization"
    assert record.error["code"] == "provider_error"
    calls = []

    def finalize(runs_dir, batch_id, record_id, *, client, cache, config, tool_config, metrics_fn):
        calls.append((batch_id, record_id, config.max_steps))
        log = RunLog(runs_dir, batch_id, record_id)
        log.event("recovery_started")
        log.write_result({"record_id": record_id, "status": "recovered_finalized", "output": {"fields": {}},
                          "recovered": True})
        log.event("recovery_finished", status="recovered_finalized")
        return {}

    restored = make_manager(tmp_path, scripted_research())
    restored._finalize_fn = finalize
    restored.retry_finalization(run_id, "1", SimpleNamespace(model="x", finalizer_model=""))
    record = wait_terminal(restored, run_id)
    assert calls == [(run_id, "1", AgentConfig().max_steps)]       # the run's own saved AgentConfig
    assert record.status == M.COMPLETED and [j["kind"] for j in record.jobs] == ["research", "finalization_retry"]
    with pytest.raises(RunRejected):
        restored.retry_finalization(run_id, "1", SimpleNamespace(model="x", finalizer_model=""))   # has output


def test_shutdown_interrupts_active_runs(tmp_path):
    gate = threading.Event()
    manager = make_manager(tmp_path, scripted_research(gate))
    run_id = manager.start(request()).run_id
    time.sleep(0.2)
    manager.shutdown(grace_s=5)
    record = wait_terminal(manager, run_id)
    assert record.status == M.INTERRUPTED
    with pytest.raises(RunRejected):
        manager.start(request(key="late"))


# --- failure explanations --------------------------------------------------------------------------------------

def test_failure_reasons_are_plain_language():
    assert classify_reason("ReadTimeout: timed out")[0] == "provider_timeout"
    assert classify_reason("x", {"status": 429})[0] == "rate_limited"
    assert classify_reason("x", {"status": 401})[0] == "auth"
    assert classify_reason("x", {"status": 503})[0] == "provider_error"
    assert classify_reason(None, None, "completed_unparsed")[0] == "malformed_response"
    assert classify_reason("checkpoint_write_failed: OSError")[0] == "storage"


def test_explain_offers_safe_recovery_only():
    failed = {"status": "finalization_failed", "error": "GLM HTTP 500", "api_error": {"status": 500,
              "headers": {"x": 1}}, "documents": ["d1"], "output": None}
    info = explain(failed)
    assert info["title"] == "Research stopped during Finalization." and info["actions"] == ["finalize", "restart"]
    assert "preserved" in info["preserved"] and "headers" not in info["technical"]
    research_failed = explain({"status": "research_failed", "error": "timed out", "output": None})
    assert research_failed["stage"] == "acquisition" and research_failed["actions"] == ["restart"]   # nothing kept
    assert explain({"status": "completed", "output": {"fields": {}}}) is None
    cancelled = explain({"status": "interrupted", "interrupted_phase": "document_sweep", "documents": ["d"]},
                        run_status=M.CANCELLED)
    assert cancelled["code"] == "cancelled" and cancelled["stage_label"] == "Document sweep"
    legacy = explain(failed, can_finalize=False)
    assert legacy["actions"] == ["restart"]


# --- configuration -----------------------------------------------------------------------------------------------

def test_missing_required_variables_are_reported_precisely(tmp_path):
    checks = validate_config(env_of({"TRIPY_DATA_DIR": str(tmp_path)}))
    glm = next(c for c in checks if c.name == "GLM")
    assert glm.level == "error" and glm.status == "Missing GLM_API_KEY, GLM_MODEL"
    assert {c.name for c in blocking_errors(checks)} >= {"GLM", "Search"}
    ok = validate_config(env_of({"TRIPY_DATA_DIR": str(tmp_path), "GLM_API_KEY": "secret-value-123",
                                 "GLM_MODEL": "glm-5.3-flash"}))
    assert not blocking_errors(ok) and "secret-value-123" not in json.dumps([c.as_dict() for c in ok])
    bad = validate_config(env_of({"TRIPY_DATA_DIR": str(tmp_path), "GLM_API_KEY": "k", "GLM_MODEL": "m",
                                  "GLM_EXTRA_BODY": "{nope", "BATCH_MAX_WORKERS": "many"}))
    problem = next(c for c in bad if c.name == "Configuration")
    assert "GLM_EXTRA_BODY" in problem.detail and "BATCH_MAX_WORKERS" in problem.detail


def test_production_is_tripy_env_or_railway():
    assert is_production(env_of({"TRIPY_ENV": "production"}))
    assert is_production(env_of({"RAILWAY_ENVIRONMENT": "production"}))
    assert not is_production(env_of({}))


def test_redaction_and_diagnostics_never_leak_secrets():
    env = {"GLM_API_KEY": "sk-live-ABCDEFG", "DATABASE_URL": "postgresql://user:pw123@db:5432/x"}
    text = redact("key sk-live-ABCDEFG at postgresql://user:pw123@db:5432/x Bearer tok_123456789 "
                  '{"api_key": "zzzzzz"}', env_of(env))
    assert "ABCDEFG" not in text and "pw123" not in text and "tok_123456789" not in text and "zzzzzz" not in text
    info = json.dumps(diagnostics(env_of({**env, "TRIPY_DATA_DIR": "/tmp/tripy-diag"})))
    assert "ABCDEFG" not in info and "pw123" not in info and '"glm_api_key_present": true' in info


# --- Railway start configuration ---------------------------------------------------------------------------------

def test_railway_starts_one_uvicorn_process_on_port():
    start = (ROOT / "scripts" / "start.sh").read_text()
    command = [line for line in start.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert "exec uvicorn src.api.app:app \\" in command and "--host 0.0.0.0 \\" in [c.strip() for c in command]
    assert '--port "${PORT:-8000}"' in start
    assert "streamlit" not in start.lower() and "--workers" not in "\n".join(command)
    assert "--reload" not in start and "tripy_server" not in start
    railway = json.loads((ROOT / "railway.json").read_text())
    assert railway["build"]["builder"] == "DOCKERFILE"
    assert railway["deploy"]["startCommand"] == "sh scripts/start.sh"
    assert railway["deploy"]["healthcheckPath"] == "/health"
    assert "_stcore" not in json.dumps(railway)
    docker = (ROOT / "Dockerfile").read_text()
    assert 'CMD ["sh", "scripts/start.sh"]' in docker and "TRIPY_DATA_DIR=/data" in docker
    assert "8501" not in docker and "_stcore" not in docker and "streamlit" not in docker.lower()
    assert "npm ci" in docker and "npm install" not in docker and "COPY --from=frontend" in docker
    ignored = (ROOT / ".dockerignore").read_text()
    assert ".env" in ignored and "runs/" in ignored and ".tripy-data/" in ignored
    assert "frontend/node_modules" in ignored and "frontend/dist" in ignored


def test_env_example_documents_every_variable_the_code_reads():
    sources = list(ROOT.glob("src/**/*.py"))
    pattern = re.compile(r"""(?:environ\.get|environ\[|env|secret|_get\(lookup,|lookup)\(\s*["']([A-Z][A-Z0-9_]{2,})["']""")
    names = set()
    for path in sources:
        names |= set(pattern.findall(path.read_text("utf-8")))
    from src.agent import AGENT_ENV, TOOL_ENV
    names |= set(AGENT_ENV.values()) | set(TOOL_ENV.values())       # read through mappings, not literals
    names |= {"TRIPY_DATA_DIR", "TRIPY_ENV", "TRIPY_MAX_ACTIVE_RUNS", "TRIPY_SHUTDOWN_GRACE_S"}
    platform = {"PORT", "RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_VOLUME_MOUNT_PATH"}
    example = (ROOT / ".env.example").read_text()
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]{2,})=", example, re.M))
    missing = sorted(n for n in names - platform if n not in documented and not n.startswith("GLM_PRICE_X"))
    assert not missing, f".env.example lacks: {missing}"
    assert not re.search(r"^GLM_API_KEY=\S", example, re.M)        # never a real credential
