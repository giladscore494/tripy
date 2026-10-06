"""Data-volume retention: run size and deletion (R1), compaction / age rule / cache cleanup / logs cap (R2) and the disk
refusal's hint (R3). The RunManager and the API are real (the test_api fixtures); no network."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from src.storage import disk
from src.storage.retention import COMPACTED_NAME, LOGS_CAP_BYTES, LOG_NAME, Retention, cap_logs

from test_api import RECORD, RUN, VEHICLE, client, ctx, data_root, gate, wait_status  # noqa: F401
from src.runstate import model as M

MB = 1024 * 1024


def _blob(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _heavy(vehicle: Path) -> None:
    """The artefacts of a finished vehicle: kept files plus the heavy ones compaction deletes."""
    vehicle.mkdir(parents=True, exist_ok=True)
    (vehicle / "result.json").write_text(json.dumps({"record_id": RECORD, "status": "completed", "output": {}}), "utf-8")
    (vehicle / "diagnostics.json").write_text("{}", "utf-8")
    (vehicle / "diagnostics.jsonl").write_text("{}\n", "utf-8")
    (vehicle / "binding_replay_summary.json").write_text("{}", "utf-8")
    (vehicle / "binding_replay.jsonl").write_text("{}\n", "utf-8")
    (vehicle / "training_feedback.jsonl").write_text("{}\n", "utf-8")
    _blob(vehicle / "documents" / "d_0123456789abcdef" / "body.bin", 2 * MB)
    _blob(vehicle / "finalizer_request.json", 1000)
    _blob(vehicle / "recovery" / "20261005T000000Z" / "research_bundle.json", 3000)
    _blob(vehicle / "result.pre-recovery-20261005T000000Z.json", 2 * MB)


# --- R1: size and deletion ------------------------------------------------------------------------------------------------

def test_the_runs_list_carries_each_runs_size(client, ctx):
    runs = {r["run_id"]: r for r in client.get("/api/runs").json()["runs"]}
    assert runs[RUN]["size_bytes"] > 0 and runs[RUN]["pinned"] is False and runs[RUN]["compacted_at"] is None
    assert client.get(f"/api/runs/{RUN}").json()["size_bytes"] == runs[RUN]["size_bytes"]


def test_delete_removes_only_that_runs_folder_and_is_logged(client, ctx):
    runs = ctx.paths.runs_dir
    others = sorted(p.name for p in runs.iterdir() if p.name != RUN)
    size = sum(f.stat().st_size for f in (runs / RUN).rglob("*") if f.is_file())
    response = client.delete(f"/api/runs/{RUN}")
    assert response.status_code == 200 and response.json() == {"run_id": RUN, "deleted": True, "bytes_freed": size}
    assert not (runs / RUN).exists() and sorted(p.name for p in runs.iterdir()) == others
    assert (ctx.paths.data_dir / "cache" / "documents").is_dir()            # the shared cache is untouched
    entry = json.loads((ctx.paths.data_dir / "derived" / LOG_NAME).read_text("utf-8").splitlines()[-1])
    assert entry["action"] == "delete_run" and entry["run_id"] == RUN and entry["bytes"] == size and entry["at"]
    assert client.get(f"/api/runs/{RUN}").status_code == 404
    assert RUN not in {r["run_id"] for r in client.get("/api/runs").json()["runs"]}
    assert client.delete(f"/api/runs/{RUN}").status_code == 404
    assert client.delete("/api/runs/..").status_code in (404, 405)


def test_delete_is_refused_while_the_run_executes(client, ctx, gate):
    started = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE})
    run_id = started.json()["run_id"]
    wait_status(ctx.manager, run_id, M.SWEEPING)
    refused = client.delete(f"/api/runs/{run_id}")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "run_executing"
    assert (ctx.paths.runs_dir / run_id).is_dir()
    gate.set()


def test_mcp_reports_a_deleted_run_as_absent(client, ctx):
    from src.mcp_server.safety import ToolInputError
    from src.mcp_server.tools import Observer

    observer = Observer(ctx.paths)
    assert RUN in {r["run_id"] for r in observer.list_runs()["runs"]}
    client.delete(f"/api/runs/{RUN}")
    assert RUN not in {r["run_id"] for r in observer.list_runs()["runs"]}
    with pytest.raises(ToolInputError):
        observer.run_status(RUN)


# --- R2: compaction -------------------------------------------------------------------------------------------------------

def test_compaction_keeps_result_diagnostics_events_and_replay_inputs_and_the_run_still_opens(client, ctx):
    from src.binding_replay import load_or_replay

    vehicle = ctx.paths.runs_dir / RUN / RECORD
    _heavy(vehicle)
    (ctx.paths.runs_dir / RUN / "diagnostics").mkdir(exist_ok=True)
    (ctx.paths.runs_dir / RUN / "diagnostics" / "benchmark.json").write_text("{}", "utf-8")
    events_before = (vehicle / "events.jsonl").read_bytes()
    assert client.put("/api/data/retention", json={"keep_newest": 0}).status_code == 200
    preview = client.get("/api/data/retention").json()["compaction"]
    planned = next(r for r in preview["runs"] if r["run_id"] == RUN)
    assert planned["bytes"] == 4 * MB + 4000
    assert client.post("/api/data/retention/compact", json={}).status_code == 422          # confirmation required
    out = client.post("/api/data/retention/compact", json={"confirm": True}).json()
    assert {"run_id": RUN, "bytes_freed": 4 * MB + 4000} in out["runs"]
    kept = sorted(p.name for p in vehicle.iterdir())
    assert kept == ["binding_replay.jsonl", "binding_replay_summary.json", "diagnostics.json", "diagnostics.jsonl",
                    "events.jsonl", "input.json", "result.json", "training_feedback.jsonl"]
    assert (vehicle / "events.jsonl").read_bytes() == events_before
    assert (ctx.paths.runs_dir / RUN / "diagnostics" / "benchmark.json").exists()
    assert (ctx.paths.runs_dir / RUN / "run_state.json").exists()
    record = json.loads((ctx.paths.runs_dir / RUN / COMPACTED_NAME).read_text("utf-8"))
    assert record["bytes_freed"] == 4 * MB + 4000 and f"{RECORD}/documents" in record["removed"]
    detail = client.get(f"/api/runs/{RUN}")
    assert detail.status_code == 200 and detail.json()["compacted_at"] == record["compacted_at"]
    assert client.get(f"/api/runs/{RUN}/results").status_code == 200
    (vehicle / "binding_replay_summary.json").unlink()                          # force a replay from the kept inputs
    (vehicle / "binding_replay.jsonl").unlink()
    replay = load_or_replay(vehicle, persist=False)
    assert replay["summary"]["record_id"] == RECORD
    assert client.get("/api/data/retention").json()["compaction"]["runs"] == []    # nothing left to free


def test_a_pinned_run_is_never_compacted(client, ctx):
    vehicle = ctx.paths.runs_dir / RUN / RECORD
    _heavy(vehicle)
    client.put("/api/data/retention", json={"keep_newest": 0})
    assert client.post(f"/api/runs/{RUN}/keep", json={"keep": True}).json() == {"run_id": RUN, "pinned": True}
    assert client.get(f"/api/runs/{RUN}").json()["pinned"] is True
    assert RUN not in {r["run_id"] for r in client.get("/api/data/retention").json()["compaction"]["runs"]}
    client.post("/api/data/retention/compact", json={"confirm": True})
    assert (vehicle / "documents").is_dir() and not (ctx.paths.runs_dir / RUN / COMPACTED_NAME).exists()
    with pytest.raises(Exception) as refused:
        ctx.manager.retention.compact_run(RUN)
    assert getattr(refused.value, "code", "") == "run_pinned"
    older = client.get("/api/data/retention/older-than", params={"days": 1}).json()["runs"]
    assert RUN not in {r["run_id"] for r in older}                                # never deleted by the age rule


def test_the_newest_runs_are_kept_whole():
    class Rec:
        def __init__(self, run_id, created):
            self.run_id, self.created_at, self.started_at, self.active, self.legacy = run_id, created, created, False, False

    class Manager:
        records = [Rec("c", "2026-10-05T00:00:00+00:00"), Rec("b", "2026-09-01T00:00:00+00:00"),
                   Rec("a", "2026-08-01T00:00:00+00:00")]

        def list_runs(self):
            return self.records

        def active_runs(self):
            return []

        def is_executing(self, run_id):
            return False

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rec in Manager.records:
            _heavy(root / "runs" / rec.run_id / "1")
            (root / "runs" / rec.run_id / "1" / "events.jsonl").write_text("{}\n", "utf-8")
        retention = Retention(Manager(), root, root / "runs", root / "cache")
        retention.set_keep_newest(1)
        assert sorted(r["run_id"] for r in retention.compaction_plan()["runs"]) == ["a", "b"]
        retention.set_pinned("b", True)
        assert [r["run_id"] for r in retention.compaction_plan()["runs"]] == ["a"]
        plan = retention.older_than_plan(10)
        assert [r["run_id"] for r in plan["runs"]] == ["a"]                       # not the newest, not the pinned
        assert retention.delete_older_than(10)["runs"] == ["a"] and not (root / "runs" / "a").exists()


# --- R2: the document cache -----------------------------------------------------------------------------------------------

def test_the_cache_cleanup_keeps_documents_of_kept_runs_and_the_research_memory(client, ctx):
    docs = ctx.paths.data_dir / "cache" / "documents"
    kept_ids = {p.name for p in docs.iterdir()}
    events = (ctx.paths.runs_dir / RUN / RECORD / "events.jsonl").read_text("utf-8")
    used = {d for d in kept_ids if d in events}
    assert used                                                                       # the fixture run cites them
    _blob(docs / "d_ffffffffffffffff" / "body.bin", MB)                              # nobody uses it
    _blob(docs / "d_eeeeeeeeeeeeeeee" / "body.bin", 1000)                            # only the memory names it
    memory = ctx.paths.data_dir / "cache" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "facts.json").write_text(json.dumps({"document_id": "d_eeeeeeeeeeeeeeee"}), "utf-8")
    plan = client.get("/api/data/retention").json()["cache"]
    assert plan["bytes"] >= MB and plan["cache_bytes"] > plan["bytes"]
    out = client.post("/api/data/retention/clean-cache", json={"confirm": True}).json()
    remaining = {p.name for p in docs.iterdir()}
    assert "d_ffffffffffffffff" not in remaining and "d_eeeeeeeeeeeeeeee" in remaining
    assert used <= remaining and out["bytes_freed"] >= MB


# --- R2: logs -------------------------------------------------------------------------------------------------------------

def test_log_rotation_caps_the_logs_folder(tmp_path):
    from src import server_logging

    assert server_logging.LOG_FILE_MAX_BYTES * (server_logging.LOG_FILE_BACKUPS + 1) <= LOGS_CAP_BYTES
    logs = tmp_path / "logs"
    for n, size in enumerate([6 * MB, 6 * MB, 6 * MB, 6 * MB]):
        path = _blob(logs / (f"tripy.log.{n}" if n else "tripy.log"), size)
        os.utime(path, (time.time() - 1000 * (5 - n), time.time() - 1000 * (5 - n)))
    out = cap_logs(logs)
    assert out["bytes"] <= LOGS_CAP_BYTES and (logs / "tripy.log").exists()
    assert out["removed"] == ["tripy.log.1"]                                        # the oldest first
    handler = logging.handlers.RotatingFileHandler(logs / "rot.log", maxBytes=1000, backupCount=3)
    logger = logging.getLogger("tripy.test.rotation")
    logger.addHandler(handler)
    for _ in range(200):
        logger.warning("x" * 100)
    handler.close()
    assert len(list(logs.glob("rot.log*"))) == 4                                    # never more than 1 + backupCount


# --- R3: the disk refusal -------------------------------------------------------------------------------------------------

def test_the_disk_refusal_says_what_compaction_would_free_and_where(client, ctx, monkeypatch):
    _heavy(ctx.paths.runs_dir / RUN / RECORD)
    client.put("/api/data/retention", json={"keep_newest": 0})
    monkeypatch.setattr(disk, "free_bytes", lambda path: 33 * MB)
    refused = client.post("/api/runs", json={"scope": "one", "record_id": VEHICLE})
    error = refused.json()["error"]
    assert refused.status_code == 507 and error["code"] == "insufficient_disk"
    assert error["compaction_bytes"] >= 4 * MB and error["storage_path"] == "/data#storage"
    assert "Compacting the old runs would free" in error["message"] and "Data → Storage" in error["message"]
