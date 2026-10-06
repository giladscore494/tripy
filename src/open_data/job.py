"""The open-data snapshot job of the process-wide RunManager (D-1): every dataset rebuilt monthly
(`refresh_interval_days` of data/open_datasets.json) plus "Rebuild now" on the Data page, one build at a time, into
<TRIPY_DATA_DIR>/derived/open/. The same shape as the catalog trim index job (src/jobs/derived_index.py): never resumed
after a restart (a build interrupted by a shutdown runs again when due), a failed or stopped build keeps the previous
snapshot, and its report is the dataset's status.

    <dir>/<dataset>.sqlite          the snapshot (src/open_data/datasets.py)
    <dir>/open_data.status.json     per dataset: status, built_at, rows, report / error, reason
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import datasets as ds

log = logging.getLogger("tripy.open_data")

STATUS_NAME = "open_data.status.json"
CHECK_S = 6 * 3600.0
FIRST_CHECK_DELAY_S = 300.0          # the first scheduled build waits for the server to settle


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OpenDataJob:
    def __init__(self, folder: Path, *, fetcher=None, check_s: float = CHECK_S,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.folder = Path(folder)
        self._fetcher = fetcher
        self.check_s, self._now = float(check_s), now
        self._lock = threading.Lock()
        self._building: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def interval(self) -> timedelta:
        return timedelta(days=float(ds.config().get("refresh_interval_days") or 30))

    def _read(self) -> dict:
        try:
            data = json.loads((self.folder / STATUS_NAME).read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def status(self) -> dict:
        """Per dataset: its config label / source URL / licence, the snapshot's meta (built_at, rows) and the last
        build's status; plus whether a build is running."""
        from ..source_authority import dataset_policy

        state = self._read()
        rows = []
        for name, cfg in ds.datasets().items():
            meta = ds.snapshot_meta(name, self.folder)
            policy = dataset_policy(cfg.get("policy_id") or name)
            rows.append({"dataset": name, "label": cfg.get("label"), "source_url": cfg.get("source_url"),
                         "market": cfg.get("market"), "routes": cfg.get("routes"),
                         "identity_only": bool(cfg.get("identity_only")), "policy": policy.get("policy"),
                         "licence": policy.get("licence"), "attribution": policy.get("attribution"),
                         "schema_verified": cfg.get("schema_verified"), "snapshot_built_at": meta.get("built_at"),
                         "snapshot_rows": meta.get("rows"), "last_build": state.get(name)})
        return {"folder": str(self.folder), "building": self._building,
                "interval_days": self.interval.days, "datasets": rows}

    def due(self, dataset: str) -> bool:
        cfg = ds.datasets().get(dataset) or {}
        if cfg.get("identity_only"):
            return False
        built = ds.snapshot_meta(dataset, self.folder).get("built_at")
        last = (self._read().get(dataset) or {}).get("attempted_at")
        stamp = built or last
        if not stamp:
            return True
        try:
            return self._now() - datetime.fromisoformat(stamp) >= self.interval
        except ValueError:
            return True

    def build(self, dataset: str, *, reason: str = "manual") -> dict:
        """Build one dataset now (blocking); never raises."""
        from .build import build_dataset

        with self._lock:
            if self._building:
                return {"status": "already_running", "building": self._building}
            self._building = dataset
        try:
            ds.set_snapshot_dir(self.folder)
            result = build_dataset(dataset, self._fetcher)
        finally:
            with self._lock:
                self._building = None
        state = self._read()
        state[dataset] = {**result, "reason": reason, "attempted_at": _utc()}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            from ..storage.atomic import atomic_write_json

            atomic_write_json(self.folder / STATUS_NAME, state, durable=True)
        except OSError:
            log.error("open data: could not write its status", exc_info=True)
        if result.get("status") != "built":
            log.warning("open data build %s: %s", dataset, result.get("reason") or result.get("error"))
        return result

    def build_all(self, *, reason: str = "manual", only_due: bool = False) -> dict:
        return {name: self.build(name, reason=reason) for name in ds.datasets()
                if not (ds.datasets()[name].get("identity_only")) and (not only_due or self.due(name))}

    def rebuild_async(self, dataset: str | None = None) -> bool:
        """'Rebuild now' (one dataset, or all): a daemon thread; False when a build is running."""
        if self._building:
            return False
        target = (lambda: self.build(dataset, reason="manual")) if dataset else (lambda: self.build_all())
        threading.Thread(target=target, name="tripy-open-data", daemon=True).start()
        return True

    def start(self) -> None:
        """The monthly schedule (checked every check_s, first after FIRST_CHECK_DELAY_S)."""
        if self._thread is not None:
            return

        def loop() -> None:
            if self._stop.wait(FIRST_CHECK_DELAY_S):
                return
            while not self._stop.is_set():
                try:
                    self.build_all(reason="monthly", only_due=True)
                except Exception:  # noqa: BLE001
                    log.error("open data schedule failed", exc_info=True)
                self._stop.wait(self.check_s)
        self._thread = threading.Thread(target=loop, name="tripy-open-data-schedule", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
