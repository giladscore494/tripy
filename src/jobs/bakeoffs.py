"""Search bake-off jobs (PR #46, S4): the web UI's "Search bake-off" runs on the server as a background job of the
process-wide RunManager (manager.bakeoffs), like a research run: one at a time per process, cancellable, stopped at the
next record on shutdown, never resumed after a restart (marked INTERRUPTED, as runs and A/B series are).

    <TRIPY_DATA_DIR>/bakeoffs/<id>/status.json     the job: status, config, progress, owner, error
    <TRIPY_DATA_DIR>/bakeoffs/<id>/summary.json    the metrics (src/search_bakeoff.summarize)
    <TRIPY_DATA_DIR>/bakeoffs/<id>/records.jsonl   one row per record x backend
    <TRIPY_DATA_DIR>/bakeoffs/<id>/queries.jsonl   every query with its raw results

Keys are the server's (GLM_API_KEY, SERPER_API_KEY, GEMINI_API_KEY); a backend without its key is refused at start
(never replaced by another one).
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from ..search_bakeoff import BakeoffConfig, new_id, run_bakeoff

log = logging.getLogger("tripy.bakeoffs")

RUNNING, COMPLETED, CANCELLED, FAILED, INTERRUPTED = "RUNNING", "COMPLETED", "CANCELLED", "FAILED", "INTERRUPTED"
SCHEMA = "tripy-bakeoff-job/1"


class BakeoffRejected(RuntimeError):
    pass


def _utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BakeoffJobs:
    def __init__(self, root: Path, *, cache, owner: dict, deps: Callable[[Any], tuple] | None = None,
                 records_loader: Callable[[list[str] | None], list[dict]] | None = None):
        self.root = Path(root)
        self.cache, self.owner = cache, dict(owner)
        self._deps = deps
        self._records_loader = records_loader
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._shutting_down = False

    # -- storage ----------------------------------------------------------------------------------------------------
    def dir(self, bakeoff_id: str) -> Path:
        if not bakeoff_id or "/" in bakeoff_id or "\\" in bakeoff_id or bakeoff_id.startswith("."):
            raise BakeoffRejected(f"invalid bake-off id {bakeoff_id!r}")
        return self.root / bakeoff_id

    def get(self, bakeoff_id: str) -> dict | None:
        try:
            value = json.loads((self.dir(bakeoff_id) / "status.json").read_text("utf-8"))
        except (OSError, ValueError, BakeoffRejected):
            return None
        if not isinstance(value, dict):
            return None
        value["executing"] = self.executing(bakeoff_id)
        return value

    def summary(self, bakeoff_id: str) -> dict | None:
        try:
            value = json.loads((self.dir(bakeoff_id) / "summary.json").read_text("utf-8"))
        except (OSError, ValueError, BakeoffRejected):
            return None
        return value if isinstance(value, dict) else None

    def list(self, limit: int | None = None) -> list[dict]:
        if not self.root.is_dir():
            return []
        out = [s for s in (self.get(p.name) for p in self.root.iterdir() if p.is_dir()) if s]
        out.sort(key=lambda s: (s.get("created_at") or "", s.get("bakeoff_id") or ""), reverse=True)
        return out[:limit] if limit else out

    def _update(self, bakeoff_id: str, mutate: Callable[[dict], None]) -> dict:
        from ..storage.atomic import atomic_write_json

        with self._lock:
            state = self.get(bakeoff_id) or {}
            state.pop("executing", None)
            mutate(state)
            state["updated_at"] = _utc()
            atomic_write_json(self.dir(bakeoff_id) / "status.json", state, durable=True)
        return state

    def executing(self, bakeoff_id: str) -> bool:
        thread = self._threads.get(bakeoff_id)
        return bool(thread and thread.is_alive())

    # -- start / cancel ---------------------------------------------------------------------------------------------
    def start(self, config: BakeoffConfig) -> str:
        if not config.backends:
            raise BakeoffRejected("Select at least one backend.")
        with self._lock:
            if self._shutting_down:
                raise BakeoffRejected("The server is shutting down; try again in a moment.")
            running = [b for b in self._threads if self.executing(b)]
            if running:
                raise BakeoffRejected(f"A search bake-off is already running ({running[0]}).")
            bakeoff_id = new_id()
            self.dir(bakeoff_id).mkdir(parents=True, exist_ok=True)
            self._update(bakeoff_id, lambda s: s.update({
                "schema": SCHEMA, "bakeoff_id": bakeoff_id, "status": RUNNING,
                "label": config.label or f"Search bake-off · {', '.join(config.backends)}", "config": config.as_dict(),
                "created_at": _utc(), "started_at": _utc(), "finished_at": None, "owner": dict(self.owner),
                "progress": {"done": 0, "total": None}, "error": None}))
            cancel = threading.Event()
            self._cancel[bakeoff_id] = cancel
            thread = threading.Thread(target=self._drive, name=f"tripy-bakeoff-{bakeoff_id}", daemon=True,
                                      args=(bakeoff_id, config, cancel))
            self._threads[bakeoff_id] = thread
            thread.start()
        log.info("bake-off %s started: %s", bakeoff_id, ",".join(config.backends))
        return bakeoff_id

    def cancel(self, bakeoff_id: str) -> bool:
        with self._lock:
            event = self._cancel.get(bakeoff_id)
            if event is None or not self.executing(bakeoff_id):
                return False
            event.set()
        return True

    def _drive(self, bakeoff_id: str, config: BakeoffConfig, cancel: threading.Event) -> None:
        from ..search_bakeoff import benchmark_records, live_dependencies

        status, error = COMPLETED, None
        try:
            make_backend, fetch = (self._deps or live_dependencies)(self.cache)
            records = (self._records_loader or benchmark_records)(config.records or None)
            self._update(bakeoff_id, lambda s: s.update({"progress": {"done": 0, "total": len(records)}}))

            def progress(p: dict) -> None:
                self._update(bakeoff_id, lambda s: s.update({"progress": {"done": p["done"], "total": p["total"]}}))
            summary = run_bakeoff(config, self.dir(bakeoff_id), make_backend=make_backend,
                                  fetch=fetch if config.fetch_top else None, cache=self.cache, cancel=cancel,
                                  progress=progress, records=records)
            if summary.get("status") == "cancelled":
                status = CANCELLED
        except Exception as exc:  # noqa: BLE001 - a failed bake-off is recorded, never the server's end
            status, error = FAILED, f"{type(exc).__name__}: {str(exc)[:300]}"
            log.error("bake-off %s failed:\n%s", bakeoff_id, traceback.format_exc())
        try:
            self._update(bakeoff_id, lambda s: s.update({"status": status, "finished_at": _utc(), "error": error}))
        except Exception:  # noqa: BLE001
            log.error("bake-off %s: could not write its final state", bakeoff_id, exc_info=True)

    # -- restart / shutdown -----------------------------------------------------------------------------------------
    def reconcile(self, boot_id: str) -> list[str]:
        """A RUNNING bake-off whose owner process is gone is INTERRUPTED (never resumed)."""
        out = []
        for state in self.list():
            if state.get("status") != RUNNING or state.get("executing"):
                continue
            if (state.get("owner") or {}).get("boot_id") == boot_id:
                continue
            self._update(state["bakeoff_id"], lambda s: s.update({
                "status": INTERRUPTED, "finished_at": _utc(),
                "error": s.get("error") or "The server stopped while this bake-off was running; it is not resumed."}))
            out.append(state["bakeoff_id"])
        return out

    def shutdown(self, grace_s: float = 5.0) -> None:
        with self._lock:
            self._shutting_down = True
            running = [(b, t) for b, t in self._threads.items() if t.is_alive()]
            for b, _ in running:
                self._cancel[b].set()
        for _, thread in running:
            thread.join(timeout=grace_s)
