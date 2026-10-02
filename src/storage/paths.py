"""The one place that decides where TRIPY keeps durable state.

Every persistent path is derived from ONE data root::

    TRIPY_DATA_DIR/                 (Railway: /data, a mounted Railway Volume)
        runs/<run_id>/...           batch.json, run_state.json, per-vehicle input/events/result, documents/
        cache/                      shared document + search cache (documents/, search/) and memory/
        feedback/                   --export-feedback output

Local development: TRIPY_DATA_DIR defaults to `<repo>/.tripy-data` (git-ignored).

Explicit legacy overrides still win, so existing setups keep working unchanged:

* MILO_RUNS_DIR  -> the runs folder (and, without MILO_CACHE_DIR, the cache at MILO_RUNS_DIR/_cache as before)
* MILO_CACHE_DIR -> the shared cache folder

Nothing here is Railway-specific: Railway is simply TRIPY_DATA_DIR=/data. Genuinely temporary files (the atomic
writer's `.<name>.<uuid>.tmp` files) live next to their destination and are never durable state.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOCAL_DATA_DIR = REPO_ROOT / ".tripy-data"


def _clean(value: str | None) -> str:
    return (value or "").strip()


@dataclass(frozen=True)
class DataPaths:
    data_dir: Path
    runs_dir: Path
    cache_dir: Path
    feedback_dir: Path
    source: str          # how data_dir was chosen: "TRIPY_DATA_DIR" | "default" | "MILO_RUNS_DIR"

    def ensure(self) -> "DataPaths":
        for path in (self.data_dir, self.runs_dir, self.cache_dir):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def as_dict(self) -> dict:
        return {"data_dir": str(self.data_dir), "runs_dir": str(self.runs_dir), "cache_dir": str(self.cache_dir),
                "feedback_dir": str(self.feedback_dir), "source": self.source}


def resolve_paths(env: Callable[[str], str | None] = os.environ.get) -> DataPaths:
    """Resolve (but do not create) every durable path from the environment."""
    data_raw = _clean(env("TRIPY_DATA_DIR"))
    data_dir = Path(data_raw).expanduser() if data_raw else DEFAULT_LOCAL_DATA_DIR
    source = "TRIPY_DATA_DIR" if data_raw else "default"
    runs_raw, cache_raw = _clean(env("MILO_RUNS_DIR")), _clean(env("MILO_CACHE_DIR"))
    if runs_raw:
        runs_dir = Path(runs_raw).expanduser()
        cache_dir = Path(cache_raw).expanduser() if cache_raw else runs_dir / "_cache"   # the historical layout
        if not data_raw:
            source = "MILO_RUNS_DIR"
    else:
        runs_dir = data_dir / "runs"
        cache_dir = Path(cache_raw).expanduser() if cache_raw else data_dir / "cache"
    return DataPaths(data_dir=data_dir, runs_dir=runs_dir, cache_dir=cache_dir,
                     feedback_dir=data_dir / "feedback", source=source)


def probe_writable(folder: Path) -> tuple[bool, str | None]:
    """Create the folder if needed and prove a file can be written, renamed and removed there."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".tripy-write-probe-{uuid.uuid4().hex}"
        probe.write_bytes(b"ok")
        moved = probe.with_name(probe.name + ".moved")
        os.replace(probe, moved)
        moved.unlink()
        return True, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc.strerror or exc}"


def storage_status(paths: DataPaths, env: Callable[[str], str | None] = os.environ.get) -> dict:
    """Is durable state really durable here? Never raises.

    On Railway the container filesystem is ephemeral: state survives a redeploy only on a mounted Volume.
    Railway exposes the mount as RAILWAY_VOLUME_MOUNT_PATH when a volume is attached to the service.
    """
    writable, error = probe_writable(paths.runs_dir)
    cache_ok, cache_error = probe_writable(paths.cache_dir)
    on_railway = bool(_clean(env("RAILWAY_ENVIRONMENT")) or _clean(env("RAILWAY_PROJECT_ID"))
                      or _clean(env("RAILWAY_SERVICE_ID")))
    mount = _clean(env("RAILWAY_VOLUME_MOUNT_PATH"))
    on_volume = None
    if mount:
        try:
            data = paths.runs_dir.resolve()
            on_volume = data == Path(mount).resolve() or Path(mount).resolve() in data.parents
        except OSError:
            on_volume = False
    if not (writable and cache_ok):
        level, message = "error", f"Data directory is not writable ({error or cache_error})."
    elif on_railway and not mount:
        level, message = "warning", ("No Railway Volume is attached: runs are stored on the container's ephemeral "
                                     "disk and are lost on every redeploy or restart.")
    elif on_railway and on_volume is False:
        level, message = "warning", (f"A Railway Volume is mounted at {mount}, but TRIPY_DATA_DIR "
                                     f"({paths.data_dir}) is outside it: runs are not persisted.")
    elif on_railway:
        level, message = "ok", f"Railway Volume at {mount}"
    else:
        level, message = "ok", "Local disk"
    return {"level": level, "message": message, "writable": writable and cache_ok, "on_railway": on_railway,
            "volume_mount": mount or None, "on_volume": on_volume, **paths.as_dict()}
