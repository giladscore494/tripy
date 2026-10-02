"""The field dictionary in data/enrichment_fields.json: complete for all 45 default Level-2 fields, Hebrew
labels for the UI, and representative positive / negative phrases through the generic matchers."""

import json
import re

import pytest

from src.candidate_harvest import Dictionary, harvest_text
from src.fields import (DICTIONARY_KEYS, field_display_name, group_display_name, load_schema, public_spec,
                        resolve_requested_fields, schema_document)

HEBREW = re.compile(r"[א-ת]")
DEFAULT_45 = [
    "torque_nm", "acceleration_0_100_s", "top_speed_kmh", "fuel_consumption_combined_l_100km",
    "energy_consumption_kwh_100km", "battery_gross_kwh", "battery_usable_kwh", "electric_range_km",
    "electric_range_standard", "ac_charging_time", "dc_charging_time", "dc_charging_window_pct",
    "ac_max_charging_power_kw", "dc_max_charging_power_kw", "length_mm", "width_mm", "height_mm", "wheelbase_mm",
    "curb_weight_kg", "ground_clearance_mm", "cargo_volume_l", "fuel_tank_l", "gearbox_type", "gear_count",
    "screen_size_in", "apple_carplay", "android_auto", "wireless_phone_projection", "power_seats", "heated_seats",
    "ventilated_seats", "climate_zones", "sunroof_panoramic", "other_comfort_features", "rim_diameter_in",
    "tire_size_front", "tire_size_rear", "alternative_tire_sizes", "local_trim_name", "list_price",
    "registration_licence_fee", "vehicle_warranty", "battery_hybrid_warranty", "warranty_km", "warranty_years"]
EXPECTED_HE = {
    "torque_nm": "מומנט מרבי", "acceleration_0_100_s": "תאוצה 0–100 קמ״ש", "battery_gross_kwh": "קיבולת סוללה ברוטו",
    "energy_consumption_kwh_100km": "צריכת חשמל", "electric_range_standard": "תקן טווח חשמלי",
    "apple_carplay": "תמיכה ב־Apple CarPlay", "warranty_km": "אחריות — קילומטראז׳", "warranty_years": "אחריות — שנים",
    "local_trim_name": "שם רמת הגימור בישראל", "sunroof_panoramic": "גג שמש / גג פנורמי"}
GROUPS_HE = {"performance": "ביצועים וצריכה", "electric_hybrid": "סוללה וטעינה", "dimensions": "מידות ומשקל",
             "transmission": "תיבת הילוכים", "multimedia": "מולטימדיה וקישוריות", "comfort": "נוחות",
             "tires_wheels": "גלגלים וצמיגים", "commercial": "מידע מסחרי ואחריות"}
UNIT_MATCHERS = ("numeric", "price")


@pytest.fixture(scope="module")
def specs():
    return load_schema()


def test_all_45_canonical_fields_are_unchanged_and_fully_described(specs):
    assert [s["name"] for s in specs] == DEFAULT_45
    labels = [s.get("display_name_he") for s in specs]
    assert all(label and label.strip() and HEBREW.search(label) for label in labels)
    assert len(set(labels)) == 45                                            # no duplicate labels
    for name, label in EXPECTED_HE.items():
        assert field_display_name(name) == label
    for spec in specs:
        assert any(HEBREW.search(a) for a in spec.get("aliases_he") or []), spec["name"]
        assert spec.get("aliases_en"), spec["name"]
        assert spec.get("value_type") in ("number", "boolean", "text", "enum"), spec["name"]
        assert spec.get("matcher"), spec["name"]
        if spec["matcher"] in UNIT_MATCHERS:
            assert spec.get("normalized_unit") and spec.get("accepted_unit_variants") and spec.get("expected_units"), \
                spec["name"]
        if spec.get("value_type") == "number" and spec["matcher"] == "numeric":
            assert spec.get("plausible_min") is not None and spec.get("plausible_max") is not None, spec["name"]
    for group, label in GROUPS_HE.items():
        assert group_display_name(group) == label
    assert {s["group"] for s in specs} == set(GROUPS_HE)


def test_dictionary_metadata_never_reaches_models_or_events(specs):
    torque = next(s for s in specs if s["name"] == "torque_nm")
    public = public_spec(torque)
    assert public["display_name_he"] == "מומנט מרבי" and "aliases_he" not in public
    assert not set(public) & set(DICTIONARY_KEYS)


def test_dictionary_counts(specs):
    """Sizes reported in the PR (also guards against an accidentally emptied dictionary)."""
    he = sum(len(s.get("aliases_he") or []) for s in specs)
    en = sum(len(s.get("aliases_en") or []) for s in specs)
    units = sum(len(s.get("accepted_unit_variants") or []) for s in specs)
    rules = sum(len(s.get(k) or []) for s in specs for k in ("positive_context_terms_he", "positive_context_terms_en",
                                                             "negative_context_terms_he", "negative_context_terms_en",
                                                             "exclusion_rules", "ambiguity_rules"))
    assert he >= 200 and en >= 200 and units >= 150 and rules >= 100, (he, en, units, rules)
    assert schema_document().get("harvest_vocabulary", {}).get("affirmative_values")


def run(text, specs, **kw):
    return {(c["field"], json.dumps(c["value"], ensure_ascii=False)) for c in harvest_text(text, specs, **kw)}


@pytest.mark.parametrize("text, expected", [
    ("מומנט מרבי 610 נ״מ", ("torque_nm", "610")),
    ("DC fast charging up to 190 kW", ("dc_max_charging_power_kw", "190")),
    ("33-inch LED display", ("screen_size_in", "33")),
    ("Battery capacity: 102 kWh", ("battery_gross_kwh", "102")),
    ("Energy consumption 236 Wh/km", ("energy_consumption_kwh_100km", "23.6")),
    ("Wheelbase 3,094 mm", ("wheelbase_mm", "3094")),
    ("Overall length 4.996 m", ("length_mm", "4996")),
    ("Tyres: front 255/45 R20", ("tire_size_front", '"255/45 R20"')),
    ("Vehicle warranty: 5 years / 150,000 km", ("vehicle_warranty", '"5 years / 150,000 km"')),
    ("9-speed automatic transmission", ("gear_count", "9")),
    ("9-speed automatic transmission", ("gearbox_type", '"automatic"')),
    ("תיבה אוטומטית 9 הילוכים", ("gear_count", "9")),
    ("Range: 502 km (EPA)", ("electric_range_standard", '"EPA"')),
    ("dual-zone climate control", ("climate_zones", "2")),
    ("10%-80% in 33 minutes with DC fast charging", ("dc_charging_window_pct", '"10-80"')),
    ("10%-80% in 33 minutes with DC fast charging", ("dc_charging_time", '"33 min"')),
    ("Standard equipment includes wireless Apple CarPlay", ("apple_carplay", "true")),
    ("מחיר: 399,990 ש\"ח", ("list_price", "399990")),
])
def test_representative_positive_phrases(specs, text, expected):
    assert expected in run(text, specs)


@pytest.mark.parametrize("text, forbidden", [
    ("190 kW electric motor", "dc_max_charging_power_kw"),
    ("Motor power 250 kW", "dc_max_charging_power_kw"),
    ("22-inch alloy wheels", "screen_size_in"),
    ("אחריות לסוללה 8 שנים או 160,000 ק״מ", "vehicle_warranty"),
    ("אחריות לסוללה 8 שנים או 160,000 ק״מ", "warranty_years"),
    ("Paint warranty 3 years", "vehicle_warranty"),
    ("513 km", "electric_range_standard"),
    ("513 km", "electric_range_km"),
    ("Torque 610 kg", "torque_nm"),
    ("3094", "wheelbase_mm"),
    ("Transmission: CVT", "gear_count"),
    ("Monthly payment from ₪3,990 per month", "list_price"),
    ("Apple CarPlay", "apple_carplay"),                       # a bare mention (menu / navigation) is no candidate
    ("Heated steering wheel", "heated_seats"),
    ("Length of the cargo floor 1,100 mm", "length_mm"),
    ("0-60 mph in 4.9 seconds", "acceleration_0_100_s"),
    ("Acceleration 0-60 mph: 4.9 s", "acceleration_0_100_s"),
    ("Battery 95 kWh usable", "battery_gross_kwh"),            # usable stated AFTER the value
    ("Battery capacity 95 kWh net", "battery_gross_kwh"),
    ("Energy consumption 6.1 l/100km", "energy_consumption_kwh_100km"),
    ("Combined fuel consumption 18.5 kWh/100km", "fuel_consumption_combined_l_100km"),
    ("Charging cable length 5 m", "length_mm"),
    ("Max towing weight 1,500 kg", "curb_weight_kg"),
    ("Consumption 22.5 kWh/100km (WLTP)", "electric_range_standard"),
])
def test_deterministic_false_positives_are_rejected(specs, text, forbidden):
    assert forbidden not in {field for field, _ in run(text, specs)}


def test_gross_and_usable_battery_stay_apart(specs):
    both = run("Battery capacity: 102 kWh (usable 95 kWh)", specs)
    assert ("battery_gross_kwh", "102") in both and ("battery_gross_kwh", "95") not in both
    assert ("battery_usable_kwh", "95") in run("Usable battery capacity 95 kWh", specs)


def test_battery_warranty_is_never_the_vehicle_warranty(specs):
    found = run("אחריות לסוללה 8 שנים או 160,000 ק״מ", specs)
    assert ("battery_hybrid_warranty", '"8 years / 160,000 km"') in found
    assert not {f for f, _ in found} & {"vehicle_warranty", "warranty_years", "warranty_km"}


def test_starting_price_keeps_its_type_hint(specs):
    cand = next(c for c in harvest_text("מחיר החל מ-399,990 ₪", specs) if c["field"] == "list_price")
    assert cand["price_type_hint"] == "starting_price" and cand["parser_confidence"] < 0.8


def test_a_new_field_needs_only_json(tmp_path, monkeypatch):
    """Ordinary fields are pure data: a new field with aliases/units is harvested with no Python change."""
    schema = {"fields": [{"name": "rear_legroom_mm", "group": "dimensions", "display_name_he": "מרווח רגליים אחורי",
                          "matcher": "numeric", "value_type": "number", "aliases_en": ["rear legroom"],
                          "aliases_he": ["מרווח רגליים אחורי"], "accepted_unit_variants": ["mm", "מ\"מ"],
                          "expected_units": ["mm"], "normalized_unit": "mm", "plausible_min": 500, "plausible_max": 1500},
                         {"name": "paint_code"}]}
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema, ensure_ascii=False), "utf-8")
    monkeypatch.setenv("ENRICHMENT_SCHEMA_PATH", str(path))
    specs = resolve_requested_fields(None)
    assert [s["name"] for s in specs] == ["rear_legroom_mm", "paint_code"]
    assert field_display_name("rear_legroom_mm") == "מרווח רגליים אחורי"
    assert ("rear_legroom_mm", "1030") in run("Rear legroom 1,030 mm", specs)
    assert len(Dictionary(specs).rules) == 1                   # a custom field without aliases is simply not harvested
    assert field_display_name("paint_code") == "paint_code"    # custom field without a label: canonical name
