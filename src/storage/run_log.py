"""Local run logs.

Layout::

    runs/<batch_id>/batch.json                 configuration of the batch
    runs/<batch_id>/<record_id>/input.json     Level 1.5 payload given to GLM
    runs/<batch_id>/<record_id>/events.jsonl   every model turn, tool call, document, evidence
    runs/<batch_id>/<record_id>/result.json    final (or partial) result + metrics; written on every exit path
    runs/<batch_id>/<record_id>/finalizer_request.json   exact compact messages sent to the finalizer
    runs/<batch_id>/<record_id>/recovery/<stamp>/        artifacts of a later --finalize-existing run

events.jsonl is append-only. A RunLog opened on an existing folder continues the
sequence numbers instead of rewriting anything.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .atomic import atomic_write_json


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_batch_id(label: str = "", runs_root: Path | str | None = None) -> str:
    """Timestamped batch id. With `runs_root`, a suffix (-2, -3, ...) keeps it unique there, so two runs
    started within the same second never share (and overwrite) a batch folder."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in label)[:40].strip("-")
    base = f"{stamp}-{safe}" if safe else stamp
    if runs_root is None:
        return base
    candidate, n = base, 1
    while (Path(runs_root) / candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _write_json(path: Path, value: Any) -> None:
    """Atomic and durable (temp file in the same folder, fsync, os.replace): a crash mid-write leaves the
    previous file or the new one, never a truncated result.json / batch.json / input.json."""
    atomic_write_json(path, value, durable=True)


class RunLog:
    """Append-only event log for one vehicle run."""

    def __init__(self, runs_root: Path | str, batch_id: str, record_id: str,
                 listener: Callable[[str, dict], None] | None = None):
        self.dir = Path(runs_root) / batch_id / str(record_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.listener = listener
        # Set once a run is interrupted (e.g. a UI stop): events are still written, but no further
        # UI callback runs while the partial result is persisted. Nothing is swallowed.
        self.listener_muted = False
        self._seq = last_seq(self.events_path)
        self._lock = threading.Lock()

    def event(self, kind: str, **data: Any) -> dict:
        with self._lock:
            self._seq += 1
            record = {"seq": self._seq, "ts": utc_now(), "kind": kind, **data}
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if self.listener and not self.listener_muted:
            try:
                self.listener(kind, record)
            except Exception:  # a UI callback must never break a run (control-flow BaseExceptions propagate)
                pass
        return record

    @property
    def seq(self) -> int:
        """Sequence number of the last event written by this log."""
        with self._lock:
            return self._seq

    def write_input(self, payload: dict) -> None:
        _write_json(self.dir / "input.json", payload)

    def write_result(self, result: dict) -> None:
        """Atomic + durable. Raises on failure (the destination keeps its previous content)."""
        _write_json(self.dir / "result.json", result)


def read_events(path: Path | str) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    events = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue  # a line cut off by a hard kill is skipped, not fatal
    return events


def last_seq(path: Path | str) -> int:
    seq = 0
    for event in read_events(path):
        if isinstance(event.get("seq"), int):
            seq = max(seq, event["seq"])
    return seq


def load_batch(runs_root: Path | str, batch_id: str) -> dict | None:
    path = Path(runs_root) / batch_id / "batch.json"
    if not path.is_file():
        return None
    try:
        info = json.loads(path.read_text("utf-8"))
    except ValueError:
        return None
    info.setdefault("batch_id", batch_id)
    return info


def write_batch(runs_root: Path | str, batch_id: str, info: dict) -> None:
    path = Path(runs_root) / batch_id / "batch.json"
    if path.exists():
        raise FileExistsError(f"{path} already exists; refusing to overwrite an existing batch")
    _write_json(path, info)


_BATCH_LOCK = threading.Lock()


def update_batch(runs_root: Path | str, batch_id: str, updates: dict) -> dict:
    """Merge keys into an existing batch.json (e.g. end-of-batch observability), atomically."""
    path = Path(runs_root) / batch_id / "batch.json"
    with _BATCH_LOCK:
        info = json.loads(path.read_text("utf-8")) if path.is_file() else {"batch_id": batch_id}
        info.update(updates)
        _write_json(path, info)
    return info


def list_batches(runs_root: Path | str) -> list[dict]:
    root = Path(runs_root)
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob("*/batch.json"), reverse=True):
        try:
            info = json.loads(path.read_text("utf-8"))
        except ValueError:
            continue
        info.setdefault("batch_id", path.parent.name)
        out.append(info)
    return out


def load_results(runs_root: Path | str, batch_id: str) -> list[dict]:
    """Only runs that wrote result.json. The UI uses run_loader.load_runs, which also shows incomplete runs."""
    folder = Path(runs_root) / batch_id
    results = []
    for path in folder.glob("*/result.json"):
        try:
            results.append(json.loads(path.read_text("utf-8")))
        except ValueError:
            continue
    results.sort(key=lambda r: r.get("ordinal") or 0)
    return results


def load_events(runs_root: Path | str, batch_id: str, record_id: str) -> list[dict]:
    return read_events(Path(runs_root) / batch_id / str(record_id) / "events.jsonl")


def load_input(runs_root: Path | str, batch_id: str, record_id: str) -> dict | None:
    path = Path(runs_root) / batch_id / str(record_id) / "input.json"
    return json.loads(path.read_text("utf-8")) if path.is_file() else None
