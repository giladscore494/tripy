"""Shared shapes: research targets, GLM tool definitions and lenient output parsing.

Nothing in this module judges truth. Target fields are suggestions handed to the
model and a yardstick for observational coverage; parsing exists only so the UI
can render whatever the model returned.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Level 2 targets, grouped as in the spec (section 7). Keys are suggested names;
# the model may return other keys and those are kept as additional fields.
LEVEL2_TARGET_FIELDS: dict[str, dict[str, str]] = {
    "performance": {
        "torque_nm": "Maximum torque (Nm)",
        "acceleration_0_100_s": "0-100 km/h (s)",
        "top_speed_kmh": "Top speed (km/h)",
        "fuel_consumption_combined_l_100km": "Combined consumption (l/100km, or kWh/100km for EV)",
    },
    "electric_hybrid": {
        "battery_gross_kwh": "Battery capacity gross (kWh)",
        "battery_usable_kwh": "Battery capacity usable (kWh)",
        "electric_range_km": "Electric range (km)",
        "electric_range_standard": "Range test standard (WLTP/EPA/CLTC/...)",
        "ac_charging_time": "AC charging time (with % window)",
        "dc_charging_time": "DC charging time (with % window)",
        "dc_charging_window_pct": "DC charging percentage window",
        "ac_max_charging_power_kw": "Max AC charging power (kW)",
        "dc_max_charging_power_kw": "Max DC charging power (kW)",
    },
    "dimensions": {
        "length_mm": "Length (mm)",
        "width_mm": "Width (mm)",
        "height_mm": "Height (mm)",
        "wheelbase_mm": "Wheelbase (mm)",
        "ground_clearance_mm": "Ground clearance (mm)",
        "cargo_volume_l": "Cargo volume (l)",
        "fuel_tank_l": "Fuel tank (l)",
    },
    "transmission": {
        "gearbox_type": "Gearbox type",
        "gear_count": "Number of gears",
    },
    "multimedia": {
        "screen_size_in": "Main screen size (in)",
        "apple_carplay": "Apple CarPlay",
        "android_auto": "Android Auto",
        "wireless_phone_projection": "Wireless CarPlay / Android Auto",
    },
    "comfort": {
        "power_seats": "Power seats",
        "heated_seats": "Heated seats",
        "ventilated_seats": "Ventilated seats",
        "climate_zones": "Climate control zones",
        "sunroof_panoramic": "Sunroof / panoramic roof",
        "other_comfort_features": "Other comfort features",
    },
    "tires_wheels": {
        "rim_diameter_in": "Rim diameter (in)",
        "tire_size_front": "Front tire size",
        "tire_size_rear": "Rear tire size",
        "alternative_tire_sizes": "Alternative tire sizes",
    },
    "commercial": {
        "list_price": "List price (with currency)",
        "vehicle_warranty": "Vehicle warranty",
        "battery_hybrid_warranty": "Battery / hybrid system warranty",
        "warranty_km": "Warranty km",
        "warranty_years": "Warranty years",
    },
}

LEVEL3_TOPICS: dict[str, str] = {
    "known_issues_reliability": "Known issues and reliability by model/year",
    "ownership_costs": "Total ownership costs",
    "maintenance": "Maintenance schedule and costs",
    "insurance": "Insurance cost indications",
    "depreciation": "Depreciation / resale value",
    "recalls": "Recalls",
}


def target_field_names(include_electric: bool = True) -> list[str]:
    names: list[str] = []
    for group, fields in LEVEL2_TARGET_FIELDS.items():
        if group == "electric_hybrid" and not include_electric:
            continue
        names.extend(fields)
    return names


# --- Tool definitions handed to GLM (OpenAI-compatible function format) -------

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SPECS: list[dict] = [
    _fn(
        "search_web",
        "General web search. Returns titles, URLs and snippets with no reliability ranking.",
        {
            "query": {"type": "string", "description": "Search query, any language."},
            "max_results": {"type": "integer", "description": "1-20, default 8."},
            "domain": {"type": "string", "description": "Optional: restrict to one domain."},
        },
        ["query"],
    ),
    _fn(
        "search_official_domains",
        "Same as search_web but biased to manufacturer/importer domains. Convenience only: "
        "it does not make other sources less acceptable. Omit domains to use defaults for "
        "this vehicle's manufacturer.",
        {
            "query": {"type": "string"},
            "domains": {"type": "array", "items": {"type": "string"}, "description": "Optional list of domains."},
        },
        ["query"],
    ),
    _fn(
        "fetch_url",
        "Download a URL (HTML, JSON, text or PDF). Stores status, headers, content-type, final URL "
        "and body; returns a document_id plus a text preview.",
        {"url": {"type": "string"}},
        ["url"],
    ),
    _fn(
        "fetch_pdf",
        "Download a PDF; stores bytes, extracted text and metadata. Returns a document_id.",
        {"url": {"type": "string"}},
        ["url"],
    ),
    _fn(
        "render_page",
        "Render a JS/SPA page in a headless browser; returns a document_id, visible text preview "
        "and links after rendering.",
        {
            "url": {"type": "string"},
            "wait_ms": {"type": "integer", "description": "Extra wait after load, default 2500."},
        },
        ["url"],
    ),
    _fn(
        "extract_html",
        "Extract headings, lists, links and readable text from a stored document. Page with offset.",
        {
            "document_id": {"type": "string"},
            "offset": {"type": "integer", "description": "Text offset to continue from, default 0."},
            "max_chars": {"type": "integer", "description": "Default 10000."},
        },
        ["document_id"],
    ),
    _fn(
        "extract_tables",
        "Extract tables (and definition-list spec grids) from a stored HTML or PDF document as rows/columns.",
        {
            "document_id": {"type": "string"},
            "max_tables": {"type": "integer", "description": "Default 15."},
        },
        ["document_id"],
    ),
    _fn(
        "find_in_document",
        "Focused search inside a stored document. scope: 'text' (visible text, default), "
        "'source' (raw HTML/JSON source) or 'structured' (embedded JSON data).",
        {
            "document_id": {"type": "string"},
            "query": {"type": "string"},
            "scope": {"type": "string", "description": "text | source | structured"},
            "context_chars": {"type": "integer", "description": "Default 300."},
        },
        ["document_id", "query"],
    ),
    _fn(
        "get_structured_data",
        "Extract JSON-LD, Next.js/Nuxt data, embedded application/json, window state objects and "
        "meta tags from a stored HTML document.",
        {"document_id": {"type": "string"}},
        ["document_id"],
    ),
    _fn(
        "get_cached_document",
        "Return an already-downloaded document by document_id or URL without fetching again.",
        {
            "key": {"type": "string", "description": "document_id or URL."},
            "offset": {"type": "integer"},
            "max_chars": {"type": "integer"},
        },
        ["key"],
    ),
    _fn(
        "store_evidence",
        "Record what you consider evidence for a value: field, value, source URL, quote/fragment "
        "and your note. This is a log, not a verifier. Returns an evidence_id to cite in your answer.",
        {
            "field": {"type": "string"},
            "value": {"type": "string", "description": "Value as found (numbers may be given as numbers)."},
            "unit": {"type": "string"},
            "source_url": {"type": "string"},
            "document_id": {"type": "string"},
            "quote": {"type": "string", "description": "Verbatim fragment from the source."},
            "note": {"type": "string"},
        },
        ["field", "value"],
    ),
]

TOOL_NAMES = [spec["function"]["name"] for spec in TOOL_SPECS]


# --- Lenient output parsing ---------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def parse_model_output(text: str | None) -> tuple[Any | None, str]:
    """Best-effort JSON extraction. Returns (parsed, note). Never raises, never rejects."""
    if not text or not text.strip():
        return None, "empty"
    candidates = [text.strip()]
    candidates += [m.strip() for m in _FENCE.findall(text)]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate), "ok"
        except (ValueError, TypeError):
            continue
    return None, "not_json"


def iter_fields(output: Any) -> list[tuple[str, dict]]:
    """Normalize the model's `fields` into (name, entry) pairs for display.

    Accepts {name: {value, unit, ...}}, {name: bare_value} or a list of entries
    carrying a `field`/`name` key. Anything else yields nothing (it is still
    shown in the raw JSON view).
    """
    if not isinstance(output, dict):
        return []
    fields = output.get("fields")
    pairs: list[tuple[str, dict]] = []
    if isinstance(fields, dict):
        for name, entry in fields.items():
            pairs.append((str(name), entry if isinstance(entry, dict) else {"value": entry}))
    elif isinstance(fields, list):
        for i, entry in enumerate(fields):
            if isinstance(entry, dict):
                name = entry.get("field") or entry.get("name") or f"item_{i}"
                pairs.append((str(name), entry))
    return pairs


def has_value(entry: dict) -> bool:
    if "value" not in entry and "values" in entry:
        value = entry.get("values")
    else:
        value = entry.get("value")
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, (list, dict)) and not value:
        return False
    return True
