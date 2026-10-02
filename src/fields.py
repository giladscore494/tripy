"""Enrichment field schema: which fields a run requests, with their metadata.

Everything downstream (prompt, field-state evaluation, targeted field recovery,
finalizer bundle, coverage metrics) works from the resolved list of field specs,
never from hard-coded field names. A field spec is a dict:

    {"name": "<field_name>", "description": "...", "group": "...", "unit": "...",
     "applies_to": ["battery_electric", ...], "recovery_attempts": 2}

Only `name` is required. Unknown names requested at run time simply get
{"name": name, "description": name}.

The same file carries each field's Hebrew UI label (`display_name_he`), the Hebrew group labels and the
field dictionary of the deterministic candidate harvester (aliases, units, context and exclusion rules;
see src/candidate_harvest.py). Dictionary keys are metadata for code: `public_spec()` strips them from
anything handed to a model or written into events.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "data" / "enrichment_fields.json"
DEFAULT_GROUP = "requested"
# Field-dictionary keys (harvester metadata). Never sent to a model; see public_spec().
DICTIONARY_KEYS = ("matcher", "component", "warranty_kind", "aliases_he", "aliases_en", "abbreviations",
                   "positive_context_terms_he", "positive_context_terms_en", "negative_context_terms_he",
                   "negative_context_terms_en", "expected_units", "accepted_unit_variants", "normalized_unit",
                   "value_type", "patterns", "plausible_min", "plausible_max", "enum_values", "conversion_rules",
                   "ambiguity_rules", "exclusion_rules", "require_context")
# Evidence-admission policy keys (src/evidence_admission.py, src/document_binding.py). Not harvester metadata (not
# part of the harvest schema hash) and not sent to a model either. `semantic_definition`, `time_sensitive` and
# `not_applicable_when` stay public: they tell the model what the field means.
POLICY_KEYS = ("binding_requirement", "semantic_exclusions")


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


def public_spec(spec: dict) -> dict:
    """A field spec without its harvester dictionary and admission policy (what models and event logs receive)."""
    return {k: v for k, v in spec.items() if k not in DICTIONARY_KEYS and k not in POLICY_KEYS}


def semantic_notes(specs: Iterable[dict]) -> dict[str, str]:
    """The semantic definitions a model must not get wrong: applicable fields whose schema restricts what quantity
    counts (semantic exclusions) or when the field exists at all (not_applicable_when). Kept short on purpose:
    every other field's definition travels with its own recovery packet."""
    return {s["name"]: s["semantic_definition"] for s in specs
            if s.get("applicable", True) and s.get("semantic_definition")
            and (s.get("semantic_exclusions") or s.get("not_applicable_when"))}


_DOC_CACHE: dict[str, tuple[float, dict]] = {}


def schema_document(path: Path | str | None = None) -> dict:
    """The whole schema file (fields, groups, harvest vocabulary); re-read only when the file changes."""
    path = Path(path or os.environ.get("ENRICHMENT_SCHEMA_PATH") or SCHEMA_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _DOC_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    data = json.loads(path.read_text("utf-8"))
    doc = data if isinstance(data, dict) else {"fields": data}
    _DOC_CACHE[str(path)] = (mtime, doc)
    return doc


def with_dictionary(spec: dict) -> dict:
    """A (possibly public) field spec completed with its schema dictionary entry (units, conversions, value type):
    run logs keep public specs, but unit-aware comparisons need the dictionary. The spec's own keys win."""
    name = normalize_field_name(spec.get("name"))
    if not name or any(k in spec for k in ("accepted_unit_variants", "expected_units", "conversion_rules")):
        return spec
    for item in schema_document().get("fields") or []:
        if isinstance(item, dict) and normalize_field_name(item.get("name")) == name:
            return {**item, **spec}
    return spec


def _default_document() -> dict:
    return schema_document(SCHEMA_PATH)


def harvest_vocabulary() -> dict:
    """Shared matcher vocabularies: from the active schema, else from the default schema."""
    return schema_document().get("harvest_vocabulary") or _default_document().get("harvest_vocabulary") or {}


_DISPLAY_CACHE: dict[tuple[int, int], dict[str, dict]] = {}


def _display_specs() -> dict[str, dict]:
    docs = (_default_document(), schema_document())
    key = (id(docs[0]), id(docs[1]))      # schema_document() returns the same object until the file changes
    specs = _DISPLAY_CACHE.get(key)
    if specs is None:
        specs = {}
        for doc in docs:
            for item in doc.get("fields") or []:
                if isinstance(item, dict) and item.get("name"):
                    specs[normalize_field_name(item["name"])] = item
        _DISPLAY_CACHE.clear()
        _DISPLAY_CACHE[key] = specs
    return specs


def field_display_name(name: Any, spec: dict | None = None, locale: str = "he") -> str:
    """The user-facing label of a field: the spec's `display_name_<locale>`, else the schema's, else the
    description (custom fields without a label), else the canonical name. Raw/debug views keep `name`."""
    key = normalize_field_name(name)
    attr = f"display_name_{locale}"
    for candidate in (spec or {}, _display_specs().get(key) or {}):
        if candidate.get(attr):
            return str(candidate[attr])
    for candidate in (spec or {}, _display_specs().get(key) or {}):
        description = candidate.get("description")
        if description and normalize_field_name(description) != key:
            return str(description)
    return str(name)


def group_display_name(group: Any, locale: str = "he") -> str:
    attr = f"display_name_{locale}"
    for doc in (schema_document(), _default_document()):
        entry = (doc.get("groups") or {}).get(str(group or ""))
        if isinstance(entry, dict) and entry.get(attr):
            return str(entry[attr])
    return str(group or "")
