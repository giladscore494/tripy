"""PR #42: the Document Variant Map on real importer pages (synthetic fixtures from the excerpts of production run
20261003T221935Z, record 101122; no network).

    F1  charging power is never motor power; an unexplained power blocks only its own region
    F2  tab labels of repeated spec groups (heading, N labels, N groups) are part of the groups' identity
    F3  catalog trim aliases (prefix / join / alias table, longest match, drivetrain consistent, union never a pick)
    F4  visually ordered right-to-left PDF text is detected and reordered before harvest
    F5  the regulatory model-code table: קוד דגם is the government degem_cd (verified), market_trim_not_offered
    F6  importer domains from data/source_rules.json, guessed URLs, 404 counters and the operational note
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from fixtures import pr42_g6_pages as G
from fixtures.corolla_harvest import put
from src import acquisition as A
from src.candidate_harvest import (dictionary_for, harvest_document, logical_rtl_line, rtl_dictionary,
                                   visual_to_logical)
from src.document_binding import (bind, catalog_family_entries, mentions, target_identity)
from src.evidence_admission import AdmissionContext, admit
from src.fields import load_schema, resolve_requested_fields
from src.source_authority import classify_source, rules as source_rules
from src.storage.cache import DocumentCache
from src.tools.extract import document_tables
from src.tools.search import default_domains, importer_domains
from src.variant_map import (assign, build_variant_map, catalog_trim_matches, fact_region, identity_vector,
                             market_trim_offer, region_verdict)

ROOT = Path(__file__).resolve().parent.parent
SPECS = resolve_requested_fields(None, propulsion="battery_electric")
IDENTITY = target_identity(G.PAYLOAD)
KEY = "אקספנג|g6|2026|suv|battery_electric|"
K485, K475, K295 = KEY + "awd|485|", KEY + "awd|475|", KEY + "two_wheel_drive|295|"


@pytest.fixture()
def world(tmp_path):
    cache = DocumentCache(tmp_path / "cache")
    adm = AdmissionContext.for_run(G.PAYLOAD, None, SPECS, "IL")
    page = put(cache, G.PAGE_URL, G.page_html(), "html")
    brochure = G.put_pdf(cache, G.BROCHURE_URL, G.brochure_pdf())
    return cache, adm, page, brochure


def store(world, field, value, quote, doc):
    cache, adm, page, brochure = world
    decision = admit(adm, cache, {"field": field, "value": value, "quote": quote, "document_id": doc}, [page, brochure])
    assert decision["accepted"], decision
    return decision["record"]


def regions(world, doc):
    cache, adm, *_ = world
    return {r["id"]: r for r in adm.material(cache, doc, None).variant_map["regions"]}


# --- F1 ---------------------------------------------------------------------------------------------------------------

def test_charging_power_is_never_a_motor_power():
    sentence = "ניתן להשיג טעינה מהירה בהספק מרבי של 451 קילוואט, ולהטעין מ־10% ל־80% תוך 12 דקות"
    assert mentions(sentence, IDENTITY)["power"] == set()
    assert mentions("הספק טעינה מהירה DC 451 קילוואט", IDENTITY)["power"] == set()
    assert mentions("V2L: הספק 3.3 kW | הספק מנוע 357 kW", IDENTITY)["power"] == {round(357 * 1.35962, 1)}
    # a motor power keeps reading (its own clause has no charging word, even after a charging sentence)
    assert mentions("טעינה מהירה 80% ב-12 דקות. הספק מרבי 357 kW", IDENTITY)["power"] == {round(357 * 1.35962, 1)}


def test_heyxpeng_page_has_no_motor_power_identity_from_451_kw(world):
    cache, adm, page, _ = world
    vmap = adm.material(cache, page, None).variant_map
    assert not any(item["power"] for item in vmap["inventory"])
    assert not any(r.get("unexplained_powers") for r in vmap["regions"])
    assert adm.material(cache, page, None).profile["mentions"]["power"] == []


def test_an_unexplained_power_blocks_only_its_own_region():
    entries = catalog_family_entries(IDENTITY)
    awd = {**identity_vector("AWD", IDENTITY, []), "trim": []}
    stray = {**awd, "drivetrain": [], "power": [613.2]}                  # a power no catalog entry explains
    stated = {**awd, "power": [486.0]}
    out = assign(awd, entries, [stray, stated])
    assert out["status"] == "assigned" and out["assignment"] == K485 and out["ignored_powers"] == [613.2]
    own = assign(stray, entries, [stray])
    assert own["status"] == "unresolved" and own["unexplained_powers"] == [613.2]
    # only unexplained powers in the inventory: no elimination at all (never a guess)
    alone = assign(awd, entries, [stray])
    assert alone["status"] == "unresolved" and set(alone["candidates_after"]) == {K475, K485}


# --- F2 + F3 on the page fixture --------------------------------------------------------------------------------------

def test_tab_labelled_groups_resolve_on_the_page(world):
    _, _, page, _ = world
    regs = regions(world, page)
    core_rwd, core_perf = regs["group:0"], regs["group:1"]
    assert core_rwd["tab_label"] == "Core RWD" and core_rwd["assignment"] == K295
    assert core_perf["tab_label"] == "Core Performance AWD" and core_perf["rule"] == "tab_labels"
    assert core_perf["identity"]["trim"] == ["CORE PERF"] and core_perf["assignment"] == K485
    assert core_perf["text"].startswith("מפרט New G6 | Core Performance AWD")      # the heading stays in the text
    awd_perf = regs["group:4"]
    assert awd_perf["tab_label"] == "AWD Performance" and awd_perf["status"] == "unresolved"
    assert set(awd_perf["candidates_after"]) == {K475, K485}


@pytest.mark.parametrize("field,value,quote", [
    ("acceleration_0_100_s", 4.1, 'תאוצה 0-100 קמ"ש | 4.1 שניות'),
    ("electric_range_km", 510, 'טווח נסיעה חשמלית | 510 ק"מ'),
    ("dc_max_charging_power_kw", 451, "הספק טעינה מהירה DC | 451 קילוואט"),
    ("screen_size_in", 15.6, "מולטימדיה | מסך מרכזי 15.6 אינץ'"),
    ("cargo_volume_l", 571, "נפח תא המטען | 571 ליטר / 1,374 ליטר (עם מושב אחורי מקופל)"),
    ("length_mm", 4758, "4,758 / 1,920 / 1,650 מ״מ"),
    ("width_mm", 1920, "4,758 / 1,920 / 1,650 מ״מ"),
    ("height_mm", 1650, "4,758 / 1,920 / 1,650 מ״מ"),
    ("curb_weight_kg", 2295, "משקל עצמי | 2,295 ק״ג"),
])
def test_core_performance_awd_group_values_bind_by_dvm_region(world, field, value, quote):
    record = store(world, field, value, quote, world[2])
    region = record["variant_map_region"]
    assert region["region_id"] == "group:1" and region["status"] == "target" and region["assignment"] == K485
    assert region["tab_label"] == "Core Performance AWD"
    assert record["binding_level"] == "exact_technical_variant" and record["binding_basis"] == "dvm_region"
    # screen size is an exact_market_trim field: Core Performance is not the MAX trim, so it stays below its requirement
    assert record["variant_match"] == ("unclear" if field == "screen_size_in" else "exact")


def test_core_rwd_group_is_another_variant_and_awd_performance_stays_unresolved(world):
    rwd = store(world, "curb_weight_kg", 2190, "משקל עצמי | 2,190 ק״ג", world[2])
    assert rwd["variant_match"] == "different" and rwd["variant_map_region"]["region_id"] == "group:0"
    assert rwd["variant_map_region"]["status"] == "other_variant"
    perf = store(world, "electric_range_km", 550, 'טווח נסיעה חשמלית | 550 ק"מ', world[2])
    assert perf["variant_match"] == "unclear" and perf["binding_level"] == "body_powertrain"
    assert perf["variant_map_region"]["status"] == "unresolved" and perf["variant_map_region"]["region_id"] == "group:4"


def test_a_value_shared_by_a_target_group_and_another_variant_group_is_the_targets(world):
    # 571 l is stated in every group, 4,758 mm in Core RWD and Core Performance AWD: the target group states it
    record = store(world, "cargo_volume_l", 571, "נפח תא המטען | 571 ליטר / 1,374 ליטר (עם מושב אחורי מקופל)",
                   world[2])
    statuses = record["variant_map_region"]["region_statuses"]
    assert statuses["group:1"] == "target" and statuses["group:0"] == "other_variant"
    assert statuses["group:4"] == "unresolved"


def test_consumption_table_rows_resolve_by_catalog_trim(world):
    cache, adm, page, _ = world
    vmap = adm.material(cache, page, None).variant_map
    for value in (184, 510):
        decision = fact_region(vmap, IDENTITY, value=value, fragment=f"G6 AWD Core Performance/Black Edition | {value}",
                               clause="", source_lines=[], line_index=None,
                               matching=[{"table_index": 1, "row_index": 3}], field_values=6)
        assert decision["status"] == "target" and decision["region_id"] == "table:1:row:3"
        assert decision["identity"]["trim"] == ["BLACKEDITION", "CORE PERF"] and decision["assignment"] == K485
    tb = fact_region(vmap, IDENTITY, value=550, fragment="G6 AWD Performance TB | 550", clause="", source_lines=[],
                     line_index=None, matching=[{"table_index": 1, "row_index": 6}], field_values=6)
    assert tb["status"] == "other_variant" and tb["assignment"] == K475 and tb["contradicts"] == "power"
    binding = bind(IDENTITY, adm.material(cache, page, None).profile["statuses"], [], market="IL",
                   region={**tb, "allowed": True})
    assert binding["variant_match"] == "different" and "power_mismatch@dvm_region" in binding["binding_veto"]


# --- F3 negatives and the matcher -----------------------------------------------------------------------------------

def test_catalog_trim_aliases_and_longest_match():
    trims = ["PERF TB", "BLACKEDITION", "CORE PERF", "STAND RANGE", "LONG RANGE", "CORE", "CORE PLUS", "CORE PLUS 20",
             "LR PR", "MAX", "SUN"]

    def named(text):
        return [m["trim"] for m in catalog_trim_matches(text, trims, IDENTITY)]
    assert named("G6 AWD Core Performance/Black Edition") == ["CORE PERF", "BLACKEDITION"]
    assert named("Performance TB") == ["PERF TB"] and named("RWD Standard Range") == ["STAND RANGE"]
    assert named("Long Range Pro") == ["LR PR"]                                  # alias table + longest match
    assert named("Core Performance") == ["CORE PERF"]                            # never also CORE
    assert named("G6 RWD Core/Core+‎") == ["CORE", "CORE PLUS"]             # "+" is "plus"
    assert named('Core+ 20" Wheels') == ["CORE PLUS 20"]
    assert named("AWD Performance") == [] and named("sunroof") == [] and named("seats") == []
    assert named("G6 MAX") == ["MAX"] and named("MAX") == []                     # generic trims need the family


def test_core_alone_with_awd_names_no_trim():
    entries = catalog_family_entries(IDENTITY)
    vec = identity_vector("G6 Core AWD", IDENTITY, sorted({t for e in entries for t in e["trims"]}), entries)
    assert vec["trim"] == [] and vec["trim_inconsistent"] == ["CORE"]
    out = assign(vec, entries, [])
    assert out["status"] == "unresolved" and set(out["candidates_after"]) == {K475, K485}


def test_a_label_count_that_differs_from_the_group_count_applies_no_labels():
    from src.tools.extract import _html_tables, visible_text
    from src.structure_harvest import page_text

    html = G.page_html(new_labels=("Core Performance AWD",))
    dictionary = AdmissionContext.for_run(G.PAYLOAD, None, SPECS, "IL").dictionary
    vmap = build_variant_map(text=visible_text(html), identity=IDENTITY, tables=_html_tables(html), html=html,
                             body_text=page_text(html), dictionary=dictionary)
    groups = [r for r in vmap["regions"] if r["kind"] == "dom_group" and r["text"].startswith("מפרט New G6")]
    assert groups and not any(r.get("tab_label") for r in groups)
    assert not any(r.get("assignment") == K485 for r in groups)


def test_a_trim_at_several_technical_variants_narrows_to_their_union():
    index = {"complete": True, "entries": {
        KEY + "two_wheel_drive|285|": {"trims": ["LONG RANGE"], "records": ["1"]},
        KEY + "awd|475|": {"trims": ["LONG RANGE", "PERF TB"], "records": ["2"]},
        KEY + "awd|485|": {"trims": ["MAX"], "records": ["3"]}}}
    entries = catalog_family_entries(IDENTITY, index)
    vec = identity_vector("G6 Long Range", IDENTITY, ["LONG RANGE", "PERF TB", "MAX"], entries)
    out = assign(vec, entries, [])
    assert vec["trim"] == ["LONG RANGE"] and out["status"] == "unresolved"
    assert set(out["candidates_after"]) == {KEY + "two_wheel_drive|285|", KEY + "awd|475|"}
    assert region_verdict({"identity": vec, **out}, IDENTITY)["status"] == "unresolved"


# --- F4 ---------------------------------------------------------------------------------------------------------------

def test_reversed_rtl_runs_are_detected_and_reordered():
    words = rtl_dictionary(dictionary_for(load_schema()).hebrew_aliases)
    assert logical_rtl_line(')ס"כ( יברמ קפסה', words) == 'הספק מרבי (כ"ס)'
    assert logical_rtl_line("ינכט טרפמ", words) == "מפרט טכני"
    assert logical_rtl_line('476 286 258 )ס"כ( יברמ קפסה', words) == 'הספק מרבי (כ"ס) 258 286 476'
    assert logical_rtl_line('4.1 6.7 6.9 )תוינש( ש"מק 100-0 הצואת', words).startswith('תאוצה 0-100 קמ"ש (שניות)')
    assert visual_to_logical("AWD RWD RWD G6 ינכט טרפמ") == "מפרט טכני AWD RWD RWD G6"   # a Latin run keeps its order
    for logical in ('הספק מרבי (כ"ס) 476', "מפרט טכני", "הנעה כפולה", 'אורך 4880 מ"מ', "Long Range"):
        assert logical_rtl_line(logical, words) is None                       # logical text is left alone


def test_reversed_brochure_power_row_is_read_and_the_awd_column_is_another_variant(world):
    cache, adm, _, brochure = world
    tables = document_tables(cache, brochure, cache.get(brochure), None)
    assert tables[0]["rows"][2] == ['הספק מרבי (כ"ס)', "258", "286", "476"]
    assert tables[0]["rows"][0] == ["מפרט טכני G6", "RWD", "RWD", "AWD"]
    column = regions(world, brochure)["table:0:col:3"]
    assert column["identity"]["power"] == [476.0] and column["assignment"] == K475
    # the harvest reads the repaired cells: the AWD column's identity carries the 476 hp power row
    cands, _ = harvest_document(cache, brochure, SPECS)
    awd = {c["field"]: c for c in cands if c.get("column_identity", "").startswith("AWD")}
    assert 'הספק מרבי (כ"ס) 476' in awd["curb_weight_kg"]["column_identity"]
    assert awd["curb_weight_kg"]["quote"] == 'משקל עצמי (ק"ג) | 2,195 (AWD)'
    for field, value in (("curb_weight_kg", 2195), ("acceleration_0_100_s", 4.1)):
        record = store(world, field, value, awd[field]["quote"], brochure)
        assert record["variant_map_region"]["region_id"] == "table:0:col:3"
        assert record["variant_map_region"]["status"] == "other_variant" and record["variant_match"] == "different"


# --- F5 ---------------------------------------------------------------------------------------------------------------

def test_model_code_equals_government_degem_cd():
    """The verification behind gov_model_code: each code of the importer page's safety table is the degem_cd of the
    government record whose catalog trim its description names, at the same technical variant (12 G6 2026 records,
    8 on the page); one of them (101136, CORE, 38) is a Level 1.5 snapshot vehicle."""
    entries = catalog_family_entries(IDENTITY)
    trims = sorted({t for e in entries for t in e["trims"]})
    by_record = {r: e["key"] for e in entries for r in e["records"]}
    for code, description in G.CODES:
        record, trim = G.GOV_CODES_2026[int(code)]
        vec = identity_vector(description, IDENTITY, trims, entries)
        assert trim in vec["trim"], (code, description, vec["trim"])
        out = assign(vec, entries, [])
        assert out["assignment"] == by_record[record], (code, description)
    snapshot = json.loads((ROOT / "data" / "benchmark_v1_level15_snapshot.json").read_text("utf-8"))
    rows = {r["upstream_record_id"]: r for r in snapshot["rows"]}
    assert rows["101136"]["degem_cd"] == 38 and rows["101136"]["ramat_gimur"] == "CORE"
    assert ("38", "G6 RWD Core") in G.CODES
    assert rows["101122"]["degem_cd"] == 31 and IDENTITY.gov_model_code == 31
    assert "31" not in {c for c, _ in G.CODES}


def test_market_trim_not_offered_on_the_importer_page(world):
    cache, adm, page, _ = world
    vmap = adm.material(cache, page, None).variant_map
    offer = market_trim_offer(vmap, IDENTITY)
    assert offer == {"status": "market_trim_not_offered", "codes": [35, 36], "target_code": 31,
                     "technical_variant": K485, "table_index": 0}
    record = store(world, "screen_size_in", 15.6, "מולטימדיה | מסך מרכזי 15.6 אינץ'", page)
    assert record["market_trim"]["status"] == "market_trim_not_offered"
    assert record["binding_level"] == "exact_technical_variant" and record["variant_match"] == "unclear"
    # a fact that names the MAX trim on such a page still stays below the market trim
    statuses = adm.material(cache, page, None).profile["statuses"]
    layers = [("quote", 'XPeng G6 MAX הנעה כפולה 486 כ"ס')]
    free = bind(IDENTITY, statuses, layers, market="IL", requirement="exact_market_trim")
    capped = bind(IDENTITY, statuses, layers, market="IL", requirement="exact_market_trim", market_trim=offer)
    assert free["binding_level"] == "exact_market_trim"
    assert capped["binding_level"] == "exact_technical_variant" and capped["variant_match"] == "unclear"
    assert "market_trim_not_offered" in capped["binding_rules"]


def test_a_region_with_the_targets_model_code_binds_the_market_trim(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    adm = AdmissionContext.for_run(G.PAYLOAD, None, SPECS, "IL")
    page = put(cache, G.PAGE_URL, G.page_html(codes=G.CODES + (("31", "G6 AWD MAX"),)), "html")
    material = adm.material(cache, page, None)
    vmap = material.variant_map
    assert market_trim_offer(vmap, IDENTITY)["status"] == "offered"
    row = next(r for r in vmap["regions"] if r.get("gov_model_code") == 31)
    verdict = region_verdict(row, IDENTITY)
    assert verdict == {"status": "target", "reason": "gov_model_code", "rule": "gov_model_code"}
    from src.evidence_admission import dvm_gate

    region = dvm_gate(adm, material, adm.spec("screen_size_in"), "IL",
                      {"status": "target", "region_id": row["id"], "identity": row["identity"], **verdict})
    assert region["trim"] == "match" and region["allowed"]
    binding = bind(IDENTITY, material.profile["statuses"], [("quote", "מסך מרכזי 15.6 אינץ'")], market="IL",
                   requirement="exact_market_trim", region=region)
    assert binding["binding_level"] == "exact_market_trim" and binding["binding_basis"] == "gov_model_code"
    assert binding["variant_match"] == "exact"
    # the same row on a foreign-market page never gives the trim
    foreign = dvm_gate(adm, material, adm.spec("screen_size_in"), "DE",
                       {"status": "target", "region_id": row["id"], "identity": row["identity"], **verdict})
    assert foreign.get("trim") != "match"


def test_incomplete_or_absent_code_tables_say_nothing(tmp_path):
    from src.tools.extract import _html_tables, visible_text

    dictionary = AdmissionContext.for_run(G.PAYLOAD, None, SPECS, "IL").dictionary
    broken = G.page_html(codes=G.CODES + (("x1", "G6 AWD"),))
    vmap = build_variant_map(text=visible_text(broken), identity=IDENTITY, tables=_html_tables(broken), html=broken,
                             dictionary=dictionary)
    assert vmap["model_codes"][0]["complete"] is False and market_trim_offer(vmap, IDENTITY) is None
    no_code = target_identity({**G.PAYLOAD, "identity": {**G.PAYLOAD["identity"], "government_codes": {}}})
    assert no_code.gov_model_code is None and market_trim_offer(vmap, no_code) is None


# --- F6 ---------------------------------------------------------------------------------------------------------------

def test_every_brand_with_importer_slugs_yields_its_co_il_domain():
    rs = source_rules()
    brands = {name: entry for name, entry in rs["brands"].items() if entry.get("importer_slugs")}
    assert "אקספנג" in brands
    for name, entry in brands.items():
        assert set(entry["importer_slugs"]) <= set(entry["slugs"]), name
        domains = importer_domains(name)
        assert [f"{s}.co.il" for s in entry["importer_slugs"]] == domains, name
        assert default_domains({"manufacturer": name})[:len(domains)] == domains
        for domain in domains:
            assert classify_source(f"https://{domain}/", name)["source_authority"] == "official_importer"
    assert default_domains({"manufacturer": "אקספנג"})[0] == "heyxpeng.co.il"


class _Ctx:
    def __init__(self, cache):
        self.cache, self.counters, self.vehicle, self.admission = cache, Counter(), {"manufacturer": "אקספנג"}, None


def test_guessed_urls_and_404s_are_counted(tmp_path):
    cache = DocumentCache(tmp_path / "c")
    ctx = _Ctx(cache)
    prov = A.url_provenance(ctx)
    prov.observe_search({"results": [{"url": "https://heyxpeng.co.il/g6/"}]})
    page = put(cache, "https://heyxpeng.co.il/g6/", '<html><body><a href="/pricing/">מחירון</a></body></html>', "html")
    row = prov.observe_fetch(ctx, "https://heyxpeng.co.il/g6", {"status": 200, "document_id": page})
    assert row["guessed_url"] is False                          # offered by the search (trailing slash aside)
    assert prov.observe_fetch(ctx, "https://www.heyxpeng.co.il/pricing/", {"status": 200})["guessed_url"] is False
    guess = prov.observe_fetch(ctx, "https://heyxpeng.co.il/g6/specs/", {"status": 404})
    assert guess["guessed_url"] is True and guess["domain"] == "heyxpeng.co.il"
    other = prov.observe_fetch(ctx, "https://www.carnews.co.il/g6", {"status": 404})
    assert other["guessed_url"] is False                        # not an official domain
    assert ctx.counters["acq_guessed_urls"] == 1 and ctx.counters["acq_404"] == 2
    assert ctx.counters["acq_guessed_404"] == 1


def test_operational_note_after_two_guessed_404s_on_one_domain(tmp_path):
    ctx = _Ctx(DocumentCache(tmp_path / "c"))
    prov = A.url_provenance(ctx)
    prov.observe_fetch(ctx, "https://heyxpeng.co.il/g6-max/", {"status": 404})
    prov.observe_fetch(ctx, "https://store.xpeng.com/il/configurator/G6", {"status": 404})
    assert prov.note() == ""                                    # one guessed 404 per domain
    prov.observe_fetch(ctx, "https://heyxpeng.co.il/price-list/", {"status": 404})
    note = prov.note()
    assert "heyxpeng.co.il" in note and "only links offered" in note and "xpeng.com" not in note
    assert prov.note() == ""                                    # said once ...
    prov.observe_fetch(ctx, "https://heyxpeng.co.il/warranty/", {"status": 404})
    assert "3 guessed URLs" in prov.note()                      # ... and again after another guessed 404
    assert prov.summary()["blocked_domains"] == ["heyxpeng.co.il"]


def test_tool_session_counts_guessed_fetches_and_notes_them(tmp_path):
    from conftest import FakeSession
    from test_tools_smoke import _call

    from src.agent import AgentConfig, ToolSession
    from src.storage.run_log import RunLog
    from src.tools import ToolConfig, ToolContext
    from src.tools.evidence import EvidenceStore

    ctx = ToolContext(cache=DocumentCache(tmp_path / "c"), evidence=EvidenceStore(), config=ToolConfig(),
                      session=FakeSession({}), vehicle={"manufacturer": "אקספנג", "model": "G6"})
    session = ToolSession(ctx, RunLog(tmp_path / "runs", "b", "1"), AgentConfig())
    session.execute([_call("a", "fetch_url", {"url": "https://heyxpeng.co.il/g6/specs/"}),
                     _call("b", "fetch_url", {"url": "https://heyxpeng.co.il/g6-max/"})], [], phase="research")
    assert ctx.counters["acq_guessed_urls"] == 2 and ctx.counters["acq_404"] == 2
    assert all(c.get("guessed_url") for c in session.tool_calls)
    assert "heyxpeng.co.il" in session.guess_note()


def test_rank_links_knows_configurator_and_spec_download_words():
    links = [{"url": "https://other.example/a", "text": "news"},
             {"url": "https://other.example/b", "text": "להורדת המפרט"},
             {"url": "https://other.example/configurator/g6", "text": ""},
             {"url": "https://other.example/pricing/", "text": ""}]
    ranked = [link["url"] for link in A.rank_links(links, "https://heyxpeng.co.il/g6/")]
    assert ranked[-1] == "https://other.example/a"
