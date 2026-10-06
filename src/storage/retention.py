"""Data-volume retention: run sizes, run deletion, run compaction, the document-cache cleanup and the logs cap.

The volume (<TRIPY_DATA_DIR>) holds the runs, the shared document cache and the logs; below 200 MB free every new run is
refused (storage/disk.check_free). Nothing here runs on its own: every action is the operator's (the Data page's
Storage section, the run view), never touches an executing run, and is logged (server log + derived/retention.jsonl).

Settings live in the volume (<data>/derived/retention.json; no environment edit):

    keep_newest   the newest N runs are kept whole (default 15)
    pinned        runs marked "keep" in the run view: never compacted, never deleted by the age rule

Compaction of an older run (neither among the newest N, nor pinned, nor executing / active) deletes only what no reader
of a finished run needs, and keeps everything the run view, the diagnostics and binding replay read:

    kept, run level       run_state.json, batch.json, diagnostics/ (benchmark.json, per_vehicle.*, parser_gaps.jsonl,
                          binding_replay_items.jsonl), compacted.json (written by the compaction)
    kept, per vehicle     result.json, events.jsonl, input.json, diagnostics.json, diagnostics.jsonl,
                          binding_replay.jsonl, binding_replay_summary.json
    deleted, per vehicle  documents/ (the per-vehicle copies of the shared cache's documents: binding replay and the run
                          view read the shared cache first), finalizer_request.json and recovery/ (the finalizer's
                          request transcripts and research bundles: audit only, no reader)
    deleted, anywhere     any other file over 1 MB (e.g. result.pre-recovery-<stamp>.json); smaller files stay

A run's raw model / tool transcripts are events inside events.jsonl (kept: every view reads it); there is no separate
transcript file to delete.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .atomic import atomic_write_json

log = logging.getLogger("tripy.storage")

SETTINGS_NAME = "retention.json"
LOG_NAME = "retention.jsonl"
COMPACTED_NAME = "compacted.json"
DEFAULT_KEEP_NEWEST = 15
LARGE_FILE_BYTES = 1024 * 1024
LOGS_CAP_BYTES = 20 * 1024 * 1024
VEHICLE_MARKERS = ("events.jsonl", "input.json", "result.json")
KEEP_RUN_FILES = {"run_state.json", "batch.json", COMPACTED_NAME}
KEEP_RUN_DIRS = {"diagnostics"}
KEEP_VEHICLE_FILES = {"result.json", "events.jsonl", "input.json", "diagnostics.json", "diagnostics.jsonl",
                      "binding_replay.jsonl", "binding_replay_summary.json"}
DELETE_VEHICLE_DIRS = {"documents", "recovery"}
DELETE_VEHICLE_FILES = {"finalizer_request.json"}
DOCUMENT_ID = re.compile(r"\bd_[0-9a-f]{16}\b")
SIZE_TTL_S = 60.0


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(ts).replace("Z", "+00:00")) if ts else None
    except ValueError:
        return None
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def tree_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


_SIZES: dict[str, tuple[float, int]] = {}
_SIZES_LOCK = threading.Lock()


def run_size(run_dir: Path, *, fresh: bool = False) -> int:
    """Bytes on disk of a run folder (cached for SIZE_TTL_S: the runs list polls)."""
    key = str(run_dir)
    now = time.monotonic()
    with _SIZES_LOCK:
        hit = _SIZES.get(key)
    if hit and not fresh and now - hit[0] < SIZE_TTL_S:
        return hit[1]
    size = tree_size(run_dir) if run_dir.exists() else 0
    with _SIZES_LOCK:
        _SIZES[key] = (now, size)
    return size


def _forget_size(run_dir: Path) -> None:
    with _SIZES_LOCK:
        _SIZES.pop(str(run_dir), None)


class RetentionError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


class Retention:
    """Retention over one data root. `manager` is the RunManager (its run list and what is executing / active)."""

    def __init__(self, manager, data_dir: Path | str, runs_dir: Path | str, cache_dir: Path | str):
        self.manager = manager
        self.data_dir, self.runs_dir, self.cache_dir = Path(data_dir), Path(runs_dir), Path(cache_dir)
        self.folder = self.data_dir / "derived"
        self._lock = threading.Lock()

    # -- settings ---------------------------------------------------------------------------------------------------
    def settings(self) -> dict:
        try:
            data = json.loads((self.folder / SETTINGS_NAME).read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        try:
            keep = max(0, int(data.get("keep_newest", DEFAULT_KEEP_NEWEST)))
        except (TypeError, ValueError):
            keep = DEFAULT_KEEP_NEWEST
        pinned = sorted({str(p) for p in data.get("pinned") or [] if isinstance(p, str)})
        return {"keep_newest": keep, "pinned": pinned, "updated_at": data.get("updated_at")}

    def _save(self, settings: dict) -> dict:
        settings = {**settings, "updated_at": _utc()}
        self.folder.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.folder / SETTINGS_NAME, settings, durable=True)
        return settings

    def set_keep_newest(self, n: int, *, by: str = "operator") -> dict:
        if not 0 <= int(n) <= 10_000:
            raise RetentionError("invalid_setting", "keep_newest must be between 0 and 10000.", 422)
        settings = self._save({**self.settings(), "keep_newest": int(n)})
        self._log("settings", by=by, keep_newest=int(n))
        return settings

    def set_pinned(self, run_id: str, keep: bool, *, by: str = "operator") -> dict:
        self._run_dir(run_id)
        pinned = set(self.settings()["pinned"])
        (pinned.add if keep else pinned.discard)(run_id)
        settings = self._save({**self.settings(), "pinned": sorted(pinned)})
        self._log("pin" if keep else "unpin", run_id=run_id, by=by)
        return settings

    # -- the log ----------------------------------------------------------------------------------------------------
    def _log(self, action: str, **fields: Any) -> dict:
        entry = {"at": _utc(), "action": action, **fields}
        log.warning("retention %s: %s", action, json.dumps(fields, ensure_ascii=False, default=str)[:500])
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with (self.folder / LOG_NAME).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.error("retention: could not append to its log", exc_info=True)
        return entry

    def history(self, limit: int = 50) -> list[dict]:
        try:
            lines = (self.folder / LOG_NAME).read_text("utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # -- runs -------------------------------------------------------------------------------------------------------
    def _run_dir(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith((".", "_")) or run_id in ("..",):
            raise RetentionError("invalid_run_id", f"invalid run id {run_id!r}", 404)
        path = self.runs_dir / run_id
        if path.resolve().parent != self.runs_dir.resolve():
            raise RetentionError("invalid_run_id", f"invalid run id {run_id!r}", 404)
        return path

    def busy_ids(self) -> set[str]:
        """Runs that must not be touched: executing in this process, or active elsewhere (fresh heartbeat)."""
        busy = {r.run_id for r in self.manager.active_runs()}
        busy |= {r.run_id for r in self.manager.list_runs() if self.manager.is_executing(r.run_id)}
        return busy

    def kept_ids(self, records: list | None = None) -> set[str]:
        records = records if records is not None else self.manager.list_runs()
        settings = self.settings()
        return {r.run_id for r in records[:settings["keep_newest"]]} | set(settings["pinned"])

    def delete_run(self, run_id: str, *, by: str = "operator") -> dict:
        path = self._run_dir(run_id)
        with self._lock:
            if not path.is_dir():
                raise RetentionError("unknown_run", f"no run {run_id!r}", 404)
            if run_id in self.busy_ids():
                raise RetentionError("run_executing", "The run is executing; stop it (or let it finish) first.")
            size = tree_size(path)
            shutil.rmtree(path)
            _forget_size(path)
            if run_id in self.settings()["pinned"]:
                self._save({**self.settings(), "pinned": [p for p in self.settings()["pinned"] if p != run_id]})
        self._log("delete_run", run_id=run_id, bytes=size, by=by)
        return {"run_id": run_id, "deleted": True, "bytes_freed": size}

    # -- compaction -------------------------------------------------------------------------------------------------
    @staticmethod
    def _vehicle_dirs(run_dir: Path) -> list[Path]:
        return [p for p in sorted(run_dir.iterdir()) if p.is_dir() and not p.name.startswith(("_", "."))
                and any((p / m).exists() for m in VEHICLE_MARKERS)]

    @staticmethod
    def removable(run_dir: Path) -> list[Path]:
        """The paths compaction deletes in a run folder (module docstring)."""
        out: list[Path] = []
        vehicles = Retention._vehicle_dirs(run_dir)
        for child in sorted(run_dir.iterdir()):
            if child in vehicles:
                continue
            if child.is_dir():
                if child.name in KEEP_RUN_DIRS:
                    continue
                out += [f for f in sorted(child.rglob("*")) if f.is_file() and f.stat().st_size > LARGE_FILE_BYTES]
            elif child.name not in KEEP_RUN_FILES and child.stat().st_size > LARGE_FILE_BYTES:
                out.append(child)
        for vehicle in vehicles:
            for child in sorted(vehicle.iterdir()):
                if child.is_dir():
                    if child.name in DELETE_VEHICLE_DIRS:
                        out.append(child)
                    else:
                        out += [f for f in sorted(child.rglob("*")) if f.is_file()
                                and f.stat().st_size > LARGE_FILE_BYTES]
                elif child.name in KEEP_VEHICLE_FILES:
                    continue
                elif child.name in DELETE_VEHICLE_FILES or child.stat().st_size > LARGE_FILE_BYTES:
                    out.append(child)
        return out

    def compaction_plan(self) -> dict:
        """The runs compaction would touch now and the bytes it would free (nothing is deleted)."""
        records = self.manager.list_runs()
        kept, busy = self.kept_ids(records), self.busy_ids()
        runs, total = [], 0
        for record in records:
            if record.run_id in kept or record.run_id in busy or record.active:
                continue
            path = self.runs_dir / record.run_id
            if not path.is_dir():
                continue
            paths = self.removable(path)
            freed = sum(tree_size(p) for p in paths)
            if freed:
                runs.append({"run_id": record.run_id, "bytes": freed, "paths": len(paths),
                             "compacted_before": (path / COMPACTED_NAME).exists()})
                total += freed
        return {"runs": runs, "bytes": total, "kept": sorted(kept), "busy": sorted(busy)}

    def compact_run(self, run_id: str, *, by: str = "operator", force: bool = False) -> dict:
        path = self._run_dir(run_id)
        if not path.is_dir():
            raise RetentionError("unknown_run", f"no run {run_id!r}", 404)
        if run_id in self.busy_ids():
            raise RetentionError("run_executing", "The run is executing.")
        if not force and run_id in self.settings()["pinned"]:
            raise RetentionError("run_pinned", "The run is marked keep; it is never compacted.")
        removed, freed = [], 0
        for item in self.removable(path):
            size = tree_size(item)
            try:
                shutil.rmtree(item) if item.is_dir() else item.unlink()
            except OSError:
                log.warning("retention: could not delete %s", item, exc_info=True)
                continue
            removed.append(str(item.relative_to(path)))
            freed += size
        previous = {}
        try:
            previous = json.loads((path / COMPACTED_NAME).read_text("utf-8"))
        except (OSError, ValueError):
            pass
        atomic_write_json(path / COMPACTED_NAME, {
            "compacted_at": _utc(), "bytes_freed": freed + int(previous.get("bytes_freed") or 0),
            "removed": (list(previous.get("removed") or []) + removed)[:500], "by": by}, durable=True)
        _forget_size(path)
        self._log("compact_run", run_id=run_id, bytes=freed, removed=len(removed), by=by)
        return {"run_id": run_id, "bytes_freed": freed, "removed": removed}

    def compact_old(self, *, by: str = "operator") -> dict:
        with self._lock:
            plan = self.compaction_plan()
            done = [self.compact_run(r["run_id"], by=by) for r in plan["runs"]]
        return {"runs": [{"run_id": d["run_id"], "bytes_freed": d["bytes_freed"]} for d in done],
                "bytes_freed": sum(d["bytes_freed"] for d in done)}

    # -- deletion by age --------------------------------------------------------------------------------------------
    def older_than_plan(self, days: int) -> dict:
        """Runs created more than `days` ago, except the newest N, pinned and busy ones."""
        if int(days) < 1:
            raise RetentionError("invalid_setting", "days must be at least 1.", 422)
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        records = self.manager.list_runs()
        kept, busy = self.kept_ids(records), self.busy_ids()
        runs, total = [], 0
        for record in records:
            created = _parse(record.created_at or record.started_at)
            if created is None or created >= cutoff or record.run_id in kept or record.run_id in busy \
                    or record.active:
                continue
            size = run_size(self.runs_dir / record.run_id, fresh=True)
            runs.append({"run_id": record.run_id, "created_at": record.created_at, "bytes": size})
            total += size
        return {"days": int(days), "runs": runs, "bytes": total}

    def delete_older_than(self, days: int, *, by: str = "operator") -> dict:
        plan = self.older_than_plan(days)
        deleted = []
        for run in plan["runs"]:
            try:
                deleted.append(self.delete_run(run["run_id"], by=by))
            except RetentionError as exc:
                log.warning("retention: %s not deleted: %s", run["run_id"], exc)
        return {"days": plan["days"], "runs": [d["run_id"] for d in deleted],
                "bytes_freed": sum(d["bytes_freed"] for d in deleted)}

    # -- the document cache -----------------------------------------------------------------------------------------
    def _referenced_documents(self, run_ids: Iterable[str]) -> set[str]:
        """Document ids a run's events / result / document copies name, plus every id the research memory names
        (verified fact reuse re-admits a remembered fact against its cached document)."""
        ids: set[str] = set()
        for run_id in run_ids:
            path = self.runs_dir / run_id
            if not path.is_dir():
                continue
            for vehicle in self._vehicle_dirs(path):
                for name in ("events.jsonl", "result.json"):
                    try:
                        with (vehicle / name).open("r", encoding="utf-8", errors="replace") as handle:
                            for line in handle:
                                ids.update(DOCUMENT_ID.findall(line))
                    except OSError:
                        continue
        memory = self.cache_dir / "memory"
        if memory.is_dir():
            for item in memory.rglob("*"):
                if item.is_file():
                    try:
                        ids.update(DOCUMENT_ID.findall(item.read_text("utf-8", errors="replace")))
                    except OSError:
                        continue
        return ids

    def cache_plan(self) -> dict:
        """The shared cache's documents no kept (newest N / pinned) or busy run uses, and their bytes."""
        docs = self.cache_dir / "documents"
        records = self.manager.list_runs()
        keep_runs = self.kept_ids(records) | self.busy_ids() | {r.run_id for r in records if r.active}
        used = self._referenced_documents(keep_runs)
        unused, total, size_all = [], 0, 0
        if docs.is_dir():
            for folder in sorted(docs.iterdir()):
                if not folder.is_dir():
                    continue
                size = tree_size(folder)
                size_all += size
                if folder.name not in used:
                    unused.append(folder.name)
                    total += size
        return {"documents": len(unused), "bytes": total, "cache_bytes": tree_size(self.cache_dir)
                if self.cache_dir.exists() else 0, "documents_bytes": size_all, "used_by_kept_runs": len(used),
                "ids": unused}

    def clean_cache(self, *, by: str = "operator") -> dict:
        with self._lock:
            plan = self.cache_plan()
            docs, freed, removed = self.cache_dir / "documents", 0, 0
            for doc_id in plan["ids"]:
                folder = docs / doc_id
                size = tree_size(folder)
                try:
                    shutil.rmtree(folder)
                except OSError:
                    log.warning("retention: could not delete cached document %s", doc_id, exc_info=True)
                    continue
                freed += size
                removed += 1
        self._log("clean_cache", documents=removed, bytes=freed, by=by)
        return {"documents": removed, "bytes_freed": freed}

    # -- the Storage section ----------------------------------------------------------------------------------------
    def overview(self) -> dict:
        compaction = self.compaction_plan()
        cache = self.cache_plan()
        cache.pop("ids", None)
        logs = self.data_dir / "logs"
        return {"settings": self.settings(), "compaction": compaction, "cache": cache,
                "logs": {"bytes": tree_size(logs) if logs.exists() else 0, "cap_bytes": LOGS_CAP_BYTES},
                "history": self.history(20)}


def cap_logs(folder: Path | str, cap: int = LOGS_CAP_BYTES, *, active: str = "tripy.log") -> dict:
    """Keep the logs folder under `cap` bytes: the oldest files go first (the active log file is never deleted)."""
    folder = Path(folder)
    if not folder.is_dir():
        return {"removed": [], "bytes_freed": 0, "bytes": 0}
    files = [p for p in folder.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    removed, freed = [], 0
    for path in sorted((p for p in files if p.name != active), key=lambda p: p.stat().st_mtime):
        if total <= cap:
            break
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path.name)
        total -= size
        freed += size
    if removed:
        log.warning("logs capped at %.0f MB: %d file(s) removed, %.1f MB freed", cap / 1024 / 1024, len(removed),
                    freed / 1024 / 1024)
    return {"removed": removed, "bytes_freed": freed, "bytes": total}
