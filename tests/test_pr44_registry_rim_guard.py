"""Regression tests for PR #44 government-registry rim emission."""

from src.gov_registry import facts


def _axle(size: str, share: float, n: int = 50) -> dict:
    return {
        "majority": size,
        "share": share,
        "n": n,
        "top": [{"size": size, "n": round(n * share)}],
    }


def test_rim_requires_both_axles_to_pass_confidence_thresholds():
    rows = facts({
        "front": _axle("255/45 R20", 0.90),
        "rear": _axle("255/45 R20", 0.70),
    })

    fields = [row["field"] for row in rows]

    assert "tire_size_front" in fields
    assert "tire_size_rear" not in fields
    assert "alternative_tire_sizes" in fields
    assert "rim_diameter_in" not in fields


def test_rim_requires_both_axles_to_be_present():
    rows = facts({
        "front": _axle("255/45 R20", 0.90),
    })

    fields = [row["field"] for row in rows]

    assert fields == ["tire_size_front", "rim_diameter_front_in"]      # D2: the reliable axle's own rim only
    assert "rim_diameter_in" not in fields
