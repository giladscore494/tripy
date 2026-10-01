"""Operator variant notes (data/variant_notes.json): context for the model, never a validation rule."""

from __future__ import annotations

import json
from pathlib import Path

NOTES_PATH = Path(__file__).resolve().parent.parent / "data" / "variant_notes.json"


def variant_notes(record_id: str | None, path: Path = NOTES_PATH) -> dict | None:
    if not record_id or not Path(path).is_file():
        return None
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except ValueError:
        return None
    return (data.get("notes") or {}).get(str(record_id))


def record_id_of(payload: dict | None) -> str | None:
    payload = payload or {}
    rid = (payload.get("identity") or {}).get("government_record_id") or (payload.get("raw_row") or {}).get(
        "upstream_record_id")
    return str(rid) if rid not in (None, "") else None
