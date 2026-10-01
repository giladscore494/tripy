"""Durable pre-finalization checkpoint: result.json (finalization_pending) exists BEFORE the paid finalizer
request starts, survives a hard process death, and --finalize-existing finishes it with exactly one finalizer
call (no research, no harvest, no sweep, no recovery, no search). Scripted GLM / fake HTTP only."""

import json
import shutil
from pathlib import Path

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
        run(tmp_path, ctx, client, requested_fields=["torque_nm"], field_recovery_max_attempts=1)
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
