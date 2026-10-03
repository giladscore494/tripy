"""On-disk document and search cache shared by every run.

One cache serves all 50 vehicles so reuse across runs is real and measurable.
Each document lives in its own folder: meta.json, body.bin and (when text was
extracted) text.txt. Derived extractions are cached next to it.

Safe for concurrent vehicle workers:

* single-flight per cache key: `hold(key)` serializes work on ONE key (one document kind + URL,
  one search key, one derived extraction), so 20 workers asking for the same uncached query make
  exactly one network call and the other 19 read the result it stored. Different keys never wait
  for each other: there is no global lock around network work.
* atomic files: every file is written to a temporary name and moved into place with os.replace,
  and meta.json (the marker of a complete document) is written last, so a concurrent reader never
  observes half-written JSON or a document without its body.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .atomic import atomic_write_bytes, atomic_write_text  # the one shared primitive (durable=False here)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_id_for(kind: str, url: str) -> str:
    return "d_" + _digest(f"{kind}:{url}")[:16]


class KeyedLocks:
    """One lock per key, created on demand and dropped when nobody holds or waits for it."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, list] = {}   # key -> [lock, users]

    @contextmanager
    def hold(self, key: str, timeout: float | None = None) -> Iterator[bool]:
        """Yields True when this caller had to wait for another holder of the same key. With `timeout` (seconds), a
        wait longer than that raises TimeoutError (the key was never held by this caller)."""
        with self._guard:
            entry = self._locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        lock = entry[0]
        waited = not lock.acquire(blocking=False)
        if waited and not lock.acquire(timeout=-1 if timeout is None else max(0.0, float(timeout))):
            self._leave(key, entry)
            raise TimeoutError(f"single-flight wait for {key!r} exceeded {timeout:.1f} s")
        try:
            yield waited
        finally:
            lock.release()
            self._leave(key, entry)

    def _leave(self, key: str, entry: list) -> None:
        with self._guard:
            entry[1] -= 1
            if entry[1] == 0 and self._locks.get(key) is entry:
                del self._locks[key]


class DocumentCache:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.docs_dir = self.root / "documents"
        self.search_dir = self.root / "search"
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.search_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._keys = KeyedLocks()
        self.stats = {"document_hits": 0, "document_misses": 0, "search_hits": 0, "search_misses": 0,
                      "cross_vehicle_cache_waits": 0, "cross_vehicle_search_singleflight_reuses": 0,
                      "cross_vehicle_document_singleflight_reuses": 0, "derived_singleflight_reuses": 0}

    # -- single flight -------------------------------------------------------------------------

    @contextmanager
    def hold(self, key: str, timeout: float | None = None) -> Iterator[bool]:
        """Single-flight section for one cache key; yields whether another worker held it first. `timeout`: see
        KeyedLocks.hold (TimeoutError when the wait is longer)."""
        with self._keys.hold(key, timeout) as waited:
            if waited:
                self.count("cross_vehicle_cache_waits")
            yield waited

    def count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.stats[name] = self.stats.get(name, 0) + n

    def stats_snapshot(self) -> dict:
        with self._lock:
            return dict(self.stats)

    # -- documents -----------------------------------------------------------

    def _dir(self, document_id: str) -> Path:
        return self.docs_dir / document_id

    def lookup(self, kind: str, url: str) -> dict | None:
        """Return cached meta for (kind, url) and count a hit or miss."""
        meta = self.get(document_id_for(kind, url))
        with self._lock:
            self.stats["document_hits" if meta else "document_misses"] += 1
        return meta

    def get(self, document_id: str) -> dict | None:
        path = self._dir(document_id) / "meta.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except ValueError:  # a file left by a writer from before atomic writes: treat as absent
            return None

    def find_by_url(self, url: str) -> dict | None:
        """Any cached document for a URL, preferring rendered > pdf > fetch."""
        for kind in ("rendered", "pdf", "fetch"):
            meta = self.get(document_id_for(kind, url))
            if meta:
                return meta
        for meta in self.list_documents():
            if meta.get("final_url") == url:
                return meta
        return None

    def put(self, kind: str, url: str, body: bytes, meta: dict, text: str | None = None) -> dict:
        document_id = document_id_for(kind, url)
        folder = self._dir(document_id)
        folder.mkdir(parents=True, exist_ok=True)
        record = {
            "document_id": document_id,
            "kind": kind,
            "url": url,
            "fetched_at": _now(),
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "text_chars": len(text) if text is not None else 0,
            **meta,
        }
        atomic_write_bytes(folder / "body.bin", body)
        if text is not None:
            atomic_write_text(folder / "text.txt", text)
        # meta.json last: its presence marks a complete document for every concurrent reader
        atomic_write_text(folder / "meta.json", json.dumps(record, ensure_ascii=False, indent=1))
        return record

    def read_body(self, document_id: str) -> bytes:
        path = self._dir(document_id) / "body.bin"
        return path.read_bytes() if path.is_file() else b""

    def read_text(self, document_id: str) -> str:
        path = self._dir(document_id) / "text.txt"
        return path.read_text("utf-8") if path.is_file() else ""

    def get_derived(self, document_id: str, name: str) -> Any | None:
        path = self._dir(document_id) / f"derived_{name}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except ValueError:
            return None

    def put_derived(self, document_id: str, name: str, value: Any) -> None:
        folder = self._dir(document_id)
        if folder.is_dir():
            atomic_write_text(folder / f"derived_{name}.json", json.dumps(value, ensure_ascii=False))

    def derived(self, document_id: str, name: str, compute) -> tuple[Any, bool]:
        """(value, cache_hit): a derived extraction computed at most once per document and name, even
        when several workers ask for it at the same time."""
        value = self.get_derived(document_id, name)
        if value is not None:
            return value, True
        with self.hold(f"derived:{document_id}:{name}") as waited:
            value = self.get_derived(document_id, name)
            if value is not None:
                if waited:
                    self.count("derived_singleflight_reuses")
                return value, True
            value = compute()
            self.put_derived(document_id, name, value)
            return value, False

    def export(self, document_id: str, dest_dir: Path | str) -> Path | None:
        """Copy one cached document (meta, body, text, derived) into a run folder."""
        source = self._dir(document_id)
        if not source.is_dir():
            return None
        target = Path(dest_dir) / document_id
        shutil.copytree(source, target, dirs_exist_ok=True)
        return target

    def list_documents(self) -> list[dict]:
        out = []
        for meta_path in sorted(self.docs_dir.glob("*/meta.json")):
            try:
                out.append(json.loads(meta_path.read_text("utf-8")))
            except ValueError:
                continue
        return out

    # -- search results --------------------------------------------------------

    def _search_path(self, key: str) -> Path:
        return self.search_dir / f"{_digest(key)[:24]}.json"

    def _read_search(self, key: str) -> Any | None:
        path = self._search_path(key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text("utf-8"))["value"]
        except (ValueError, KeyError):
            return None

    def get_search(self, key: str) -> Any | None:
        value = self._read_search(key)
        with self._lock:
            self.stats["search_hits" if value is not None else "search_misses"] += 1
        return value

    def has_search(self, key: str) -> bool:
        """Is this search already cached? (a peek for budgeting: no hit/miss statistics)"""
        return self._read_search(key) is not None

    def put_search(self, key: str, value: Any) -> None:
        atomic_write_text(self._search_path(key),
                          json.dumps({"key": key, "stored_at": _now(), "value": value}, ensure_ascii=False))

    def search_singleflight(self, key: str, compute) -> tuple[Any, bool]:
        """(results, cache_hit). Exactly one caller per key runs `compute` (the network search) while
        the others wait and then reuse what it stored. A failed compute stores nothing."""
        value = self.get_search(key)
        if value is not None:
            return value, True
        with self.hold(f"search:{_digest(key)}") as waited:
            value = self._read_search(key)
            if value is not None:
                if waited:
                    self.count("cross_vehicle_search_singleflight_reuses")
                return value, True
            value = compute()
            self.put_search(key, value)
            return value, False
