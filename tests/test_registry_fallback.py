"""Government registry coverage: tyre spellings, model-year fallback, coverage reasons (fixtures, no network).

    T1  tyre parsing: the registry's real unparsed spellings ("\\" as "/", "R/" and "/R" around the rim, a bare rim)
    T2  hierarchical fallback: level B (tozeret_cd | degem_cd | year, all trims) only when level A has no entry,
        n >= 20, share >= 0.95 and every trim with n >= 3 agrees; else nothing, or the trims as alternatives
    T3  coverage reasons per benchmark record (no key / n < 20 / unparsed / level B used)
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

from fixtures import pr43_audi_pages as A
from src.evidence_admission import AdmissionContext
from src.fields import resolve_requested_fields
from src.storage.cache import DocumentCache

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))     # registry build / coverage scripts

Q8_KEY = "21|260|2025|SLINE SUPER"                                               # A.Q8_PAYLOAD's registry key


@pytest.fixture()
def cache(tmp_path):
    return DocumentCache(tmp_path / "cache")


# --- T1: tyre parsing ------------------------------------------------------------------------------------------------

# the top unparsed strings of data/gov_registry_index.json (stats of 2026-10-05T16:46), with their vehicle counts
REGISTRY_TOP_UNPARSED = [
    ("195\\60 R15", "195/60 R15"), ("195\\65R15", "195/65 R15"), ("225/55R/17", "225/55 R17"),
    ("195/65R/15", "195/65 R15"), ("205/55/R16", "205/55 R16"), ("235/45/18", "235/45 R18"),
    ("205/55R/16", "205/55 R16"), ("245\\70R16", "245/70 R16"), ("175\\65 R14", "175/65 R14"),
    ("225/60R/17", "225/60 R17"), ("185/70R/14", "185/70 R14"), ("265\\65R17", "265/65 R17"),
]


@pytest.mark.parametrize("raw, expected", REGISTRY_TOP_UNPARSED + [
    ("195/65/R15", "195/65 R15"), ("205/55/16", "205/55 R16"), ("235/40/19", "235/40 R19"),
    ("265\\65 R17", "265/65 R17"), ("205/55/R/16", "205/55 R16"), ("235/45/18 94W XL", "235/45 R18"),
    ("225/55 R/ 17", "225/55 R17"), ("195/50/12", "195/50 R12"), ("295/30/24", "295/30 R24"),
])
def test_registry_spellings_parse(raw, expected):
    from src.gov_registry import normalize_tire

    assert normalize_tire(raw) == expected


@pytest.mark.parametrize("raw", [
    "235/45/11", "235/45/25", "235/45/10",              # a bare third number is a rim only for 12-24
    "235/45/18/91W", "235/45/18 2019",                  # another number after the size
    "195\\60 R15 \\ 205/55R16", "225/55R/17/225/55R/17",  # two sizes, whatever the slash
    "195R14C", "91V", "R16 205/55",                     # still not ONE metric size
])
def test_registry_spellings_still_refused(raw):
    from src.gov_registry import normalize_tire

    assert normalize_tire(raw) is None


def test_build_parses_the_registry_spellings():
    import build_gov_registry_index as B

    records = [{"tozeret_cd": 19, "degem_cd": 994, "shnat_yitzur": 2020, "ramat_gimur": "BASE", "zmig_kidmi": raw,
                "zmig_ahori": raw} for raw, _ in REGISTRY_TOP_UNPARSED]
    stats: dict = {}
    counts = B.aggregate(records, stats)
    assert stats.get("unparsed_rows", 0) == 0 and stats.get("unparsed_sizes", 0) == 0
    assert sum(counts["19|994|2020|BASE"]["front"].values()) == len(REGISTRY_TOP_UNPARSED)


# --- T2: hierarchical fallback ----------------------------------------------------------------------------------------

def _vehicles(n, front, rear=None, *, trim, degem=260, year=2025, maker=21):
    return [{"tozeret_cd": maker, "degem_cd": degem, "shnat_yitzur": year, "ramat_gimur": trim, "zmig_kidmi": front,
             "zmig_ahori": rear or front} for _ in range(n)]


def _index(records):
    import build_gov_registry_index as B

    stats: dict = {}
    counts = B.aggregate(records, stats)
    return json.loads(B.dumps(B.build_index(counts, source="fixture", resource_id=None, rows=len(records),
                                            stats=stats)))


def _adm(payload):
    propulsion = payload["engine_drivetrain"]["propulsion_normalized"]
    return AdmissionContext.for_run(payload, None, resolve_requested_fields(None, propulsion=propulsion), "IL")


def _emit(cache, data, payload=A.Q8_PAYLOAD):
    from src.gov_registry import emit
    from src.tools.evidence import EvidenceStore

    class Ctx:
        def __init__(self):
            self.cache, self.counters, self.evidence = cache, Counter(), EvidenceStore()
            self.admission, self.events, self.documents = _adm(payload), [], []

        def emit(self, kind, **data):
            self.events.append((kind, data))

        def note_document(self, doc, cache_hit=None):
            self.documents.append(doc)

    ctx = Ctx()
    return emit(ctx, payload, data), ctx


def test_build_writes_the_model_year_pools():
    data = _index(_vehicles(7, "285/45R21", trim="SLINE SUPER") + _vehicles(30, "285\\45 R21", trim="SLINE")
                  + _vehicles(4, "285/45R/21", trim="SLINE", year=2024))
    pool = data["model_years"]["21|260|2025"]
    assert pool["front"] == {"majority": "285/45 R21", "share": 1.0, "n": 37, "majority_n": 37}
    assert pool["trims"] == {"SLINE": {"rows": 30, "front": ["285/45 R21", 30, 30], "rear": ["285/45 R21", 30, 30]},
                             "SLINE SUPER": {"rows": 7, "front": ["285/45 R21", 7, 7], "rear": ["285/45 R21", 7, 7]}}
    assert "front" not in data["model_years"]["21|260|2024"]                     # 4 vehicles: no pooled summary
    assert Q8_KEY not in data["entries"] and data["stats"]["model_years"] == 2
    assert data["stats"]["model_years_pooled"] == 1


def _model_year(target_n):
    """The target's model year: SLINE (30) + a trim with 1 odd vehicle (n < 3, no vote) + the target trim SLINE SUPER
    with target_n vehicles (none when 0), all on 285/45 R21; another degem and another year that are never pooled."""
    return _index((_vehicles(target_n, "285/45R21", trim="SLINE SUPER") if target_n else [])
                  + _vehicles(30, "285\\45 R21", trim="SLINE") + _vehicles(1, "285/40R22", trim="BLACK EDITION")
                  + _vehicles(40, "255/55R19", trim="SLINE SUPER", degem=261)
                  + _vehicles(40, "255/55R19", trim="SLINE SUPER", year=2024))


def _states(ctx, fields):
    from src.field_recovery import evaluate_fields

    specs = resolve_requested_fields(fields, propulsion="plug_in")
    return {e["field"]: e["state"] for e in evaluate_fields(specs, [{"kind": k, **d} for k, d in ctx.events], "IL")}


def test_level_b_confirmed_by_the_target_trim_binds_the_market_trim_and_fills_the_fields(cache):
    from src.binding_replay import replay_fact

    out, ctx = _emit(cache, _model_year(10))                 # the target trim: 10 vehicles (no level A), same majority
    assert out["level"] == "B" and out["stored"] == 5 and "reason" not in out
    items = {e["field"]: e for e in ctx.evidence.items}
    assert set(items) == {"tire_size_front", "tire_size_rear", "rim_diameter_in", "rim_diameter_front_in",
                          "rim_diameter_rear_in"}                                  # D2: each axle's rim too
    assert items["tire_size_front"]["value"] == "285/45 R21" and items["rim_diameter_in"]["value"] == 21
    for item in items.values():
        assert item["registry_level"] == "B" and item["variant_match"] == "exact"
        assert item["binding_level"] == "exact_market_trim"
        assert item["binding_basis"] == "gov_registry_model_year_trim_confirmed"
    assert (items["tire_size_front"]["target_trim_n"], items["tire_size_front"]["target_trim_majority"]) == \
        (10, "285/45 R21")
    assert items["rim_diameter_in"]["target_trim_n"] == {"front": 10, "rear": 10}
    assert items["rim_diameter_in"]["target_trim_majority"] == {"front": "285/45 R21", "rear": "285/45 R21"}
    assert _states(ctx, ["tire_size_front", "tire_size_rear", "rim_diameter_in"]) == \
        {"tire_size_front": "ok", "tire_size_rear": "ok", "rim_diameter_in": "ok"}
    doc = ctx.cache.get(items["tire_size_front"]["document_id"])
    assert doc["registry_key"] == "21|260|2025" and doc["registry_level"] == "B"
    # Binding Replay re-reads the pool on the document: the same binding for the target, none for another year, and only
    # the technical variant for another trim of the same model year (it does not confirm the fact)
    for field in ("tire_size_front", "rim_diameter_in"):
        item = items[field]
        kwargs = dict(field=item["field"], value=item["value"], quote=item["quote"], document_id=item["document_id"],
                      source_url=item["source_url"])
        now = replay_fact(_adm(A.Q8_PAYLOAD), cache, **kwargs)
        assert (now["binding_level_now"], now["variant_match_now"]) == ("exact_market_trim", "exact"), field
        other_year = dict(A.Q8_PAYLOAD, identity={**A.Q8_PAYLOAD["identity"], "year": 2024})
        assert replay_fact(_adm(other_year), cache, **kwargs)["variant_match_now"] == "different"
        other_trim = dict(A.Q8_PAYLOAD, identity={**A.Q8_PAYLOAD["identity"], "trim": "SLINE"})
        moved = replay_fact(_adm(other_trim), cache, **kwargs)
        assert (moved["binding_level_now"], moved["variant_match_now"]) == ("exact_market_trim", "exact")
        third = dict(A.Q8_PAYLOAD, identity={**A.Q8_PAYLOAD["identity"], "trim": "COMPETITION"})
        moved = replay_fact(_adm(third), cache, **kwargs)
        assert (moved["binding_level_now"], moved["variant_match_now"]) == ("exact_technical_variant", "unclear")


@pytest.mark.parametrize("target_n", [2, 0], ids=["target_trim_n_2", "target_trim_absent"])
def test_level_b_without_the_target_trim_stays_at_the_technical_variant(cache, target_n):
    from src.field_recovery import binding_satisfies

    out, ctx = _emit(cache, _model_year(target_n))
    assert out["level"] == "B" and out["stored"] == 5
    items = {e["field"]: e for e in ctx.evidence.items}
    for item in items.values():
        assert item["registry_level"] == "B" and item["variant_match"] == "unclear"
        assert item["binding_level"] == "exact_technical_variant"
        assert item["binding_basis"] == "gov_registry_model_year"
        assert not binding_satisfies(item, "exact_market_trim")
    assert items["tire_size_front"]["target_trim_n"] == target_n
    assert items["tire_size_front"].get("target_trim_majority") == ("285/45 R21" if target_n else None)
    assert items["rim_diameter_in"]["target_trim_n"] == {"front": target_n, "rear": target_n}
    states = _states(ctx, ["tire_size_front", "tire_size_rear", "rim_diameter_in"])
    assert "ok" not in states.values(), states                                    # the fields stay unfilled


def test_level_b_rim_needs_both_axles_of_the_target_trim():
    from src.gov_registry import target_trim_check

    axle = {"majority": "285/45 R21", "share": 1.0, "n": 40, "majority_n": 40}
    pool = {"front": axle, "rear": axle,
            "trims": {"SLINE SUPER": {"rows": 10, "front": ["285/45 R21", 10, 10], "rear": ["285/45 R21", 2, 2]}}}
    words = ("sline", "super")
    assert target_trim_check(pool, words, "tire_size_front")["confirmed"] is True
    assert target_trim_check(pool, words, "tire_size_rear")["confirmed"] is False                # rear n = 2
    rim = target_trim_check(pool, words, "rim_diameter_in")
    assert rim == {"confirmed": False, "target_trim_n": {"front": 10, "rear": 2},
                   "target_trim_majority": {"front": "285/45 R21", "rear": "285/45 R21"}}
    pool["trims"]["SLINE SUPER"]["rear"] = ["285/40 R22", 9, 10]                  # n 10, another majority
    assert target_trim_check(pool, words, "tire_size_rear")["confirmed"] is False
    assert target_trim_check(pool, words, "alternative_tire_sizes")["confirmed"] is False       # never confirmed
    pool["trims"]["S LINE"] = {"rows": 5, "front": ["285/45 R21", 5, 5], "rear": ["285/45 R21", 5, 5]}
    assert target_trim_check(pool, ("s_line",), "rim_diameter_in")["confirmed"] is True     # "S_LINE" = "S LINE"


def test_level_a_items_carry_their_level(cache):
    data = _index(_vehicles(25, "285/45R21", trim="SLINE SUPER"))
    out, ctx = _emit(cache, data)
    assert out["level"] == "A" and out["stored"] == 5
    assert {e["registry_level"] for e in ctx.evidence.items} == {"A"}
    assert {e["binding_level"] for e in ctx.evidence.items} == {"exact_market_trim"}


def test_level_b_refused_when_the_trims_disagree(cache):
    data = _index(_vehicles(7, "285/45R21", trim="SLINE SUPER") + _vehicles(30, "285/40R22", trim="SLINE")
                  + _vehicles(3, "265/55R20", trim="BASE"))
    out, ctx = _emit(cache, data)
    assert out["level"] == "B" and out["reason"] == "level_b_trims_disagree"
    assert [e["field"] for e in ctx.evidence.items] == ["alternative_tire_sizes"]   # never a standard size, no rim
    item = ctx.evidence.items[0]
    assert item["value"] == "285/40 R22, 285/45 R21, 265/55 R20" and item["registry_level"] == "B"
    assert "SLINE 285/40 R22 (30 of 30)" in item["quote"] and "SLINE SUPER 285/45 R21 (7 of 7)" in item["quote"]
    assert "BASE 265/55 R20 (3 of 3)" in item["quote"]


@pytest.mark.parametrize("records, refusal", [
    # trims agree, but the pooled share is 36 / 38 = 0.947 < 0.95 (two odd vehicles under trims of n < 3)
    (_vehicles(7, "285/45R21", trim="SLINE SUPER") + _vehicles(29, "285/45R21", trim="SLINE")
     + _vehicles(1, "285/40R22", trim="X") + _vehicles(1, "285/40R22", trim="Y"), "level_b_below_thresholds"),
    # the whole model year has 19 vehicles
    (_vehicles(7, "285/45R21", trim="SLINE SUPER") + _vehicles(12, "285/45R21", trim="SLINE"),
     "level_b_pooled_n_below"),
    # the agreement lives in another year / another degem code only
    (_vehicles(7, "285/45R21", trim="SLINE SUPER") + _vehicles(40, "285/45R21", trim="SLINE", year=2024)
     + _vehicles(40, "285/45R21", trim="SLINE", degem=261), "level_b_pooled_n_below"),
])
def test_level_b_emits_nothing_below_its_thresholds(cache, records, refusal):
    out, ctx = _emit(cache, _index(records))
    assert ctx.evidence.items == [] and out["stored"] == 0 and out["level"] is None and out["reason"] == refusal


def test_level_b_share_is_exact_not_the_rounded_one():
    from src.gov_registry import model_year_facts

    axle = {"majority": "285/45 R21", "share": round(19000 / 20001, 4), "n": 20001, "majority_n": 19000}
    assert axle["share"] == 0.95                                                  # 0.94995... rounds up when stored
    assert model_year_facts({"front": axle, "rear": axle, "trims": {}}) == ([], "below_thresholds")


def test_level_b_never_overrides_a_level_a_entry(cache):
    # the trim's own entry (n >= 20) is split below 0.8: the alternatives of level A, never the model-year pool
    data = _index(_vehicles(12, "285/45R21", trim="SLINE SUPER") + _vehicles(10, "285/40R22", trim="SLINE SUPER")
                  + _vehicles(400, "285/45R21", trim="SLINE"))
    out, ctx = _emit(cache, data)
    assert out["level"] == "A"
    assert [(e["field"], e["registry_level"]) for e in ctx.evidence.items] == [("alternative_tire_sizes", "A")]


def test_an_index_without_model_years_has_no_level_b(cache):
    data = _index(_vehicles(7, "285/45R21", trim="SLINE SUPER") + _vehicles(30, "285/45R21", trim="SLINE"))
    data.pop("model_years")
    out, ctx = _emit(cache, data)
    assert ctx.evidence.items == [] and out["reason"] == "no_entry"


# --- T3: coverage reasons ---------------------------------------------------------------------------------------------

def _row(record, trim, degem, *, year=2025, maker=21, name="Q8"):
    return {"upstream_record_id": record, "tozar": "אאודי", "kinuy_mishari": name, "shnat_yitzur": year,
            "ramat_gimur": trim, "tozeret_cd": maker, "degem_cd": degem, "equipment": {}}


def test_coverage_reasons_per_benchmark_record(tmp_path, capsys):
    import gov_registry_coverage as C

    records = (_vehicles(25, "285/45R21", trim="SLINE SUPER")                                    # 1: level A
               + _vehicles(5, "255/55R19", trim="BASE", degem=261)                               # 2: level B used
               + _vehicles(30, "255/55\\R19", trim="SPORT", degem=261)
               + _vehicles(30, "235/60R18", trim="BASE", degem=262, year=2024)                    # 3: no key at all
               + _vehicles(30, "235/60R18", trim="DESIGN", degem=265)                             # 7: level B used,
               + _vehicles(6, "235/55R19", trim="BASE", degem=263)                                # 4: n<20, trims
               + _vehicles(30, "255/45R20", trim="SPORT", degem=263)                              #    disagree
               + _vehicles(8, "235/60R18", trim="BASE", degem=264)                                # 5: unparsed
               + _vehicles(15, "garbage", trim="BASE", degem=264))
    index = tmp_path / "index.json"
    index.write_text(json.dumps(_index(records)), "utf-8")
    no_codes = {"upstream_record_id": "6", "tozar": "אאודי", "kinuy_mishari": "Q9", "shnat_yitzur": 2025,
                "ramat_gimur": "BASE", "equipment": {}}
    rows = [_row("1", "SLINE SUPER", 260), _row("2", "BASE", 261), _row("3", "BASE", 262), _row("4", "BASE", 263),
            _row("5", "BASE", 264), _row("7", "BASE", 265), no_codes]
    level15 = tmp_path / "level15.json"
    level15.write_text(json.dumps({"rows": rows}, ensure_ascii=False), "utf-8")
    args = ["--index", str(index), "--level15-json", str(level15), "--catalog", ""]
    assert C.main([*args, "--json"]) == 0
    bench = json.loads(capsys.readouterr().out)["benchmark"]
    by = {m["record"]: m for m in bench["missing"]}
    assert set(by) == {"2", "3", "4", "5", "6", "7"} and bench["with_entry"] == 1 and bench["with_level_b"] == 2
    assert (by["2"]["reason"], by["2"]["under"], by["2"]["n"], by["2"]["level"]) == ("level_b", "small", 5, "B")
    assert by["2"]["fields"] == ["rim_diameter_front_in", "rim_diameter_in", "rim_diameter_rear_in", "tire_size_front",
                                 "tire_size_rear"]
    assert (by["3"]["reason"], by["3"]["trims"]) == ("no_key", [])                # other years are never looked at
    assert (by["7"]["reason"], by["7"]["under"], by["7"]["trims"]) == ("level_b", "no_key", ["DESIGN"])
    assert (by["4"]["reason"], by["4"]["n"]) == ("small", 6)
    assert by["4"]["level_b"] == {"used": True, "refusal": "level_b_trims_disagree",
                                  "fields": ["alternative_tire_sizes"], "market_trim": []}
    assert by["2"]["level_b"]["market_trim"] == ["rim_diameter_front_in", "rim_diameter_in", "rim_diameter_rear_in",
                                                "tire_size_front", "tire_size_rear"]
    assert by["7"]["level_b"]["market_trim"] == []                               # the target trim is not in the pool
    assert (by["5"]["reason"], by["5"]["n"], by["5"]["unparsed"]) == ("unparsed", 8, [15, 15])
    assert by["6"]["reason"] == "no_government_codes"
    assert bench["reasons"] == {"level_b": 2, "no_government_codes": 1, "no_key": 1, "small": 1, "unparsed": 1}
    assert C.main(args) == 0
    text = capsys.readouterr().out
    assert "no entry: 2 אאודי Q8 2025 key=21|261|2025|BASE: level B used: rim_diameter_front_in, rim_diameter_in, " \
           "rim_diameter_rear_in, tire_size_front, tire_size_rear (trim n=5); confirmed by the trim (exact_market_trim): " \
           "rim_diameter_front_in, rim_diameter_in, rim_diameter_rear_in, tire_size_front, tire_size_rear" in text
    assert "key=21|262|2025|BASE: no key at all (model year trims: none); level B refused: no_pool" in text
    assert "key=21|265|2025|BASE: level B used: rim_diameter_front_in, rim_diameter_in, rim_diameter_rear_in, " \
           "tire_size_front, tire_size_rear (trim: no_key); " \
           "not confirmed by the trim (exact_technical_variant, fields stay unfilled)" in text
    assert "key=21|263|2025|BASE: n<20 (n=6); level B refused: trims_disagree (evidence: alternative_tire_sizes)" \
        in text
    assert "key=21|264|2025|BASE: unparsed (n=8 parsed, unparsed cells front/rear=[15, 15])" in text
    assert "6 אאודי Q9 2025 key=None: no government codes on the record" in text


def test_coverage_on_an_index_without_model_years(tmp_path, capsys):
    import gov_registry_coverage as C

    data = _index(_vehicles(5, "285/45R21", trim="SLINE SUPER"))
    data.pop("model_years")
    index = tmp_path / "index.json"
    index.write_text(json.dumps(data), "utf-8")
    level15 = tmp_path / "level15.json"
    level15.write_text(json.dumps({"rows": [_row("1", "SLINE SUPER", 260)]}, ensure_ascii=False), "utf-8")
    assert C.main(["--index", str(index), "--level15-json", str(level15), "--catalog", ""]) == 0
    assert "index built before the model-year pools (no model_years)" in capsys.readouterr().out
