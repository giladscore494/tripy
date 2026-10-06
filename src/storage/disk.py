"""The data volume's disk: usage, the free-space guard and the open-data cleanup.

The volume holds the runs, the document cache and the logs: when it is full a run cannot write its events. So:

    check_free(path, what)      every job that writes to the volume (a run, the trim index, open data) checks the free
                                space first and refuses with `insufficient_disk` below MIN_FREE_BYTES (200 MB) instead
                                of failing mid-write
    cleanup_open_data(folder)   at startup: the open-data snapshots are built by a GitHub Action into the repository
                                (data/open/), never on the server; what an earlier server-side build left in
                                <data>/derived/open/ (partial / temp files, a *.sqlite without `built_at`) is deleted
    delete_open_data(folder)    the Data page's "Delete open-data files" (only <data>/derived/open/)
    usage(data_dir)             the Data page's Storage panel: volume total / used / free and the size per top-level
                                folder
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Iterable

log = logging.getLogger("tripy.storage")

MIN_FREE_BYTES = 200 * 1024 * 1024
TEMP_SUFFIXES = (".tmp", "-journal", "-wal", "-shm", ".partial")


class InsufficientDisk(RuntimeError):
    """Less than MIN_FREE_BYTES free on the volume: the write is refused before it starts."""

    code = "insufficient_disk"

    def __init__(self, path: Path, free: int, needed: int, what: str):
        super().__init__(f"{what}: only {free / 1024 / 1024:.0f} MB free on the data volume "
                         f"(at least {needed / 1024 / 1024:.0f} MB needed); nothing was written")
        self.path, self.free, self.needed, self.what = path, free, needed, what


def free_bytes(path: Path | str) -> int:
    """Free bytes of the file system holding `path` (its nearest existing parent)."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def check_free(path: Path | str, what: str, minimum: int = MIN_FREE_BYTES, *, free=None) -> None:
    """Raise InsufficientDisk (and log it) when the volume of `path` has less than `minimum` bytes free."""
    available = free_bytes(path) if free is None else int(free)
    if available < minimum:
        error = InsufficientDisk(Path(path), available, minimum, what)
        log.error("insufficient_disk: %s", error)
        raise error


def _size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def usage(data_dir: Path | str, folders: Iterable[tuple[str, Path]] | None = None) -> dict:
    """{volume: {total, used, free}, folders: [{name, path, bytes}], min_free_bytes}."""
    data_dir = Path(data_dir)
    probe = data_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    disk = shutil.disk_usage(probe)
    named = list(folders) if folders is not None else []
    known = {p.resolve() for _, p in named if p.exists()}
    if data_dir.is_dir():
        for child in sorted(data_dir.iterdir()):
            if child.resolve() not in known:
                named.append((child.name + ("/" if child.is_dir() else ""), child))
    rows = [{"name": name, "path": str(path), "bytes": _size(path) if path.exists() else 0} for name, path in named]
    return {"volume": {"total": disk.total, "used": disk.used, "free": disk.free, "path": str(probe)},
            "folders": sorted(rows, key=lambda r: -r["bytes"]), "min_free_bytes": MIN_FREE_BYTES,
            "low": disk.free < MIN_FREE_BYTES}


def _built(path: Path) -> bool:
    """A snapshot sqlite is built when its meta table states `built_at`."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return bool(row and row[0] not in (None, "", "null"))


def cleanup_open_data(folder: Path | str) -> dict:
    """Delete what a server-side open-data build left behind: temp / partial files and every *.sqlite without
    `built_at`. Built snapshots are kept (the Data page's "Delete open-data files" removes them). Never raises."""
    folder = Path(folder)
    removed, freed = [], 0
    if not folder.is_dir():
        return {"removed": removed, "bytes_freed": freed}
    for path in sorted(folder.iterdir()):
        if not path.is_file():
            continue
        partial = path.name.startswith(".") or path.name.endswith(TEMP_SUFFIXES)
        unbuilt = path.suffix == ".sqlite" and not partial and not _built(path)
        if not (partial or unbuilt):
            continue
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            log.warning("open data cleanup: could not delete %s", path, exc_info=True)
            continue
        removed.append(path.name)
        freed += size
    log.info("open data cleanup: %d file(s) removed from %s, %.1f MB freed", len(removed), folder, freed / 1024 / 1024)
    return {"removed": removed, "bytes_freed": freed}


def delete_open_data(folder: Path | str, *, by: str = "operator") -> dict:
    """Delete <data>/derived/open/ entirely (only that folder). Logged."""
    folder = Path(folder)
    if not folder.exists():
        return {"deleted": False, "bytes_freed": 0, "path": str(folder)}
    size = _size(folder)
    shutil.rmtree(folder, ignore_errors=True)
    log.warning("open data files deleted by %s: %s (%.1f MB freed)", by, folder, size / 1024 / 1024)
    return {"deleted": True, "bytes_freed": size, "path": str(folder)}
