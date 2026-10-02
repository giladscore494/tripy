"""The redesigned Streamlit app, rendered by Streamlit's AppTest from durable state only."""

import json
from pathlib import Path

import pytest

from src.runstate import model as M
from src.runstate.model import RunRecord
from src.runstate.repository import FileRunRepository
from src.storage.run_log import RunLog, write_batch

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    for name in ("GLM_API_KEY", "GLM_MODEL", "MILO_RUNS_DIR", "MILO_CACHE_DIR", "TRIPY_ENV", "RAILWAY_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRIPY_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data" / "runs"


def app_test():
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(APP, default_timeout=60)


def texts(app) -> str:
    parts = [m.value for m in app.markdown] + [c.value for c in app.caption] + [e.value for e in app.error]
    parts += [w.value for w in app.warning] + [i.value for i in app.info] + [b.label for b in app.button]
    return "\n".join(str(p) for p in parts)


def make_run(runs_dir: Path, run_id: str, *, status: str, engine_status: str, events: list[tuple[str, dict]],
             result: dict | None, owner: dict | None = None, error: dict | None = None) -> None:
    write_batch(runs_dir, run_id, {"batch_id": run_id, "record_ids": ["101122"], "selection": "One vehicle",
                                   "research_model": "glm-5.3-flash", "agent_config": {}})
    log = RunLog(runs_dir, run_id, "101122")
    for kind, data in events:
        log.event(kind, **data)
    if result is not None:
        log.write_result(result)
    FileRunRepository(runs_dir).create(RunRecord(
        run_id=run_id, status=status, started_at="2026-10-01T10:00:00+00:00",
        finished_at=None if status in M.ACTIVE_STATUSES else "2026-10-01T10:05:00+00:00",
        target={"label": "XPeng G6 · 2026 · MAX", "scope": "One vehicle", "record_ids": ["101122"],
                "vehicles": [{"record_id": "101122", "label": "XPeng G6 · 2026 · MAX"}]},
        request={"research_model": "glm-5.3-flash"}, owner=owner or {"boot_id": "dead-process"},
        vehicles={"101122": {"status": status if status in M.ACTIVE_STATUSES else M.FAILED,
                             "engine_status": engine_status}},
        error=error, jobs=[{"kind": "research", "started_at": "2026-10-01T10:00:00+00:00"}]))


FAILED_EVENTS = [("run_started", {"agent_config": {"layered_harvest_enabled": True}}),
                 ("tool_result", {"result": {"document_id": "d1", "url": "https://importer.example/spec"}}),
                 ("research_stopped", {"reason": "model_finished"}),
                 ("deterministic_harvest_summary", {"candidate_count_total": 66, "candidate_fields_total": 26}),
                 ("document_sweep_started", {"phase": "document_sweep"}),
                 ("document_sweep_finished", {"unique_fields_resolved": 2}),
                 ("field_evaluation", {"stage": "primary"}), ("field_evaluation", {"stage": "after_recovery"}),
                 ("finalization_checkpoint_written", {}),
                 ("finalization_failed", {"error": "GLMError: HTTP 504 Bearer sk-should-not-show",
                                          "api_error": {"status": 504}}),
                 ("run_finished", {"status": "finalization_failed"})]


def test_production_without_secrets_shows_a_precise_configuration_error(data_env, monkeypatch):
    monkeypatch.setenv("TRIPY_ENV", "production")
    app = app_test()
    app.run()
    assert not app.exception, app.exception
    body = texts(app)
    assert "Missing GLM_API_KEY, GLM_MODEL" in body and "No research yet" in body
    start = next(b for b in app.button if b.label == "Start research")
    assert start.disabled
    assert not any("API key" in t.label for t in app.text_input)        # never typed into the UI in production


def test_development_allows_a_session_api_key(data_env):
    app = app_test()
    app.run()
    assert any("API key (development only)" in t.label for t in app.text_input)


def test_a_failed_run_is_restored_cleanly_from_disk(data_env, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "sk-should-not-show")
    monkeypatch.setenv("GLM_MODEL", "glm-5.3-flash")
    result = {"record_id": "101122", "batch_id": "20261001T100000Z-failed", "status": "finalization_failed",
              "error": "GLMError: HTTP 504 Bearer sk-should-not-show", "api_error": {"status": 504},
              "documents": ["d1"], "output": None, "duration_s": 300}
    make_run(data_env, "20261001T100000Z-failed", status=M.FAILED, engine_status="finalization_failed",
             events=FAILED_EVENTS, result=result,
             error={"stage": "finalization", "code": "provider_error", "message": "x"})
    app = app_test()
    app.run()
    assert not app.exception, app.exception
    body = texts(app)
    assert "Research stopped during Finalization." in body
    assert "The model provider returned a server error (HTTP 504)." in body
    assert "harvested candidates and admitted evidence were preserved" in body
    assert "Retry finalization" in body and "Restart research" in body
    assert "Traceback" not in body
    rendered = json.dumps([e.value for e in [*app.markdown, *app.error, *app.caption, *app.code, *app.json]], default=str)
    assert "sk-should-not-show" not in rendered


def test_an_orphaned_active_run_is_reported_interrupted_after_a_restart(data_env):
    make_run(data_env, "20261001T100000Z-orphan", status=M.SWEEPING, engine_status=None,
             events=FAILED_EVENTS[:5], result=None, owner={"boot_id": "previous-container"})
    app = app_test()
    app.run()
    assert not app.exception, app.exception
    record = FileRunRepository(data_env).get("20261001T100000Z-orphan")
    assert record.status == M.INTERRUPTED and record.error["code"] == "server_stopped"
    body = texts(app)
    assert "Research stopped during Document sweep." in body and "The server stopped" in body
    assert "Finalize from preserved research" in body


def test_history_lists_runs_and_the_url_selects_one(data_env):
    make_run(data_env, "20261001T090000Z-older", status=M.FAILED, engine_status="finalization_failed",
             events=FAILED_EVENTS, result={"record_id": "101122", "status": "finalization_failed", "output": None,
                                           "documents": ["d1"]})
    done = {"record_id": "101122", "status": "completed", "output": {"summary": "Done summary.", "fields": {
        "torque_nm": {"value": 660, "unit": "Nm", "market": "IL", "evidence_ids": ["e1"]}}}, "documents": ["d1"]}
    make_run(data_env, "20261001T100000Z-newer", status=M.COMPLETED, engine_status="completed",
             events=FAILED_EVENTS[:8] + [("run_finished", {"status": "completed"})], result=done)
    app = app_test()
    app.run()
    body = texts(app)
    assert "Done summary." in body                                   # newest run opens by default
    assert sum(1 for b in app.button if b.label.startswith(("▸ XPeng", "XPeng"))) == 2
    app.query_params["run"] = "20261001T090000Z-older"
    app.run()
    assert not app.exception, app.exception
    assert "Research stopped during Finalization." in texts(app)
