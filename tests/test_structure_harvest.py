"""Candidate yield, deterministic parts (PR #31): structural DOM pairs (Part A), PDF tables without ruling lines
(Part B) and unit-anchored extraction (Part C). Candidates only: nothing here is evidence."""

import json

import pytest

from fixtures import corolla_harvest
from fixtures.mini_pdf import make_pdf, spec_matrix

from src.candidate_harvest import (HARVESTER_VERSION, dictionary_for, document_segments, harvest_document,
                                   harvest_text, merge_additions, unit_anchor_candidates)
from src.document_sweep import TABLE_METHODS
from src.evidence_admission import DocumentMaterial, document_text, quote_in_source
from src.fields import load_schema
from src.storage.cache import DocumentCache
from src.structure_harvest import MAX_PAIRS, html_pairs
from src.tools import extract as extract_mod
from src.tools.extract import TableVocabulary, _html_tables, _pdf_tables, visible_text

SPEC_PAGE = "https://www.toyota.co.il/new-cars/corolla/specifications"
SPEC_HTML = """<html><head><title>טויוטה קורולה 2024 - מפרט טכני</title></head><body>
<header><div><div>אורך</div><div>9,999 מ"מ</div></div></header>
<nav><ul><li><span>רוחב</span><span>1,111 מ"מ</span></li></ul></nav>
<div id="cookie-consent"><p>גובה: 1,222 מ"מ</p></div>
<h1>טויוטה קורולה 2024 1.8 היברידי</h1>
<h2>מידות</h2>
<dl><dt>אורך</dt><dd>4,630 מ"מ</dd><dt>רוחב</dt><dd>1,780 מ"מ</dd></dl>
<h3>רמת גימור BUSINESS EDI</h3>
<ul class="spec-list">
<li class="spec"><span class="k">בסיס גלגלים</span><span class="v">2,700 מ"מ</span></li>
<li class="spec"><span class="k">נפח מיכל דלק</span><span class="v">43 ליטר</span></li>
<li class="spec"><span class="k">מסך מולטימדיה</span><span class="v">10.5 אינץ'</span></li>
</ul>
<div class="cards">
<div class="card-row"><div class="label">אחריות</div><div class="value">3 שנים או 100,000 ק"מ</div></div>
<div class="card-row"><div class="label">מחיר</div><div class="value">179,990 ש"ח</div></div>
<div class="card-row"><div class="label">רמת גימור</div><div class="value">BUSINESS EDI</div></div>
</div>
<p>משקל עצמי: 1,385 ק"ג</p>
<div style="display:none"><div><div>גובה</div><div>1,999 מ"מ</div></div></div>
<footer><div><div>רוחב</div><div>1,333 מ"מ</div></div></footer>
</body></html>"""


@pytest.fixture(scope="module")
def specs():
    return load_schema()


def _put_html(cache, url, html):
    meta = {"status": 200, "final_url": url, "doc_type": "html", "content_type": "text/html"}
    return cache.put("fetch", url, html.encode("utf-8"), meta, visible_text(html))["document_id"]


def by_field(cands):
    out = {}
    for c in cands:
        out.setdefault(c["field"], []).append(c)
    return out


# --- Part A -------------------------------------------------------------------------------------------------------

def test_dom_pairs_from_dl_list_cards_and_label_value_text(tmp_path, specs):
    cache = DocumentCache(tmp_path)
    doc = _put_html(cache, SPEC_PAGE, SPEC_HTML)
    cands, _ = harvest_document(cache, doc, specs)
    fields = by_field(cands)
    pairs = {c["field"]: c for c in cands if c["extraction_method"] == "dom_pair"}
    for name, value in (("wheelbase_mm", 2700), ("fuel_tank_l", 43), ("screen_size_in", 10.5),
                        ("curb_weight_kg", 1385)):
        assert pairs[name]["value"] == value, name
    # <dl> rows are already table rows (extract._html_tables); the pair agrees and the table row stays
    assert fields["length_mm"][0]["value"] == 4630 and fields["width_mm"][0]["value"] == 1780
    dl_pairs = html_pairs(SPEC_HTML, visible_text(SPEC_HTML), alias_pattern=dictionary_for(specs).any_alias,
                          trim_header=dictionary_for(specs).trim_header)
    assert {("אורך", '4,630 מ"מ'), ("רוחב", '1,780 מ"מ')} <= {(p["label"], p["value"]) for p in dl_pairs}
    # quotes are "label | value", like table rows
    assert pairs["wheelbase_mm"]["quote"] == 'בסיס גלגלים | 2,700 מ"מ'
    # a trim heading over a repeated group becomes the segment header (variant hint)
    assert pairs["wheelbase_mm"]["variant_hint"] == "רמת גימור BUSINESS EDI"
    # nav / footer / header / cookie / hidden pairs are never read as pairs
    values = {(p["label"], p["value"]) for p in dl_pairs}
    for bad in ('9,999 מ"מ', '1,111 מ"מ', '1,222 מ"מ', '1,999 מ"מ', '1,333 מ"מ'):
        assert not any(v == bad for _, v in values)
    assert not any(c["extraction_method"] == "dom_pair" and c["value"] in (9999, 1111, 1222, 1999, 1333)
                   for c in cands)
    assert "dom_pair" in TABLE_METHODS


def test_every_dom_pair_quote_passes_admission_quote_check(tmp_path, specs):
    cache = DocumentCache(tmp_path)
    doc = _put_html(cache, SPEC_PAGE, SPEC_HTML)
    cands, _ = harvest_document(cache, doc, specs)
    material = DocumentMaterial(doc=document_text(cache, cache.get(doc)), profile={}, authority={}, candidates=[])
    pairs = [c for c in cands if c["extraction_method"] == "dom_pair"]
    assert pairs
    for cand in pairs:
        assert quote_in_source(material, cand["quote"]), cand["quote"]
    d = dictionary_for(specs)
    for pair in html_pairs(SPEC_HTML, cache.read_text(doc), alias_pattern=d.any_alias, trim_header=d.trim_header):
        assert quote_in_source(material, pair["quote"]), pair


def test_pair_quote_is_joined_or_fragment_joined_else_dropped(specs):
    from src.structure_harvest import admission_haystack, admissible_quote

    d = dictionary_for(specs)
    # a <dt> with two <dd>: the second value is not next to its label in the text admission reads
    html = '<dl><dt>אורך</dt><dd>4,630 מ"מ</dd><dd>4,700 מ"מ</dd></dl>'
    pairs = html_pairs(html, visible_text(html), alias_pattern=d.any_alias, trim_header=d.trim_header)
    assert [p["quote"] for p in pairs] == ['אורך | 4,630 מ"מ', 'אורך … 4,700 מ"מ']
    # label and value further apart than admission's fragment window: never emitted
    far = admission_haystack("", "אורך " + "מילה " * 200 + '4,630 מ"מ')
    assert admissible_quote(far, "אורך", '4,630 מ"מ') is None
    assert admissible_quote(admission_haystack("", "רוחב 1"), "אורך", '4,630 מ"מ') is None


def test_isolated_pair_needs_an_alias_repeated_group_does_not(specs):
    d = dictionary_for(specs)
    lone = "<div><div><span>Opening hours</span><span>9-17</span></div></div>"
    assert html_pairs(lone, visible_text(lone), alias_pattern=d.any_alias, trim_header=d.trim_header) == []
    group = "<ul>" + "".join(f"<li><span>Item {i}</span><span>{i} units</span></li>" for i in range(3)) + "</ul>"
    assert len(html_pairs(group, visible_text(group), alias_pattern=d.any_alias, trim_header=d.trim_header)) == 3


def test_pair_limits_and_dedupe(specs):
    d = dictionary_for(specs)
    rows = "".join(f"<li><span>Row {i}</span><span>{i} mm</span></li>" for i in range(MAX_PAIRS + 50))
    dup = "<li><span>Row 1</span><span>1 mm</span></li>" * 3
    html = f"<ul>{rows}{dup}</ul>"
    pairs = html_pairs(html, visible_text(html), alias_pattern=d.any_alias, trim_header=d.trim_header)
    assert len(pairs) == MAX_PAIRS
    assert len({(p["label"], p["value"]) for p in pairs}) == len(pairs)


def test_page_where_old_harvester_found_nothing_now_yields_and_golden_candidates_stay(tmp_path, specs):
    text = visible_text(SPEC_HTML)
    before = by_field(harvest_text(text, specs, tables=_html_tables(SPEC_HTML)))       # no html: lines / tables only
    after = by_field(harvest_text(text, specs, tables=_html_tables(SPEC_HTML), html=SPEC_HTML))
    for name in ("vehicle_warranty", "warranty_years", "warranty_km", "list_price", "local_trim_name"):
        assert name not in before and name in after, name
        assert after[name][0]["extraction_method"] == "dom_pair"
    assert after["list_price"][0]["value"] == 179990 and after["local_trim_name"][0]["value"] == "BUSINESS EDI"
    # golden: no candidate the harvester emitted on the Corolla fixtures before Parts A-C disappears
    golden = json.loads(corolla_harvest.GOLDEN.read_text("utf-8"))
    now = set(corolla_harvest.candidate_keys(corolla_harvest.harvest_all(DocumentCache(tmp_path / "g"))))
    missing = [k for k in golden["candidate_keys"] if k not in now]
    assert not missing, missing
    assert set(golden["fields_with_candidates"]) <= set(corolla_harvest.fields_with_candidates(
        corolla_harvest.harvest_all(DocumentCache(tmp_path / "g2"))))


def test_harvester_version_bumped():
    assert HARVESTER_VERSION == "harvest-v4"


# --- Part B -------------------------------------------------------------------------------------------------------

BORDERLESS = [("Version", "1.8 Hybrid 140", "2.0 Hybrid 196"), ("Length", "4,650 mm", "4,650 mm"),
              ("Width", "1,790 mm", "1,790 mm"), ("Wheelbase", "2,700 mm", "2,700 mm"),
              ("Fuel tank capacity", "43 l", "43 l")]


def test_borderless_pdf_spec_table_yields_text_strategy_rows(tmp_path, specs):
    body = make_pdf([spec_matrix(BORDERLESS)])
    assert _pdf_tables(body) == []                          # the default (ruling-line) pass finds nothing
    tables = _pdf_tables(body, vocabulary=TableVocabulary.default())
    assert [t["source"] for t in tables] == ["pdf_table_text"]
    rows = tables[0]["rows"]
    assert rows[0] == ["Version", "1.8 Hybrid 140", "2.0 Hybrid 196"] and rows[1] == ["Length", "4,650 mm", "4,650 mm"]
    # through the cache, as the harvester reads it
    cache = DocumentCache(tmp_path)
    url = "https://www.toyota.co.il/media/corolla-brochure.pdf"
    doc = cache.put("pdf", url, body, {"status": 200, "final_url": url, "doc_type": "pdf"},
                    "[page 1]\nVersion 1.8 Hybrid 140 2.0 Hybrid 196\nLength 4,650 mm 4,650 mm")["document_id"]
    cands = by_field(harvest_document(cache, doc, specs)[0])
    assert cands["wheelbase_mm"][0]["value"] == 2700 and cands["wheelbase_mm"][0]["extraction_method"] == "table_row"
    assert cands["fuel_tank_l"][0]["value"] == 43
    assert cands["wheelbase_mm"][0]["variant_hint"] == "1.8 Hybrid 140"


def test_page_without_spec_aliases_is_not_table_parsed_twice(monkeypatch):
    body = make_pdf([spec_matrix(BORDERLESS), [(72, 700, "Welcome to our dealers"), (72, 680, "Opening hours")]])
    calls = []
    real = extract_mod._text_strategy_tables

    def spy(page, vocabulary):
        calls.append(page.page_number)
        return real(page, vocabulary)

    monkeypatch.setattr(extract_mod, "_text_strategy_tables", spy)
    tables = _pdf_tables(body, vocabulary=TableVocabulary.default())
    assert calls == [1] and len(tables) == 1


def test_text_tables_keep_only_spec_matrices_and_reverse_hebrew_cells():
    vocab = TableVocabulary.default()
    assert not extract_mod.text_table_ok([["a", "b"], ["c", "d"], ["e", "f"]], vocab)
    assert not extract_mod.text_table_ok([["Length", "1"], ["Width", "2"]], vocab)        # < 3 rows
    assert extract_mod.text_table_ok([["Length", "1"], ["Width", "2"], ["x", "y"]], vocab)
    assert extract_mod._logical_cell("ךרוא", vocab) == "אורך"


def test_second_pass_failure_keeps_default_tables(monkeypatch):
    body = make_pdf([spec_matrix(BORDERLESS)])

    def boom(page, vocabulary):
        raise RuntimeError("broken page")

    monkeypatch.setattr(extract_mod, "_text_strategy_tables", boom)
    assert _pdf_tables(body, vocabulary=TableVocabulary.default()) == []


# --- Part C -------------------------------------------------------------------------------------------------------

def test_unit_anchor_in_prose_and_ambiguity(specs):
    d = dictionary_for(specs)

    def anchors(text):
        return unit_anchor_candidates(document_segments(text, None, None, is_pdf=False, dictionary=d), d)

    one = anchors('הרכב החדש מציע אורך 4,495 מ"מ ותא נוסעים מרווח.')
    assert [(c["field"], c["value"], c["extraction_method"]) for c in one] == [("length_mm", 4495, "unit_anchor")]
    assert "אורך" in one[0]["quote"] and '4,495 מ"מ' in one[0]["quote"]
    assert one[0]["parser_confidence"] < 0.85
    assert anchors('אורך ורוחב 4,495 מ"מ בהתאמה.') == []          # two fields' aliases in range: ambiguous
    # the alias on the previous line
    prev = anchors('נפח מיכל הדלק\nעומד על 43 ליטר')
    assert [(c["field"], c["value"]) for c in prev] == [("fuel_tank_l", 43)]
    # a longer alias of another field owns its shorter word ("גובה" inside "גובה גחון")
    assert [c["field"] for c in anchors('גובה גחון של 135 מ"מ')] == ["ground_clearance_mm"]


def test_unit_anchor_never_replaces_existing_candidates(specs):
    text = 'אורך: 4,495 מ"מ'
    cands = harvest_text(text, specs)
    assert [c["extraction_method"] for c in cands if c["field"] == "length_mm"] == ["alias_proximity"]
    existing = [{"field": "length_mm", "value": 4495, "extraction_method": "alias_proximity"}]
    added = merge_additions(existing, [{"field": "length_mm", "value": 4495.0, "extraction_method": "unit_anchor"},
                                       {"field": "width_mm", "value": 1780, "extraction_method": "unit_anchor"}])
    assert added[0] is existing[0] and [c["field"] for c in added] == ["length_mm", "width_mm"]


def test_corolla_coverage_table_before_vs_after(tmp_path):
    """The PR table: fields with >= 1 candidate before vs after Parts A-C (deterministic)."""
    table = corolla_harvest.coverage_table(DocumentCache(tmp_path))
    rows = table["rows"]
    assert table["applicable_fields"] == 37
    assert all(not row["lost"] for row in rows.values())                  # nothing disappears anywhere
    assert rows["existing Corolla fixtures"]["after"] >= rows["existing Corolla fixtures"]["before"]
    structural = rows["structural fixtures (PR #31)"]
    assert (structural["before"], structural["after"]) == (14, 19)
    assert structural["gained"] == ["list_price", "local_trim_name", "vehicle_warranty", "warranty_km",
                                    "warranty_years"]
