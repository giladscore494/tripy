"""On-disk document and search cache shared by every run.

One cache serves all 50 vehicles so reuse across runs is real and measurable.
Each document lives in its own folder: meta.json, body.bin and (when text was
extracted) text.txt. Derived extractions are cached next to it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_id_for(kind: str, url: str) -> str:
    return "d_" + _digest(f"{kind}:{url}")[:16]


class DocumentCache:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.docs_dir = self.root / "documents"
        self.search_dir = self.root / "search"
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.search_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.stats = {"document_hits": 0, "document_misses": 0, "search_hits": 0, "search_misses": 0}

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
        return json.loads(path.read_text("utf-8"))

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
        (folder / "body.bin").write_bytes(body)
        if text is not None:
            (folder / "text.txt").write_text(text, "utf-8")
        (folder / "meta.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), "utf-8")
        return record

    def read_body(self, document_id: str) -> bytes:
        path = self._dir(document_id) / "body.bin"
        return path.read_bytes() if path.is_file() else b""

    def read_text(self, document_id: str) -> str:
        path = self._dir(document_id) / "text.txt"
        return path.read_text("utf-8") if path.is_file() else ""

    def get_derived(self, document_id: str, name: str) -> Any | None:
        path = self._dir(document_id) / f"derived_{name}.json"
        return json.loads(path.read_text("utf-8")) if path.is_file() else None

    def put_derived(self, document_id: str, name: str, value: Any) -> None:
        folder = self._dir(document_id)
        if folder.is_dir():
            (folder / f"derived_{name}.json").write_text(json.dumps(value, ensure_ascii=False), "utf-8")

    def list_documents(self) -> list[dict]:
        out = []
        for meta_path in sorted(self.docs_dir.glob("*/meta.json")):
            try:
                out.append(json.loads(meta_path.read_text("utf-8")))
            except ValueError:
                continue
        return out

    # -- search results --------------------------------------------------------

    def get_search(self, key: str) -> Any | None:
        path = self.search_dir / f"{_digest(key)[:24]}.json"
        hit = path.is_file()
        with self._lock:
            self.stats["search_hits" if hit else "search_misses"] += 1
        return json.loads(path.read_text("utf-8"))["value"] if hit else None

    def put_search(self, key: str, value: Any) -> None:
        path = self.search_dir / f"{_digest(key)[:24]}.json"
        path.write_text(json.dumps({"key": key, "stored_at": _now(), "value": value}, ensure_ascii=False), "utf-8")
