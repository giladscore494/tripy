"""PR #46 P1: generation / multi-version precision (the A6 2018 / 319.pdf case of production run 20261005T184853Z).

The fixture (fixtures/pr46_a6_319.json) is the document exactly as production cached it: its full extracted text and its
pdfplumber tables (the cache's derived tables_v3), so the replay below takes the production path (variant map region
`none`, alias-proximity candidate) that bound length / width / height at exact_technical_variant.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from fixtures import pr45_pages as F
from fixtures.corolla_harvest import put
from src.binding_replay import replay_fact
from src.evidence_admission import AdmissionContext
from src.fields import resolve_requested_fields
from src.storage.cache import DocumentCache

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "pr46_a6_319.json").read_text("utf-8"))
DIMENSIONS = [("length_mm", 4939, 'אורך (מ"מ) 4,939 4,939'), ("width_mm", 1886, 'רוחב (מ"מ) 1,886 1,886'),
              ("height_mm", 1411, 'גובה (מ"מ) 1,411 1,411')]


@pytest.fixture()
def cache(tmp_path):
    return DocumentCache(tmp_path / "cache")


def _adm(payload=F.A6_PAYLOAD):
    propulsion = payload["engine_drivetrain"]["propulsion_normalized"]
    return AdmissionContext.for_run(payload, None, resolve_requested_fields(None, propulsion=propulsion), "IL")


def _doc(cache, text=FIXTURE["text"], url=FIXTURE["url"]):
    doc = put(cache, url, text, "pdf")
    cache.put_derived(doc, "tables_v3", copy.deepcopy(FIXTURE["tables"]))
    return doc


def _replay(cache, doc, field, value, quote, adm=None, url=FIXTURE["url"]):
    adm = adm or _adm()
    pool = {doc: [{"field": field, "value": value, "quote": quote, "extraction_method": "alias_proximity",
                   "document_id": doc}]}
    return replay_fact(adm, cache, field=field, value=value, quote=quote, document_id=doc, source_url=url,
                       candidates=pool)


# --- P1.1: power rows ---------------------------------------------------------------------------------------------------

def test_the_319_power_row_is_read_with_its_column_identity():
    from src.power_rows import power_rows, powers_by_designation

    text_only = power_rows(FIXTURE["text"])
    assert text_only["powers"] == [245.0, 340.0]                     # the reversed-RTL line alone
    both = power_rows(FIXTURE["text"], FIXTURE["tables"])
    assert both["powers"] == [245.0, 340.0]
    assert powers_by_designation(both) == {"55tfsi": [340.0], "45tfsi": [245.0]}
    table_columns = [c for c in both["columns"] if c["source"].startswith("table:")]
    assert {c["identity"] for c in table_columns} == {"A6 Design 55 TFSI quattro", "A6 Design 45 TFSI quattro*"}


@pytest.mark.parametrize("line, powers", [
    ('הספק מרבי (כ"ס) 190 245', [190.0, 245.0]),
    ('הספק (כ"ס/סל"ד) 150/5,500', [150.0]),
    ("כוח סוס 204", [204.0]),
    ('הספק מנוע חשמלי (קילוואט) 150', [203.9]),               # kW in a motor context: converted to PS
    ('הספק טעינה מרבי (קילוואט) 11', []),                     # PR #42: charging power is never motor power
    ('מומנט מרבי (קג"מ) 32.6 25.5', []),                       # torque rows never
    ("הספק מרבי 245", []),                                     # no horsepower unit: not read
])
def test_power_row_labels(line, powers):
    from src.power_rows import power_rows

    assert power_rows(line)["powers"] == powers


def test_document_versions_now_lists_the_powers(cache):
    doc = _doc(cache)
    row = _replay(cache, doc, *DIMENSIONS[0])
    versions = row["document_versions_now"]
    assert versions["powers"] == [245.0, 340.0] and versions["designations"] == ["45tfsi", "55tfsi"]
    assert {(c["designation"], c["power"]) for c in versions["power_columns"]} == {("55tfsi", 340.0),
                                                                                    ("45tfsi", 245.0)}


# --- P1.2: the R4 "repeated in every column" exception ------------------------------------------------------------------

def test_a6_319_dimensions_are_not_ok_for_the_2018_252_hp_target(cache):
    """Proof gate 1: run 20261005T184853Z record 12949, length / width / height -> not ok."""
    doc = _doc(cache)
    for field, value, quote in DIMENSIONS:
        row = _replay(cache, doc, field, value, quote)
        assert row["variant_match_now"] != "exact", (field, row)
        assert row["binding_level_now"] == "body_powertrain"
        assert row["binding_gap_now"] == ["repeated_without_target_version"]
        listed = row["document_versions_now"]["target_listed"]
        # 245 hp is within 3 % of 252 hp, but the catalog states no model year: the near power alone proves nothing
        assert listed == {"designation": False, "power": True, "catalog_single_entry": False, "year_stated": False}


def test_torque_and_top_speed_stay_as_the_binding_decides(cache):
    doc = _doc(cache)
    torque = _replay(cache, doc, "torque_nm", 500, F.A6_319_TORQUE_QUOTE)
    assert torque["variant_match_now"] != "exact"
    speed = _replay(cache, doc, "top_speed_kmh", 250, 'מהירות מירבית (קמ"ש) 250 250')
    assert speed["variant_match_now"] != "exact"


def test_the_same_document_with_a_stated_matching_year_stays_exact(cache):
    text = FIXTURE["text"].replace("[page 3]\n", "[page 3]\nA6 2018\n", 1)
    doc = _doc(cache, text=text, url="https://static.auto.co.il/media/fixture/319-2018.pdf")
    row = _replay(cache, doc, *DIMENSIONS[0], url="https://static.auto.co.il/media/fixture/319-2018.pdf")
    assert row["document_versions_now"]["target_listed"]["year_stated"] is True
    assert row["binding_level_now"] == "exact_technical_variant" and row["variant_match_now"] == "exact", row


def test_a_no_year_document_naming_the_target_designation_stays_exact(cache):
    payload = copy.deepcopy(F.A6_PAYLOAD)
    payload["identity"]["commercial_name"] = "A6 45 TFSI"
    row = _replay(cache, _doc(cache), *DIMENSIONS[0], adm=_adm(payload))
    assert row["document_versions_now"]["target_listed"]["designation"] is True
    assert row["binding_level_now"] == "exact_technical_variant", row


def test_a_single_entry_catalog_stays_exact(cache, monkeypatch):
    from src import il_version_pages

    monkeypatch.setattr(il_version_pages, "single_catalog_entry",
                        lambda identity, index=None: {"key": "אאודי|a6|2018|sedan|conventional|awd|250|2.0",
                                                      "records": ["12949"]})
    row = _replay(cache, _doc(cache), *DIMENSIONS[0])
    assert row["document_versions_now"]["target_listed"]["catalog_single_entry"] is True
    assert row["binding_level_now"] == "exact_technical_variant", row


# --- P1.3: power-less multi-version documents ---------------------------------------------------------------------------

def test_a_powerless_two_designation_document_without_a_year_is_version_unproven():
    from src.document_binding import bind, binding_gaps, target_identity

    identity = target_identity(F.A6_PAYLOAD)
    statuses = {"model": "match", "year": "absent", "body": "absent", "propulsion": "absent",
                "displacement": "match", "power": "absent", "drivetrain": "match", "model_code": "absent",
                "trim": "absent", "manufacturer": "match"}
    mv = {"versions": 2, "powers": [], "designations": ["45tfsi", "55tfsi"], "repeated": True,
          "target_listed": {"year_stated": False}}
    out = bind(identity, statuses, [], market="IL", multi_version=mv)
    assert out["binding_level"] == "body_powertrain" and out["binding_flags"] == ["version_unproven"]
    assert binding_gaps(out) == ["version_unproven"]
    # a fact-level power naming the version, or the catalog's single entry, still identifies it
    own = bind(identity, statuses, [("value_clause", "45 TFSI 2.0 252 כ\"ס")], market="IL", multi_version=mv)
    assert "version_unproven" not in (own.get("binding_flags") or [])
    single = bind(identity, statuses, [], market="IL",
                  multi_version={**mv, "target_listed": {"catalog_single_entry": True}})
    assert single["binding_level"] == "exact_technical_variant"
