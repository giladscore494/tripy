"""Open-data fixtures (no network). The build host of this PR could not reach the dataset hosts (EEA DISCODATA,
fueleconomy.gov, NRCan, Transport Canada, data.gouv.fr: denied by the environment's egress policy), so these rows are
SYNTHETIC canonical snapshot rows written from the values the manual Cadillac CTS 2018 benchmark (record 85095) and
E1 (record 23678) state: the row ids are the benchmark's (EPA 38704 / 38918 / 38921 / 38916 / 38919 / 38922, NRCan
lines 3457-3462, CVS lines 153-158), the values are the benchmark's where it states them (wheelbase 2910, CVS
4970 / 1830 / 1450 / curb 1642, EEA running-order mass 1734, EPA 25 mpg + Automatic (S8), NRCan 9.5 l/100km AS8,
EEA 1998 cc / 203 kW kept, 6162 cc / 477 kW and 172 kW rejected) and plausible placeholders elsewhere. The Kona rows
(record 29053, CREATIVE 64) stand for "one BEV of the benchmark": a 64 kWh / 150 kW configuration, a 65 kWh one at the
same 150 kW (another battery) and a 48 kWh / 115 kW one.
"""

from __future__ import annotations

CTS_EEA = [
    {"row_id": "eea-2018-cts-a", "make": "CADILLAC", "model": "CTS", "type_approval": "e4*2007/46*1102", "variant": "AL",
     "version": "A1AK1", "fuel": "petrol", "fuel_mode": "M", "displacement_cc": 1998, "power_kw": 203,
     "co2_wltp": None, "co2_nedc": 172, "wheelbase_mm": 2910, "mass_running_order_kg": 1734, "year": 2018},
    {"row_id": "eea-2019-cts-b", "make": "CADILLAC", "model": "CTS", "type_approval": "e4*2007/46*1102", "variant": "AL",
     "version": "A1AK2", "fuel": "petrol", "fuel_mode": "M", "displacement_cc": 1998, "power_kw": 203,
     "co2_wltp": None, "co2_nedc": 175, "wheelbase_mm": 2910, "mass_running_order_kg": 1734, "year": 2019},
    {"row_id": "eea-2018-cts-v", "make": "CADILLAC", "model": "CTS", "type_approval": "e4*2007/46*1102", "variant": "AV",
     "version": "A1AV1", "fuel": "petrol", "fuel_mode": "M", "displacement_cc": 6162, "power_kw": 477,
     "co2_wltp": None, "co2_nedc": 298, "wheelbase_mm": 2910, "mass_running_order_kg": 1920, "year": 2018},
    {"row_id": "eea-2018-cts-172", "make": "CADILLAC", "model": "CTS", "type_approval": "e4*2007/46*1102",
     "variant": "AL", "version": "A1AJ1", "fuel": "petrol", "fuel_mode": "M", "displacement_cc": 1998, "power_kw": 172,
     "co2_wltp": None, "co2_nedc": 170, "wheelbase_mm": 2910, "mass_running_order_kg": 1720, "year": 2018},
]

CTS_EPA = [
    {"row_id": "38704", "year": 2018, "make": "CADILLAC", "model": "CTS", "base_model": "CTS", "displacement_l": "2.0",
     "cylinders": 4, "transmission": "Automatic (S8)", "drive": "Rear-Wheel Drive", "vehicle_class": "Midsize Cars",
     "fuel": "Premium Gasoline", "combined_mpg": 25, "luggage_4door_ft3": 14},
    {"row_id": "38918", "year": 2018, "make": "CADILLAC", "model": "CTS AWD", "base_model": "CTS", "displacement_l": "2.0",
     "cylinders": 4, "transmission": "Automatic (S8)", "drive": "All-Wheel Drive", "vehicle_class": "Midsize Cars",
     "fuel": "Premium Gasoline", "combined_mpg": 24, "luggage_4door_ft3": 14},
    {"row_id": "38921", "year": 2018, "make": "CADILLAC", "model": "CTS", "base_model": "CTS", "displacement_l": "3.6",
     "cylinders": 6, "transmission": "Automatic (S8)", "drive": "Rear-Wheel Drive", "vehicle_class": "Midsize Cars",
     "fuel": "Regular Gasoline", "combined_mpg": 22, "luggage_4door_ft3": 14},
    {"row_id": "38916", "year": 2018, "make": "CADILLAC", "model": "CTS AWD", "base_model": "CTS", "displacement_l": "3.6",
     "cylinders": 6, "transmission": "Automatic (S8)", "drive": "All-Wheel Drive", "vehicle_class": "Midsize Cars",
     "fuel": "Regular Gasoline", "combined_mpg": 21, "luggage_4door_ft3": 14},
    {"row_id": "38919", "year": 2018, "make": "CADILLAC", "model": "CTS V-Sport", "base_model": "CTS",
     "displacement_l": "3.6", "cylinders": 6, "transmission": "Automatic (S8)", "drive": "Rear-Wheel Drive",
     "vehicle_class": "Midsize Cars", "fuel": "Premium Gasoline", "combined_mpg": 19, "luggage_4door_ft3": 14},
    {"row_id": "38922", "year": 2018, "make": "CADILLAC", "model": "CTS-V", "base_model": "CTS", "displacement_l": "6.2",
     "cylinders": 8, "transmission": "Automatic (S8)", "drive": "Rear-Wheel Drive", "vehicle_class": "Midsize Cars",
     "fuel": "Premium Gasoline", "combined_mpg": 16, "luggage_4door_ft3": 14},
]

CTS_NRCAN = [
    {"row_id": "nrcan-3457", "year": 2018, "make": "CADILLAC", "model": "CTS", "vehicle_class": "MID-SIZE",
     "displacement_l": "2.0", "cylinders": 4, "transmission": "AS8", "fuel": "Z", "combined_l_100km": 9.5},
    {"row_id": "nrcan-3458", "year": 2018, "make": "CADILLAC", "model": "CTS AWD", "vehicle_class": "MID-SIZE",
     "displacement_l": "2.0", "cylinders": 4, "transmission": "AS8", "fuel": "Z", "combined_l_100km": 10.1},
    {"row_id": "nrcan-3459", "year": 2018, "make": "CADILLAC", "model": "CTS", "vehicle_class": "MID-SIZE",
     "displacement_l": "3.6", "cylinders": 6, "transmission": "AS8", "fuel": "X", "combined_l_100km": 11.0},
    {"row_id": "nrcan-3460", "year": 2018, "make": "CADILLAC", "model": "CTS AWD", "vehicle_class": "MID-SIZE",
     "displacement_l": "3.6", "cylinders": 6, "transmission": "AS8", "fuel": "X", "combined_l_100km": 11.6},
    {"row_id": "nrcan-3461", "year": 2018, "make": "CADILLAC", "model": "CTS V-SPORT", "vehicle_class": "MID-SIZE",
     "displacement_l": "3.6", "cylinders": 6, "transmission": "AS8", "fuel": "Z", "combined_l_100km": 12.4},
    {"row_id": "nrcan-3462", "year": 2018, "make": "CADILLAC", "model": "CTS-V", "vehicle_class": "MID-SIZE",
     "displacement_l": "6.2", "cylinders": 8, "transmission": "A8", "fuel": "Z", "combined_l_100km": 14.8},
]

CTS_CVS = [
    {"row_id": "cvs-153", "year": 2018, "make": "CADILLAC", "model": "CTS 4DR SEDAN 2.0L RWD", "length_cm": 497.0,
     "width_cm": 183.0, "height_cm": 145.0, "wheelbase_cm": 291.0, "curb_weight_kg": 1642},
    {"row_id": "cvs-154", "year": 2018, "make": "CADILLAC", "model": "CTS 4DR SEDAN 2.0L AWD", "length_cm": 497.0,
     "width_cm": 183.0, "height_cm": 145.0, "wheelbase_cm": 291.0, "curb_weight_kg": 1720},
    {"row_id": "cvs-155", "year": 2018, "make": "CADILLAC", "model": "CTS 4DR SEDAN 3.6L RWD", "length_cm": 497.0,
     "width_cm": 183.0, "height_cm": 145.0, "wheelbase_cm": 291.0, "curb_weight_kg": 1700},
    {"row_id": "cvs-156", "year": 2018, "make": "CADILLAC", "model": "CTS 4DR SEDAN 3.6L AWD", "length_cm": 497.0,
     "width_cm": 183.0, "height_cm": 145.0, "wheelbase_cm": 291.0, "curb_weight_kg": 1780},
    {"row_id": "cvs-157", "year": 2018, "make": "CADILLAC", "model": "CTS VSPORT 4DR SEDAN 3.6L RWD", "length_cm": 497.0,
     "width_cm": 183.0, "height_cm": 145.0, "wheelbase_cm": 291.0, "curb_weight_kg": 1780},
    {"row_id": "cvs-158", "year": 2018, "make": "CADILLAC", "model": "CTS-V 4DR SEDAN 6.2L RWD", "length_cm": 502.0,
     "width_cm": 183.0, "height_cm": 145.0, "wheelbase_cm": 291.0, "curb_weight_kg": 1880},
]

CTS_ROWS = {"eea_co2_cars": CTS_EEA, "epa_fueleconomy": CTS_EPA, "nrcan_fuel_ratings": CTS_NRCAN, "tc_cvs": CTS_CVS}

M4_EEA = [
    {"row_id": "eea-2024-m4-31az", "make": "BMW", "model": "M4 COMPETITION", "type_approval": "e1*2007/46*1985",
     "variant": "31AZ", "version": "31AZ-2024", "fuel": "petrol", "fuel_mode": "M", "displacement_cc": 2993,
     "power_kw": 375, "co2_wltp": 223, "wheelbase_mm": 2857, "mass_running_order_kg": 1800,
     "fuel_consumption_l_100km": 9.8, "year": 2024},
    {"row_id": "eea-2024-m4-21hk", "make": "BMW", "model": "M4 COMPETITION", "type_approval": "e1*2007/46*1985",
     "variant": "21HK", "version": "21HK-2024", "fuel": "petrol", "fuel_mode": "M", "displacement_cc": 2993,
     "power_kw": 375, "co2_wltp": 221, "wheelbase_mm": 2857, "mass_running_order_kg": 1800,
     "fuel_consumption_l_100km": 9.7, "year": 2024},
]

KONA_EEA = [
    {"row_id": "eea-2024-kona-150", "make": "HYUNDAI", "model": "KONA", "variant": "SX2", "version": "EV150",
     "fuel": "electric", "fuel_mode": "E", "displacement_cc": None, "power_kw": 150, "co2_wltp": 0,
     "wheelbase_mm": 2660, "mass_running_order_kg": 1690, "energy_wh_km": 147, "electric_range_km": 514, "year": 2024},
    {"row_id": "eea-2024-kona-115", "make": "HYUNDAI", "model": "KONA", "variant": "SX2", "version": "EV115",
     "fuel": "electric", "fuel_mode": "E", "displacement_cc": None, "power_kw": 115, "co2_wltp": 0,
     "wheelbase_mm": 2660, "mass_running_order_kg": 1590, "energy_wh_km": 144, "electric_range_km": 377, "year": 2024},
]

KONA_ADEME = [
    {"row_id": "ademe-kona-64", "make": "HYUNDAI", "model": "KONA", "version": "KONA ELECTRIC 64 KWH CREATIVE",
     "fuel": "électrique", "power_kw": 150, "mass_running_order_kg": 1690, "energy_wh_km": 147,
     "electric_range_km": 514, "date": "2026-09"},
    {"row_id": "ademe-kona-65", "make": "HYUNDAI", "model": "KONA", "version": "KONA ELECTRIC 65 KWH LONG RANGE",
     "fuel": "électrique", "power_kw": 150, "mass_running_order_kg": 1700, "energy_wh_km": 149,
     "electric_range_km": 505, "date": "2026-09"},
    {"row_id": "ademe-kona-48", "make": "HYUNDAI", "model": "KONA", "version": "KONA ELECTRIC 48 KWH",
     "fuel": "électrique", "power_kw": 115, "mass_running_order_kg": 1590, "energy_wh_km": 144,
     "electric_range_km": 377, "date": "2026-09"},
]
