import json
import re
from json import JSONDecodeError
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from services import llm

SNIPPET_MAX_LENGTH = 500


class ItineraryResult(NamedTuple):
    data: Dict[str, Any]
    raw: str
    error: Optional[str]
    warnings: List[str]

SCHEMA_TEXT = r"""
Return ONLY JSON (no prose, no markdown) with ALL of these top-level keys:
{
  "trip_summary": {
    "destination": "...",
    "days": 5,  // NUMBER of days
    "traveler_type": "...",
    "budget": "...",
    "pace": "...",
    "high_level_focus": ["..."]
  },
  "days": [  // REQUIRED array with one object per day
    {
      "day": 1,
      "title": "Day 1 title",
      "morning": [ {"activity": "...", "area": "...", "duration_est": "...", "notes": "...", "sources": [{"title":"...","domain":"...","url":"..."}]} ],
      "afternoon": [ {"activity": "...", "area": "...", "duration_est": "...", "notes": "...", "sources": [{"title":"...","domain":"...","url":"..."}]} ],
      "evening": [ {"activity": "...", "area": "...", "duration_est": "...", "notes": "...", "sources": [{"title":"...","domain":"...","url":"..."}]} ],
      "notes": "",
      "plan_b": [ {"reason": "rain|low_energy|crowded", "alternative": "...", "sources": [{"title":"...","domain":"...","url":"..."}]} ],
      "food": [ {"name": "...", "type": "breakfast|lunch|dinner|snack", "budget": "low|mid|high", "area": "...", "diet_fit": ["kosher|vegan|halal|gluten-free|none"], "sources": [{"title":"...","domain":"...","url":"..."}]} ],
      "sources": [{"title":"...","domain":"...","url":"..."}]
    }
  ],
  "alternatives": [ {"destination": "...", "why": "...", "sources": [{"title":"...","domain":"...","url":"..."}]} ],
  "lodging": [ {"name": "...", "type": "hotel|hostel|apartment", "area": "...", "why": "...", "price_note": "...", "pros": ["..."], "cons": ["..."], "sources": [{"title":"...","domain":"...","url":"..."}]} ],
  "food": [],
  "transport": {}
}
Rules: trip_summary.days is a NUMBER, but top-level "days" MUST be an ARRAY of day objects. Always include all required top-level keys even if empty arrays/objects. JSON only, no markdown. Include citations from grounded search. If data is uncertain, note it in assumptions. Cap activities to 2-3 per day. Keep neighborhoods consistent.
"""


def _system_prompt() -> str:
    return (
        "You are TripPilot, a travel planner. Use Google Search tool for grounding; include "
        "source titles and domains. Never hallucinate URLs. If a tool fails, note that in assumptions. "
        "Never provide illegal, unsafe, or guaranteed claims. "
        "Always follow the schema exactly. " + SCHEMA_TEXT
    )


def _build_user_prompt(profile: Dict[str, Any], destination: str, refinement: Optional[str]) -> str:
    base = [
        f"Primary destination: {destination}",
        f"Days: {profile.get('days')}",
        f"Traveler type: {profile.get('traveler_type')}",
        f"Budget: {profile.get('budget')}",
        f"Pace: {profile.get('pace')}",
        f"Mobility: {profile.get('mobility')}",
        f"Interests: {', '.join(profile.get('interests', [])) or 'unspecified'}",
        f"Lodging preference: {profile.get('lodging')}",
        f"Dietary/accessibility constraints: {profile.get('constraints') or 'none'}",
        f"Open to alternatives: {profile.get('open_to_alternatives')}",
    ]
    if profile.get("dates"):
        base.append(f"Dates: {profile['dates']}")
    instructions = (
        "Generate a realistic day-by-day itinerary with morning/afternoon/evening blocks, "
        "restaurants matched to dietary and budget, lodging options with pros/cons and neighborhoods, "
        "and plan B options per day. Provide 2-3 alternatives destinations if open_to_alternatives. "
        "Ensure transit notes are brief and plausible. Top-level keys must exactly match the schema and "
        "include trip_summary, days (array of day objects), alternatives, lodging, food, and transport. "
        "Respond with JSON only."
    )
    if refinement:
        instructions += f" Apply this refinement request: {refinement}. Only adjust necessary parts."
    return "\n".join(base + [instructions])


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        if text.endswith("```"):
            text = text[: -3].strip()
    return text


def _ensure_dict(obj: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    if isinstance(obj, dict):
        return obj, None
    if isinstance(obj, str):
        try:
            inner = json.loads(obj.strip())
            if isinstance(inner, dict):
                return inner, None
        except (JSONDecodeError, ValueError):
            pass
    return {}, f"Invalid JSON type: expected object, got {type(obj).__name__}"


def _try_parse_json_object(text: str) -> Tuple[Optional[Any], Optional[str]]:
    decoder = json.JSONDecoder()
    last_error: Optional[str] = None
    for match in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text, idx=match.start())
            return obj, None
        except JSONDecodeError as err:
            last_error = str(err)
            continue
        except ValueError as err:  # pragma: no cover - defensive
            last_error = str(err)
            continue
    return None, last_error


def _extract_json(text: str) -> Tuple[Dict[str, Any], Optional[str]]:
    cleaned = _strip_code_fences(text)
    last_error: Optional[str] = None

    parsed: Any = None
    try:
        parsed = json.loads(cleaned)
    except (JSONDecodeError, ValueError) as err:
        last_error = str(err)

    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed.strip())
        except (JSONDecodeError, ValueError) as err:  # pragma: no cover - defensive
            last_error = str(err)

    if isinstance(parsed, dict):
        return parsed, None

    if parsed is not None:
        ensured, err = _ensure_dict(parsed)
        if err is None:
            return ensured, None
        last_error = err

    fallback, err = _try_parse_json_object(cleaned)
    if fallback is not None:
        ensured, ensure_err = _ensure_dict(fallback)
        if ensure_err is None:
            return ensured, None
        last_error = ensure_err
    elif err:
        last_error = err

    snippet = cleaned[:SNIPPET_MAX_LENGTH]
    last_error = last_error or "No JSON object found"
    return {}, f"Invalid JSON returned from model. Last error: {last_error}. Snippet: {snippet}"


def _ensure_list(obj: Any) -> List[Any]:
    return obj if isinstance(obj, list) else []


def _ensure_sources(container: Dict[str, Any]) -> None:
    sources = container.get("sources")
    if isinstance(sources, list):
        container["sources"] = [s for s in sources if isinstance(s, dict)]
    else:
        container["sources"] = []


def _clean_list_of_dicts(items: Any) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return cleaned
    for entry in items:
        if isinstance(entry, dict):
            _ensure_sources(entry)
            cleaned.append(entry)
    return cleaned


def normalize_itinerary(itin: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], Optional[str]]:
    warnings: List[str] = []
    if not isinstance(itin, dict):
        return {}, warnings, "invalid_root_type"

    trip_summary = itin.get("trip_summary")
    if not isinstance(trip_summary, dict):
        return {}, warnings, "trip_summary_missing_or_not_object"

    days = itin.get("days")
    normalized_days: List[Dict[str, Any]] = []
    if not isinstance(days, list):
        ts_days = trip_summary.get("days")
        if isinstance(ts_days, int) and ts_days > 0:
            for i in range(1, ts_days + 1):
                normalized_days.append(
                    {
                        "day": i,
                        "title": f"Day {i}",
                        "morning": [],
                        "afternoon": [],
                        "evening": [],
                        "notes": "",
                        "plan_b": [],
                        "food": [],
                        "sources": [],
                    }
                )
            warnings.append("MISSING_DAY_BY_DAY_PLAN")
        else:
            return {}, warnings, "MISSING_TOP_LEVEL_DAYS_LIST"
    else:
        for idx, day in enumerate(days):
            if not isinstance(day, dict):
                warnings.append(f"DAY_NOT_OBJECT_{idx + 1}")
                continue
            normalized_day = {
                "day": day.get("day", idx + 1),
                "title": day.get("title") or day.get("theme") or f"Day {idx + 1}",
                "morning": _clean_list_of_dicts(day.get("morning")),
                "afternoon": _clean_list_of_dicts(day.get("afternoon")),
                "evening": _clean_list_of_dicts(day.get("evening")),
                "notes": day.get("notes", ""),
                "plan_b": _clean_list_of_dicts(day.get("plan_b")),
                "food": _clean_list_of_dicts(day.get("food")),
                "sources": [],
                "blocks": _clean_list_of_dicts(day.get("blocks")),
            }
            _ensure_sources(normalized_day)
            normalized_days.append(normalized_day)
        if not normalized_days:
            return {}, warnings, "MISSING_TOP_LEVEL_DAYS_LIST"
    itin["days"] = normalized_days

    for key in ["alternatives", "lodging", "food"]:
        val = itin.get(key)
        if val is None:
            itin[key] = []
            continue
        if not isinstance(val, list):
            warnings.append(f"{key.upper()}_NOT_LIST")
            itin[key] = []
        else:
            cleaned_items = []
            for item in val:
                if not isinstance(item, dict):
                    warnings.append(f"{key.upper()}_ITEM_INVALID")
                    continue
                _ensure_sources(item)
                cleaned_items.append(item)
            itin[key] = cleaned_items

    transport = itin.get("transport")
    if transport is None:
        itin["transport"] = {}
    elif not isinstance(transport, dict):
        warnings.append("TRANSPORT_NOT_OBJECT")
        itin["transport"] = {}

    assumptions = itin.get("assumptions")
    if assumptions is not None and not isinstance(assumptions, list):
        warnings.append("ASSUMPTIONS_NOT_LIST")
        itin["assumptions"] = []

    disclaimer = itin.get("disclaimer")
    if disclaimer is not None and not isinstance(disclaimer, str):
        warnings.append("DISCLAIMER_NOT_STRING")
        itin["disclaimer"] = ""

    if "days" in itin and isinstance(trip_summary.get("days"), int) is False:
        trip_summary["days"] = len(itin.get("days", []))

    return itin, warnings, None


def generate_itinerary(
    profile: Dict[str, Any],
    destination: str,
    refinement: Optional[str] = None,
    current_itinerary: Optional[Dict[str, Any]] = None,
) -> ItineraryResult:
    system_prompt = _system_prompt()
    user_prompt = _build_user_prompt(profile, destination, refinement)
    if current_itinerary:
        user_prompt += (
            "\n\nUpdate the following existing itinerary to satisfy the request. Keep schema intact and return the "
            "full updated itinerary JSON only:\n"
            f"{json.dumps(current_itinerary)}"
        )
    payload = {
        "destination": destination,
        "profile": profile,
        "refinement": refinement or "",
        "has_current_itinerary": bool(current_itinerary),
        "current_itinerary": current_itinerary or {},
    }
    text = llm.generate_json(system_prompt, user_prompt, payload)
    parsed, parse_error = _extract_json(text)
    warnings: List[str] = []
    if not parse_error:
        parsed, warnings, parse_error = normalize_itinerary(parsed)
    if parse_error:
        parsed = parsed if isinstance(parsed, dict) else {}
    return ItineraryResult(parsed, text, parse_error, warnings)
