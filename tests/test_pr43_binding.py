"""PR #43: hybrid / combustion binding precision (synthetic fixtures from the production excerpts of runs 20261003T221935Z
and 20261004T171912Z; no network).

    H1  hybrid / plug-in technical identity: another power in the fact (system_power_unmapped) or a document naming
        several powertrain versions (several_powertrain_versions) never binds by displacement alone; only a target region
        that names the version (designation / model code / catalog trim) does
    H2  a value clause referring to another version ("הבכיר יותר") binds at most body_powertrain
    H3  a publication >= 2 years before the target model year that never states that year caps at generation
    H4  combustion / hybrid region identity: designations ("40TFSI", "55 TFSI e"), litres before a designation, gearbox
    H5  "אורך x רוחב x גובה | 4,758 / 1,920 / 1,650 מ״מ" and L×W×H triples: one candidate per field, height admitted
    H6  a wheel size ("עם חישוקי ״20") is never a range / consumption value
"""

from __future__ import annotations

import pytest

from fixtures import pr42_g6_pages as G
from fixtures import pr43_audi_pages as P
from fixtures.corolla_harvest import put
from src.candidate_harvest import harvest_text
from src.document_binding import binding_gaps, designations, mentions, target_identity
from src.evidence_admission import AdmissionContext, admit, article_date
from src.fields import resolve_requested_fields
from src.storage.cache import DocumentCache
from src.variant_map import identity_vector


def _admit(cache, payload, url, html, field, value, quote):
    propulsion = payload["engine_drivetrain"]["propulsion_normalized"]
    adm = AdmissionContext.for_run(payload, None, resolve_requested_fields(None, propulsion=propulsion), "IL")
    doc = put(cache, url, html, "html")
    decision = admit(adm, cache, {"field": field, "value": value, "quote": quote, "document_id": doc}, [doc])
    assert decision["accepted"], decision
    return decision["record"], adm.material(cache, doc, None)


@pytest.fixture()
def cache(tmp_path):
    return DocumentCache(tmp_path / "cache")


# --- H1 + H3: the Q8 2020 article ---------------------------------------------------------------------------------------

def test_q8_2020_article_acceleration_is_not_exact(cache):
    record, material = _admit(cache, P.Q8_PAYLOAD, P.Q8_2020_URL, P.q8_2020_html(), "acceleration_0_100_s", 5.8,
                              P.Q8_2020_QUOTE)
    # recorded in production as exact_technical_variant ("proven" by displacement 3.0 alone)
    assert record["variant_match"] != "exact"
    assert record["binding_level"] == "generation"
    flags = record["binding_flags"]
    assert {"system_power_unmapped", "several_powertrain_versions", "stale_publication"} <= set(flags)
    assert record["stale_publication"] == {"publication_date": "2020", "basis": "article_text", "target_year": 2025}
    assert set(binding_gaps(record)) >= {"system_power_unmapped", "stale_publication"}
    versions = material.profile["powertrain_versions"]
    assert versions["designations"] == ["55tfsie", "60tfsie"] and {340.0, 381.0, 462.0} <= set(versions["powers"])
    assert material.profile["states_target_year"] is False


def test_without_the_stale_date_the_system_power_alone_keeps_it_unresolved(cache):
    html = P.q8_2020_html().replace("<p>אוקטובר 13, 2020</p>", "")
    record, _ = _admit(cache, P.Q8_PAYLOAD, P.Q8_2020_URL + "-x", html, "acceleration_0_100_s", 5.8, P.Q8_2020_QUOTE)
    assert record["binding_level"] == "body_powertrain" and record["variant_match"] == "unclear"
    assert "stale_publication" not in record["binding_flags"] and "system_power_unmapped" in record["binding_flags"]
    assert record["binding_dimensions"]["displacement"]["status"] == "match"     # displacement alone is not enough


def test_q8_2025_single_version_page_binds_by_its_designation_region(cache):
    record, material = _admit(cache, P.Q8_PAYLOAD, P.Q8_2025_URL, P.q8_2025_html(), "acceleration_0_100_s", 5.7,
                              'תאוצה 0-100 קמ"ש: 5.7 שניות')
    assert record["variant_match"] == "exact" and record["binding_level"] == "exact_technical_variant"
    assert record["binding_basis"] == "dvm_region"
    region = record["variant_map_region"]
    assert region["status"] == "target" and region["identity"]["designation"] == ["55tfsie"]
    assert region["identity"]["power"] == [340.0] and region["assignment"] == "אאודי|q8|2025|suv|plug_in|awd|340|3.0"
    # the electric motor's 136 hp makes it a multi-power document: only the region naming the version proved it
    assert record["binding_flags"] == ["several_powertrain_versions"]
    assert record["binding_dimensions"]["version"]["designation"] == ["55tfsie"]


def test_hybrid_rules_leave_conventional_and_electric_targets_alone():
    q3 = target_identity(P.Q3_PAYLOAD)
    from src.document_binding import bind, statuses
    doc = statuses("Audi Q3 2.0 TFSI quattro SUV", q3)
    out = bind(q3, doc, [("quote", "Q3 35 TFSI 150 כ\"ס ו-Q3 40 TFSI 190 כ\"ס")], requirement="exact_technical_variant",
               powertrain_versions={"powers": [150.0, 190.0], "designations": ["35tfsi", "40tfsi"]})
    assert "binding_flags" not in out                    # power stays a veto / mixed dimension for conventional cars


# --- H3 -----------------------------------------------------------------------------------------------------------------

def test_stale_official_launch_page_caps_at_generation(cache):
    xpeng = G.PAYLOAD
    html = """<html><head><title>XPeng G6 | השקה בישראל</title></head><body><h1>XPeng G6</h1>
<p>רכב חשמלי SUV, הנעה כפולה AWD</p><p>הספק מרבי: 486 כ"ס</p><p>משקל עצמי: 2,180 ק"ג</p></body></html>"""
    stale, _ = _admit(cache, xpeng, "https://www.xpeng.co.il/2024/03/xpeng-g6-launch", html, "curb_weight_kg", 2180,
                      'משקל עצמי: 2,180 ק"ג')
    assert stale["binding_level"] == "generation" and stale["binding_flags"] == ["stale_publication"]
    assert stale["stale_publication"]["basis"] == "url"
    fresh, _ = _admit(cache, xpeng, "https://www.xpeng.co.il/2025/03/xpeng-g6-launch", html, "curb_weight_kg", 2180,
                      'משקל עצמי: 2,180 ק"ג')
    assert fresh["variant_match"] == "exact" and "binding_flags" not in fresh
    # an official page states no article date of its own: only metadata / URL dates count for it
    dated = html.replace("<h1>XPeng G6</h1>", "<h1>XPeng G6</h1><p>מרץ 3, 2023</p>")
    official, _ = _admit(cache, xpeng, "https://www.xpeng.co.il/g6/launch", dated, "curb_weight_kg", 2180,
                         'משקל עצמי: 2,180 ק"ג')
    assert official["variant_match"] == "exact"


def test_stale_document_with_a_designation_region_assigned_by_power_is_the_exception(cache):
    html = (P.q8_2025_html().replace(" פלאג-אין 2025", " פלאג-אין").replace("TFSI e 2025", "TFSI e")
            .replace("<head>", '<head><script type="application/ld+json">{"@type": "Article", '
                               '"datePublished": "2022-05-01"}</script>'))
    record, material = _admit(cache, P.Q8_PAYLOAD, P.Q8_2025_URL + "?v=2022", html, "acceleration_0_100_s", 5.7,
                              'תאוצה 0-100 קמ"ש: 5.7 שניות')
    assert material.doc.publication_date == "2022-05-01" and not material.profile["states_target_year"]
    assert record["variant_match"] == "exact" and "stale_publication" not in record.get("binding_flags", [])


def test_article_date_is_the_first_standalone_date_line():
    assert article_date("כותרת\nאוקטובר 13, 2020\nטקסט\nרביעי, 2026.09.16") == "2020"
    assert article_date("13/10/2021\nx") == "2021" and article_date("ללא תאריך 2020 בשורה") is None


# --- H2: the Q7 article -------------------------------------------------------------------------------------------------

def test_q7_senior_version_wheels_are_a_relative_reference(cache):
    record, _ = _admit(cache, P.Q7_PAYLOAD, P.Q7_URL, P.q7_html(), "rim_diameter_in", 22, P.Q7_QUOTE_22)
    # recorded in production as exact_market_trim (qualified_trim_phrase "q7 s line" in the section heading)
    assert record["binding_level"] == "body_powertrain" and record["variant_match"] == "unclear"
    assert "relative_variant_reference" in record["binding_flags"] and record["relative_reference"] == "הבכיר יותר"
    assert "relative_variant_reference" in binding_gaps(record)


def test_q7_21_inch_binds_only_through_a_target_region_with_its_designation(cache):
    plain, _ = _admit(cache, P.Q7_PAYLOAD, P.Q7_URL, P.q7_html(), "rim_diameter_in", 21, P.Q7_QUOTE_21)
    assert plain["variant_match"] != "exact" and "several_powertrain_versions" in plain["binding_flags"]
    linked, material = _admit(cache, P.Q7_PAYLOAD, P.Q7_URL + "?table=1", P.q7_html(with_table=True),
                              "rim_diameter_in", 21, P.Q7_QUOTE_21)
    region = linked["variant_map_region"]
    assert region["reason"] == "designation_region" and region["designation"] == "55tfsie"
    assert region["region_id"] == "table:0:col:1" and region["assignment"] == "אאודי|q7|2025|suv|plug_in|awd|340|3.0"
    assert linked["variant_match"] == "exact" and linked["binding_basis"] == "designation_region"
    # the senior version's 22" stays a relative reference even with the table
    senior, _ = _admit(cache, P.Q7_PAYLOAD, P.Q7_URL + "?table=1", P.q7_html(with_table=True), "rim_diameter_in", 22,
                       P.Q7_QUOTE_22)
    assert senior["binding_level"] == "body_powertrain"
    regions = {r["id"]: r for r in material.variant_map["regions"]}
    assert regions["table:0:col:2"]["assignment"] == "אאודי|q7|2025|suv|plug_in|awd|490|3.0"


def test_relative_terms_come_from_the_vocabulary():
    from src.document_binding import relative_reference, vocabulary

    terms = vocabulary()["relative_variant_terms"]["terms"]
    assert {"הבכיר", "הבכירה", "הגרסה החזקה", "בגרסה העליונה", "the top version", "the more powerful", "flagship",
            "senior"} <= set(terms)
    assert relative_reference("The flagship adds 22-inch wheels") == "flagship"
    assert relative_reference("חישוקי 21 אינץ'") is None


# --- H4: combustion / hybrid identity in the DVM ------------------------------------------------------------------------

def test_cartube_40tfsi_page_assigns_to_the_190_hp_awd_catalog_variant(cache):
    record, material = _admit(cache, P.Q3_PAYLOAD, P.Q3_URL, P.q3_html(), "acceleration_0_100_s", 7.3,
                              "תאוצה 0-100 | 7.3 שנ'")
    group = {r["id"]: r for r in material.variant_map["regions"]}["group:0"]
    assert group["identity"]["designation"] == ["40tfsi"] and group["identity"]["displacement"] == [2.0]
    assert group["identity"]["power"] == [190.0] and group["identity"]["drivetrain"] == ["awd"]
    assert group["assignment"] == "אאודי|q3|2024|suv|conventional|awd|190|2.0"
    assert record["variant_match"] == "exact" and record["variant_map_region"]["region_id"] == "group:0"


@pytest.mark.parametrize("text,expected", [
    ("Q8 55 TFSI e quattro", {"55tfsie"}), ("2.0 40TFSI 4X4", {"40tfsi"}), ("Q7 60 TFSIe", {"60tfsie"}),
    ("Golf 1.5 TSI", set()), ("Wrangler 4xe", {"4xe"}), ("Range Rover P400e", {"p400e"}), ("Q5 40 TDI", {"40tdi"}),
])
def test_designations(text, expected):
    assert designations(text.lower()) == expected


def test_combustion_identity_vectors():
    q3 = target_identity(P.Q3_PAYLOAD)
    vec = identity_vector("אודי Q3 2.0 40TFSI 4X4 S tronic", q3, [])
    assert vec["displacement"] == [2.0] and vec["designation"] == ["40tfsi"] and vec["gearbox"] == ["automatic"]
    assert identity_vector("1.5 TSI DSG 150 PS", q3, [])["displacement"] == [1.5]
    assert mentions('הספק מרבי 140 kW', q3)["power"] == {round(140 * 1.35962, 1)}         # kW in a motor context
    assert mentions("טעינה מהירה בהספק של 140 kW", q3)["power"] == set()                  # charging stays excluded
    assert mentions('נפח מנוע 1,984 סמ"ק', q3)["displacement"] == {2.0}


def test_a_region_whose_gearbox_contradicts_the_target_stays_unresolved():
    from src.variant_map import region_verdict

    q3 = target_identity(P.Q3_PAYLOAD)
    region = {"identity": identity_vector("Q3 40 TFSI ידני 190 כ\"ס", q3, []), "status": "assigned",
              "assignment": "אאודי|q3|2024|suv|conventional|awd|190|2.0"}
    assert region_verdict(region, q3)["reason"] == "gearbox"


# --- H5 + H6 ------------------------------------------------------------------------------------------------------------

SPECS_BEV = resolve_requested_fields(None, propulsion="battery_electric")


@pytest.mark.parametrize("text", ["אורך x רוחב x גובה | 4,758 / 1,920 / 1,650 מ״מ",
                                  "Dimensions (L×W×H): 4758 x 1920 x 1650 mm",
                                  "אורך x רוחב x גובה (מ\"מ) | 4758 x 1920 x 1650"])
def test_dimension_triples_give_one_candidate_per_field(text):
    got = sorted((c["field"], c["value"]) for c in harvest_text(text, SPECS_BEV) if c["field"].endswith("_mm"))
    assert got == [("height_mm", 1650), ("length_mm", 4758), ("width_mm", 1920)]


def test_g6_dimension_triple_is_admitted_for_each_field_and_height_is_not_rejected(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    adm = AdmissionContext.for_run(G.PAYLOAD, None, SPECS_BEV, "IL")
    page = put(cache, G.PAGE_URL, G.page_html(), "html")
    quote = "אורך x רוחב x גובה | 4,758 / 1,920 / 1,650 מ״מ"
    for field, value in (("length_mm", 4758), ("width_mm", 1920), ("height_mm", 1650)):
        decision = admit(adm, cache, {"field": field, "value": value, "quote": quote, "document_id": page}, [page])
        assert decision["accepted"], (field, decision)      # was value_belongs_to_other_field / semantic_mismatch
        assert decision["record"]["variant_match"] == "exact"
        assert decision["record"]["variant_map_region"]["tab_label"] == "Core Performance AWD"
    # a value of another field is still refused (#38 sibling rule)
    wrong = admit(adm, cache, {"field": "width_mm", "value": 4758, "quote": quote, "document_id": page}, [page])
    assert not wrong["accepted"]


def test_the_seat_exclusion_still_reads_the_values_own_clause(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    adm = AdmissionContext.for_run(G.PAYLOAD, None, SPECS_BEV, "IL")
    page = put(cache, G.PAGE_URL + "?h", "<html><body><p>גובה מושב: 1,650 מ\"מ</p></body></html>", "html")
    decision = admit(adm, cache, {"field": "height_mm", "value": 1650, "quote": 'גובה מושב: 1,650 מ"מ',
                                  "document_id": page}, [page])
    assert not decision["accepted"] and decision["reasons"] == ["semantic_mismatch"]


@pytest.mark.parametrize("text", ['צריכת חשמל / עם חישוקי ״20 (kWh/100 km) 17.5 / 17.9 18.4',
                                  'טווח נסיעה חשמלית / עם חישוקי ״20 (km) 535 / 525 510',
                                  "Range with 20\" wheels: 20 km"])
def test_wheel_sizes_are_not_range_or_consumption_candidates(text):
    cands = harvest_text(text, SPECS_BEV)
    assert not [c for c in cands if c["field"] in ("electric_range_km", "energy_consumption_kwh_100km")
                and c["value"] == 20]
    assert [c["value"] for c in cands if c["field"] == "rim_diameter_in"] == [20]      # still the rim's value


def test_a_wheel_size_is_never_admitted_as_consumption(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    adm = AdmissionContext.for_run(G.PAYLOAD, None, SPECS_BEV, "IL")
    page = put(cache, G.BROCHURE_URL + "?x", "<html><body><p>צריכת חשמל / עם חישוקי ״20 kWh/100km</p></body></html>",
               "html")
    decision = admit(adm, cache, {"field": "energy_consumption_kwh_100km", "value": 20,
                                  "quote": "צריכת חשמל / עם חישוקי ״20 kWh/100km", "document_id": page}, [page])
    assert not decision["accepted"], decision


# --- the proof-gate fixture runs (recorded by main @ 43eff6d) -----------------------------------------------------------

def test_pr43_fixture_runs_replay():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from pr43_proof_gate import replay_fixture

    runs = {r["run"]: r for r in replay_fixture()["runs"]}
    changes = {run: r["field_state_changes"] for run, r in runs.items()}
    assert changes == {
        "SYNTH-pr43/101122": {"height_mm": {"state_recorded": "variant_not_exact", "state_now": "ok"}},
        "SYNTH-pr43/15376": {f: {"state_recorded": "ok", "state_now": "variant_not_exact"}
                             for f in ("acceleration_0_100_s", "gear_count", "gearbox_type")},
        "SYNTH-pr43/15400": {"rim_diameter_in": {"state_recorded": "ok", "state_now": "variant_not_exact"}},
        "SYNTH-pr43/94995": {}}
    q8 = {i["field"]: i for i in runs["SYNTH-pr43/15376"]["items"] if i["kind"] == "evidence"}
    assert q8["top_speed_kmh"]["variant_match_now"] == "exact" and q8["top_speed_kmh"]["region"]["status"] == "target"
    g6 = [i for i in runs["SYNTH-pr43/101122"]["items"] if i["kind"] == "candidate" and i["field"] in
          ("length_mm", "width_mm") and i["value"] in (4758, 1920)]
    assert len(g6) == 2 and all(i["variant_match_now"] == "exact" for i in g6)
    assert all(not i["entailment_now"].startswith("failed") for i in g6)
