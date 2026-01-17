from typing import Any, Dict, Optional, Tuple


def validate_profile(profile: Dict) -> Tuple[bool, str]:
    required = ["destination", "days", "traveler_type", "budget", "pace", "mobility"]
    for field in required:
        if not profile.get(field):
            return False, f"Missing required field: {field}"
    try:
        days = int(profile.get("days", 0))
        if days <= 0:
            return False, "Days must be a positive number."
    except ValueError:
        return False, "Days must be numeric."
    return True, ""


def validate_itinerary_schema(itinerary: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    if not isinstance(itinerary, dict):
        return False, "INVALID_ROOT_TYPE"
    if "trip_summary" not in itinerary:
        return False, "MISSING_TRIP_SUMMARY"
    trip_summary = itinerary.get("trip_summary")
    if not isinstance(trip_summary, dict):
        return False, "TRIP_SUMMARY_NOT_OBJECT"
    if "days" not in itinerary:
        return False, "MISSING_DAYS_LIST"
    if not isinstance(itinerary.get("days"), list):
        return False, "DAYS_NOT_LIST"
    return True, None
