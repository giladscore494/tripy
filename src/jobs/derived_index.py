"""The weekly catalog trim index rebuild inside the server (PR #47, B2): a background job of the process-wide
RunManager that rebuilds data/catalog_trim_index.json from the live MILO catalog (DATABASE_URL, read-only, the same
query scripts/build_trim_index.py runs) into the data volume, so the engine's catalog rules stay current without a
GitHub secret or a pull request.

    <TRIPY_DATA_DIR>/derived/catalog_trim_index.json        the rebuilt index (written atomically)
    <TRIPY_DATA_DIR>/derived/catalog_trim_index.status.json last build: status, built_at, rows, entries, error

The engine reads the derived copy when it is newer than the repository's (src/document_binding.trim_index_path). A
build runs when the last one is older than INTERVAL_S (checked every CHECK_S), or on "Rebuild now" (the UI). One build
at a time; without DATABASE_URL there is no job (the repository's index stays in use). Never resumed after a restart:
a build interrupted by a shutdown simply runs again when due. A failed build keeps the previous file.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("tripy.derived_index")

INDEX_NAME = "catalog_trim_index.json"
STATUS_NAME = "catalog_trim_index.status.json"
INTERVAL_S = 7 * 24 * 3600.0
CHECK_S = 3600.0


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DerivedIndexJob:
    def __init__(self, folder: Path, *, rows: Callable[[], list[dict]] | None, interval_s: float = INTERVAL_S,
                 check_s: float = CHECK_S, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.folder = Path(folder)
        self._rows = rows
        self.interval_s, self.check_s, self._now = float(interval_s), float(check_s), now
        self._lock = threading.Lock()
        self._building = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return self._rows is not None

    @property
    def index_path(self) -> Path:
        return self.folder / INDEX_NAME

    def status(self) -> dict:
        try:
            state = json.loads((self.folder / STATUS_NAME).read_text("utf-8"))
        except (OSError, ValueError):
            state = {}
        return {"available": self.available, "building": self._building, "path": str(self.index_path),
                "interval_days": round(self.interval_s / 86400, 2), **(state if isinstance(state, dict) else {})}

    def due(self) -> bool:
        built = self.status().get("built_at")
        if not built:
            return True
        try:
            last = datetime.fromisoformat(built)
        except ValueError:
            return True
        return self._now() - last >= timedelta(seconds=self.interval_s)

    def build(self, *, reason: str = "manual") -> dict:
        """Rebuild now (blocking). {status, ...}; never raises."""
        from ..catalog_trim_index import build_index, dumps
        from ..storage.atomic import atomic_write_json, atomic_write_text

        if not self.available:
            return {"status": "unavailable", "error": "DATABASE_URL is not set"}
        from ..storage.disk import InsufficientDisk, check_free

        try:
            check_free(self.folder, "catalog trim index build")
        except InsufficientDisk as exc:
            return {"status": "insufficient_disk", "error": str(exc), "reason": reason}
        with self._lock:
            if self._building:
                return {"status": "already_running"}
            self._building = True
        started = time.monotonic()
        try:
            rows = self._rows()
            index = build_index(rows, source=f"database: public.catalog_variants_current (derived job, {reason})")
            self.folder.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.index_path, dumps(index))
            state = {"status": "built", "built_at": index["generated_at"], "rows": index["rows"],
                     "entries": len(index["entries"]), "reason": reason,
                     "duration_s": round(time.monotonic() - started, 1), "error": None}
        except Exception as exc:  # noqa: BLE001 - a failed build keeps the previous file
            previous = self.status()
            state = {**{k: previous.get(k) for k in ("built_at", "rows", "entries")}, "status": "failed",
                     "failed_at": _utc(), "reason": reason, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            log.error("derived catalog index build failed: %s", state["error"])
        finally:
            with self._lock:
                self._building = False
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.folder / STATUS_NAME, state, durable=True)
        except OSError:
            log.error("derived catalog index: could not write its status", exc_info=True)
        return state

    def rebuild_async(self) -> bool:
        """'Rebuild now': start a build on a daemon thread; False when one is running or there is no database."""
        if not self.available or self._building:
            return False
        threading.Thread(target=self.build, kwargs={"reason": "manual"}, name="tripy-derived-index",
                         daemon=True).start()
        return True

    def start(self) -> None:
        """The weekly schedule: a daemon thread that builds when due (checked every check_s)."""
        if not self.available or self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    if self.due():
                        self.build(reason="weekly")
                except Exception:  # noqa: BLE001
                    log.error("derived catalog index schedule failed", exc_info=True)
                self._stop.wait(self.check_s)
        self._thread = threading.Thread(target=loop, name="tripy-derived-index-schedule", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
