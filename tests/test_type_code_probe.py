"""Make coverage (the reviewed tozar of 2026-10-09) and the read-only type-code probe (MCP type_code_probe). No network:
a fake read-only query stands in for MILO and the EEA rows are fixture rows."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.mcp_server.safety import ToolInputError
from src.mcp_server.tools import Observer
from src.open_data import makes as mk
from src.open_data import type_code_probe as P

ROOT = Path(__file__).resolve().parent.parent

# --- M1: the reviewed tozar -> make entries ------------------------------------------------------------------------------

NEW_TOZAR = {
    "מ.ג": ["MG"], "צ'רי": ["CHERY"], "בי ווי די": ["BYD"], "בנטלי": ["BENTLEY"], "פרארי": ["FERRARI"],
    "סאנגיונג": ["SSANGYONG"], "קיי גי מוביליט": ["KGM", "SSANGYONG"], "סאאב": ["SAAB"], "קופרה": ["CUPRA"],
    "מיני": ["MINI"], "ניאו רכב": ["NIO"], "ניאו": ["NIO"], "למבורגיני": ["LAMBORGHINI"], "ג'אקו": ["JAECOO"],
    "אומודה": ["OMODA"], "אף אי דאבל יו": ["FAW"], "ליפמוטור": ["LEAPMOTOR"], "אורה": ["ORA", "GWM ORA"],
    "זיקר": ["ZEEKR"], "לינק אנד קו": ["LYNK & CO"], "רולס רויס": ["ROLLS-ROYCE"], "סקיוול": ["SKYWELL"],
    "וואי": ["WEY"], "אלפין": ["ALPINE"], "סרס": ["SERES"], "וויה": ["VOYAH"], "פולסטאר": ["POLESTAR"],
    "איווייס": ["AIWAYS"], "ג'אק": ["JAC"], "ג'יי איי סי": ["JAC"], "קארמה": ["KARMA"], "פורתינג": ["FORTHING"],
    "ארקפוקס": ["ARCFOX"], "איון": ["AION"], "גי.אי.סי": ["GAC"], "באייק": ["BAIC"], "אוואטר": ["AVATR"],
    "נטע": ["NETA"], "לוטוס": ["LOTUS"], "יודו": ["YUDO"], "לינקולן": ["LINCOLN"], "אל אי וי סי": ["LEVC"],
    "פונטיאק": ["PONTIAC"], "אינאוס": ["INEOS"], "ג'יי.אמ.סי": ["JMC"], "מורגן": ["MORGAN"], "איווקו": ["IVECO"],
    "אקס אי וי": ["XEV"], "מקלארן": ["MCLAREN"], "לנצ'יה": ["LANCIA"], "אס דאבל יו אמ": ["SWM"], "איי אם": ["IM"],
    "מרוטי סוזוקי": ["MARUTI SUZUKI"], "אסטון מרטין": ["ASTON MARTIN"],
}


@pytest.mark.parametrize("tozar,makes", sorted(NEW_TOZAR.items()))
def test_every_reviewed_tozar_maps_as_listed(tozar, makes):
    assert mk.tozar_makes(tozar) == (makes, [])
    assert all(mk.canonical_of(m) == m for m in makes)                 # each make is its own normalization key


def test_the_new_makes_share_no_normalization_key_with_another_make():
    keys: dict[str, set[str]] = {}
    for make in mk.canonical_makes():
        keys.setdefault(mk.normalize_make(make), set()).add(make)
    assert {k: v for k, v in keys.items() if len(v) > 1} == {}


@pytest.mark.parametrize("spelling,make", [
    ("MERCEDES-BENZ", "MERCEDES-BENZ"), ("MERCEDES BENZ", "MERCEDES-BENZ"), ("MERCEDES - BENZ", "MERCEDES-BENZ"),
    ("MERCEDES-BENZ AG", "MERCEDES-BENZ"), ("HYUNDAI MOTOR (ROK)", "HYUNDAI"),
    ("LYNK & CO", "LYNK & CO"), ("LYNK&CO", "LYNK & CO"), ("ROLLS ROYCE", "ROLLS-ROYCE"), ("MC LAREN", "MCLAREN"),
])
def test_normalize_make_already_folds_the_spelling_variants(spelling, make):
    assert mk.normalize_make(spelling) == mk.normalize_make(make)
    assert mk.canonical_of(spelling) == make


def test_the_catalogue_list_check_flags_an_unknown_tozar():
    spec = importlib.util.spec_from_file_location("build_make_aliases", ROOT / "scripts" / "build_make_aliases.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    result = script.generate({"מ.ג", "גמס", "מותג חדש"}, {}, {})
    assert result["review"]["not_in_canonical"] == ["מותג חדש"]          # neither mapped nor listed as unmapped
    assert result["review"]["unmapped"] == ["גמס"]
    assert result["aliases"]["מ.ג"] == ["MG"]


# --- MINI / CUPRA: by make alone for their own tozar only; model-gated under ב מ וו / סיאט exactly as before ----------------

def gate_keys(tozar, models, **override):
    from src.open_data.match import target_keys
    from test_open_data_match_fixes import _keys

    keys = _keys(type_code=None, co2_wltp=None, drivetrain=None, transmission=None, body=None, doors=None)
    return {**keys, "manufacturer": tozar, "makes": [m.upper() for m in mk.aliases().get(tozar) or []],
            "models": models, **override}


def make_row(n, make, model):
    return {"row_id": f"eea-2024-F-{n}", "make": make, "model": model, "variant": "", "version": "", "year": 2024,
            "fuel": "petrol", "power_kw": 100, "displacement_cc": 1499}


def candidate_ids(source_out):
    return sorted([r["row_id"] for r in source_out.get("survivors") or []]
                  + [r["row_id"] for r in source_out.get("vetoed") or []])


MINI_ROWS = [make_row(1, "BMW", "118I"), make_row(2, "MINI", "MINI COOPER S"), make_row(3, "MINI", "MINI"),
             make_row(4, "MINI", "MINI PACEMAN"), make_row(5, "MINI", "118I MINI")]
CUPRA_ROWS = [make_row(11, "SEAT", "LEON"), make_row(12, "CUPRA", "CUPRA FORMENTOR"),
              make_row(13, "CUPRA", "CUPRA TAVASCAN"), make_row(14, "CUPRA", "CUPRA")]


def test_the_own_tozar_only_makes_and_the_gated_tozar():
    assert mk.own_tozar_only_makes() == {"MINI", "CUPRA"}
    assert mk.tozar_gate("ב מ וו") is not None and mk.tozar_gate("סיאט") is not None
    assert mk.tozar_gate("מיני") is None and mk.tozar_gate("קופרה") is None
    assert mk.tozar_gate("דיימלר קרייזלר") is None and mk.tozar_gate("קרייזלר") is None   # unchanged: never gated here


def test_a_bmw_118i_row_never_gets_mini_candidates():
    from src.open_data.match import match_source

    out = match_source("eea_co2_cars", gate_keys("ב מ וו", ["118I"]), rows=MINI_ROWS)
    # 118I MINI names a ב מ וו family but is a MINI row: kept before only through the gate, and the gate holds
    assert [i for i in candidate_ids(out) if i != "eea-2024-F-1"] == ["eea-2024-F-5"]
    assert "eea-2024-F-3" not in candidate_ids(out) and "eea-2024-F-4" not in candidate_ids(out)
    # a ב מ וו row of the MINI family: only the MINI rows a ב מ וו family (COOPER, COUNTRYMAN, ...) names, never by make
    gated = match_source("eea_co2_cars", gate_keys("ב מ וו", ["MINI"]), rows=MINI_ROWS)
    assert candidate_ids(gated) == ["eea-2024-F-2", "eea-2024-F-5"]


def test_a_mini_row_gets_mini_candidates_by_make_alone():
    from src.open_data.match import match_source

    assert mk.aliases()["מיני"] == ["MINI"]
    out = match_source("eea_co2_cars", gate_keys("מיני", ["MINI"]), rows=MINI_ROWS)
    assert candidate_ids(out) == ["eea-2024-F-2", "eea-2024-F-3", "eea-2024-F-4", "eea-2024-F-5"]
    paceman = match_source("eea_co2_cars", gate_keys("מיני", ["PACEMAN"]), rows=MINI_ROWS)
    assert candidate_ids(paceman) == ["eea-2024-F-4"]                     # no ב מ וו family names PACEMAN


def test_cupra_by_make_alone_for_its_own_tozar_and_gated_under_seat():
    from src.open_data.match import match_source

    seat = match_source("eea_co2_cars", gate_keys("סיאט", ["CUPRA"]), rows=CUPRA_ROWS)
    assert candidate_ids(seat) == ["eea-2024-F-12"]                       # FORMENTOR is a סיאט family; TAVASCAN is not
    leon = match_source("eea_co2_cars", gate_keys("סיאט", ["LEON"]), rows=CUPRA_ROWS)
    assert candidate_ids(leon) == ["eea-2024-F-11"]
    cupra = match_source("eea_co2_cars", gate_keys("קופרה", ["CUPRA"]), rows=CUPRA_ROWS)
    assert candidate_ids(cupra) == ["eea-2024-F-12", "eea-2024-F-13", "eea-2024-F-14"]


# --- M3: the type-code probe -------------------------------------------------------------------------------------------

def eea(n, va, ve, kw, cc, co2, model, ta="E1*2001/116*0123"):
    return {"row_id": f"eea-2022-F-{n}", "make": "FIXTURE", "model": model, "type_approval": ta, "variant": va,
            "version": ve, "power_kw": kw, "displacement_cc": cc, "co2_wltp": co2, "year": 2022}


EEA_ROWS = [eea(1, "WX31", "31AB", 110, 1998, 150, "X1"), eea(2, "WX32", "32CD", 140, 1998, 160, "X1"),
            eea(3, "205040", "205040AB", 135, 1991, 170, "C"), eea(4, "205042", "205042AB", 150, 1991, 180, "C"),
            eea(5, "ZWE211L-DEXGBW(1D)", "ZWE211L-DEXGBW", 90, 1798, 100, "COROLLA"),
            eea(6, "K2813ABC", "K2813ABC01", 145, 1598, 151, "KONA"), eea(7, "MX1234", "MX1234Z", 50, 999, 120, "OTHER"),
            eea(8, "OCT5E", "AANX-NX33LDZ", 81, 999, 119, "OCTAVIA")]


def gov(degem, hp, cc, co2, kinuy, year=2022):
    return {"degem_nm": degem, "kinuy_mishari": kinuy, "engine_cc": cc, "horsepower": hp, "co2_wltp": co2,
            "shnat_yitzur": year}


CATALOG_ROWS = [gov("WX31", 150, 1998, "150", "X1 SDRIVE18I"),            # Va equal; 110.3 kW = 110: consistent
                gov("205.040", 200, 1991, "999", "C200"),                  # Va equal, Ve prefix; 147.1 kW != 135
                gov("ZWE211L DEXGBW", 122, 1798, None, "COROLLA"),         # Va prefix, Ve equal; consistent
                gov("NX-NX33LD", 110, 999, "119", "OCTAVIA"),              # Ve contains; 80.9 kW = 81, co2 equal
                gov("K9999", 198, 1598, "151", "KONA")]                    # no relation in any field


def test_relations_are_equal_prefix_contains_or_none_on_the_normalized_codes():
    assert P.norm(" 205.040 ") == "205040" and P.norm("zwe211l dexgbw") == "ZWE211LDEXGBW" and P.norm("NX-NX33LD") == "NXNX33LD"
    assert P.relation("205040", "205040") == "equal"
    assert P.relation("205040", "205040AB") == "prefix" and P.relation("205040AB", "205040") == "prefix"
    assert P.relation("NXNX33LD", "AANXNX33LDZ") == "contains"
    assert P.relation("WX3", "WX31") == "none"                              # the shorter side is under 4 characters
    assert P.relation("", "WX31") == "none"


def test_the_fixture_make_gives_the_expected_relations_and_consistency():
    out = P.probe("FIXTURE", ["טוזר"], 2022, 2022, 60, CATALOG_ROWS, EEA_ROWS)
    assert out["n"] == 5 and out["eea_rows"] == 8 and out["truncated"] is False
    counts = {f: {r: v["n"] for r, v in rel.items()} for f, rel in out["relations"].items()}
    assert counts == {"type_approval": {"equal": 0, "prefix": 0, "contains": 0, "none": 5},
                      "variant": {"equal": 2, "prefix": 1, "contains": 0, "none": 2},
                      "version": {"equal": 1, "prefix": 1, "contains": 1, "none": 2}}
    assert out["relations"]["variant"]["equal"]["pct"] == 40.0
    c = out["consistency"]
    assert set(c) == {"equal|variant", "prefix|variant", "equal|version", "prefix|version", "contains|version"}
    assert (c["equal|variant"]["n"], c["equal|variant"]["power_cc_agree"], c["equal|variant"]["consistency_rate"]) == (2, 1, 50.0)
    assert (c["equal|variant"]["co2_comparable"], c["equal|variant"]["co2_agree"]) == (2, 1)
    assert c["prefix|variant"]["consistency_rate"] == 100.0 and c["prefix|variant"]["co2_agree_rate"] is None
    assert c["equal|version"]["consistency_rate"] == 100.0
    assert c["prefix|version"]["consistency_rate"] == 0.0
    assert c["contains|version"]["consistency_rate"] == 100.0 and c["contains|version"]["co2_agree_rate"] == 100.0
    example = out["examples"]["equal|variant"][0]
    assert (example["degem_nm"], example["eea_value"], example["agree"]) == ("WX31", "WX31", True)
    assert example["power"] == {"catalog_hp": 150.0, "catalog_kw": 110.3, "eea_kw": 110.0}
    assert example["cc"] == {"catalog": 1998.0, "eea": 1998.0}
    assert out["without_relation"] == 1
    none = out["none_examples"][0]
    assert none["degem_nm"] == "K9999" and none["basis"] == "same_model_text" and none["model_rows"] == 1
    assert none["closest"]["variant"][0][0] == "K2813ABC"


def test_the_budget_truncates_and_returns_what_is_done():
    ticks = iter(range(100))
    out = P.probe("FIXTURE", ["טוזר"], 2022, 2022, 60, CATALOG_ROWS, EEA_ROWS, budget_s=2.5,
                  started=0, clock=lambda: next(ticks))
    assert out["truncated"] is True and out["n"] < 5


class FakeMilo:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __call__(self, sql, params):
        self.calls.append((sql, params))
        assert "catalog_variants_current" in sql and "md5(variant_identity_key)" in sql
        assert not any(w in sql.upper() for w in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP"))
        return [dict(r) for r in self.rows]


def test_the_mcp_tool_reads_one_sample_of_the_make_tozar_and_rejects_per_year_over_200(tmp_path, monkeypatch):
    seen = {}

    def candidates(make, tozar, low, high, folder=None):
        seen.update(make=make, tozar=tozar, years=(low, high))
        return EEA_ROWS

    monkeypatch.setattr(P, "eea_candidates", candidates)
    milo = FakeMilo(CATALOG_ROWS)
    observer = Observer(query=milo)
    out = observer.type_code_probe("mercedes benz", 2019, 2025, 40)
    assert seen == {"make": "MERCEDES-BENZ", "tozar": ["מרצדס", "מרצדס בנץ"], "years": (2019, 2025)}
    assert len(milo.calls) == 1
    assert milo.calls[0][1] == {"tozar": ["מרצדס", "מרצדס בנץ"], "year_from": 2019, "year_to": 2025, "per_year": 40}
    assert out["make"] == "MERCEDES-BENZ" and out["n"] == 5 and out["relations"]["variant"]["equal"]["n"] == 2
    assert observer.type_code_probe("MG", 2022, 2025, 200)["tozar"] == ["מ.ג"]
    with pytest.raises(ToolInputError, match="per_year"):
        observer.type_code_probe("MG", 2022, 2025, 201)
    with pytest.raises(ToolInputError, match="canonical make"):
        observer.type_code_probe("STUDEBAKER", 2022, 2025, 40)
    with pytest.raises(ToolInputError, match="model_year_from"):
        observer.type_code_probe("MG", 2025, 2022, 40)
    assert len(milo.calls) == 2                                          # a rejected call reads nothing


def test_without_milo_the_tool_says_so(monkeypatch):
    import src.db as db

    monkeypatch.setattr(db, "database_url", lambda: None)
    out = Observer(query=None).type_code_probe("MG", 2022, 2025, 40)
    assert out == {"available": False, "error": "catalog_unavailable",
                   "message": "DATABASE_URL is not set: the MILO catalogue is not available"}


def test_the_eea_candidates_are_the_make_rows_of_the_window_by_the_existing_make_filter(monkeypatch):
    from src.open_data import datasets as ds

    asked = {}

    def query_rows(source, *, makes, years=None, folder=None, report=None):
        asked.update(source=source, makes=list(makes), years=list(years))
        return [{"row_id": "b", "make": "BMW"}, {"row_id": "a", "make": "MINI"}, {"row_id": "c", "make": "B.M.W."}]

    monkeypatch.setattr(ds, "query_rows", query_rows)
    rows = P.eea_candidates("BMW", ["ב מ וו"], 2019, 2021)
    assert asked["source"] == "eea_co2_cars" and asked["years"] == [2019, 2020, 2021, 2022]   # registration years
    assert "BMW" in asked["makes"] and "MINI" in asked["makes"]           # the tozar's alias spellings
    assert [r["row_id"] for r in rows] == ["b", "c"]                      # MINI is another canonical make
