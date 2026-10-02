"""The one atomic file-write primitive used for every file a reader must never see half-written.

    serialize completely in memory
      → write to a temporary file in the SAME directory (same filesystem, so the rename is atomic)
      → flush (+ os.fsync when durability matters)
      → os.replace(temp, destination)          (atomic: readers see the old file or the new one)
      → fsync the directory (best effort, POSIX) so the rename itself survives a crash
      → on ANY failure: remove the temp file and re-raise; the destination is left untouched

`durable=True` is for recovery-critical state (result.json, notably the finalization_pending
checkpoint written before a paid finalizer request). The shared document cache uses durable=False:
its files are rebuildable and are written far more often.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def _fsync_dir(folder: Path) -> None:
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path | str, data: bytes, *, durable: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            if durable:
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    if durable:
        _fsync_dir(path.parent)


def atomic_write_text(path: Path | str, text: str, *, durable: bool = False) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), durable=durable)


def atomic_write_json(path: Path | str, value: Any, *, durable: bool = False, indent: int | None = 1) -> None:
    """Serialize first (a serialization error never touches the destination), then write atomically."""
    text = json.dumps(value, ensure_ascii=False, indent=indent, default=str)
    atomic_write_text(path, text, durable=durable)
