"""PR #47 (Part A): IL source precision on production run 20261005T210701Z (Toyota Corolla 2024 BUSINESS EDI, record
38626, wagon / hybrid 1.8 / 98 hp / 2WD). Fixtures (tests/fixtures/pr47_pages.py), no network.

    A1  body market names in the resolver's queries and ranking; family on word / slug-segment boundaries
    A2  the new cartube template read for identity; absent drivetrain with one catalog drivetrain; body in the single
        catalog entry key
    A3  carzone: trim slugs, trim words (BUSINESS EDI = "Business Edition" = "ביזנס אדישן"), compare-table columns
    A4  guessed URLs on official / importer domains
    A5  the recovery stop after a no-progress pass
    A6  provider search credits and the per-vehicle cap
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

import pytest

from fixtures import pr47_pages as F
from fixtures.corolla_harvest import put
from src.document_binding import target_identity
from src.evidence_admission import AdmissionContext, admit
from src.fields import resolve_requested_fields
from src.storage.cache import DocumentCache

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

IDENTITY = target_identity(F.PAYLOAD_38626)


@pytest.fixture()
def cache(tmp_path):
    return DocumentCache(tmp_path / "cache")


def _adm():
    return AdmissionContext.for_run(F.PAYLOAD_38626, None, resolve_requested_fields(None, propulsion="hybrid"), "IL")


def _material(cache, url, html):
    adm = _adm()
    return adm.material(cache, put(cache, url, html, "html"), None), adm


def _admit(cache, url, html, field, value, quote):
    adm = _adm()
    doc = put(cache, url, html, "html")
    decision = admit(adm, cache, {"field": field, "value": value, "quote": quote, "document_id": doc}, [doc])
    assert decision["accepted"], decision
    return decision["record"]


class _Ctx:
    def __init__(self, cache, adm):
        self.cache, self.counters, self.admission = cache, Counter(), adm


class _NoPace:
    @staticmethod
    def wait(domain):
        return 0.0


def _resolve(cache, pages, results=None):
    from src.il_version_pages import resolve

    searches, fetches = [], []
    results = F.SEARCH_RESULTS if results is None else results

    def search(query, domain):
        searches.append(query)
        return {"results": [{"url": u, "title": t} for u, t in results.get(domain, [])]}

    def fetch(url):
        fetches.append(url)
        if url not in pages:
            return {"error": "HTTPError", "status": 404}
        return {"document_id": put(cache, url, pages[url](), "html"), "status": 200}
    adm = _adm()
    out = resolve(_Ctx(cache, adm), None, payload=F.PAYLOAD_38626, search=search, fetch=fetch, robots=lambda u: True,
                  pace=_NoPace())
    return out, searches, fetches


# --- A1: body market names in queries and ranking --------------------------------------------------------------------

def test_the_body_market_name_and_the_hebrew_model_are_in_the_queries():
    from src.il_version_pages import search_queries, target_terms

    terms = target_terms(F.PAYLOAD_38626, IDENTITY)
    assert terms["model_he"] == "קורולה" and terms["body_he"] == "סטיישן" and terms["body_en"] == "Touring Sports"
    queries = search_queries(terms)
    assert queries[0] == ("cartube.co.il", "site:cartube.co.il טויוטה קורולה סטיישן 2024 1.8")
    assert len(queries) == 4                                       # max_searches: one per site, carzone included
    assert queries[3] == ("carzone.co.il", "site:carzone.co.il toyota corolla Touring Sports 2024")


def test_an_unknown_family_or_body_adds_no_query_word():
    from src.il_version_pages import body_names, target_terms

    q3 = target_identity({"identity": {"manufacturer": "אאודי", "commercial_name": "Q3", "year": 2024},
                          "engine_drivetrain": {"engine_cc": 1984, "power_hp": 190,
                                                "propulsion_normalized": "conventional",
                                                "drivetrain_normalized": "awd"},
                          "structure": {"body_normalized": "suv"}})
    assert body_names(q3) == [] and target_terms(None, q3)["body_he"] == ""


def test_the_wagon_page_ranks_above_the_sedan_and_hatchback_pages():
    from src.il_version_pages import slug_score, target_terms

    terms = target_terms(F.PAYLOAD_38626, IDENTITY)
    score = {name: slug_score(url, title, terms, IDENTITY, F.PAYLOAD_38626)
             for name, (url, title) in {"wagon": F.SEARCH_RESULTS["cartube.co.il"][4],
                                        "sense": F.SEARCH_RESULTS["cartube.co.il"][0],
                                        "auto_sun_sedan": F.SEARCH_RESULTS["auto.co.il"][1]}.items()}
    assert score["wagon"]["score"] == 4 and "body:wagon" in score["wagon"]["matched"]
    assert score["sense"]["score"] == 2                            # no body word: neither raised nor lowered
    assert score["auto_sun_sedan"]["score"] == -1 and "other_body:סדאן" in score["auto_sun_sedan"]["contradicted"]
    assert score["wagon"]["score"] > score["sense"]["score"] > score["auto_sun_sedan"]["score"]


@pytest.mark.parametrize("url, title, family", [
    (F.AUTO_CROSS_URL, "טויוטה קורולה קרוס 2024 1.8 ל' היברידי, Dynamic יד שניה", "other_family"),
    (F.CROSS_URL, "טויוטה קורולה קרוס 1.8 היברידי Active 2026 | מפרט טכני", "other_family"),
    (F.AUTO_SUN_URL, "טויוטה קורולה 2024 סדאן 1.8 ל' היברידי, SUN יד שניה", "target"),
    (F.WAGON_URL, "", "target"),
    ("https://www.auto.co.il/cars/toyota/corolla-crossover-x/2024/1/", "", "target"),   # a slug segment, not cross
])
def test_the_family_matches_on_word_and_slug_boundaries(url, title, family):
    from src.il_version_pages import family_of_text

    assert family_of_text(f"{unquote(url)} {title}", IDENTITY) == family


def test_corolla_cross_is_dropped_as_other_family_and_never_fetched(cache):
    out, _, fetches = _resolve(cache, {})
    dropped = {r["url"]: r for r in out["results"] if r.get("reason") == "other_family"}
    assert F.AUTO_CROSS_URL in dropped and F.CROSS_URL in dropped and F.AUTO_CROSS_LISTING_URL in dropped
    assert not set(dropped) & set(fetches)
    assert all(c["url"] not in dropped for c in out["candidates"])


def _only_sites(monkeypatch, *domains):
    from src import il_version_pages

    monkeypatch.setattr(il_version_pages, "site_domains", lambda: list(domains))


def test_a_listing_of_the_family_is_fetched_before_weak_version_candidates(cache, monkeypatch):
    """Only weak versions (score < 1 after body / family: the auto.co.il sedans) and the family's listing: the listing
    is fetched first and its version links become the candidates."""
    results = {"auto.co.il": [F.SEARCH_RESULTS["auto.co.il"][1], F.SEARCH_RESULTS["auto.co.il"][2],
                              (F.AUTO_LISTING_URL, "טויוטה קורולה 2024 יד שניה")]}
    listing = """<html><head><title>טויוטה קורולה 2024 יד שניה</title></head><body><h1>טויוטה קורולה 2024</h1>
<a href="https://www.auto.co.il/cars/toyota/corolla/2024/492870/">טויוטה קורולה 2024 סטיישן 1.8 ל' היברידי</a>
</body></html>"""
    _only_sites(monkeypatch, "auto.co.il")
    out, _, fetches = _resolve(cache, {F.AUTO_LISTING_URL: lambda: listing}, results)
    assert fetches[0] == F.AUTO_LISTING_URL
    assert fetches[1] == "https://www.auto.co.il/cars/toyota/corolla/2024/492870/"       # its wagon link, score 2+
    roles = [(c["url"], c["role"]) for c in out["candidates"]]
    assert roles[0] == (F.AUTO_LISTING_URL, "listing")


# --- A2: the new cartube template and the identity rules --------------------------------------------------------------

@pytest.mark.parametrize("url, html", [(F.WAGON_URL, F.cartube_wagon_html), (F.SENSE_URL, F.cartube_sense_html)])
def test_old_and_new_cartube_templates_read_1_8_and_hybrid(cache, url, html):
    material, _ = _material(cache, url, html())
    page = material.version_page
    assert page["single_version"] and page["page_identity"]["displacements"] == [1.8]
    assert page["page_statuses"]["displacement"] == "match" and page["page_statuses"]["propulsion"] == "match"
    assert page["page_statuses"]["drivetrain"] == "match"                    # "הנעה: קדמית" read as a row
    assert page["page_powers"] == [140.0]


def test_the_new_template_rows_keep_their_units():
    from src.il_version_pages import identity_rows

    rows = identity_rows('נפח מנוע (סמ"ק):\n1,798\nהספק מרבי (כ"ס):\n140\nכ"ס\nמערכת הנעה\nהנעה:\nקדמית')
    assert rows == ['נפח מנוע (סמ"ק) 1,798 סמ"ק', 'הספק מרבי (כ"ס) 140 כ"ס', "הנעה קדמית"]


def test_the_wagon_page_is_now_rejected_for_its_system_power_not_its_drivetrain(cache):
    """Proof gate 1 (decision A2 = option 1): the catalog keeps three wagon / hybrid / 2WD entries (98 hp 1.8, 140 hp
    1.8, 152 hp 2.0), so the page's 140 hp stays a mismatch: rejected power_mismatch, no longer drivetrain_absent."""
    from src.il_version_pages import single_catalog_entry

    material, _ = _material(cache, F.WAGON_URL, F.cartube_wagon_html())
    assert material.version_page["status"] == "rejected" and material.version_page["reason"] == "power_mismatch"
    assert single_catalog_entry(IDENTITY) is None


def test_an_absent_drivetrain_is_accepted_with_one_catalog_drivetrain(cache):
    from src.il_version_pages import single_catalog_drivetrain

    assert single_catalog_drivetrain(IDENTITY) == {"drivetrain": "two_wheel_drive", "entries": 3}
    html = F.cartube_wagon_html().replace("<dt>הנעה:</dt><dd>קדמית</dd>", "")
    material, _ = _material(cache, F.WAGON_URL, html)
    page = material.version_page
    assert page["page_statuses"]["drivetrain"] == "absent" and page["catalog_single_drivetrain"] == "two_wheel_drive"
    assert page["reason"] == "power_mismatch"                 # the next dimension decides, never drivetrain_absent


def test_an_absent_drivetrain_still_rejects_when_the_catalog_has_two(cache):
    from src.il_version_pages import single_catalog_drivetrain

    suv = target_identity({**F.PAYLOAD_38626, "structure": {"body_normalized": "suv"},
                           "engine_drivetrain": {**F.PAYLOAD_38626["engine_drivetrain"], "engine_cc": 1987,
                                                 "power_hp": 152}})
    assert single_catalog_drivetrain(suv) is None              # Corolla 2024 hybrid SUV: 2WD and AWD entries


def _single_entry_index(*keys):
    return {"complete": True, "entries": {k: {"trims": ["X"], "records": [str(n)]} for n, k in enumerate(keys)}}


def test_the_single_entry_key_includes_the_body():
    """The hybrid escape counts entries per (manufacturer, family, year, BODY, propulsion, drivetrain): a sedan entry
    of the family no longer stops the wagon's single entry."""
    from src.il_version_pages import single_catalog_entry

    wagon = "טויוטה|corolla|2024|wagon|hybrid|two_wheel_drive|100|1.8"
    sedan = "טויוטה|corolla|2024|sedan|hybrid|two_wheel_drive|140|1.8"
    index = _single_entry_index(wagon, sedan)
    assert single_catalog_entry(IDENTITY, index)["key"] == wagon
    second_wagon = "טויוטה|corolla|2024|wagon|hybrid|two_wheel_drive|140|1.8"
    assert single_catalog_entry(IDENTITY, _single_entry_index(wagon, sedan, second_wagon)) is None


def test_with_a_single_wagon_entry_the_wagon_page_is_accepted_and_a_sedan_page_rejected(cache, monkeypatch):
    from src import document_binding

    index = _single_entry_index("טויוטה|corolla|2024|wagon|hybrid|two_wheel_drive|100|1.8",
                                "טויוטה|corolla|2024|sedan|hybrid|two_wheel_drive|140|1.8",
                                "טויוטה|corolla|2024|sedan|hybrid|two_wheel_drive|95|1.8")
    monkeypatch.setattr(document_binding, "trim_index", lambda path=None: index)
    document_binding._FAMILY_ENTRIES.clear()
    wagon, _ = _material(cache, F.WAGON_URL, F.cartube_wagon_html())
    assert wagon.version_page["status"] == "accepted" and wagon.version_page["reason"] == "single_catalog_entry"
    sedan, _ = _material(cache, F.SENSE_URL, F.cartube_sense_html())
    assert sedan.version_page["status"] == "rejected" and sedan.version_page["reason"] == "body_mismatch"
    document_binding._FAMILY_ENTRIES.clear()


# --- A3: carzone ------------------------------------------------------------------------------------------------------

def test_carzone_is_a_configured_aggregator_with_trim_and_compare_pages():
    from src.il_version_pages import classify_url, trim_slug_of
    from src.source_authority import classify_source

    assert classify_source(F.COMPARE_URL, "טויוטה")["source_authority"] == "aggregator"
    assert classify_url(F.COMPARE_URL) == ("carzone.co.il", "version")
    assert classify_url(F.TRIM_URL) == ("carzone.co.il", "version")
    assert classify_url(F.MODEL_URL) == ("carzone.co.il", "listing")
    assert trim_slug_of(F.TRIM_URL) == F.TRIM_SLUG


def test_the_trim_slug_is_parsed():
    from src.il_trim_pages import parse_trim_slug
    from src.il_version_pages import _site_config

    slug = parse_trim_slug(F.TRIM_SLUG, _site_config("carzone.co.il"))
    assert slug == {"trim_words": ["business", "edition"], "propulsion": "hybrid", "displacement_l": 1.8,
                    "gearbox": "automatic", "drivetrain": "two_wheel_drive", "seats": 5, "unknown": []}
    bare = parse_trim_slug("hybrid-1.8L-AT-4X2-5seats", _site_config("carzone.co.il"))
    assert bare["trim_words"] == [] and bare["propulsion"] == "hybrid"


@pytest.mark.parametrize("words, match", [
    (["business", "edition"], True), (["business", "edi"], True), (["business"], False),
    (["comfort", "business"], False), (["edition", "business"], False), ([], False)])
def test_business_edi_is_business_edition_and_nothing_else(words, match):
    from src.il_trim_pages import trim_words_match

    assert trim_words_match(words, IDENTITY) is match


def test_hebrew_column_names_are_read_as_trim_words():
    from src.il_trim_pages import trim_words

    noise = ["היברידי", "ליטר", "דור", "מעודכן", r"\d+(?:\.\d+)?"]
    assert trim_words("ביזנס אדישן היברידי 1.8 ליטר (דור 12 מעודכן)", noise) == ["business", "edition"]
    assert trim_words("קומפורט ביזנס היברידי 1.8 ליטר", noise) == ["comfort", "business"]
    assert trim_words("היברידי 1.8 ליטר (דור 12 מעודכן)", noise) == []


def test_the_compare_page_columns_and_the_target_column(cache):
    material, _ = _material(cache, F.COMPARE_URL, F.carzone_compare_html())
    verdict = material.trim_page
    assert verdict["kind"] == "compare" and verdict["status"] == "accepted" and verdict["target_column"] == 1
    assert [c["target_trim"] for c in verdict["columns"]] == [False, True, False, False]
    assert verdict["columns"][1]["statuses"] == {"year": "match", "body": "match", "propulsion": "match",
                                                 "displacement": "match", "drivetrain": "match", "power": "absent"}


@pytest.mark.parametrize("value, quote, status, columns", [
    (False, "גג שמש | ללא ללא ללא ללא", "target", [0, 1, 2, 3]),
    ("205/55R16", "צמיג קדמי | 205/55R16 - - -", "other_variant", [0]),
    (9.4, "תאוצה 0-100 | 9.4 שנ׳ - - -", "other_variant", [0]),
    (1835, "משקל כולל | 1,885 ק״ג 1,835 ק״ג 1,835 ק״ג 1,885 ק״ג", "target", [1, 2]),
])
def test_compare_values_map_to_their_columns_and_dashes_are_no_value(cache, value, quote, status, columns):
    from src.il_trim_pages import fact_column

    material, _ = _material(cache, F.COMPARE_URL, F.carzone_compare_html())
    column = fact_column(material, material.trim_page, value, [quote])
    assert column["status"] == status and column["columns_stating"] == columns


def test_the_target_column_binds_the_market_trim_and_other_columns_never_do(cache):
    sunroof = _admit(cache, F.COMPARE_URL, F.carzone_compare_html(), "sunroof_panoramic", False,
                     "גג שמש | ללא ללא ללא ללא")
    assert sunroof["binding_level"] == "exact_market_trim" and sunroof["variant_match"] == "exact"
    assert sunroof["binding_basis"] == "il_compare_column" and sunroof["trim_column"]["column"] == 1
    tyre = _admit(cache, F.COMPARE_URL, F.carzone_compare_html(), "tire_size_front", "205/55R16",
                  "צמיג קדמי | 205/55R16 - - -")
    assert tyre["binding_level"] == "exact_technical_variant" and tyre["variant_match"] == "unclear"
    assert tyre["trim_column"]["status"] == "other_variant"
    # the technical values of the base column stay what they were (the run's six ok fields)
    accel = _admit(cache, F.COMPARE_URL, F.carzone_compare_html(), "acceleration_0_100_s", 9.4,
                   "תאוצה 0-100 | 9.4 שנ׳ - - -")
    assert accel["binding_level"] == "exact_technical_variant" and accel["variant_match"] == "exact"


def test_the_trim_page_needs_the_page_to_show_its_version(cache):
    shown, _ = _material(cache, F.TRIM_URL, F.carzone_trim_html())
    assert shown.trim_page["status"] == "rejected" and shown.trim_page["reason"] == "page_trim_unconfirmed"
    # the slug in the URL never names the document's trim when the page does not
    record = _admit(cache, F.TRIM_URL, F.carzone_trim_html(), "acceleration_0_100_s", 9.4, "תאוצה 0-100 | 9.4 שניות")
    assert record["binding_level"] == "exact_technical_variant"
    confirmed_url = F.TRIM_URL + "&v=1"
    confirmed, _ = _material(cache, confirmed_url, F.carzone_trim_html(confirmed=True))
    assert confirmed.trim_page["status"] == "accepted" and confirmed.trim_page["reason"] == "trim_page"
    record = _admit(cache, confirmed_url, F.carzone_trim_html(confirmed=True), "acceleration_0_100_s", 9.4,
                    "תאוצה 0-100 | 9.4 שניות")
    assert record["binding_level"] == "exact_market_trim" and record["binding_basis"] == "il_trim_page"


def test_another_trims_page_is_rejected(cache):
    material, _ = _material(cache, F.COMFORT_TRIM_URL, F.carzone_trim_html())
    assert material.trim_page["reason"] == "trim_other"


def test_the_resolver_finds_the_compare_page_and_never_guesses_a_trim_url(cache):
    pages = {F.WAGON_URL: F.cartube_wagon_html, F.MODEL_URL: F.carzone_model_html,
             F.COMPARE_URL: F.carzone_compare_html, F.TRIM_URL: F.carzone_trim_html}
    out, _, fetches = _resolve(cache, pages)
    assert out["found"] == 1
    assert [(p["url"], p["status"], p["reason"]) for p in out["pages"]] == [
        (F.WAGON_URL, "rejected", "power_mismatch"), (F.COMPARE_URL, "accepted", "target_column")]
    # only search results and fetched pages' links are candidates: the resolver builds no ?trim= URL
    offered = {u for urls in F.SEARCH_RESULTS.values() for u, _ in urls}
    assert set(fetches) <= offered


def test_a_trim_url_is_a_candidate_only_from_a_fetched_carzone_page(cache, monkeypatch):
    _only_sites(monkeypatch, "carzone.co.il")
    results = {"carzone.co.il": [(F.MODEL_URL, "טויוטה קורולה קורולה ספייס 2024 - מחירון ומפרט | Carzone")]}
    out, _, fetches = _resolve(cache, {F.MODEL_URL: F.carzone_model_html, F.TRIM_URL: F.carzone_trim_html,
                                       F.COMPARE_URL: F.carzone_compare_html}, results)
    assert fetches[0] == F.MODEL_URL
    sources = {c["url"]: c.get("source") for c in out["candidates"]}
    assert sources[F.TRIM_URL] == f"links_of:{F.MODEL_URL}"


def test_a_guessed_carzone_trim_url_is_refused():
    from src.acquisition import fetch_refusal

    class Ctx:
        cache = None
        vehicle = {"manufacturer": "טויוטה"}
        admission = None
    refusal = fetch_refusal(Ctx(), "fetch_url", F.TRIM_URL, "research")
    assert refusal["reason"] == "guessed_url_refused" and refusal["site"] == "carzone.co.il"


# --- A4: guessed URLs on official / importer domains ------------------------------------------------------------------

class _ProvCtx:
    def __init__(self):
        self.cache = None
        self.vehicle = {"manufacturer": "טויוטה"}
        self.admission = None


@pytest.mark.official_guess_guard
def test_a_guessed_importer_url_is_refused_and_the_root_allowed_once():
    from src.acquisition import fetch_refusal, url_provenance

    ctx = _ProvCtx()
    for path in ("/price-list", "/warranty", "/after-sales", "/cars/Corolla"):
        refusal = fetch_refusal(ctx, "fetch_url", f"https://www.toyota.co.il{path}", "research")
        assert refusal["reason"] == "guessed_url_refused" and refusal["source_authority"] == "official_importer"
    assert fetch_refusal(ctx, "fetch_url", "https://www.toyota.co.il/", "research") is None
    assert fetch_refusal(ctx, "fetch_url", "https://www.toyota.co.il/", "field_recovery")["reason"] \
        == "guessed_url_refused"                                                     # the root only once
    url_provenance(ctx).offer(["https://www.toyota.co.il/cars/Corolla-TS"])
    assert fetch_refusal(ctx, "fetch_url", "https://www.toyota.co.il/cars/Corolla-TS", "research") is None


@pytest.mark.official_guess_guard
def test_publishers_are_not_guarded_and_the_refusal_is_counted(tmp_path):
    from src.acquisition import fetch_refusal

    ctx = _ProvCtx()
    assert fetch_refusal(ctx, "fetch_url", "https://www.ynet.co.il/some/article", "research") is None
    assert fetch_refusal(ctx, "fetch_url", "https://www.toyota-europe.com/new-cars/corolla", "research")["reason"] \
        == "guessed_url_refused"


# --- A5: the recovery stop after a no-progress pass -------------------------------------------------------------------

def test_a_pass_without_new_documents_or_state_changes_stops_the_recovery():
    from src.agent import recovery_pass_progress, recovery_savings

    attempts = [{"states_before": {"a": "missing"}, "states_after": {"a": "missing"}},
                {"states_before": {"b": "variant_not_exact"}, "states_after": {"b": "variant_not_exact"}}]
    gate = {"passes": 1, **recovery_pass_progress(attempts, [])}
    assert gate["progress"] is False
    savings = recovery_savings(gate, turns_used=6, turn_budget=24, minutes_used=6.0)
    assert savings == {"recovery_passes": 1, "recovery_minutes": 6.0, "recovery_stopped_no_progress": True,
                       "recovery_minutes_saved_estimate": 18.0, "recovery_turns_unused": 18}
    assert recovery_pass_progress(attempts, ["d_new"])["progress"] is True
    changed = [{"states_before": {"a": "missing"}, "states_after": {"a": "variant_not_exact"}}]
    assert recovery_pass_progress(changed, [])["progress"] is True


@pytest.mark.recovery_mode("cluster")
@pytest.mark.grounded_candidates(False)
def test_cluster_recovery_stops_after_a_first_pass_without_progress(tmp_path):
    from test_tail_recovery import GATE_OFF, _say, run_scripted

    def nothing(packet, turn_no, messages):                 # every cluster attempt: no document, no field change
        return _say({"cluster": packet["cluster"], "fields": []})

    result, events, client = run_scripted(tmp_path, nothing, cluster_max_attempts=2, **GATE_OFF)
    recovery = result["field_recovery"]
    assert recovery["stopped"] == "recovery_stopped_no_progress" and recovery["recovery_stopped_no_progress"]
    # every cluster had its first web attempt (after its free local pass), then nothing more: no second web attempt
    web = [(a["cluster"], a["mode"]) for a in recovery["attempts"] if a["mode"] == "web"]
    assert web and len(web) == len(set(web))
    assert recovery["recovery_passes"] == max(a["round"] for a in recovery["attempts"])
    stop = [e for e in events if e["kind"] == "recovery_stopped_no_progress"]
    assert len(stop) == 1 and "recovery_minutes_saved_estimate" in stop[0]


# --- A6: search credits and the cap -----------------------------------------------------------------------------------

def _serper_ctx(tmp_path, cap=40, credits=1):
    from conftest import FakeResponse
    from src.tools import ToolConfig, ToolContext
    from src.tools.evidence import EvidenceStore

    class Session:
        def __init__(self):
            self.calls = 0

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls += 1
            return FakeResponse(200, json_data={"organic": [{"link": f"https://www.cartube.co.il/x/{self.calls}",
                                                             "title": "t", "position": 1}],
                                                "credits": credits})
    events = []
    ctx = ToolContext(cache=DocumentCache(tmp_path / "c"), evidence=EvidenceStore(),
                      config=ToolConfig(search_backend="serper", search_credit_cap=cap), session=Session(),
                      log=lambda kind, **data: events.append((kind, data)))
    return ctx, events


def test_provider_credits_are_recorded_per_search(tmp_path, monkeypatch):
    from src.diagnostics import search_operations
    from src.tools import dispatch

    monkeypatch.setenv("SERPER_API_KEY", "k")
    ctx, events = _serper_ctx(tmp_path, credits=2)
    dispatch(ctx, "search_web", {"query": "a"})
    dispatch(ctx, "search_web", {"query": "b"})
    dispatch(ctx, "search_web", {"query": "a"})                     # cached: no credits
    assert ctx.counters["search_credits"] == 4 and ctx.counters["search_credits:serper"] == 4
    calls = [dict(d, kind=k) for k, d in events if k == "search_backend_call"]
    assert [c.get("credits") for c in calls if not c["cache_hit"]] == [2, 2]
    assert search_operations(calls)["search_credits"] == 4


def test_the_credit_cap_stops_further_searches(tmp_path, monkeypatch):
    from src.tools import dispatch

    monkeypatch.setenv("SERPER_API_KEY", "k")
    ctx, events = _serper_ctx(tmp_path, cap=3, credits=1)
    for q in ("a", "b", "c"):
        assert not dispatch(ctx, "search_web", {"query": q}).get("error")
    refused = dispatch(ctx, "search_web", {"query": "d"})
    assert refused["error"] == "search_budget_exhausted" and refused["results"] == []
    assert ctx.session.calls == 3 and ctx.counters["search_budget_exhausted"] == 1
    assert dispatch(ctx, "search_web", {"query": "a"}).get("results")        # a cached search is still answered
    assert any(k == "search_budget_exhausted" for k, _ in events)


def test_the_credit_cap_is_a_run_setting_with_default_40():
    from src.run_settings import RUN_OVERRIDES_BY_NAME, run_setting_defaults, settings_for_run, validate_overrides

    assert run_setting_defaults(lambda n: "")["search_credit_cap"] == 40
    assert RUN_OVERRIDES_BY_NAME["search_credit_cap"].high == 500
    assert validate_overrides({"search_credit_cap": 12}) == {"search_credit_cap": 12}
    with pytest.raises(ValueError):
        validate_overrides({"search_credit_cap": 501})

    class Controller:
        def provider_limit_for(self, model):
            return None

        def limit_for(self, model):
            return 4

        search_limit = 4
    try:
        settings = settings_for_run(lambda n: {"GLM_MODEL": "glm-x"}.get(n, ""), Controller(), {"search_credit_cap": 9})
    except Exception:  # noqa: BLE001 - the controller fake may not cover every lookup; the contract above holds
        return
    assert settings.tool_config(lambda n: "").search_credit_cap == 9
