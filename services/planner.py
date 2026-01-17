import json
import re
from json import JSONDecodeError
from typing import Any, Dict, NamedTuple, Optional, Tuple

from services import llm

SNIPPET_MAX_LENGTH = 500


class ItineraryResult(NamedTuple):
    data: Dict[str, Any]
    raw: str
    error: Optional[str]

SCHEMA_TEXT = r"""
Return ONLY JSON with this schema:
{
  "trip_summary": {
    "destination": "...",
    "days": 5,
    "traveler_type": "...",
    "budget": "...",
    "pace": "...",
    "high_level_focus": ["..."]
  },
  "alternatives": [
    {"destination": "...", "why": "...", "sources": [{"title":"...","domain":"...","url":"..."}]}
  ],
  "lodging": [
    {
      "name": "...",
      "type": "hotel|hostel|apartment",
      "area": "...",
      "why": "...",
      "price_note": "avoid exact prices unless verified; otherwise ranges",
      "pros": ["..."],
      "cons": ["..."],
      "sources": [{"title":"...","domain":"...","url":"..."}]
    }
  ],
  "days": [
    {
      "day": 1,
      "theme": "...",
      "blocks": [
        {
          "time": "morning|afternoon|evening",
          "activity": "...",
          "area": "...",
          "duration_est": "...",
          "transit_note": "brief, realistic assumption (e.g., 20–40 min rideshare)",
          "notes": "...",
          "sources": [{"title":"...","domain":"...","url":"..."}]
        }
      ],
      "food": [
        {
          "name": "...",
          "type": "breakfast|lunch|dinner|snack",
          "budget": "low|mid|high",
          "area": "...",
          "diet_fit": ["kosher-friendly|vegan|halal|gluten-free|none"],
          "booking_note": "optional",
          "sources": [{"title":"...","domain":"...","url":"..."}]
        }
      ],
      "plan_b": [
        {
          "reason": "rain|low_energy|crowded",
          "alternative": "...",
          "sources": [{"title":"...","domain":"...","url":"..."}]
        }
      ]
    }
  ],
  "assumptions": ["..."],
  "disclaimer": "..."
}
Rules: JSON only, no markdown. Include citations from grounded search. If data is uncertain, note it in assumptions. Cap activities to 2-3 per day. Keep neighborhoods consistent.
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
        "Ensure transit notes are brief and plausible."
    )
    if refinement:
        instructions += f" Apply this refinement request: {refinement}. Only adjust necessary parts."
    return "\n".join(base + [instructions])


def _strip_fences(text: str) -> str:
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        if text.endswith("```"):
            text = text[: -3].strip()
    return text


def _extract_json(text: str) -> Tuple[Dict[str, Any], Optional[str]]:
    cleaned = _strip_fences(text.strip())
    try:
        return json.loads(cleaned), None
    except (JSONDecodeError, ValueError) as err:
        last_error = str(err)

    decoder = json.JSONDecoder()
    for idx in range(len(cleaned)):
        try:
            obj, _ = decoder.raw_decode(cleaned, idx=idx)
            return obj, None
        except JSONDecodeError:
            continue
        except ValueError as err:  # pragma: no cover - defensive
            last_error = str(err)
            continue

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        snippet = cleaned[start : end + 1]
        try:
            return json.loads(snippet), None
        except (JSONDecodeError, ValueError) as err:
            last_error = str(err)

    snippet = cleaned[:SNIPPET_MAX_LENGTH]
    return {}, f"Invalid JSON returned from model. Last error: {last_error}. Snippet: {snippet}"


def generate_itinerary(
    profile: Dict[str, Any], destination: str, refinement: Optional[str] = None
) -> ItineraryResult:
    system_prompt = _system_prompt()
    user_prompt = _build_user_prompt(profile, destination, refinement)
    payload = {"destination": destination, "profile": profile, "refinement": refinement or ""}
    text = llm.generate_json(system_prompt, user_prompt, payload)
    parsed, parse_error = _extract_json(text)
    return ItineraryResult(parsed, text, parse_error)
