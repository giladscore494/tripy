"""The one atomic file-write primitive used for every file a reader must never see half-written.

    serialize completely in memory
      → write to a temporary file in the SAME directory (same filesystem, so the rename is atomic)
      → flush (+ os.fsync when durability matters)
      → os.replace(temp, destination)          (atomic: readers see the old file or the new one)
      → fsync the directory (best effort, POSIX) so the rename itself survives a crash
      → on ANY failure: remove the temp file and re-raise; the destination is left untouched

`gunzip_once` decompresses a committed .gz snapshot into the temp dir the same way, once per target even when several
request threads ask for it at the same moment (a lock per target; the temp name is unique per call).

`durable=True` is for recovery-critical state (result.json, notably the finalization_pending
checkpoint written before a paid finalizer request). The shared document cache uses durable=False:
its files are rebuildable and are written far more often.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import threading
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


_TARGET_LOCKS: dict[str, threading.Lock] = {}
_TARGET_LOCKS_GUARD = threading.Lock()


def _target_lock(path: Path) -> threading.Lock:
    with _TARGET_LOCKS_GUARD:
        return _TARGET_LOCKS.setdefault(str(path), threading.Lock())


def gunzip_once(source: Path | str, target: Path | str) -> Path:
    """Decompress `source` (.gz) to `target` unless it already exists; atomic (a unique temp file in the target's
    folder, then os.replace) and serialized per target, so concurrent callers never share a temp file: the first one
    decompresses, the others wait and find the target. Raises (OSError, EOFError, gzip.BadGzipFile) on a real failure,
    leaving no temp file behind."""
    source, target = Path(source), Path(target)
    with _target_lock(target):
        if target.exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with gzip.open(source, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            os.replace(tmp, target)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
    return target
