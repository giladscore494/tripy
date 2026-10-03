"""Durable pre-finalization checkpoint: result.json (finalization_pending) exists BEFORE the paid finalizer
request starts, survives a hard process death, and --finalize-existing finishes it with exactly one finalizer
call (no research, no harvest, no sweep, no recovery, no search). Scripted GLM / fake HTTP only."""

import json
import shutil
from pathlib import Path

import pytest

from fixtures.cadillac_lyriq import put_documents
from test_layered_pipeline import PhaseGLM, read_docs, run, say, turn
from test_tools_smoke import _call

from src.agent import AgentConfig
from src.recovery import finalize_existing_run, plan_recovery
from src.storage.run_loader import load_runs
from src.storage.run_log import read_events, write_batch
from src.ui import labels_he as he


class ProcessDied(BaseException):
    """Stands in for SIGKILL: raised inside the finalizer request after the on-disk state was snapshotted."""


@pytest.mark.sweep_mode("legacy")   # encodes the legacy tool-loop sweep
@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_checkpoint_survives_a_hard_death_and_finalize_existing_needs_one_call(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)
    dead = tmp_path / "dead"

    def sweep(packet, turn_no):
        c = packet["deterministic_candidates"]["torque_nm"][0]
        return turn(_call("s", "store_evidence", {"field": "torque_nm", "value": c["value"], "unit": "Nm",
                                                  "document_id": c["document_id"], "quote": c["quote"],
                                                  "market": "IL", "variant_match": "exact"}))

    client = PhaseGLM([read_docs(ids[:3]), say({"summary": "primary", "fields": {}})], sweep=sweep)

    def die():
        # The finalizer HTTP request has begun: copy what is on disk right now (all a killed process leaves).
        shutil.copytree(tmp_path / "runs", dead)
        raise ProcessDied()

    client.on_finalizer = die
    write_batch(tmp_path / "runs", "b", {"batch_id": "b", "record_ids": ["85095"]})
    try:
        run(tmp_path, ctx, client, requested_fields=["torque_nm", "wheelbase_mm", "list_price"],
            field_recovery_max_attempts=1)
    except ProcessDied:
        pass

    run_dir = dead / "b" / "85095"
    saved = json.loads((run_dir / "result.json").read_text("utf-8"))
    assert saved["status"] == "finalization_pending" and saved["partial"] is True
    assert saved["output"] is None and saved["finalization"]["status"] == "started"      # nothing fabricated
    assert [e["field"] for e in saved["evidence"]] == ["torque_nm"]                     # evidence retained
    assert saved["candidate_summary"]["documents_harvested"] == 3                       # candidate summary retained
    assert saved["research_bundle"]["field_states"]["torque_nm"]["state"] == "ok"        # current field states
    assert saved["field_recovery"]["attempt_count"] >= 1 and saved["document_sweep"]["model_calls"] == 1
    kinds = [e["kind"] for e in read_events(run_dir / "events.jsonl")]
    assert kinds.index("finalization_checkpoint_written") < kinds.index("finalization_started")
    assert "run_finished" not in kinds and "interrupted" not in kinds

    loaded = load_runs(dead, "b")[0]                                                     # reload: not "incomplete"
    assert loaded["status"] == "finalization_pending" and not loaded.get("synthesized")
    assert "ניתן להריץ Finalizer מחדש בלי לבצע שוב את המחקר" in he.FINALIZATION_PENDING_MESSAGE_HE
    assert he.run_status_label("finalization_pending") == "ממתין לבניית התוצאה הסופית"

    plan = plan_recovery(dead, "b", "85095", cache=ctx.cache)
    assert plan["prior_status"] == "finalization_pending" and plan["research_events_sent_to_finalizer"] == 0

    finisher = PhaseGLM([])
    session_calls = len(ctx.session.calls)
    result = finalize_existing_run(dead, "b", "85095", client=finisher, cache=ctx.cache, config=AgentConfig())
    assert finisher.calls == {"research": 0, "document_sweep": 0, "field_recovery": 0, "finalization": 1}
    assert len(ctx.session.calls) == session_calls                                       # no fetch, no search
    assert result["status"] == "recovered_finalized" and result["output"]["summary"] == "final"
    assert result["recovery"]["prior_status"] == "finalization_pending"
    assert result["recovery"]["searches_performed"] == 0 and not result["partial"]
    assert [e["field"] for e in result["evidence"]] == ["torque_nm"]
    assert Path(result["recovery"]["prior_result_preserved_as"]).is_file()


@pytest.mark.acquisition_mode("legacy")   # encodes the legacy research contract
def test_runs_that_need_no_finalizer_write_no_checkpoint(tmp_path, make_ctx):
    ctx = make_ctx({})
    final = {"summary": "direct", "fields": {}}
    client = PhaseGLM([say(final)])
    result, events, _ = run(tmp_path, ctx, client, requested_fields=["torque_nm"], field_recovery_enabled=False)
    assert result["status"] == "completed" and result["output"]["summary"] == "direct"
    assert "finalization_checkpoint_written" not in [e["kind"] for e in events] and client.calls["finalization"] == 0


def test_streamlit_shows_the_hebrew_pending_message(tmp_path, make_ctx, monkeypatch):
    from streamlit.testing.v1 import AppTest

    ctx = make_ctx({})
    client = PhaseGLM([say({"summary": "primary", "fields": {}})])
    dead = tmp_path / "dead"

    def die():
        shutil.copytree(tmp_path / "runs", dead)   # all a killed process leaves on disk
        raise ProcessDied()

    client.on_finalizer = die
    write_batch(tmp_path / "runs", "b", {"batch_id": "b", "record_ids": ["85095"]})
    try:
        run(tmp_path, ctx, client, requested_fields=["torque_nm"], field_recovery_max_attempts=1,
            primary_research_min_base_documents=0, primary_research_min_base_scoped_coverage=0)
    except ProcessDied:
        pass
    assert json.loads((dead / "b" / "85095" / "result.json").read_text("utf-8"))["status"] == "finalization_pending"
    monkeypatch.setenv("MILO_RUNS_DIR", str(dead))
    monkeypatch.setenv("MILO_CACHE_DIR", str(tmp_path / "cache2"))
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=60)
    app.run()
    assert not app.exception, app.exception
    infos = " ".join(i.value for i in app.info)
    assert "המחקר הושלם וכל הראיות נשמרו" in infos and "ניתן להריץ Finalizer מחדש" in infos


# --- the write boundary: atomic, durable, fail closed ----------------------------------------------------

def test_the_checkpoint_is_complete_json_before_the_finalizer_is_invoked(tmp_path, make_ctx):
    ctx = make_ctx({})
    ids = put_documents(ctx.cache)
    client = PhaseGLM([read_docs(ids[:2]), say({"summary": "primary", "fields": {}})])
    seen = {}

    def inspect_disk():
        path = tmp_path / "runs" / "b" / "85095" / "result.json"
        seen["checkpoint"] = json.loads(path.read_text("utf-8"))           # complete, parseable, on disk
        seen["tmp_files"] = list(path.parent.glob(".result.json.*.tmp"))

    client.on_finalizer = inspect_disk
    result, events, _ = run(tmp_path, ctx, client, requested_fields=["torque_nm", "wheelbase_mm"], primary_research_min_base_documents=0, primary_research_min_base_scoped_coverage=0,
                            field_recovery_max_attempts=1)
    assert seen["checkpoint"]["status"] == "finalization_pending" and seen["checkpoint"]["output"] is None
    assert seen["tmp_files"] == [] and result["status"] == "completed"


def test_a_failed_checkpoint_write_never_reaches_the_finalizer(tmp_path, make_ctx, monkeypatch):
    """FAIL CLOSED: the paid finalizer request cannot happen unless the checkpoint was persisted."""
    import os

    import src.storage.atomic as atomic_mod

    ctx = make_ctx({})
    ids = put_documents(ctx.cache)
    real_replace = os.replace
    failed = []

    def replace(src, dst):
        if str(dst).endswith("result.json") and not failed:
            payload = json.loads(Path(src).read_text("utf-8"))
            if payload.get("status") == "finalization_pending":
                failed.append(dst)
                raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_mod.os, "replace", replace)
    client = PhaseGLM([read_docs(ids[:2]), say({"summary": "primary", "fields": {}})])
    result, events, log = run(tmp_path, ctx, client, requested_fields=["torque_nm", "wheelbase_mm"], primary_research_min_base_documents=0, primary_research_min_base_scoped_coverage=0,
                              field_recovery_max_attempts=1)
    kinds = [e["kind"] for e in events]
    assert failed and client.calls["finalization"] == 0                       # no paid finalizer request
    assert "finalization_started" not in kinds and "finalization_checkpoint_written" not in kinds
    write_failed = [e for e in events if e["kind"] == "result_write_failed"]
    assert write_failed and write_failed[0]["stage"] == "checkpoint" and "No space left" in write_failed[0]["error"]
    not_started = next(e for e in events if e["kind"] == "finalization_not_started")
    assert not_started["reason"] == "checkpoint_write_failed"
    last_model = max(i for i, e in enumerate(events) if e["kind"] == "model_response")
    assert last_model < kinds.index("result_write_failed")                   # no model call after the failure
    assert result["status"] == "finalization_failed" and result["output"] is None
    assert result["finalization"]["status"] == "not_started" and result["error"].startswith("checkpoint_write_failed")
    saved = json.loads((log.dir / "result.json").read_text("utf-8"))          # the safest state, once disk allows
    assert saved["status"] == "finalization_failed" and saved["evidence"] == result["evidence"]
    assert not list(log.dir.glob(".result.json.*.tmp"))


def test_atomic_writes_never_replace_a_valid_file_with_garbage(tmp_path, monkeypatch):
    import os

    import src.storage.atomic as atomic_mod
    from src.storage.run_log import RunLog

    log = RunLog(tmp_path, "b", "1")
    log.write_result({"status": "finalization_pending", "evidence": [1, 2, 3]})
    path = log.dir / "result.json"
    good = path.read_bytes()

    synced = []
    monkeypatch.setattr(atomic_mod.os, "fsync", lambda fd: synced.append(fd))
    log.write_result({"status": "completed", "evidence": [1, 2, 3]})
    assert json.loads(path.read_text("utf-8"))["status"] == "completed" and len(synced) >= 1   # durable: fsynced
    good = path.read_bytes()

    def boom_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(atomic_mod.os, "replace", boom_replace)
    with pytest.raises(OSError):
        log.write_result({"status": "interrupted", "evidence": []})
    assert path.read_bytes() == good and not list(log.dir.glob(".*.tmp"))  # untouched, temp removed

    monkeypatch.setattr(atomic_mod.os, "replace", os.replace)

    def boom_fsync(fd):
        raise OSError("fsync failed mid-write")

    monkeypatch.setattr(atomic_mod.os, "fsync", boom_fsync)
    with pytest.raises(OSError):
        log.write_result({"status": "interrupted", "evidence": []})
    assert path.read_bytes() == good and not list(log.dir.glob(".*.tmp"))

    with pytest.raises(AttributeError):                    # serialization fails before any file is touched
        atomic_mod.atomic_write_text(path, None)  # type: ignore[arg-type]
    assert path.read_bytes() == good
