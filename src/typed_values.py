"""Conservative typed representation of an evidence value (stored next to the value as given).

    scalar   {"type": "scalar", "value": 43, "unit": "l"}
    range    {"type": "range", "min": 581, "max": 588, "unit": "l", "condition": null}
    boolean  {"type": "boolean", "value": false}
    enum     {"type": "enum", "value": "e-cvt"}
    text     {"type": "text", "value": "Business"}
    compound {"type": "compound", "parts": [{"type": "scalar", "value": 5, "unit": "years"},
                                            {"type": "scalar", "value": 100000, "unit": "km"}]}

`value` itself stays exactly as stored before (backward compatible: "581-588" is still the display/raw
value); `typed_value` is what deterministic comparison, containment and conflict classification read.
A condition is kept only when the source states it; this module never invents one.
"""

from __future__ import annotations

import re
from typing import Any

from .candidate_harvest import parse_number

_NUM = r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:[.,]\d+)?"
RANGE = re.compile(rf"^\s*(?:from\s+|מ-?\s*)?({_NUM})\s*(?:-|–|—|to|עד|~)\s*({_NUM})\s*([^\d()\[\]]*?)\s*"
                   rf"(?:[(\[]\s*([^)\]]+?)\s*[)\]])?\s*$", re.I)
SCALAR = re.compile(rf"^\s*({_NUM})\s*([^\d()\[\]]*?)\s*(?:[(\[]\s*([^)\]]+?)\s*[)\]])?\s*$")
TRUE_WORDS = {"true", "yes", "y", "כן", "יש", "standard", "סטנדרט", "סטנדרטי", "included", "כלול", "קיים", "✓", "✔",
              "●", "1"}
FALSE_WORDS = {"false", "no", "n", "לא", "אין", "ללא", "not available", "none", "לא קיים", "לא זמין", "✗", "✘", "0",
               "-"}


def _number(text: str) -> float | None:
    return parse_number(text.lstrip("-")) if text else None


def _num_out(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(value, 4)


def as_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    return None


def numbers_in(value: Any) -> list[float]:
    """Every number in a value ('5 years / 100,000 km' -> [5, 100000])."""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    out = []
    for m in re.finditer(rf"(?<![\w.,])({_NUM.replace('-?', '')})(?![\d])", str(value or "")):
        n = parse_number(m.group(1))
        if n is not None:
            out.append(n)
    return out


def typed_value(value: Any, *, unit: str | None = None, value_type: str | None = None, matcher: str | None = None,
                condition: str | None = None) -> dict:
    """Typed form of a stored value. Never raises; unknown shapes become text."""
    cond = condition or None
    if value_type == "boolean" or isinstance(value, bool):
        parsed = as_boolean(value)
        if parsed is not None:
            return {"type": "boolean", "value": parsed}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"type": "scalar", "value": _num_out(float(value)), "unit": unit, "condition": cond}
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    if value_type == "enum" or matcher in ("enum", "gearbox") and value_type != "number":
        return {"type": "enum", "value": text.lower()}
    if value_type == "number" or matcher in ("numeric", "price"):
        m = RANGE.match(text)
        if m:
            low, high = _number(m.group(1)), _number(m.group(2))
            if low is not None and high is not None and low <= high:
                return {"type": "range", "min": _num_out(low), "max": _num_out(high),
                        "unit": unit or (m.group(3) or "").strip() or None, "condition": cond or m.group(4)}
        m = SCALAR.match(text)
        if m and _number(m.group(1)) is not None:
            return {"type": "scalar", "value": _num_out(_number(m.group(1))),
                    "unit": unit or (m.group(2) or "").strip() or None, "condition": cond or m.group(3)}
    numbers = numbers_in(text)
    if matcher in ("warranty",) and len(numbers) >= 2:
        parts = []
        for m in re.finditer(rf"({_NUM.replace('-?', '')})\s*([^\d/,;]*)", text):
            n = parse_number(m.group(1))
            if n is not None:
                parts.append({"type": "scalar", "value": _num_out(n), "unit": m.group(2).strip(" /") or None})
        return {"type": "compound", "parts": parts, "text": text}
    return {"type": "text", "value": text}


def range_contains(rng: dict, scalar: dict) -> bool | None:
    """Is a scalar inside a range (same unit, or one side unitless)? None when not comparable."""
    if rng.get("type") != "range" or scalar.get("type") != "scalar":
        return None
    u1, u2 = (rng.get("unit") or "").lower(), (scalar.get("unit") or "").lower()
    if u1 and u2 and u1 != u2:
        return None
    return rng["min"] <= scalar["value"] <= rng["max"]
