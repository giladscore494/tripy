from typing import Dict, Tuple


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
