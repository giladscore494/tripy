"""PR #40: single-propulsion / single-body catalog (R1 / R1b), the Document Variant Map (R2), the discriminating power
tolerance (R3), brand-policy warranties (R4), BEV gap labels (R5), replay `rejected_now` (R6), the recovery search gate
(R7) and the remaining candidate noise (R8). Synthetic text, no network."""

import copy
import json

import pytest

from fixtures.admission_records import XPENG
from fixtures.corolla_harvest import put

from src.adjudication import candidate_request
from src.candidate_harvest import collect_candidate_rejections, harvest_document, harvest_text
from src.document_binding import (bind, binding_gaps, power_tolerance, single_body_catalog, single_propulsion_catalog,
                                  statuses, target_identity)
from src.evidence_admission import AdmissionContext, admit, brand_policy_eligibility
from src.fields import load_schema, resolve_requested_fields
from src.storage.cache import DocumentCache
from src.variant_map import build_variant_map, document_variant_map

SPECS = load_schema()
G6 = "אקספנג|g6|2026|suv|"
AWD485 = G6 + "battery_electric|awd|485|"
AWD475 = G6 + "battery_electric|awd|475|"
RWD285 = G6 + "battery_electric|two_wheel_drive|285|"


def xpeng(**changes):
    payload = copy.deepcopy(XPENG)
    for part, values in changes.items():
        payload[part].update(values)
    return target_identity(payload)


@pytest.fixture
def index(tmp_path, monkeypatch):
    """Point the runtime at a fixture catalog index: index({key: entry}, complete=True)."""
    def write(entries, complete=True):
        path = tmp_path / f"trim_index_{len(list(tmp_path.glob('trim_index_*')))}.json"
        path.write_text(json.dumps({"complete": complete, "entries": entries}, ensure_ascii=False), "utf-8")
        monkeypatch.setenv("CATALOG_TRIM_INDEX_PATH", str(path))
        return path
    return write


def g6_bind(text, *, requirement="body_powertrain", propulsions=None, layers=None, **kw):
    ident = xpeng()
    found = statuses(text, ident)
    return bind(ident, found, layers, market="IL", requirement=requirement,
                document_propulsions=[] if propulsions is None else propulsions, **kw)


# --- R1: single-propulsion catalog ------------------------------------------------------------------------------------

def test_r1_a_bev_only_family_with_propulsion_absent_reaches_body_powertrain(index):
    index({AWD485: {"trims": ["MAX"]}, RWD285: {"trims": ["LONG RANGE"]}})
    result = g6_bind("XPeng G6 SUV")
    assert result["binding_level"] == "body_powertrain" and result["variant_match"] == "exact"
    assert result["binding_basis"] == "single_propulsion_catalog"
    assert result["binding_dimensions"]["propulsion"] == {"status": "match", "basis": "single_propulsion_catalog",
                                                          "was": "absent", "catalog_entries": 2}


@pytest.mark.parametrize("entries,complete,propulsions", [
    ({AWD485: {"trims": ["MAX"]}, G6 + "plug_in|awd|485|": {"trims": ["PHEV"]}}, True, []),   # BEV and PHEV entries
    ({AWD485: {"trims": ["MAX"]}, RWD285: {"trims": ["LR"], "complete": False}}, True, []),   # an incomplete entry
    ({AWD485: {"trims": ["MAX"]}}, False, []),                                                # an incomplete index
    ({}, True, []),                                                                            # no catalog entry
    ({AWD485: {"trims": ["MAX"]}}, True, ["hybrid"]),                                          # the document names one
])
def test_r1_fails_closed(index, entries, complete, propulsions):
    index(entries, complete)
    result = g6_bind("XPeng G6 SUV", propulsions=propulsions)
    assert result["binding_level"] == "generation" and result["variant_match"] == "unclear"
    assert "binding_rules" not in result


def test_r1_never_applies_when_a_fact_layer_names_another_propulsion(index):
    index({AWD485: {"trims": ["MAX"]}})
    assert g6_bind("XPeng G6 SUV")["binding_level"] == "body_powertrain"
    # unknown document mentions (None): the rule does not apply
    assert g6_bind("XPeng G6 SUV", propulsions=None)["binding_level"] == "body_powertrain"
    ident = xpeng()
    unknown = bind(ident, statuses("XPeng G6 SUV", ident), market="IL", requirement="body_powertrain")
    assert unknown["binding_level"] == "generation"
    assert single_propulsion_catalog(ident) is not None


# --- R1b: single-body catalog -----------------------------------------------------------------------------------------

def test_r1b_other_bodies_of_other_models_bind_the_body_of_a_single_body_family(index):
    index({AWD485: {"trims": ["MAX"]}, RWD285: {"trims": ["LR"]}})
    # an aggregator / menu: the target's SUV plus another model's sedan and MPV on the page
    result = g6_bind("XPeng G6 SUV חשמלי | sedan | MPV")
    assert result["binding_dimensions"]["body"]["rule"] == "single_body_catalog"
    assert result["binding_dimensions"]["body"]["was"] == "mixed"
    assert result["binding_level"] == "body_powertrain" and result["binding_basis"] == "single_body_catalog"
    # a body mismatch stays a veto
    vetoed = g6_bind("XPeng G6 sedan חשמלי", requirement="exact_technical_variant")
    assert vetoed["variant_match"] == "different" and "body_mismatch@document" in vetoed["binding_veto"]
    # a family with two bodies: mixed stays mixed
    index({AWD485: {"trims": ["MAX"]}, G6 .replace("suv", "sedan") + "battery_electric|awd|485|": {"trims": ["S"]}})
    assert single_body_catalog(xpeng()) is None
    assert g6_bind("XPeng G6 SUV חשמלי | sedan | MPV")["binding_level"] == "generation"


# --- R3: discriminating power tolerance ------------------------------------------------------------------------------

def test_r3_a_neighbouring_catalog_power_tightens_the_tolerance(index):
    index({AWD485: {"trims": ["MAX"]}, AWD475: {"trims": ["PERF TB"]}})
    ident = xpeng()
    tolerance = power_tolerance(ident)
    assert tolerance["neighbour_hp"] == 475 and 0.011 < tolerance["tolerance"] < 0.012
    assert statuses('XPeng G6 475 כ"ס', ident)["power"] == "mismatch"
    assert statuses('XPeng G6 486 כ"ס', ident)["power"] == "match"
    assert statuses("XPeng G6 485 hp", ident)["power"] == "match"
    result = g6_bind('XPeng G6 SUV 486 כ"ס', requirement="exact_technical_variant")
    assert result["binding_dimensions"]["power"]["tolerance"] == tolerance["tolerance"]


def test_r3_without_a_neighbour_3_percent_still_applies(index):
    index({AWD485: {"trims": ["MAX"]}})
    ident = xpeng()
    assert power_tolerance(ident) == {"tolerance": 0.03, "neighbour_hp": None}
    assert statuses('XPeng G6 475 כ"ס', ident)["power"] == "match"          # 2.3 % < 3 %
    assert statuses('XPeng G6 470 כ"ס', ident)["power"] == "mismatch"


def test_r3_kw_figures_are_compared_in_metric_horsepower(index):
    index({AWD485: {"trims": ["MAX"]}, AWD475: {"trims": ["PERF TB"]}})
    # 357 kW is the catalog's 486 כ"ס (metric hp); in mechanical hp (x1.341) it would be 478.7 and miss the tighter
    # tolerance
    assert statuses("XPeng G6 הספק 357 kW", xpeng())["power"] == "match"


# --- R5: gap labels ---------------------------------------------------------------------------------------------------

def test_r5_no_displacement_gap_for_a_bev():
    item = {"binding_level": "body_powertrain", "variant_match": "unclear", "binding_requirement":
            "exact_technical_variant", "binding_dimensions": {"model": {"status": "match"}}}
    assert binding_gaps(item, propulsion="battery_electric") == ["model_code_absent", "power_absent"]
    assert binding_gaps(item) == ["displacement_absent", "power_absent"]           # conventional / unknown: unchanged
    mixed = {**item, "binding_dimensions": {"model": {"status": "match"}, "displacement": {"status": "mixed"},
                                            "drivetrain": {"status": "mixed"}}}
    assert binding_gaps(mixed, propulsion="battery_electric") == ["drivetrain_mixed"]


# --- R2: the Document Variant Map -------------------------------------------------------------------------------------

IL_SPEC = "https://www.xpeng.co.il/g6/specifications"


def page(rows, extra="", title="XPeng G6 - מפרט טכני"):
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<html><head><title>{title}</title></head><body><h1>XPeng G6</h1><table>{body}</table>{extra}</body></html>"


COLUMNS = ["", 'RWD 286 כ"ס', 'AWD 486 כ"ס']


def admitted_all(tmp_path, url, html, fields=None, payload=XPENG):
    """{(field, value): record} of every harvested candidate admit() accepts, plus the document's material."""
    cache = DocumentCache(tmp_path / "cache")
    doc = put(cache, url, html, "html")
    adm = AdmissionContext.for_run(payload, None, SPECS, "IL")
    out = {}
    for cand in harvest_document(cache, doc, SPECS)[0]:
        if fields and cand["field"] not in fields:
            continue
        decision = admit(adm, cache, candidate_request(cand["field"], {**cand, "document_id": doc}), [doc])
        if decision["accepted"]:
            out[(cand["field"], cand["value"])] = decision["record"]
    return out, adm.material(cache, doc, None), (cache, doc, adm)


def test_r2_a_column_region_binds_the_target_and_vetoes_the_other_variant(tmp_path):
    out, material, _ = admitted_all(tmp_path, IL_SPEC, page([COLUMNS, ["טווח", 'נסיעה 570 ק"מ', '550 ק"מ']]),
                                    ["electric_range_km"])
    target = out[("electric_range_km", 550)]
    assert target["variant_match"] == "exact" and target["binding_basis"] == "dvm_region"
    region = target["variant_map_region"]
    assert region["region_id"] == "table:0:col:2" and region["status"] == "target"
    assert region["candidates_after"] == [AWD485] and region["identity"]["power"] == [486.0]
    assert target["binding_dimensions"]["drivetrain"]["basis"] == "dvm_region"
    other = out[("electric_range_km", 570)]
    assert other["variant_match"] == "different" and other["binding_veto"] == ["drivetrain_mismatch@dvm_region"]
    assert other["variant_map_region"]["region_id"] == "table:0:col:1"


def test_r2_elimination_assigns_a_partial_region_only_when_one_catalog_variant_remains(tmp_path):
    rows = [["", "RWD", "AWD"], ["טווח", '570 ק"מ', '550 ק"מ']]
    out, material, _ = admitted_all(tmp_path, IL_SPEC, page(rows, '<p>הספק מרבי 486 כ"ס</p>'), ["electric_range_km"])
    region = out[("electric_range_km", 550)]["variant_map_region"]
    assert region["status"] == "target" and region["region_id"] == "table:0:col:2"
    assert sorted(region["candidates_before"]) == [AWD475, AWD485] and region["candidates_after"] == [AWD485]
    assert out[("electric_range_km", 550)]["variant_match"] == "exact"
    # the same page also mentions 475 hp: two AWD candidates remain -> unresolved, no rise
    html = page(rows, '<p>הספק מרבי 486 כ"ס</p><p>הדור הקודם: 475 כ"ס</p>')
    out, _, _ = admitted_all(tmp_path / "475", IL_SPEC, html, ["electric_range_km"])
    record = out[("electric_range_km", 550)]
    assert record["variant_map_region"]["status"] == "unresolved"
    assert sorted(record["variant_map_region"]["candidates_after"]) == [AWD475, AWD485]
    assert record["variant_match"] == "unclear" and record["binding_level"] == "body_powertrain"
    # no power anywhere: nothing to eliminate with -> unresolved
    out, _, _ = admitted_all(tmp_path / "none", IL_SPEC, page(rows), ["electric_range_km"])
    assert out[("electric_range_km", 550)]["variant_map_region"]["status"] == "unresolved"


def test_r2_a_value_stated_once_on_an_official_il_page_is_shared(tmp_path):
    html = page([COLUMNS, ["טווח", '570 ק"מ', '550 ק"מ'], ["משקל עצמי", '2,100 ק"ג', '2,190 ק"ג']],
                '<p>בסיס גלגלים 2,890 מ"מ</p>')
    out, _, _ = admitted_all(tmp_path, IL_SPEC, html, ["wheelbase_mm", "curb_weight_kg"])
    wheelbase = out[("wheelbase_mm", 2890)]
    assert wheelbase["variant_match"] == "exact" and wheelbase["binding_basis"] == "dvm_shared"
    assert wheelbase["variant_map_region"]["status"] == "shared"
    assert wheelbase["variant_map_region"]["inventory_contains_target"] is True
    # curb weight differs per column: never shared; 2,190 binds only through the AWD region
    assert out[("curb_weight_kg", 2190)]["binding_basis"] == "dvm_region"
    assert out[("curb_weight_kg", 2190)]["variant_map_region"]["region_id"] == "table:0:col:2"
    assert out[("curb_weight_kg", 2100)]["variant_match"] == "different"


def test_r2_a_row_identical_in_every_variant_column_is_shared(tmp_path):
    html = page([COLUMNS, ["בסיס גלגלים", '2,890 מ"מ', '2,890 מ"מ'], ["טווח", '570 ק"מ', '550 ק"מ']])
    out, _, _ = admitted_all(tmp_path, IL_SPEC, html, ["wheelbase_mm"])
    record = out[("wheelbase_mm", 2890)]
    assert record["binding_basis"] == "dvm_shared" and record["variant_map_region"]["region_kind"] == "table_shared_row"


def test_r2_shared_row_fails_closed_when_an_unmapped_data_column_disagrees(tmp_path):
    rows = [["", 'RWD 286 כ"ס', 'AWD 486 כ"ס', "Mystery"],
            ["בסיס גלגלים", '2,890 מ"מ', '2,890 מ"מ', '3,000 מ"מ']]
    out, _, _ = admitted_all(tmp_path, IL_SPEC, page(rows), ["wheelbase_mm"])
    record = out[("wheelbase_mm", 2890)]
    assert record["variant_match"] == "unclear"
    assert record["variant_map_region"]["status"] == "unresolved"
    assert "dvm_shared" not in (record.get("binding_rules") or [])


def test_r2_a_hedged_value_never_rises(tmp_path):
    html = page([COLUMNS, ["טווח", '570 ק"מ', '550 ק"מ']], "<p>הספק טעינה מהירה עד 451 kW</p>")
    out, _, _ = admitted_all(tmp_path, IL_SPEC, html, ["dc_max_charging_power_kw"])
    record = out[("dc_max_charging_power_kw", 451)]
    assert record["variant_match"] == "unclear" and record["binding_level"] == "body_powertrain"
    assert "dvm_shared" not in (record.get("binding_rules") or [])


def test_r2_two_awd_columns_that_cannot_be_told_apart_stay_unresolved(tmp_path):
    rows = [["", "AWD", "AWD Performance"], ["טווח", '560 ק"מ', '550 ק"מ']]
    html = page(rows, '<p>הספק 475 כ"ס או 486 כ"ס</p>')
    out, material, _ = admitted_all(tmp_path, IL_SPEC, html, ["electric_range_km"])
    for value in (560, 550):
        record = out[("electric_range_km", value)]
        assert record["variant_map_region"]["status"] == "unresolved", value
        assert record["variant_match"] == "unclear" and "dvm_region" not in (record.get("binding_rules") or [])


def test_r2_foreign_market_and_non_official_pages(tmp_path):
    html = page([COLUMNS, ["טווח", '570 ק"מ', '550 ק"מ'], ["בסיס גלגלים", '2,890 מ"מ', '2,890 מ"מ']])
    # a foreign-market page: no rise for a field without portability; a portable field may rise (the evaluator's
    # portability verdict then decides whether it counts for IL)
    out, _, _ = admitted_all(tmp_path, "https://www.xpeng.com/de-de/g6/specs", html,
                             ["electric_range_km", "wheelbase_mm"])
    record = out[("electric_range_km", 550)]
    assert record["market"] != "IL" and record["variant_match"] == "unclear"
    assert "market" in record["variant_map_region"]["blocked_by"]
    # a non-official target-market page: no dvm_shared
    out, _, _ = admitted_all(tmp_path / "pub", "https://www.carnews.co.il/xpeng-g6", html, ["wheelbase_mm"])
    record = out[("wheelbase_mm", 2890)]
    assert record["source_authority"] not in ("official_importer", "official_manufacturer")
    assert record["variant_match"] == "unclear"
    assert "shared_needs_official_non_price" in record["variant_map_region"]["blocked_by"]


def test_r2_market_trim_regions(tmp_path):
    # a market-trim field in a region bound only technically: the technical variant, never the market trim
    html = page([COLUMNS, ["צמיגים", "255/45 R19", "255/45 R20"]])
    out, _, _ = admitted_all(tmp_path, IL_SPEC, html, ["tire_size_front"])
    record = out[("tire_size_front", "255/45 R20")]
    assert record["binding_level"] == "exact_technical_variant" and record["variant_match"] == "unclear"
    # a price-list line "G6 MAX" in an official IL page: the trim region binds the market trim
    prices = """<html><head><title>מחירון XPeng G6 2026</title></head><body><h1>מחירון XPeng G6</h1>
<p>G6 RWD 286 כ"ס</p><p>G6 AWD 486 כ"ס</p><p>מחיר G6 PRO: 199,990 ₪</p><p>מחיר G6 MAX: 239,990 ₪</p></body></html>"""
    out, _, _ = admitted_all(tmp_path / "p", "https://www.xpeng.co.il/g6/prices", prices, ["list_price"])
    record = out[("list_price", 239990)]
    assert record["binding_level"] == "exact_market_trim" and record["binding_basis"] == "dvm_region"
    assert record["variant_map_region"]["identity"]["trim"] == ["MAX"]
    assert out[("list_price", 199990)]["variant_match"] == "unclear"
    # a bare "MAX" (no family): the #34 qualified-phrase rule, never the trim
    bare = prices.replace("מחיר G6 MAX", "מחיר MAX")
    out, _, _ = admitted_all(tmp_path / "b", "https://www.xpeng.co.il/g6/prices", bare, ["list_price"])
    assert out[("list_price", 239990)]["variant_match"] == "unclear"


def test_r2_a_veto_in_the_values_own_clause_wins_over_its_region(tmp_path):
    html = page([COLUMNS, ["טווח", '570 ק"מ', 'RWD 550 ק"מ']])
    out, _, _ = admitted_all(tmp_path, IL_SPEC, html, ["electric_range_km"])
    record = out[("electric_range_km", 550)]
    assert record["variant_match"] == "different"
    assert not any(v.endswith("@dvm_region") for v in record["binding_veto"])
    assert "dvm_region" not in (record.get("binding_rules") or [])


def test_r2_the_map_is_deterministic_and_reproduced_from_the_cache_by_the_replay(tmp_path):
    from src import binding_replay as R

    html = page([COLUMNS, ["טווח", '570 ק"מ', '550 ק"מ']], '<p>בסיס גלגלים 2,890 מ"מ</p>')
    out, material, (cache, doc, adm) = admitted_all(tmp_path, IL_SPEC, html, ["electric_range_km", "wheelbase_mm"])
    stored = [p for p in (tmp_path / "cache" / "documents" / doc).glob("derived_variant_map_*.json")]
    assert len(stored) == 1 and json.loads(stored[0].read_text("utf-8")) == material.variant_map
    rebuilt = build_variant_map(text=material.text, identity=adm.identity, tables=None, html=None)
    assert rebuilt == build_variant_map(text=material.text, identity=adm.identity, tables=None, html=None)
    # recomputed in memory (a read-only replay cache without the stored file) gives the same map
    stored[0].unlink()
    replay_cache = R.ReadOnlyCache(tmp_path / "cache")
    recomputed = document_variant_map(replay_cache, material.doc, adm.identity, adm.dictionary,
                                      zone=material.variant_map["regions"][-1]["text"])
    assert recomputed == material.variant_map and not stored[0].exists()
    record = out[("electric_range_km", 550)]
    events = [{"kind": "run_started", "seq": 1, "target_market": "IL", "vehicle_label": {},
               "requested_field_specs": [{"name": "electric_range_km"}, {"name": "wheelbase_mm"}]},
              {"kind": "evidence", "seq": 2, "evidence": {**record, "evidence_id": "e1"}},
              {"kind": "evidence", "seq": 3, "evidence": {**out[("wheelbase_mm", 2890)], "evidence_id": "e2"}}]
    run_dir = tmp_path / "runs" / "R" / "101122"
    run_dir.mkdir(parents=True)
    (run_dir / "input.json").write_text(json.dumps(XPENG, ensure_ascii=False), "utf-8")
    (run_dir / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), "utf-8")
    replay = R.replay_run(run_dir, tmp_path / "cache", write=False)
    rows = {r["evidence_id"]: r for r in replay["items"]}
    assert rows["e1"]["variant_map_region_now"] == record["variant_map_region"]
    assert rows["e1"]["binding_basis_now"] == "dvm_region" and rows["e2"]["binding_basis_now"] == "dvm_shared"
    assert replay["summary"]["vehicle"]["fields_ok_now"] == 2


# --- R4: brand-policy warranty ----------------------------------------------------------------------------------------

WARRANTY = "https://www.xpeng.co.il/warranty"


def test_r4_an_importer_warranty_statement_without_a_model_binds_at_body_powertrain(tmp_path):
    html = """<html><head><title>אחריות | XPENG ישראל</title></head><body><h1>אחריות</h1>
<p>אחריות לרכב: 7 שנים או 160,000 ק"מ, המוקדם מביניהם.</p></body></html>"""
    out, material, _ = admitted_all(tmp_path, WARRANTY, html, ["vehicle_warranty", "warranty_years", "warranty_km"])
    assert {k[0] for k in out} == {"vehicle_warranty", "warranty_years", "warranty_km"}
    for key, record in out.items():
        assert record["binding_level"] == "body_powertrain" and record["variant_match"] == "exact", key
        assert record["binding_basis"] == "brand_policy", key
        assert record["binding_policy"]["source_authority"] == "official_importer", key


def test_r4_a_per_model_warranty_table_keeps_the_model_binding(tmp_path):
    html = """<html><head><title>אחריות | XPENG ישראל</title></head><body><h1>אחריות</h1><table>
<tr><td>G6</td><td>אחריות 7 שנים או 160,000 ק"מ</td></tr><tr><td>G9</td><td>אחריות 5 שנים או 100,000 ק"מ</td></tr>
</table></body></html>"""
    out, _, _ = admitted_all(tmp_path, WARRANTY, html, ["warranty_years"])
    own, other = out[("warranty_years", 7)], out[("warranty_years", 5)]
    assert own["variant_match"] == "exact" and own["binding_basis"] != "brand_policy"
    assert own["binding_dimensions"]["model"]["status"] == "match"
    assert other["variant_match"] != "exact" and "binding_policy" not in other


def test_r4_brand_policy_scope_does_not_leak_to_other_fields(tmp_path):
    html = """<html><head><title>אחריות | XPENG ישראל</title></head><body><h1>אחריות</h1>
<p>אחריות לרכב: 7 שנים או 160,000 ק"מ.</p><p>אגרת רישוי: 1,200 ש"ח</p><p>אורך: 4,753 מ"מ</p></body></html>"""
    out, material, (cache, doc, adm) = admitted_all(tmp_path, WARRANTY, html)
    scoped = {s["name"] for s in SPECS if s.get("brand_policy_scope")}
    assert scoped == {"vehicle_warranty", "warranty_years", "warranty_km", "battery_hybrid_warranty"}
    others = [r for (name, _), r in out.items() if name not in scoped]
    assert others and all(r.get("binding_basis") != "brand_policy" and r["binding_level"] == "unknown" for r in others)
    for spec in SPECS:
        if spec["name"] not in scoped:
            assert brand_policy_eligibility(adm, material, spec, spec["name"], 1, "IL") is None


# --- R6: replay rejected_now ------------------------------------------------------------------------------------------

def test_r6_stored_braked_towing_evidence_is_rejected_now_with_its_reason(tmp_path):
    from src import binding_replay as R
    from src import diagnostics as D

    cache = DocumentCache(tmp_path / "cache")
    html = """<html><head><title>XPeng G6 - מפרט טכני</title></head><body><h1>XPeng G6</h1>
<p>משקל גרירה עם בלמים 750 ק"ג</p><p>משקל עצמי 2,190 ק"ג</p></body></html>"""
    doc = put(cache, IL_SPEC, html, "html")
    stored = {"evidence_id": "e1", "field": "curb_weight_kg", "value": 750, "unit": "kg", "document_id": doc,
              "source_url": IL_SPEC, "quote": 'משקל גרירה עם בלמים 750 ק"ג', "market": "IL",
              "binding_level": "exact_technical_variant", "variant_match": "exact",
              "binding_requirement": "exact_technical_variant", "source_authority": "official_importer"}
    events = [{"kind": "run_started", "seq": 1, "target_market": "IL", "vehicle_label": {},
               "requested_field_specs": [{"name": "curb_weight_kg"}]},
              {"kind": "evidence", "seq": 2, "evidence": stored}]
    run_dir = tmp_path / "runs" / "R" / "101122"
    run_dir.mkdir(parents=True)
    (run_dir / "input.json").write_text(json.dumps(XPENG, ensure_ascii=False), "utf-8")
    (run_dir / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), "utf-8")
    replay = R.replay_run(run_dir, tmp_path / "cache")
    row = next(r for r in replay["items"] if r["evidence_id"] == "e1")
    assert row["rejected_now"]["reason"] == "semantic_mismatch"
    assert "towing" in row["rejected_now"]["note"] or "גרירה" in row["rejected_now"]["note"]
    assert row["would_be_state"] == "missing"                 # counted as removed
    vehicle = replay["summary"]["vehicle"]
    assert vehicle["rejected_now"] == 1 and vehicle["rejected_now_reasons"] == {"semantic_mismatch": 1}
    assert replay["summary"]["fields"]["curb_weight_kg"]["rejected_now"] == 1
    assert D.vehicle_row(D.load_vehicle_diagnostics(run_dir))["replay_rejected_now"] == 1


# --- R7: the recovery search gate -------------------------------------------------------------------------------------

def test_r7_only_fields_without_admitted_evidence_get_searches():
    from src.tail_planner import search_eligibility

    exact = {"evidence_id": "x", "variant_match": "exact", "binding_level": "exact_technical_variant",
             "binding_requirement": "exact_technical_variant"}
    unclear = {"evidence_id": "u", "variant_match": "unclear", "binding_level": "body_powertrain",
               "binding_requirement": "exact_technical_variant", "binding_dimensions": {"model": {"status": "match"}}}
    evaluation = {"missing": {"state": "missing"}, "binding": {"state": "variant_not_exact"},
                  "conflict_binding": {"state": "conflicting", "conflict_evidence_ids": ["x", "u"]},
                  "conflict_value": {"state": "conflicting", "conflict_evidence_ids": ["x", "y"]},
                  "foreign": {"state": "foreign_market_only"}, "unresolved": {"state": "unresolved"}}
    evidence = {"binding": [unclear], "conflict_binding": [exact, unclear],
                "conflict_value": [exact, {**exact, "evidence_id": "y"}], "foreign": [exact]}
    gate = search_eligibility(list(evaluation), evaluation, evidence)
    assert gate["fields"] == ["missing", "unresolved"]
    assert gate["skipped_binding_blocked"] == ["binding", "conflict_binding"]
    assert gate["skipped_not_missing"] == ["conflict_value", "foreign"]


# --- R8: remaining candidate noise ------------------------------------------------------------------------------------

def harvested(text, field, propulsion="battery_electric"):
    specs = resolve_requested_fields(None, propulsion=propulsion)
    with collect_candidate_rejections() as rejected:
        cands = harvest_text(text, specs)
    return ([c["value"] for c in cands if c["field"] == field],
            [(r["value"], r["rejection"]) for r in rejected if r["field"] == field])


@pytest.mark.parametrize("text,field,bad", [
    ("גרסה: אגרת רישוי לשנה הראשונה", "local_trim_name", "אגרת רישוי לשנה הראשונה"),      # construct form of אגרה
    ("גרסה: דרגת זיהום אוויר", "local_trim_name", "דרגת זיהום אוויר"),                       # construct form of דרגה
    ("Version: Produktplatzierung", "local_trim_name", "Produktplatzierung"),
    ("Version: Teaser", "local_trim_name", "Teaser"),
    ('תא מטען אורך x רוחב x גובה: 1100 x 1200 x 800 מ"מ', "width_mm", 1200),           # a cargo composite
    ('מידות תא המטען - אורך x רוחב x גובה: 1100 x 1200 x 950 מ"מ', "height_mm", 950),
    ("XPeng G6 cargo volume 2026", "cargo_volume_l", 2026),                              # the model year
    ("Trunk capacity: 48.5 ft3", "cargo_volume_l", 48.5),                                # cubic feet
    ("Zulässiges Gesamtgewicht (weight) 2590 kg", "curb_weight_kg", 2590),
    ('משקל כולל מותר 2,553 ק"ג', "curb_weight_kg", 2553),
    ("Trailer weight 1500 kg", "curb_weight_kg", 1500),
    ("Range\n1401", "electric_range_km", 1401),                                           # a bare number below a label
    ("Price range 1401", "electric_range_km", 1401),
    ("Range 26", "electric_range_km", 26),                                               # BEV range below 50 km
    ("WLTP range\n175 Wh/km", "electric_range_km", 175),
    ("Battery MAXXIS 184", "battery_gross_kwh", 184),                                    # no kWh
    ("battery 5", "battery_gross_kwh", 5),
    ("6-speed gearbox", "gear_count", 6),                                                # a BEV has <= 2 gears
    ("Comfort: ★ ★ ★ ★ ★", "other_comfort_features", "★ ★ ★ ★ ★"),
])
def test_r8_noise_is_rejected_at_its_origin(text, field, bad):
    values, _ = harvested(text, field)
    assert bad not in values


@pytest.mark.parametrize("text,field,good,propulsion", [
    ("גרסה: MAX", "local_trim_name", "MAX", "battery_electric"),
    ('אורך x רוחב x גובה: 4753 x 1920 x 1650 מ"מ', "width_mm", 1920, "battery_electric"),
    ('אורך x רוחב x גובה: 4753 x 1920 x 1650 מ"מ', "height_mm", 1650, "battery_electric"),
    ("Trunk capacity: 571 L", "cargo_volume_l", 571, "battery_electric"),
    ('משקל עצמי 2,190 ק"ג', "curb_weight_kg", 2190, "battery_electric"),
    ('טווח נסיעה 550 ק"מ', "electric_range_km", 550, "battery_electric"),
    ("Battery capacity 80.8 kWh", "battery_gross_kwh", 80.8, "battery_electric"),
    ("single-speed gearbox", "gear_count", 1, "battery_electric"),
    ("6-speed gearbox", "gear_count", 6, "conventional"),
    ("Comfort: heated steering wheel", "other_comfort_features", "heated steering wheel", "battery_electric"),
])
def test_r8_the_normal_value_still_passes(text, field, good, propulsion):
    values, _ = harvested(text, field, propulsion)
    assert good in values


def test_r8_rejections_carry_their_reason_and_seats_folded_volume_shows_as_rejected():
    _, rejected = harvested("Trunk capacity: 1374 L (seats folded)", "cargo_volume_l")
    assert (1374, "seats-folded / maximum / front-trunk volume") in rejected
    _, rejected = harvested("XPeng G6 cargo volume 2026", "cargo_volume_l")
    assert (2026, "a model year, not a value of this field") in rejected
    _, rejected = harvested("Battery MAXXIS 184", "battery_gross_kwh")
    assert (184, "no unit of this field stated with the value") in rejected


def test_r8_model_proposed_values_hit_the_same_gate(tmp_path):
    html = """<html><head><title>XPeng G6</title></head><body><h1>XPeng G6</h1>
<p>גרסה: אגרת רישוי לשנה הראשונה</p><p>Trailer weight 1500 kg</p></body></html>"""
    cache = DocumentCache(tmp_path / "cache")
    doc = put(cache, IL_SPEC, html, "html")
    adm = AdmissionContext.for_run(XPENG, None, SPECS, "IL")
    for field, value, quote in (("local_trim_name", "אגרת רישוי לשנה הראשונה", "גרסה: אגרת רישוי לשנה הראשונה"),
                                ("curb_weight_kg", 1500, "Trailer weight 1500 kg")):
        decision = admit(adm, cache, {"field": field, "value": value, "quote": quote, "document_id": doc}, [doc])
        assert not decision["accepted"] and decision["reasons"] == ["semantic_mismatch"], field


def test_r3_ps_is_metric_horsepower_like_the_government_power(index):
    index({AWD485: {"trims": ["MAX"]}, AWD475: {"trims": ["PERF TB"]}})
    # 486 PS is the catalog's 486 כ"ס; x0.986 (479 hp) would miss the tighter tolerance and veto an official page
    assert statuses("XPENG G6 AWD Performance 486 PS", xpeng())["power"] == "match"


# --- proof gate: every stored fixture run -----------------------------------------------------------------------------

def test_the_stored_synthetic_g6_run_replays_from_binding_v3_to_v4(tmp_path):
    """tests/fixtures/pr40_runs: a SYNTHETIC G6 run recorded on main (binding-v3); see tests/fixtures/pr40_g6_run.py."""
    import shutil

    from src import binding_replay as R
    from src.ui.diagnostics_view import binding_rows

    shutil.copytree(R.ROOT.parent / "tests" / "fixtures" / "pr40_runs", tmp_path / "runs")
    replay = R.replay_run(tmp_path / "runs" / "SYNTH-pr40" / "101122", tmp_path / "runs" / "_cache", write=False)
    vehicle = replay["summary"]["vehicle"]
    assert (vehicle["fields_ok_recorded"], vehicle["fields_ok_now"]) == (1, 10)
    assert vehicle["gap_counts_recorded"] == {"propulsion_absent": 9, "model_absent": 3}
    assert vehicle["fields_newly_ok_by_rule"] == {
        "ac_max_charging_power_kw": ["dvm_shared"], "acceleration_0_100_s": ["dvm_region"],
        "cargo_volume_l": ["dvm_shared"], "curb_weight_kg": ["dvm_region"], "electric_range_km": ["dvm_region"],
        "height_mm": ["dvm_shared"], "vehicle_warranty": ["brand_policy"], "warranty_km": ["brand_policy"],
        "warranty_years": ["brand_policy"]}
    # the hedged DC figure ("עד 451 kW") is the one field that stays open
    assert replay["summary"]["fields"]["dc_max_charging_power_kw"]["would_be_state"] == "variant_not_exact"
    # nothing exact before is lost (the foreign page's "486 PS" items stay exact)
    assert not [r for r in replay["items"] if r["kind"] == "evidence" and r["variant_match_recorded"] == "exact"
                and r["variant_match_now"] != "exact"]
    rows = binding_rows(replay["items"])
    assert any(r["basis now"].startswith("dvm_region") and r["variant map region"].startswith("table:0:col:2 target")
               for r in rows)
