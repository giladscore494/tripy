"""Run-state repository: the durable index of runs the UI discovers runs from.

`RunRepository` is the interface; `FileRunRepository` stores one `run_state.json` per run folder
(`<runs_dir>/<run_id>/run_state.json`, next to the engine's batch.json and per-vehicle folders), written with
the same atomic primitive as result.json. A database-backed repository can replace it later by implementing the
same five methods; nothing else in the app touches run_state.json directly.

Run folders written before run_state.json existed (or by the CLI) are listed too: their state is derived,
read-only, from batch.json and the engine's per-vehicle result.json / events.jsonl. Nothing is written into them.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from ..storage.atomic import atomic_write_json
from .model import INTERRUPTED, RunRecord, aggregate_status, status_for_engine_result

STATE_FILE = "run_state.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


class RunRepository(Protocol):
    def create(self, record: RunRecord) -> RunRecord: ...

    def get(self, run_id: str) -> RunRecord | None: ...

    def update(self, run_id: str, mutate: Callable[[RunRecord], None], *, durable: bool = True) -> RunRecord: ...

    def list_runs(self, limit: int | None = None) -> list[RunRecord]: ...

    def exists(self, run_id: str) -> bool: ...


class RunStateError(RuntimeError):
    pass


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class FileRunRepository:
    """One run_state.json per run folder. Thread-safe within a process (one lock per run id)."""

    def __init__(self, runs_dir: Path | str, *, vehicle_label: Callable[[str], str] | None = None):
        self.runs_dir = Path(runs_dir)
        self.vehicle_label = vehicle_label
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._legacy_cache: dict[str, tuple[tuple, RunRecord]] = {}

    # -- paths / locks --------------------------------------------------------------------------------
    def path(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
            raise RunStateError(f"invalid run id {run_id!r}")
        return self.runs_dir / run_id / STATE_FILE

    def _lock(self, run_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(run_id, threading.Lock())

    # -- CRUD -----------------------------------------------------------------------------------------
    def exists(self, run_id: str) -> bool:
        return self.path(run_id).is_file()

    def create(self, record: RunRecord) -> RunRecord:
        path = self.path(record.run_id)
        with self._lock(record.run_id):
            if path.exists():
                raise RunStateError(f"run {record.run_id} already exists")
            now = utc_now()
            record.created_at = record.created_at or now
            record.updated_at = now
            atomic_write_json(path, record.to_dict(), durable=True)
        return record

    def get(self, run_id: str) -> RunRecord | None:
        data = _read_json(self.path(run_id))
        if data is not None:
            try:
                return RunRecord.from_dict(data)
            except (TypeError, ValueError):
                return None
        if (self.runs_dir / run_id / "batch.json").is_file():
            return self._legacy(run_id)
        return None

    def update(self, run_id: str, mutate: Callable[[RunRecord], None], *, durable: bool = True) -> RunRecord:
        """Read-modify-write under the run's lock; atomic on disk. `durable=False` for frequent heartbeats."""
        path = self.path(run_id)
        with self._lock(run_id):
            data = _read_json(path)
            if data is None:
                raise RunStateError(f"no run state for {run_id}")
            record = RunRecord.from_dict(data)
            mutate(record)
            record.updated_at = utc_now()
            atomic_write_json(path, record.to_dict(), durable=durable)
        return record

    def list_runs(self, limit: int | None = None) -> list[RunRecord]:
        """Newest first: managed runs (run_state.json) and legacy run folders (batch.json only)."""
        if not self.runs_dir.is_dir():
            return []
        out: list[RunRecord] = []
        for child in sorted(self.runs_dir.iterdir(), key=lambda p: p.name, reverse=True):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            record = self.get(child.name) if ((child / STATE_FILE).is_file() or (child / "batch.json").is_file()) \
                else None
            if record is not None:
                out.append(record)
        out.sort(key=lambda r: (r.created_at or "", r.run_id), reverse=True)
        return out[:limit] if limit else out

    # -- legacy run folders (read-only derivation) -------------------------------------------------------
    def _legacy(self, run_id: str) -> RunRecord | None:
        folder = self.runs_dir / run_id
        stamp = []
        for child in sorted(folder.glob("*/result.json")) + sorted(folder.glob("*/events.jsonl")) + [
                folder / "batch.json"]:
            try:
                st = child.stat()
                stamp.append((str(child), st.st_mtime_ns, st.st_size))
            except OSError:
                continue
        key = tuple(stamp)
        hit = self._legacy_cache.get(run_id)
        if hit and hit[0] == key:
            return hit[1]
        record = derive_legacy_record(folder, vehicle_label=self.vehicle_label)
        if record is not None:
            self._legacy_cache[run_id] = (key, record)
        return record


def _last_event(path: Path) -> dict | None:
    """The last complete line of events.jsonl without reading the whole file."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            lines = fh.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def derive_legacy_record(folder: Path, *, vehicle_label: Callable[[str], str] | None = None) -> RunRecord | None:
    """A read-only RunRecord for a run folder that has batch.json but no run_state.json."""
    info = _read_json(folder / "batch.json")
    if info is None:
        return None
    run_id = folder.name
    record_ids = [str(r) for r in info.get("record_ids") or []]
    for child in sorted(folder.iterdir()) if folder.is_dir() else []:
        if child.is_dir() and child.name not in record_ids and (
                (child / "result.json").is_file() or (child / "events.jsonl").is_file()):
            record_ids.append(child.name)
    vehicles: dict[str, dict] = {}
    statuses: list[str] = []
    started, finished = [], []
    for rid in record_ids:
        result = _read_json(folder / rid / "result.json")
        engine_status = (result or {}).get("status")
        if engine_status is None or engine_status == "finalization_pending":
            last = _last_event(folder / rid / "events.jsonl")
            if last and last.get("kind") in ("run_finished", "recovery_finished"):
                engine_status = last.get("status") or engine_status
        # No liveness is guessed for folders without run_state.json: a run that never logged run_finished is
        # INTERRUPTED (a CLI run still in progress shows that way until it finishes).
        status = status_for_engine_result(engine_status)
        vehicles[rid] = {"status": status, "engine_status": engine_status or "incomplete",
                         "label": vehicle_label(rid) if vehicle_label else rid}
        statuses.append(status)
        if result:
            if result.get("started_at"):
                started.append(result["started_at"])
            if result.get("finished_at"):
                finished.append(result["finished_at"])
    status = aggregate_status(statuses) if statuses else INTERRUPTED
    labels = [vehicles[r]["label"] for r in record_ids]
    label = labels[0] if len(labels) == 1 else f"{info.get('selection') or 'Batch'} · {len(labels)} vehicles"
    return RunRecord(
        run_id=run_id, status=status, created_at=info.get("created_at") or (min(started) if started else None),
        started_at=min(started) if started else info.get("created_at"),
        finished_at=max(finished) if finished else None,
        target={"label": label, "scope": info.get("selection"), "record_ids": record_ids,
                "vehicles": [{"record_id": r, "label": vehicles[r]["label"]} for r in record_ids]},
        request={"research_model": info.get("research_model") or info.get("model"),
                 "finalizer_model": info.get("finalizer_model"), "search_backend": info.get("search_backend")},
        vehicles=vehicles, legacy=True,
        notes=["Run without run_state.json (CLI or an earlier version): its state is derived from its files."])
