"""Local run logs.

Layout::

    runs/<batch_id>/batch.json                 configuration of the batch
    runs/<batch_id>/<record_id>/input.json     Level 1.5 payload given to GLM
    runs/<batch_id>/<record_id>/events.jsonl   every model turn, tool call, document, evidence
    runs/<batch_id>/<record_id>/result.json    final structured result + metrics
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_batch_id(label: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in label)[:40].strip("-")
    return f"{stamp}-{safe}" if safe else stamp


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), "utf-8")


class RunLog:
    """Append-only event log for one vehicle run."""

    def __init__(self, runs_root: Path | str, batch_id: str, record_id: str,
                 listener: Callable[[str, dict], None] | None = None):
        self.dir = Path(runs_root) / batch_id / str(record_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.listener = listener
        self._seq = 0
        self._lock = threading.Lock()

    def event(self, kind: str, **data: Any) -> dict:
        with self._lock:
            self._seq += 1
            record = {"seq": self._seq, "ts": utc_now(), "kind": kind, **data}
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if self.listener:
            try:
                self.listener(kind, record)
            except Exception:  # a UI callback must never break a run
                pass
        return record

    def write_input(self, payload: dict) -> None:
        _write_json(self.dir / "input.json", payload)

    def write_result(self, result: dict) -> None:
        _write_json(self.dir / "result.json", result)


def write_batch(runs_root: Path | str, batch_id: str, info: dict) -> None:
    _write_json(Path(runs_root) / batch_id / "batch.json", info)


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
    path = Path(runs_root) / batch_id / str(record_id) / "events.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text("utf-8").splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


def load_input(runs_root: Path | str, batch_id: str, record_id: str) -> dict | None:
    path = Path(runs_root) / batch_id / str(record_id) / "input.json"
    return json.loads(path.read_text("utf-8")) if path.is_file() else None
