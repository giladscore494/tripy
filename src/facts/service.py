"""The facts service: the government read, the open-data cache and the call log.

    government   one short read-only query by variant_identity_key on EVERY call (db.LEVEL15_BY_KEY_SQL, the private
                 segment): MILO updates appear without a sync. No DATABASE_URL, or the query fails ->
                 CatalogUnavailable (503 catalog_unavailable); never the 50-record benchmark snapshot, which is not the
                 catalogue
    open data    record.open_data_part, cached by (variant_identity_key, the row's content_sha256, the snapshot
                 manifest sha, the matcher version, the admission version, the zero-semantics version, the gov
                 manifest sha and the recall model map sha): an in-process
                 LRU (LRU_SIZE entries) in front of <data>/derived/facts_cache/<digest>.json on the volume (written only
                 when storage.disk.check_free allows it; a refused write costs only the next cold call)
    snapshots    the shards stay decompressed in the temp dir (open_data.datasets); only the year-window shards are
                 opened; a decompression the free-space guard refuses -> SnapshotsUnavailable (503
                 snapshots_unavailable), never a smaller answer, and nothing is cached
    gov data     src/gov_data/facts.gov_part: the government-dataset fields (data/gov/ snapshots), computed with the
                 open part and cached with it (its key carries the gov manifest sha)
    log          one line per call: time, keys, status per key, latency, cache hit per key, facts count, withheld
                 count by reason. Never a value.

No vPIC, no web, no model, no write to MILO.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..catalog import CatalogUnavailable
from ..gov_data.facts import gov_part
from ..server_logging import get_logger
from . import versions as V
from .record import build_record, government_facts, not_found, open_data_part, withheld_counts

LRU_SIZE = 2000
MAX_KEYS = 3
log = get_logger("facts")


class SnapshotsUnavailable(RuntimeError):
    """A snapshot shard could not be decompressed (the free-space guard refused it)."""


def dumps(value: Any) -> str:
    """The canonical JSON of the API (sorted keys, no whitespace variance): equal data gives equal bytes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class FactsService:
    def __init__(self, query: Callable[[str, dict], list[dict]] | None, cache_dir: Path | str | None = None, *,
                 lru_size: int = LRU_SIZE, folder: Path | None = None, rows_by_source: Any = None,
                 clock: Callable[[], float] = time.perf_counter):
        self._query, self.lru_size, self.clock = query, int(lru_size), clock
        self.cache_dir = Path(cache_dir) if cache_dir else None
        # tests: a snapshot folder, or injected rows ({source: rows}, or a function of the government row giving them)
        self.folder, self.rows_by_source = folder, rows_by_source
        self._lru: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._query is not None

    # -- government -----------------------------------------------------------------------------------------------
    def government_rows(self, keys: list[str]) -> dict[str, dict]:
        from ..db import load_level15_by_keys

        if self._query is None:
            raise CatalogUnavailable("DATABASE_URL is not set: the MILO catalogue is not available")
        try:
            return load_level15_by_keys(keys, self._query)
        except Exception as exc:  # noqa: BLE001 - an unreachable MILO is reported, never a fallback
            raise CatalogUnavailable(f"The MILO catalogue query failed: {type(exc).__name__}") from None

    # -- open data (cached) -------------------------------------------------------------------------------------------
    @staticmethod
    def cache_key(key: str, row: dict) -> str:
        v = V.versions()
        parts = [key, str(row.get("content_sha256") or ""), v["snapshots_sha"], v["matcher"], v["admission"],
                 v["zero_semantics"], v["gov_datasets"], v["gov_recall_model_map"]]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def _remember(self, digest: str, part: dict) -> None:
        with self._lock:
            self._lru[digest] = part
            self._lru.move_to_end(digest)
            while len(self._lru) > self.lru_size:
                self._lru.popitem(last=False)

    def _disk_path(self, digest: str) -> Path | None:
        return self.cache_dir / f"{digest}.json" if self.cache_dir else None

    def open_part(self, key: str, row: dict, government: dict) -> tuple[dict, str]:
        """(the open-data part, cache: memory | disk | miss)."""
        from ..open_data import datasets as ds

        digest = self.cache_key(key, row)
        with self._lock:
            hit = self._lru.get(digest)
            if hit is not None:
                self._lru.move_to_end(digest)
                return hit, "memory"
        path = self._disk_path(digest)
        if path is not None and path.is_file():
            try:
                part = json.loads(path.read_text("utf-8"))
                self._remember(digest, part)
                return part, "disk"
            except (OSError, ValueError):
                pass
        refusals = ds.disk_refusals()
        rows = self.rows_by_source(row) if callable(self.rows_by_source) else self.rows_by_source
        part = open_data_part(row, government, folder=self.folder, rows_by_source=rows)
        gov = gov_part(row)
        part["gov_facts"], part["gov_withheld"] = gov["facts"], gov["withheld"]
        if ds.disk_refusals() != refusals or "insufficient_disk" in (part.get("skipped_shards") or []):
            raise SnapshotsUnavailable("an open-data snapshot shard could not be decompressed: the free-space guard "
                                       "refused it")
        if part.get("skipped_shards"):
            return part, "miss"                       # a missing / mismatched shard: answered, never cached
        self._remember(digest, part)
        self._write(path, part)
        return part, "miss"

    @staticmethod
    def _write(path: Path | None, part: dict) -> None:
        if path is None:
            return
        from ..storage.atomic import atomic_write_text
        from ..storage.disk import InsufficientDisk, check_free

        try:
            check_free(path.parent, "facts cache write")
            atomic_write_text(path, dumps(part))
        except (InsufficientDisk, OSError):
            log.warning("facts cache: %s not written", path.name)

    # -- the records ----------------------------------------------------------------------------------------------
    def records(self, keys: list[str], *, debug: bool = False) -> tuple[list[dict], dict]:
        """([vehicle-facts/1 record per key, in order], the call's log line). Raises CatalogUnavailable /
        SnapshotsUnavailable."""
        started = self.clock()
        keys = [str(k).strip() for k in keys]
        line: dict[str, Any] = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "keys": keys,
                                "debug": bool(debug)}
        try:
            rows = self.government_rows(sorted(set(keys)))
            out, statuses, cache, counts, withheld_all = [], {}, {}, {}, []
            for key in keys:
                row = rows.get(key)
                if row is None:
                    out.append(not_found(key))
                    statuses[key] = "not_found"
                    continue
                gov = government_facts(row)
                part, cache[key] = self.open_part(key, row, gov[0])
                record = build_record(key, row, part, debug=debug, government=gov)
                withheld_all += [*gov[1], *(part.get("withheld") or []), *(part.get("gov_withheld") or [])]
                out.append(record)
                statuses[key], counts[key] = "ok", len(record["facts"])
            line.update(status=statuses, cache=cache, facts=counts, withheld=withheld_counts(withheld_all))
            return out, line
        except (CatalogUnavailable, SnapshotsUnavailable) as exc:
            line["status"] = "catalog_unavailable" if isinstance(exc, CatalogUnavailable) else "snapshots_unavailable"
            raise
        finally:
            line["latency_ms"] = round((self.clock() - started) * 1000, 1)
            log.info("facts call %s", dumps(line))

    def preview(self, key: str) -> dict:
        """The MCP facts_preview: the same record plus `withheld` (and its counts)."""
        return self.records([key], debug=True)[0][0]
