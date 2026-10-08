"""The dataset config (data/gov_datasets.json) and the column schema of a downloaded file.

A live header is resolved case-insensitively (a BOM and surrounding whitespace dropped) against the dataset's
`columns`: `required` (missing -> the dataset stops: required_column_missing), `kept` (stored when present) and
`never` (dropped by the projection: never read into a stored, logged or sampled value). The schema hash is the sha256
of the live header as stated (order kept), so a renamed, added or moved column changes it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "gov_datasets.json"
_CACHE: dict[str, tuple[float, dict]] = {}


def config(path: Path | str | None = None) -> dict:
    path = Path(path or CONFIG_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    hit = _CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    data = json.loads(path.read_text("utf-8"))
    _CACHE[str(path)] = (mtime, data)
    return data


def datasets(cfg: dict | None = None) -> dict[str, dict]:
    cfg = config() if cfg is None else cfg
    return {k: v for k, v in (cfg.get("datasets") or {}).items() if isinstance(v, dict) and not k.startswith("_")}


def selected(text: str | None, cfg: dict | None = None) -> list[str]:
    """`all` or a comma list -> the dataset names (an unknown name is a ValueError)."""
    names = list(datasets(cfg))
    wanted = [n.strip() for n in str(text or "all").split(",") if n.strip()]
    if not wanted or wanted == ["all"]:
        return names
    unknown = [n for n in wanted if n not in names]
    if unknown:
        raise ValueError(f"unknown gov dataset(s) {unknown}; known: {names}")
    return [n for n in names if n in wanted]


def clean_name(name: Any) -> str:
    return str(name or "").replace("﻿", "").strip()


def schema_hash(header: list[str]) -> str:
    return hashlib.sha256("\x1f".join(clean_name(h) for h in header).encode("utf-8")).hexdigest()


def required_columns(spec: dict, role: str | None = None) -> list[str]:
    cols = spec.get("columns") or {}
    return [*(cols.get("required") or []), *((cols.get("required_by_role") or {}).get(role or "") or [])]


def projection(header: list[str], spec: dict, role: str | None = None) -> tuple[dict[str, int], list[str]]:
    """({canonical column: index in the header}, missing required columns). Only required and kept columns are in the
    projection; `never` columns and every other column are not."""
    cols = spec.get("columns") or {}
    never = {str(c).lower() for c in cols.get("never") or []}
    by_lower: dict[str, int] = {}
    for index, name in enumerate(header):
        key = clean_name(name).lower()
        if key and key not in by_lower:
            by_lower[key] = index
    wanted = [*required_columns(spec, role), *(cols.get("kept") or [])]
    out: dict[str, int] = {}
    for column in wanted:
        if column.lower() in never:
            continue
        if column.lower() in by_lower:
            out[column] = by_lower[column.lower()]
    missing = [c for c in required_columns(spec, role) if c not in out]
    return out, missing
