"""Open-data snapshot builds: the headers the builders are tested against.

PROVENANCE (read before trusting a header here). The build host of this PR had no network access to any source (the
egress proxy refused discodata.eea.europa.eu, data.ademe.fr, fueleconomy.gov and open.canada.ca), so NOTHING here was
recorded from a live source by the author. Each header below says where it comes from:

    EEA_2018_HEADER        verbatim from the reviewer's brief (DISCODATA `SELECT TOP 1 * ... WHERE year=2018 AND
                           status='F'`, verified by the reviewer against the live source on 2026-10-06)
    ADEME_ORIGINAL_NAMES   the original names the reviewer's brief lists from the live field schema (the brief elides
                           the rest with "..."); the Min / Max split of the "Min/Max" names is this PR's reading of the
                           brief's map table, NOT verified
    EPA_HEADER             the map's own spellings plus the luggage / passenger / range columns the brief names
                           (lv2, lv4, hlv, pv2, pv4, hpv; range, rangeA, combE); the full live header is longer
    NRCAN_*, CVS_*         SYNTHETIC: the brief gives only the file names; these exercise the per-file / per-group
                           logic and are not the live headers

Row VALUES are synthetic everywhere. The live build (and the real headers) is the operator's step before merge.
"""

from __future__ import annotations

EEA_2018_HEADER = [
    "ID", "MS", "Mp", "VFN", "Mh", "Man", "MMS", "TAN", "T", "Va", "Ve", "Mk", "Cn", "Ct", "Cr", "M (kg)", "Mt",
    "Enedc (g/km)", "Ewltp (g/km)", "W (mm)", "At1 (mm)", "At2 (mm)", "Ft", "Fm", "Ec (cm3)", "Ep (KW)", "Z (Wh/km)",
    "IT", "Ernedc (g/km)", "Erwltp (g/km)", "De", "Vf", "R", "Year", "Status", "Version_file", "E (g/km)", "Er (g/km)",
    "Zr", "Dr", "Fc",
]

ADEME_ORIGINAL_NAMES = [
    "Marque", "Libellé modèle", "Modèle", "Groupe", "Description Commerciale", "Energie", "Carrosserie", "Cylindrée",
    "Gamme", "Puissance fiscale", "Puissance maximale", "Puissance nominale électrique", "Poids à vide",
    "Rapport poids-puissance", "Type de boite", "Nombre rapports", "Conso vitesse mixte Min", "Conso vitesse mixte Max",
    "Conso elec Min", "Conso elec Max", "Autonomie elec Min", "Autonomie elec Max", "Autonomie elec urbain Min",
    "Autonomie elec urbain Max", "CO2 vitesse mixte Min", "CO2 vitesse mixte Max", "Masse OM Min", "Masse OM Max",
    "Prix véhicule",
]

EPA_HEADER = [
    "id", "year", "make", "model", "baseModel", "displ", "cylinders", "trany", "drive", "VClass", "fuelType", "comb08",
    "lv2", "lv4", "hlv", "pv2", "pv4", "hpv", "range", "rangeA", "combE",
]

# SYNTHETIC (see the module docstring)
NRCAN_CONVENTIONAL_HEADER = ["Model year", "Make", "Model", "Vehicle class", "Engine size (L)", "Cylinders",
                             "Transmission", "Fuel type", "Combined (L/100 km)"]
NRCAN_BEV_HEADER = ["Model year", "Make", "Model", "Vehicle class", "Motor (kW)", "Range (km)"]
CVS_HEADER = ["MYR", "MAKE", "MODEL", "OL", "OW", "OH", "WB", "CW"]
CVS_DICTIONARY_ROWS = [
    ["Code", "Description"], ["MYR", "Model year"], ["MAKE", "Make"], ["MODEL", "Model"],
    ["OL", "Overall length (cm)"], ["OW", "Overall width (cm)"], ["OH", "Overall height (cm)"],
    ["WB", "Wheelbase (cm)"], ["CW", "Curb weight (kg)"],
]


def eea_row(header=EEA_2018_HEADER, **values):
    """One DISCODATA row: every column of the header, synthetic values."""
    row = {column: None for column in header}
    row.update({"Mk": "CADILLAC", "Cn": "CTS", "T": "e4", "Va": "AL", "Ve": "A1AK1", "Ft": "petrol", "Fm": "M",
                "Ec (cm3)": 1998, "Ep (KW)": 203, "Enedc (g/km)": 172, "W (mm)": 2910, "At1 (mm)": 1560,
                "At2 (mm)": 1590, "M (kg)": 1734, "Mt": 1810, "R": 3, "Year": 2018, "Status": "F"})
    row.update(values)
    return {k: v for k, v in row.items() if k in header}


def csv_text(header: list[str], rows: list[list], delimiter: str = ",") -> bytes:
    lines = [delimiter.join(header)] + [delimiter.join("" if v is None else str(v) for v in row) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")
