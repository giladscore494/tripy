"""The facts API's data files and the versions a record carries (and its open-data cache is keyed by).

    data/facts_admission.json        which open-data offers become facts (version: `admission`)
    data/facts_zero_semantics.json   what a government 0 means per field (version: `zero_semantics`)
    data/facts_contract.json         the `vehicle-facts/1` JSON schema (GET /api/facts/v1/contract)
    data/open/manifest.json          the committed snapshots: its sha256 is `snapshots_sha`, its built_at the contract's
    data/gov/manifest.json           the government-dataset snapshots (src/gov_data): its sha256 is `gov_datasets`
    data/gov/recall_model_map.json   the reviewed recall model map: its sha256 is `gov_recall_model_map`
    matcher                          match.MATCH_VERSION + data/open_datasets.json version + the facts engine version

Each file is re-read when its mtime changes (the reviewer edits the data, never the code).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import CONTRACT, FACTS_ENGINE_VERSION

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
ADMISSION_PATH = DATA_DIR / "facts_admission.json"
ZERO_SEMANTICS_PATH = DATA_DIR / "facts_zero_semantics.json"
CONTRACT_PATH = DATA_DIR / "facts_contract.json"
_FILES: dict[str, tuple[int, Any]] = {}


def _cached(path: Path, read) -> Any:
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return read(None)
    hit = _FILES.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    value = read(path)
    _FILES[str(path)] = (mtime, value)
    return value


def _json(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def admission(path: Path | None = None) -> dict:
    return _cached(Path(path or ADMISSION_PATH), _json)


def zero_semantics(path: Path | None = None) -> dict:
    return _cached(Path(path or ZERO_SEMANTICS_PATH), _json)


def contract_schema(path: Path | None = None) -> dict:
    return _cached(Path(path or CONTRACT_PATH), _json)


def _manifest(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {"sha256": "no_manifest", "built_at": None}
    data = path.read_bytes()
    try:
        built = json.loads(data).get("built_at")
    except (ValueError, AttributeError):
        built = None
    return {"sha256": hashlib.sha256(data).hexdigest(), "built_at": built}


def snapshots() -> dict:
    """{sha256, built_at} of the committed snapshot manifest (data/open/manifest.json, or the repo dir tests set)."""
    from ..open_data import datasets as ds

    return dict(_cached(ds.repo_dir() / ds.MANIFEST_NAME, _manifest))


def matcher_version() -> str:
    from ..open_data import datasets as ds
    from ..open_data.match import MATCH_VERSION

    return f"{MATCH_VERSION}+{ds.config().get('version') or 'no_config'}+{FACTS_ENGINE_VERSION}"


def gov() -> dict:
    """{gov_datasets: sha256 of data/gov/manifest.json, gov_recall_model_map: sha256 of the reviewed map} (re-read
    when either file's mtime changes)."""
    from ..gov_data import snapshot as SN

    folder = SN.gov_dir()
    manifest = dict(_cached(folder / SN.MANIFEST_NAME, _manifest))["sha256"]
    model_map = dict(_cached(folder / SN.MAP_NAME, _manifest))["sha256"]
    return {"gov_datasets": manifest, "gov_recall_model_map": "no_map" if model_map == "no_manifest" else model_map}


def versions() -> dict:
    """The `versions` block of a record (no timestamp: two calls with the same data give the same bytes)."""
    return {"contract": CONTRACT, "admission": admission().get("version") or "none",
            "zero_semantics": zero_semantics().get("version") or "none", "snapshots_sha": snapshots()["sha256"],
            "matcher": matcher_version(), **gov()}
