"""Enrichment field schema: which fields a run requests, with their metadata.

Everything downstream (prompt, field-state evaluation, targeted field recovery,
finalizer bundle, coverage metrics) works from the resolved list of field specs,
never from hard-coded field names. A field spec is a dict:

    {"name": "<field_name>", "description": "...", "group": "...", "unit": "...",
     "applies_to": ["battery_electric", ...], "recovery_attempts": 2}

Only `name` is required. Unknown names requested at run time simply get
{"name": name, "description": name}.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "data" / "enrichment_fields.json"
DEFAULT_GROUP = "requested"


def normalize_field_name(name: Any) -> str:
    return re.sub(r"[\s\-]+", "_", str(name or "").strip().lower())


def load_schema(path: Path | str | None = None) -> list[dict]:
    """Field specs from a JSON file: {"fields": [...]} or a bare list. Default: data/enrichment_fields.json."""
    path = Path(path or os.environ.get("ENRICHMENT_SCHEMA_PATH") or SCHEMA_PATH)
    data = json.loads(path.read_text("utf-8"))
    raw = data.get("fields") if isinstance(data, dict) else data
    specs = []
    for item in raw or []:
        spec = {"name": item} if isinstance(item, str) else dict(item)
        if not spec.get("name"):
            continue
        spec["name"] = normalize_field_name(spec["name"])
        spec.setdefault("description", spec["name"])
        spec.setdefault("group", DEFAULT_GROUP)
        specs.append(spec)
    return specs


def parse_field_list(value: Any) -> list[Any]:
    """`ENRICHMENT_FIELDS` / --fields: comma separated names, a JSON list of names/specs, or a list."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            return list(json.loads(text))
        return [part.strip() for part in text.split(",") if part.strip()]
    return list(value)


def resolve_requested_fields(requested: Iterable[Any] | None = None, schema: list[dict] | None = None,
                             propulsion: str | None = None) -> list[dict]:
    """The field specs a run requests, in order, each marked `applicable` for this vehicle.

    `requested` may hold names (looked up in the schema for metadata) or full specs;
    empty/None means "every field in the schema".
    """
    schema = schema if schema is not None else load_schema()
    by_name = {s["name"]: s for s in schema}
    items = list(requested) if requested else list(schema)
    out, seen = [], set()
    for item in items:
        if isinstance(item, dict):
            name = normalize_field_name(item.get("name"))
            spec = {**by_name.get(name, {}), **item, "name": name}
        else:
            name = normalize_field_name(item)
            spec = dict(by_name.get(name) or {"name": name})
        if not name or name in seen:
            continue
        seen.add(name)
        spec.setdefault("description", name)
        spec.setdefault("group", DEFAULT_GROUP)
        applies = spec.get("applies_to")
        spec["applicable"] = not applies or not propulsion or propulsion in applies
        out.append(spec)
    return out


def propulsion_of(payload: dict | None, vehicle_meta: dict | None = None) -> str | None:
    engine = (payload or {}).get("engine_drivetrain") or {}
    return engine.get("propulsion_normalized") or (vehicle_meta or {}).get("propulsion") or None


def applicable_names(specs: Iterable[dict]) -> list[str]:
    return [s["name"] for s in specs if s.get("applicable", True)]


def grouped(specs: Iterable[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for spec in specs:
        groups.setdefault(spec.get("group") or DEFAULT_GROUP, []).append(spec)
    return groups
