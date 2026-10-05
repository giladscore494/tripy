"""PR #45: the IL version-page resolver actually finds pages, precision fixes, registry coverage (fixtures, no network).

    R1  resolver candidate selection and logging: version-URL patterns, slug ranking, home pages / listings never
        candidates, the `site:` -> domain-filter retry inside the budget, every result and decision logged
    R2  page identity from titles (escaped entities, "TFSI 45", a bare "45", "1984" without a thousands separator)
    R3  body sub-variants are identity (Q3 Sportback page for a Q3; A1 SPORTBACK)
    R4  single values on multi-version documents (the A6 319.pdf torque) + the region / binding invariant
    R5  internally inconsistent version pages (1984 cc next to 341 hp)
    R6  gearbox type mapping (S tronic / DSG / PDK / כפולת מצמדים -> dct)
    R7  registry tyre parsing, join-key normalisation, merged trims, build diagnostics, coverage report
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

from fixtures import pr43_audi_pages as A
from fixtures import pr44_pages as P
from fixtures import pr45_pages as F
from fixtures.corolla_harvest import put
from src.evidence_admission import AdmissionContext, admit
from src.fields import resolve_requested_fields
from src.storage.cache import DocumentCache

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))     # registry build / coverage scripts


@pytest.fixture()
def cache(tmp_path):
    return DocumentCache(tmp_path / "cache")


def _adm(payload):
    propulsion = payload["engine_drivetrain"]["propulsion_normalized"]
    return AdmissionContext.for_run(payload, None, resolve_requested_fields(None, propulsion=propulsion), "IL")


def _material(cache, payload, url, body, kind="html"):
    adm = _adm(payload)
    doc = put(cache, url, body, kind)
    return adm.material(cache, doc, None), adm, doc


def _record(cache, payload, url, body, field, value, quote, kind="html"):
    adm = _adm(payload)
    doc = put(cache, url, body, kind)
    decision = admit(adm, cache, {"field": field, "value": value, "quote": quote, "document_id": doc}, [doc])
    assert decision["accepted"], decision
    return decision["record"]


# --- R1: resolver candidate selection and logging ---------------------------------------------------------------------

class _Ctx:
    def __init__(self, cache, payload):
        self.cache, self.counters, self.admission = cache, Counter(), _adm(payload)


class _Log:
    def __init__(self):
        self.events = []

    def event(self, kind, **data):
        self.events.append((kind, data))


class _Web:
    """Fake search / fetch / robots over fixture pages. `results[(domain, mode)]` (mode site | domain_filter) or
    `results[domain]` for both modes; items are URLs or {url, title}."""

    def __init__(self, cache, pages: dict, results: dict):
        self.cache, self.pages, self.results = cache, pages, results
        self.searches, self.fetches = [], []

    def search(self, query, domain):
        mode = "site" if query.startswith("site:") else "domain_filter"
        self.searches.append((query, domain, mode))
        items = self.results.get((domain, mode), self.results.get(domain, []))
        return {"results": [i if isinstance(i, dict) else {"url": i} for i in items]}

    def fetch(self, url):
        self.fetches.append(url)
        if url not in self.pages:
            return {"error": "HTTPError", "status": 404}
        return {"document_id": put(self.cache, url, self.pages[url], "html"), "status": 200}

    @staticmethod
    def robots(url):
        return True


class _NoPace:
    @staticmethod
    def wait(domain):
        return 0.0


def _pages():
    pages = {P.Q3_MODEL_URL: P.q3_model_html()}
    for url, (version, power) in P.Q3_VERSIONS.items():
        body = "קופה" if "SPORTBACK" in version else "קרוסאובר"
        pages[url] = P.q3_cartube_html(synthetic_row=False, version=version, power=power, body=body)
    return pages


def _resolve(cache, web, payload=None, log=None):
    from src.il_version_pages import resolve

    ctx = _Ctx(cache, payload or A.Q3_PAYLOAD)
    out = resolve(ctx, log, payload=payload or A.Q3_PAYLOAD, search=web.search, fetch=web.fetch, robots=web.robots,
                  pace=_NoPace())
    return out, ctx


HOME = "https://www.cartube.co.il/"
V325, V328, V330 = (P.Q3_MODEL_URL + "/325-1-5-35tfsi-2024", P.Q3_MODEL_URL + "/328-2-0-45tfsi-4x4-2024",
                    P.Q3_MODEL_URL + "/330-2-0-40tfsi-4x4-sportback-2024")


@pytest.mark.parametrize("url, kind", [
    (HOME, "home"), ("https://www.cartube.co.il", "home"), ("https://www.auto.co.il", "home"),
    (P.Q3_MODEL_URL, "listing"), (P.Q3_CARTUBE_URL, "version"), (V330, "version"),
    ("https://www.icar.co.il/אודי/אודי_q3/2024/version12345/", "version"),
    ("https://www.icar.co.il/אודי/אודי_q3/", "listing"),
    ("https://www.auto.co.il/cars/audi/a4/2020/530178/", "version"),
    ("https://www.auto.co.il/cars/audi/a4/2020/", "listing"), ("https://www.auto.co.il/cars/audi/a4/", "listing"),
    ("https://www.example.com/q3", "off_site"),
])
def test_version_url_patterns_are_data(url, kind):
    from src.il_version_pages import classify_url

    assert classify_url(url)[1] == kind


def test_home_page_and_listing_are_never_candidates_and_versions_are_ranked_first(cache):
    results = {"cartube.co.il": [{"url": HOME, "title": "cartube"}, {"url": P.Q3_MODEL_URL, "title": "אודי Q3"},
                                 {"url": V330, "title": "אודי Q3 ספורטבק"}, V325, P.Q3_CARTUBE_URL]}
    web = _Web(cache, _pages(), results)
    log = _Log()
    out, ctx = _resolve(cache, web, log=log)
    # the 2.0 / 4x4 / 2024 slug of the target outranks the Sportback (a sub-variant the Q3 lacks) and the 1.5
    assert web.fetches == [P.Q3_CARTUBE_URL] and out["found"] == 1
    assert out["pages"] == [{"url": P.Q3_CARTUBE_URL, "role": "version", "document_id": out["pages"][0]["document_id"],
                             "status": "accepted", "reason": "power_match"}]
    decisions = {(r["url"], r["mode"]): r for r in out["results"]}
    assert decisions[(HOME, "site")]["decision"] == "dropped" and decisions[(HOME, "site")]["reason"] == "home_page"
    assert decisions[(P.Q3_MODEL_URL, "site")]["decision"] == "listing_for_links"
    target = decisions[(P.Q3_CARTUBE_URL, "site")]
    assert target["decision"] == "version_candidate" and target["rank"] == 5 and target["score"] == 3
    assert decisions[(V330, "site")]["contradicted"] == ["body_subvariant:sportback"]
    assert decisions[(V325, "site")]["contradicted"] == ["engine:1.5"]
    not_fetched = {c["url"]: c["decision"] for c in out["candidates"] if c["decision"].startswith("not_fetched")}
    assert not_fetched == {V330: "not_fetched:found_earlier", V325: "not_fetched:found_earlier",
                           P.Q3_MODEL_URL: "not_fetched:found_earlier"}
    # the event carries every search result (url, title, rank) and every candidate decision, dropped ones included
    (kind, event), = log.events
    assert kind == "il_version_pages" and event["results"] == out["results"] and event["candidates"]
    assert {"url", "title", "rank", "decision", "query", "mode"} <= set(event["results"][0])
    assert ctx.counters["acq_il_version_pages_found"] == 1


def test_listing_is_fetched_only_after_the_versions_and_only_for_its_links(cache):
    web = _Web(cache, _pages(), {"cartube.co.il": [P.Q3_MODEL_URL, V328]})
    out, _ = _resolve(cache, web)
    assert web.fetches == [V328, P.Q3_MODEL_URL, P.Q3_CARTUBE_URL]          # version first, then the listing's links
    roles = {p["url"]: (p["role"], p["status"], p["reason"]) for p in out["pages"]}
    assert roles[V328] == ("version", "rejected", "power_mismatch")
    assert roles[P.Q3_MODEL_URL] == ("listing", "rejected", "several_versions")
    assert roles[P.Q3_CARTUBE_URL] == ("version", "accepted", "power_match")
    link = next(c for c in out["candidates"] if c["url"] == P.Q3_CARTUBE_URL)
    assert link["source"] == f"links_of:{P.Q3_MODEL_URL}"


def test_an_unhelpful_site_search_is_retried_once_with_the_domain_filter(cache):
    results = {("cartube.co.il", "site"): ["https://www.example.com/q3", HOME],
               ("cartube.co.il", "domain_filter"): [P.Q3_CARTUBE_URL]}
    web = _Web(cache, _pages(), results)
    out, _ = _resolve(cache, web)
    assert [(d, m) for _, d, m in web.searches] == [("cartube.co.il", "site"), ("cartube.co.il", "domain_filter"),
                                                    ("icar.co.il", "site")]
    assert web.searches[1][0] == "אודי Q3 2024 2.0 190"                         # no site: prefix
    assert out["searches"] == 3 and out["found"] == 1
    off = next(r for r in out["results"] if r["url"] == "https://www.example.com/q3")
    assert off["decision"] == "dropped" and off["reason"] == "off_site"


def test_default_search_sends_site_queries_as_written_and_retries_with_the_domain_filter(monkeypatch):
    from src.il_version_pages import _default_search
    from src.tools import search as search_module

    calls = []
    monkeypatch.setattr(search_module, "search_web", lambda ctx, q, domain=None: calls.append((q, domain)) or {})
    _default_search(None, "site:cartube.co.il אודי Q3 2024", "cartube.co.il")
    _default_search(None, "אודי Q3 2024", "cartube.co.il")
    assert calls == [("site:cartube.co.il אודי Q3 2024", None), ("אודי Q3 2024", "cartube.co.il")]


def test_resolver_end_to_end_accepts_the_cartube_q3_327_page_and_binds_its_values(cache):
    web = _Web(cache, _pages(), {"cartube.co.il": [HOME, P.Q3_CARTUBE_URL]})
    out, _ = _resolve(cache, web)
    assert web.fetches == [P.Q3_CARTUBE_URL] and out["found"] == 1
    record = _record(cache, A.Q3_PAYLOAD, P.Q3_CARTUBE_URL, P.q3_cartube_html(synthetic_row=False), "top_speed_kmh",
                     222, "מהירות מרבית | 222 קמ״ש")
    assert record["variant_match"] == "exact" and record["binding_basis"] == "il_version_page"


# --- R2: page identity from titles --------------------------------------------------------------------------------------

def test_an_escaped_title_alone_names_displacement_designation_and_drivetrain(cache):
    page = (f"<html><head><title>{F.A4_TITLE_ESCAPED}</title></head><body><main><p>יד שניה</p></main></body></html>")
    material, adm, _ = _material(cache, F.A4_PAYLOAD, F.A4_530178_URL, page)
    from src.il_version_pages import page_identity

    found = page_identity(material, adm.identity)["found"]
    assert found["displacement"] == {2.0} and "45" in found["designation"] and found["drivetrain"] == {"awd"}


def test_a4_530178_reaches_displacement_match_and_is_rejected_on_power(cache):
    material, _, _ = _material(cache, F.A4_PAYLOAD, F.A4_530178_URL, F.a4_530178_html())
    page = material.version_page
    assert page["single_version"] and page["page_statuses"]["displacement"] == "match"     # was displacement_absent
    assert page["page_identity"] == {"displacements": [2.0], "designations": ["45"], "drivetrains": ["awd"]}
    # the page is the 45 TFSI 245 hp version; the record is 265 hp: rejected on power
    assert page["status"] == "rejected" and page["reason"] == "power_mismatch" and page["page_powers"] == [245.0]
    assert 'נפח מנוע 1984 סמ"ק' in page["rows"]


def test_reversed_and_bare_designations():
    from src.document_binding import designations
    from src.il_version_pages import designation_count, title_designations

    assert designations("אודי a6 2018 tfsi 45 אוט'") == {"45tfsi"} == designations("a6 45 tfsi quattro")
    assert title_designations("אודי A4 2020 45, אוט', 2.0 ל' טורבו", ["45, אוט'"]) == {"45"}
    assert title_designations("cartube | 2024 | אודי Q3 2.0 40TFSI 4X4") == set()
    assert designation_count({"45", "45tfsi"}) == 1 and designation_count({"45tfsi", "55tfsi"}) == 2
    assert designation_count({"45tfsi", "45tfsie"}) == 2


# --- R3: body sub-variants are identity --------------------------------------------------------------------------------

def test_q3_sportback_page_is_different_for_a_q3_target(cache):
    material, _, _ = _material(cache, A.Q3_PAYLOAD, F.Q3_SPORTBACK_URL, F.q3_sportback_html())
    page = material.version_page
    assert page["status"] == "rejected" and page["reason"] == "body_mismatch"
    assert page["body_subvariant"] == {"status": "mismatch", "subvariant": "sportback", "direction": "page_only"}
    for field, value, quote in (("length_mm", 4449, "אורך | 4449 מ״מ"), ("top_speed_kmh", 220, "מהירות מרבית | 220 קמ״ש")):
        record = _record(cache, A.Q3_PAYLOAD, F.Q3_SPORTBACK_URL, F.q3_sportback_html(), field, value, quote)
        assert record["variant_match"] == "different" and "body_mismatch@document" in record["binding_veto"]


def test_plain_q3_page_stays_exact(cache):
    for field, value, quote in (("length_mm", 4484, "אורך | 4484 מ״מ"), ("top_speed_kmh", 222, "מהירות מרבית | 222 קמ״ש")):
        record = _record(cache, A.Q3_PAYLOAD, P.Q3_CARTUBE_URL, F.q3_plain_html(), field, value, quote)
        assert record["variant_match"] == "exact" and record["binding_basis"] == "il_version_page", record


def test_a_sub_variant_in_any_identity_zone_vetoes_the_body(cache):
    url = "https://www.example.com/audi-q3-sportback-40-tfsi-quattro-specs"
    page = ("<html><head><title>Audi Q3 Sportback 40 TFSI quattro - technical data</title></head><body><main>"
            "<h1>Audi Q3 Sportback 40 TFSI quattro</h1><p>Length 4500 mm</p></main></body></html>")
    material, _, _ = _material(cache, A.Q3_PAYLOAD, url, page)
    assert material.profile["statuses"]["body"] == "mismatch"
    assert material.profile["body_subvariant"]["subvariant"] == "sportback"


def test_sub_variant_directions_and_implied_bodies():
    from fixtures.corolla_touring import PAYLOAD as COROLLA, VEHICLE as COROLLA_VEHICLE
    from src.document_binding import subvariant_status, target_identity

    a1 = target_identity(F.A1_PAYLOAD)
    assert a1.body_subvariants == ["sportback"]
    assert subvariant_status("אודי A1 ספורטבק 30 TFSI", a1, reverse=True)["status"] == "match"
    assert subvariant_status("אודי A1 30 TFSI", a1, reverse=True) == {"status": "mismatch", "subvariant": "sportback",
                                                                        "direction": "target_only"}
    assert subvariant_status("אודי A1 30 TFSI", a1) is None                  # the reverse is read on version pages only
    q3 = target_identity(A.Q3_PAYLOAD)
    assert subvariant_status("Audi Q3 Gran Coupe", q3)["subvariant"] == "gran_coupe"   # never also "coupe"
    corolla = target_identity(COROLLA, COROLLA_VEHICLE)
    if corolla.body == "wagon":
        assert subvariant_status("Toyota Corolla Touring Sports", corolla) is None   # touring implies the wagon body


# --- R4: single values on multi-version documents ------------------------------------------------------------------------

def test_a6_319_pdf_torque_is_not_exact_for_the_2_0_252_hp_target(cache):
    record = _record(cache, F.A6_PAYLOAD, F.A6_319_URL, F.A6_319_TEXT, "torque_nm", 500, F.A6_319_TORQUE_QUOTE, "pdf")
    assert record["variant_match"] != "exact" and record["binding_level"] == "body_powertrain", record
    assert "single_value_multi_version" in record["binding_flags"]
    region = record["variant_map_region"]
    assert region["status"] == "unresolved" and region["reason"] == "inventory_without_target"
    assert record["document_versions"]["designations"] == ["45tfsi", "55tfsi"]


def test_a6_319_pdf_repeated_row_is_still_not_exact_when_the_inventory_lacks_the_target(cache):
    from src.evidence_admission import region_agreement_violations

    record = _record(cache, F.A6_PAYLOAD, F.A6_319_URL, F.A6_319_TEXT, "length_mm", 4939, 'אורך (מ"מ) 4,939 4,939', "pdf")
    assert record["document_versions"]["repeated"] is True                   # stated once per column ...
    assert record["variant_match"] != "exact"                                 # ... of versions none of which is the target
    assert region_agreement_violations(record, record["document_versions"]) == []


def test_the_region_binding_invariant_is_asserted():
    from src.evidence_admission import region_agreement_violations

    bad = {"binding_level": "exact_technical_variant",
           "variant_map_region": {"status": "unresolved", "reason": "inventory_without_target"}}
    assert region_agreement_violations(bad, {"versions": 2}) == ["exact_with_inventory_without_target"]
    assert region_agreement_violations(bad, {"versions": 1}) == []
    assert region_agreement_violations({**bad, "binding_level": "body_powertrain"}, {"versions": 2}) == []


def _bind_a6(layers=(), multi_version=None, region=None):
    from src.document_binding import bind, target_identity

    identity = target_identity(F.A6_PAYLOAD)
    statuses = {"model": "match", "year": "match", "body": "match", "propulsion": "match", "displacement": "match",
                "power": "match", "drivetrain": "match", "model_code": "absent", "trim": "absent", "manufacturer": "match"}
    return bind(identity, statuses, list(layers), market="IL", multi_version=multi_version, region=region)


def test_single_value_rule_in_bind():
    assert _bind_a6()["binding_level"] == "exact_technical_variant"                       # one version: unchanged
    capped = _bind_a6(multi_version={"versions": 2, "repeated": False})
    assert capped["binding_level"] == "body_powertrain" and capped["binding_flags"] == ["single_value_multi_version"]
    assert _bind_a6(multi_version={"versions": 2, "repeated": True})["binding_level"] == "exact_technical_variant"
    no_target = _bind_a6(multi_version={"versions": 2, "repeated": True, "inventory_without_target": True})
    assert no_target["binding_level"] == "body_powertrain"
    # the value's own clause names the version: identified per fact
    own = _bind_a6([("value_clause", "45 TFSI 2.0 252 hp")], multi_version={"versions": 2, "repeated": False})
    assert own["binding_level"] == "exact_technical_variant"
    region = {"status": "target", "allowed": True, "region_id": "table:0:col:2"}
    assert _bind_a6(multi_version={"versions": 2}, region=region)["binding_level"] == "exact_technical_variant"


def test_repeated_for_every_version():
    from src.evidence_admission import FactContext, repeated_for_every_version

    def ctx(line):
        return FactContext(fragment=line, clause=line, source_lines=[line], headings=[])
    assert repeated_for_every_version(4939, ctx('4,939 4,939 )מ"מ( ךרוא'), 2)
    assert not repeated_for_every_version(2933, ctx('2,933 )מ"מ( םינרס קחרמ'), 2)
    assert not repeated_for_every_version(500, ctx("51.0/1,370-4,500 )מ\"גק/ד\"לס( יברימ טנמומ"), 2)


# --- R5: internally inconsistent version pages ---------------------------------------------------------------------------

def test_a6_506502_page_is_inconsistent_and_never_binds_above_body_powertrain(cache):
    material, _, _ = _material(cache, F.A6_PAYLOAD, F.A6_506502_URL, F.a6_506502_html())
    page = material.version_page
    assert page["single_version"] and page["status"] == "rejected" and page["reason"] == "page_inconsistent"
    assert page["page_inconsistent"] == {"displacement_l": 2.0, "power_hp": 341.0, "catalog_powers": [250]}
    record = _record(cache, F.A6_PAYLOAD, F.A6_506502_URL, F.a6_506502_html(), "length_mm", 4940, 'אורך (ס"מ) 494')
    assert "page_inconsistent" in record["binding_flags"] and record["variant_match"] != "exact"
    assert record["version_page"]["page_inconsistent"]["power_hp"] == 341.0


def test_a_consistent_page_is_not_flagged(cache):
    material, _, _ = _material(cache, F.A4_PAYLOAD, F.A4_530178_URL, F.a4_530178_html())
    assert "page_inconsistent" not in material.version_page            # 245 hp is a 2.0 l catalog power of the A4


# --- R6: gearbox type mapping -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("תיבת הילוכים | רובוטית כפולת מצמדים", "dct"),
    ("Gearbox: 7-speed S tronic", "dct"), ("gearbox: 6-speed DSG", "dct"), ("Transmission: PDK", "dct"),
    ("תיבת הילוכים | אוטומטית רובוטית כפולת מצמד", "dct"), ("תיבת הילוכים | כפול מצמד", "dct"),
    ("Transmission: 8-speed Tiptronic", "automatic"), ("תיבת הילוכים | אוטומטית פלנטרית (רגילה)", "automatic"),
    ("תיבת הילוכים | רובוטית", "amt"), ("Transmission: robotized manual", "amt"),
])
def test_gearbox_type_mapping(text, expected):
    from src.candidate_harvest import harvest_text

    values = {c["value"] for c in harvest_text(text, resolve_requested_fields(None, propulsion="conventional"))
              if c["field"] == "gearbox_type"}
    assert values == {expected}


def test_q3_s_tronic_gearbox_is_dct(cache):
    record = _record(cache, A.Q3_PAYLOAD, P.Q3_CARTUBE_URL, F.q3_plain_html(), "gearbox_type", "dct",
                     "תיבת הילוכים | רובוטית כפולת מצמדים")
    assert record["value"] == "dct" and record["variant_match"] == "exact"
    harvested = admit(_adm(A.Q3_PAYLOAD), cache, {"field": "gearbox_type", "value": "amt",
                                                  "quote": "תיבת הילוכים | רובוטית כפולת מצמדים",
                                                  "document_id": put(cache, P.Q3_CARTUBE_URL, F.q3_plain_html(), "html")})
    assert not harvested["accepted"]                                          # the S tronic is never an amt


# --- R7: government registry coverage ----------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("235/50R19", "235/50 R19"), ("235/50 R19", "235/50 R19"), ("235/50ZR19", "235/50 R19"),
    ("235/50 ZR 19", "235/50 R19"), ("245/40R20XL", "245/40 R20"), ("245/45R19 98Y", "245/45 R19"),
    ("245/45 R19 98W XL", "245/45 R19"), ("245/45R1998Y", "245/45 R19"), ("225/45R17 RFT", "225/45 R17"),
    ("225/45R17 ROF", "225/45 R17"), ("225/45 R17 RF", "225/45 R17"), (" 235/50  R19 ‏", "235/50 R19"),
    ("צמיג 235/50R19 קדמי", "235/50 R19"), ("255/35 ZR 20 97Y XL", "255/35 R20"), ("245/45 R19 (98Y)", "245/45 R19"),
    ("P225/45R17", "225/45 R17"), ("225/45-17", "225/45 R17"),
    ("abc", None), ("", None), (None, None), ("0", None), ("999/99R99", None), ("235/50R195", None),
    ("235/50R19 / 255/45R19", None), ("235/50R19 2019", None), ("195R15C", None), ("31X10.50R15", None),
])
def test_registry_tyre_forms(raw, expected):
    from src.gov_registry import normalize_tire

    assert normalize_tire(raw) == expected


def test_registry_join_key_normalisation():
    from src.gov_registry import normalize_trim, payload_key, registry_key

    assert normalize_trim("S-LINE") == normalize_trim("S LINE") == normalize_trim(" s  line ") == "S LINE"
    assert normalize_trim("I.E_40 ADVAN") == "I E 40 ADVAN"
    assert registry_key("19", 994, "2020", "S-LINE") == "19|994|2020|S LINE"
    assert payload_key(F.A4_PAYLOAD) == "19|994|2020|S LINE"


def _rows(n, front, *, trim, degem=994, maker=19, rear=None):
    return [{"tozeret_cd": maker, "degem_cd": degem, "shnat_yitzur": 2020, "ramat_gimur": trim, "zmig_kidmi": front,
             "zmig_ahori": rear or front} for _ in range(n)]


def test_build_merges_trims_and_stores_diagnostics():
    import build_gov_registry_index as B

    records = (_rows(12, "245/35R19 93Y XL", trim="S-LINE") + _rows(10, "245/35 ZR 19", trim="S LINE")
               + _rows(25, "garbage tyre", trim="BASE", degem=995)              # unparsed: dropped by unparsed sizes
               + _rows(5, "225/50R17", trim="SPORT", degem=996, maker=21))     # n < 20
    stats: dict = {}
    counts = B.aggregate(records, stats)
    index = B.build_index(counts, source="fixture", resource_id=None, rows=len(records), stats=stats)
    entry = index["entries"]["19|994|2020|S LINE"]
    assert entry["front"]["n"] == 22 and entry["front"]["majority"] == "245/35 R19"
    assert entry["merged_trims"] == {"S LINE": 10, "S-LINE": 12}
    st = index["stats"]
    assert (st["keys"], st["kept"], st["dropped_unparsed"], st["dropped_small"], st["merged_keys"]) == (3, 1, 1, 1, 1)
    assert st["unparsed_top"] == [{"raw": "garbage tyre", "n": 50}] and st["unparsed_rows"] == 25
    assert st["manufacturers"] == {"19": {"kept": 1, "dropped": 1}, "21": {"kept": 0, "dropped": 1}}
    text = B.report(index)
    assert "dropped by unparsed sizes: 1" in text and "'garbage tyre'" in text


def test_runtime_lookup_normalises_an_old_index_and_refuses_a_clash():
    from src.gov_registry import entry_for

    axle = {"majority": "245/35 R19", "share": 1.0, "n": 30, "top": [{"size": "245/35 R19", "n": 30}]}
    old = {"complete": True, "entries": {"19|994|2020|S-LINE": {"front": axle, "rear": axle}}}
    assert entry_for("19|994|2020|S LINE", old)["front"]["n"] == 30
    clash = {"complete": True, "entries": {"19|994|2020|S-LINE": {"front": axle}, "19|994|2020|S LINE": {"rear": axle}}}
    assert entry_for("19|994|2020|S LINE", clash) == {"rear": axle}                 # the exact normalized key wins
    clash2 = {"complete": True, "entries": {"19|994|2020|S-LINE": {"front": axle}, "19|994|2020|S.LINE": {"rear": axle}}}
    assert entry_for("19|994|2020|S LINE", clash2) is None                        # two raw trims, no merge: nothing


def test_coverage_script_on_a_small_fixture(tmp_path, capsys):
    import gov_registry_coverage as C

    axle = {"majority": "245/35 R19", "share": 0.95, "n": 40, "top": [{"size": "245/35 R19", "n": 38}]}
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"complete": True, "generated_at": "2026-10-05T00:00:00+00:00", "rows": 80,
                                 "entries": {"19|994|2020|S LINE": {"front": axle, "rear": axle},
                                             "19|763|2018|LIMITED": {"front": {**axle, "n": 10}}}}), "utf-8")
    rows = [{"upstream_record_id": "13362", "tozar": "אאודי", "kinuy_mishari": "A4", "shnat_yitzur": 2020,
             "ramat_gimur": "S-LINE", "tozeret_cd": 19, "degem_cd": 994, "equipment": {}},
            {"upstream_record_id": "12949", "tozar": "אאודי", "kinuy_mishari": "A6", "shnat_yitzur": 2018,
             "ramat_gimur": "LIMITED", "tozeret_cd": 19, "degem_cd": 763, "equipment": {}},
            {"upstream_record_id": "94995", "tozar": "אאודי", "kinuy_mishari": "Q3", "shnat_yitzur": 2024,
             "ramat_gimur": "S LINE", "tozeret_cd": 990, "degem_cd": 308, "equipment": {}}]
    level15 = tmp_path / "level15.json"
    level15.write_text(json.dumps({"rows": rows}, ensure_ascii=False), "utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"entries": {"אאודי|a4|2020|sedan|conventional|awd|265|2.0": {
        "trims": ["S-LINE"], "records": ["13354", "13362"]}, "אאודי|q3|2024|suv|conventional|awd|190|2.0": {
        "trims": ["SLINE"], "records": ["94995"]}}}, ensure_ascii=False), "utf-8")
    assert C.main(["--index", str(index), "--level15-json", str(level15), "--catalog", str(catalog), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    bench = report["benchmark"]
    assert (bench["records"], bench["with_entry"], bench["with_tyre"], bench["with_rim"]) == (3, 2, 1, 1)
    assert [m["record"] for m in bench["missing"]] == ["94995"]
    assert report["catalog"]["records"] == 3 and report["catalog"]["year_trim_in_registry"] == 2
    assert C.main(["--index", str(index), "--level15-json", str(level15), "--catalog", str(catalog)]) == 0
    assert "benchmark Level 1.5 records: entry 2/3" in capsys.readouterr().out
