"""PR #44: Israeli source playbook (fixtures, no network).

    P1  government registry index: CKAN page aggregation (majority, share, N), tyre / rim evidence emission rules
    P2  Israeli version-page resolver: page identity, acceptance, budgets, robots.txt, accounting
    P3  torque rpm: an engine speed is never a torque; a kgf·m (קג"מ) row is converted to whole Nm
    P4  H1 relaxed: engine-invariant fields, single-version Israeli pages, the hybrid single-version power tightening
    P5  render diagnosis: challenge shells are unreadable on the first hit and never rendered; a local JS render
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from fixtures import pr43_audi_pages as A
from fixtures import pr44_pages as P
from fixtures.corolla_harvest import put
from src.candidate_harvest import RPM_REASON, harvest_text
from src.evidence_admission import AdmissionContext, admit
from src.fields import resolve_requested_fields
from src.storage.cache import DocumentCache

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))     # build_gov_registry_index


@pytest.fixture()
def cache(tmp_path):
    return DocumentCache(tmp_path / "cache")


def _adm(payload):
    propulsion = payload["engine_drivetrain"]["propulsion_normalized"]
    return AdmissionContext.for_run(payload, None, resolve_requested_fields(None, propulsion=propulsion), "IL")


def _decide(cache, payload, url, html, field, value, quote):
    adm = _adm(payload)
    doc = put(cache, url, html, "html")
    return admit(adm, cache, {"field": field, "value": value, "quote": quote, "document_id": doc}, [doc]), adm, doc


def _specs(propulsion="conventional"):
    return resolve_requested_fields(None, propulsion=propulsion)


# --- P3: torque rpm ---------------------------------------------------------------------------------------------------

def test_rpm_row_is_never_torque_and_kgfm_rows_convert():
    text = 'סל״ד מומנט מרבי | 1,500\nמומנט מרבי (קג"מ) | 32.6'
    values = [c["value"] for c in harvest_text(text, _specs()) if c["field"] == "torque_nm"]
    assert values == [320]                                   # 32.6 x 9.80665 = 319.7 -> 320, never 1500


def test_real_cartube_rows_give_324_nm_never_1500(cache):
    doc = put(cache, P.Q3_CARTUBE_URL, P.q3_cartube_html(synthetic_row=False), "html")
    from src.candidate_harvest import harvest_document

    candidates, _ = harvest_document(cache, doc, _specs())
    torque = sorted(c["value"] for c in candidates if c["field"] == "torque_nm")
    assert torque == [324]                                   # "מומנט | 33 קג״מ": 33 x 9.80665 = 323.6 -> 324
    assert 1500 not in torque


@pytest.mark.parametrize("text, expected", [
    ("max torque 320 Nm at 1,500-4,100 rpm", [320]),
    ("Max. torque: 320 Nm @ 1500 rpm", [320]),
    ('מומנט מירבי (סל"ד/ קג"מ) 1,500-3,500 / 25.5', [250]),     # the range is the rpm, 25.5 kgf·m the torque
    ("מומנט מרבי 1,500 סל\"ד", []),
    ("מומנט\n1,500 סל\"ד", []),
])
def test_rpm_forms(text, expected):
    assert [c["value"] for c in harvest_text(text, _specs()) if c["field"] == "torque_nm"] == expected


def test_admission_rejects_the_rpm_row_and_admits_the_kgfm_rows(cache):
    html = P.q3_cartube_html()
    rejected, _, _ = _decide(cache, A.Q3_PAYLOAD, P.Q3_CARTUBE_URL, html, "torque_nm", 1500, "סל״ד מומנט מרבי | 1,500")
    assert not rejected["accepted"] and rejected["reasons"] == ["semantic_mismatch"]
    assert rejected["semantic_note"] == RPM_REASON
    for value, quote in ((320, 'מומנט מרבי (קג"מ) | 32.6'), (324, "מומנט | 33 קג״מ")):
        decision, _, _ = _decide(cache, A.Q3_PAYLOAD, P.Q3_CARTUBE_URL, html, "torque_nm", value, quote)
        assert decision["accepted"], decision
        assert decision["record"]["value"] == value


def test_replay_sanity_rejects_the_recorded_rpm_torque(cache):
    from src.evidence_admission import sanity_rejection

    adm = _adm(A.Q3_PAYLOAD)
    doc = put(cache, P.Q3_CARTUBE_URL, P.q3_cartube_html(), "html")
    material = adm.material(cache, doc, None)
    rejection = sanity_rejection(adm, material, adm.spec("torque_nm"), 1500, "סל״ד מומנט מרבי | 1,500")
    assert rejection == {"reason": "semantic_mismatch", "note": RPM_REASON}


# --- P4: H1 relaxed without reopening the #43 errors ---------------------------------------------------------------------

Q8_ROWS = {"gearbox_type": ("automatic", "תיבת הילוכים | אוטומטית פלנטרית (רגילה)"),
           "gear_count": (8, "מספר הילוכים | 8"),
           "length_mm": (4990, 'אורך (ס"מ) | 499'), "width_mm": (2000, 'רוחב (ס"מ) | 200'),
           "height_mm": (1630, 'גובה (ס"מ) | 163'), "wheelbase_mm": (3000, 'בסיס גלגלים (ס"מ) | 300')}


@pytest.mark.parametrize("field", sorted(Q8_ROWS))
def test_q8_538115_engine_invariant_fields_are_exact(cache, field):
    value, quote = Q8_ROWS[field]
    decision, adm, doc = _decide(cache, A.Q8_PAYLOAD, P.Q8_538115_URL, P.q8_538115_html(), field, value, quote)
    assert decision["accepted"], decision
    record = decision["record"]
    assert record["variant_match"] == "exact" and record["binding_level"] == "exact_technical_variant", record
    assert record["binding_basis"] == "engine_invariant"
    assert "system_power_unmapped" in record["binding_flags"]              # the page's 394 hp is not the 340 hp record
    page = record["version_page"]
    assert page["status"] == "rejected" and page["reason"] == "power_mismatch" and page["single_version"]
    assert "catalog_single_entry" not in page                                # 3 plug-in AWD entries (340 / 395 / 490)


@pytest.mark.parametrize("field, value, quote", [
    ("acceleration_0_100_s", 5.7, '0 ל-100 קמ"ש (שניות) | 5.7'),
    ("top_speed_kmh", 240, 'מהירות מירבית (קמ"ש) | 240'),
    ("curb_weight_kg", 2415, 'משקל (ק"ג) | 2415'),
])
def test_q8_538115_power_dependent_fields_stay_unresolved(cache, field, value, quote):
    decision, _, _ = _decide(cache, A.Q8_PAYLOAD, P.Q8_538115_URL, P.q8_538115_html(), field, value, quote)
    assert decision["accepted"], decision
    record = decision["record"]
    assert record["variant_match"] != "exact" and record["binding_level"] == "body_powertrain", record
    assert record["binding_flags"] == ["system_power_unmapped"]


def test_q8_538115_page_identity_reads_the_spec_rows(cache):
    adm = _adm(A.Q8_PAYLOAD)
    doc = put(cache, P.Q8_538115_URL, P.q8_538115_html(), "html")
    page = adm.material(cache, doc, None).version_page
    # the comparison widgets ("GLE קופה") made the full text's body `mixed`; the spec rows say SUV, 2995 cc, 4X4, 2025
    assert page["page_statuses"] == {"year": "match", "body": "match", "propulsion": "match",
                                     "displacement": "match", "power": "mismatch", "drivetrain": "match"}
    assert page["single_version"] and page["page_powers"] == [394.0]


def test_q8_2020_article_gearbox_and_acceleration_stay_non_exact(cache):
    for field, value, quote in (("acceleration_0_100_s", 5.8, A.Q8_2020_QUOTE),
                                ("gear_count", 8, A.Q8_GEARBOX),
                                ("gearbox_type", "automatic", A.Q8_GEARBOX)):
        decision, _, _ = _decide(cache, A.Q8_PAYLOAD, A.Q8_2020_URL, A.q8_2020_html(), field, value, quote)
        assert decision["accepted"], decision
        record = decision["record"]
        assert record["variant_match"] != "exact", (field, record)
        assert {"several_powertrain_versions", "stale_publication"} <= set(record["binding_flags"])


def test_q7_senior_version_rim_stays_non_exact(cache):
    decision, _, _ = _decide(cache, A.Q7_PAYLOAD, A.Q7_URL, A.q7_html(), "rim_diameter_in", 22,
                             A.Q7_QUOTE_22)
    assert decision["accepted"], decision
    record = decision["record"]
    assert record["variant_match"] != "exact" and "relative_variant_reference" in record["binding_flags"]


def _index(tmp_path, monkeypatch, entries: dict) -> None:
    import json

    path = tmp_path / "catalog_trim_index.json"
    path.write_text(json.dumps({"complete": True, "entries": entries}), "utf-8")
    monkeypatch.setenv("CATALOG_TRIM_INDEX_PATH", str(path))


Q8_SINGLE = {"אאודי|q8|2025|suv|plug_in|awd|340|3.0": {"trims": ["SLINE SUPER"], "records": ["15376"]},
             "אאודי|q8|2025|suv|conventional|awd|285|3.0": {"trims": ["S-LINE"], "records": ["15372"]}}


def test_single_plug_in_catalog_entry_accepts_the_page_and_binds_every_value(cache, tmp_path, monkeypatch):
    _index(tmp_path, monkeypatch, Q8_SINGLE)
    decision, _, _ = _decide(cache, A.Q8_PAYLOAD, P.Q8_538115_URL, P.q8_538115_html(), "acceleration_0_100_s", 5.7,
                             '0 ל-100 קמ"ש (שניות) | 5.7')
    record = decision["record"]
    assert record["variant_match"] == "exact" and record["binding_basis"] == "il_version_page", record
    assert "system_power_unmapped" not in (record.get("binding_flags") or [])
    page = record["version_page"]
    assert page["status"] == "accepted" and page["reason"] == "single_catalog_entry"


def test_two_plug_in_catalog_entries_leave_the_page_unresolved(cache, tmp_path, monkeypatch):
    _index(tmp_path, monkeypatch, {**Q8_SINGLE, "אאודי|q8|2025|suv|plug_in|awd|490|3.0": {"trims": ["STANDARD"],
                                                                                          "records": ["15404"]}})
    decision, _, _ = _decide(cache, A.Q8_PAYLOAD, P.Q8_538115_URL, P.q8_538115_html(), "acceleration_0_100_s", 5.7,
                             '0 ל-100 קמ"ש (שניות) | 5.7')
    record = decision["record"]
    assert record["variant_match"] != "exact" and "system_power_unmapped" in record["binding_flags"]
    assert record["version_page"]["status"] == "rejected"


# --- P5: bot / challenge shells and the render diagnosis --------------------------------------------------------------

# audi.co.il / championmotors.co.il in production: HTTP 247, ~584 bytes, nothing readable even rendered (Radware)
RADWARE_SHELL = (b'<html><head><meta charset="utf-8"><title></title></head><body><script src="https://validate.'
                 b'perfdrive.com/static/js/sb.js"></script><noscript>Please enable JS</noscript></body></html>')


def _shell_ctx(make_ctx, monkeypatch, routes):
    from src.tools import render as render_module

    events, renders = [], []
    ctx = make_ctx(routes)
    ctx.vehicle = {"manufacturer": "אאודי"}
    ctx.log = lambda kind, **data: events.append((kind, data))
    monkeypatch.setattr(render_module, "_render", lambda *a, **k: renders.append(a) or {})
    return ctx, events, renders


@pytest.mark.parametrize("status, body, reason", [
    (247, RADWARE_SHELL, "http_247_shell"),
    (403, b"<html><head><title>Access Denied</title></head><body>Access Denied</body></html>", "http_403_shell"),
    (429, b"slow down", "http_429_shell"),
    (503, b"<html><body>busy</body></html>", "http_503_shell"),
    (200, b"<html><head><title>Just a moment...</title></head><body><div id='cf-challenge'>Checking your browser"
          b"</div></body></html>", "challenge_marker"),
])
def test_a_challenge_shell_is_unreadable_on_the_first_hit_and_never_rendered(make_ctx, monkeypatch, status, body,
                                                                              reason):
    from conftest import FakeResponse
    from src.tools import dispatch

    url, other = "https://www.audi.co.il/models/q8", "https://www.audi.co.il/price-list"
    ctx, events, renders = _shell_ctx(make_ctx, monkeypatch, {url: FakeResponse(body, status=status),
                                                              other: FakeResponse(body, status=status)})
    result = dispatch(ctx, "fetch_url", {"url": url})
    assert result["unreadable"] == reason and "rendered_fallback" not in result
    assert renders == []                                                   # never rendered
    assert ctx.unreadable_domains == {"audi.co.il": reason} and ctx.counters["acq_unreadable_domains"] == 1
    marked = [d for k, d in events if k == "domain_unreadable"]
    assert len(marked) == 1 and marked[0]["reason"] == reason and marked[0]["stage"] == "fetch"
    calls = len(ctx.session.calls)
    assert dispatch(ctx, "fetch_url", {"url": other})["error"] == "domain_unreadable"
    assert dispatch(ctx, "render_page", {"url": other})["error"] == "domain_unreadable"
    assert len(ctx.session.calls) == calls and renders == []               # no network, no browser for the domain


def test_a_full_page_and_a_large_403_are_not_shells(make_ctx, monkeypatch):
    from conftest import FakeResponse
    from src.tools import dispatch

    page = ("<html><head><title>Q8</title></head><body>" + "<p>מפרט טכני מלא של הדגם</p>" * 80 + "</body></html>")
    ctx, _, _ = _shell_ctx(make_ctx, monkeypatch, {
        "https://www.cartube.co.il/q8": FakeResponse(page.encode()),
        "https://www.cartube.co.il/denied": FakeResponse(page.encode(), status=403)})
    assert "unreadable" not in dispatch(ctx, "fetch_url", {"url": "https://www.cartube.co.il/q8"})
    assert "unreadable" not in dispatch(ctx, "fetch_url", {"url": "https://www.cartube.co.il/denied"})
    assert not ctx.unreadable_domains


def test_render_page_logs_one_line_per_call(make_ctx, monkeypatch, caplog):
    import logging

    from src.tools import dispatch, render as render_module

    ctx, _, _ = _shell_ctx(make_ctx, monkeypatch, {})
    monkeypatch.setattr(render_module, "_render", lambda url, wait_ms, timeout_s: {
        "status": 200, "final_url": url, "html": "<html><body><p>" + "x " * 400 + "</p></body></html>", "links": []})
    with caplog.at_level(logging.INFO, logger="tripy.render"):
        dispatch(ctx, "render_page", {"url": "https://www.cartube.co.il/q8"})
    lines = [r.getMessage() for r in caplog.records if r.name == "tripy.render"]
    assert len(lines) == 1 and "url=https://www.cartube.co.il/q8 status=200" in lines[0]
    assert "text_chars=" in lines[0] and " ms=" in lines[0] and "outcome=ok" in lines[0]


def _chromium_launches() -> bool:
    from src.tools.render import boot_check

    return boot_check(timeout_s=60).get("launch") == "ok"


@pytest.mark.skipif(not _chromium_launches(), reason="no Chromium on this host (the Docker image and CI install it)")
def test_render_page_runs_javascript_of_a_local_page(make_ctx, monkeypatch, tmp_path):
    """A real headless Chromium renders a local page whose content only JavaScript writes."""
    import http.server
    import socketserver
    import threading

    from src.tools import dispatch, render as render_module

    (tmp_path / "index.html").write_text(
        "<html><head><title>js page</title></head><body><div id='app'></div><script>document.getElementById('app')"
        ".innerHTML = '<p>' + ['rendered', 'by', 'javascript', String(6 * 7)].join(' ') + '</p>';</script></body>"
        "</html>", "utf-8")
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(tmp_path), **k)  # noqa: E731
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
        monkeypatch.setattr(render_module, "_render", render_module._playwright_render)
        ctx = make_ctx({})
        result = dispatch(ctx, "render_page", {"url": url, "wait_ms": 200})
        server.shutdown()
    assert result.get("error") is None, result
    assert "rendered by javascript 42" in ctx.cache.read_text(result["document_id"])


def test_boot_check_reports_version_path_and_launch():
    from src.tools.render import boot_check

    result = boot_check(timeout_s=60)
    assert result["playwright"] and set(result) >= {"launch", "ms"}
    assert result["launch"] == "ok" or result.get("error")
    if result["launch"] == "ok":
        assert result["js_ok"] is True and result["chromium_path"]


# --- P2: the Israeli version-page resolver ----------------------------------------------------------------------------

class _Ctx:
    def __init__(self, cache, payload):
        from collections import Counter

        self.cache, self.counters, self.admission = cache, Counter(), _adm(payload)


class _Web:
    """Fake search / fetch / robots over fixture pages; records every call."""

    def __init__(self, cache, pages: dict, results: dict, disallow=()):
        self.cache, self.pages, self.results, self.disallow = cache, pages, results, set(disallow)
        self.searches, self.fetches = [], []

    def search(self, query, domain):
        self.searches.append((query, domain))
        return {"results": [{"url": u} for u in self.results.get(domain, [])]}

    def fetch(self, url):
        self.fetches.append(url)
        if url not in self.pages:
            return {"error": "HTTPError", "status": 404}
        return {"document_id": put(self.cache, url, self.pages[url], "html"), "status": 200}

    def robots(self, url):
        return url not in self.disallow


class _NoPace:
    def __init__(self):
        self.waits = []

    def wait(self, domain):
        self.waits.append(domain)
        return 0.0


def _q3_pages():
    pages = {P.Q3_MODEL_URL: P.q3_model_html()}
    for url, (version, power) in P.Q3_VERSIONS.items():
        body = "קופה" if "SPORTBACK" in version else "קרוסאובר"
        pages[url] = P.q3_cartube_html(synthetic_row=False, version=version, power=power, body=body)
    return pages


def _resolve(cache, web, payload=None):
    from src.il_version_pages import resolve

    ctx = _Ctx(cache, payload or A.Q3_PAYLOAD)
    pace = _NoPace()
    out = resolve(ctx, payload=payload or A.Q3_PAYLOAD, search=web.search, fetch=web.fetch, robots=web.robots,
                  pace=pace)
    return out, ctx, pace


def test_model_page_links_lead_to_the_matching_version_page(cache):
    hp245 = P.Q3_MODEL_URL + "/328-2-0-45tfsi-4x4-2024"
    web = _Web(cache, _q3_pages(), {"cartube.co.il": [hp245, P.Q3_MODEL_URL]})
    out, ctx, pace = _resolve(cache, web)
    statuses = {p["url"]: (p["status"], p["reason"]) for p in out["pages"]}
    assert statuses[hp245] == ("rejected", "power_mismatch")               # 245 hp is another version
    assert statuses[P.Q3_MODEL_URL] == ("rejected", "several_versions")    # the model page: follow its links
    assert statuses[P.Q3_CARTUBE_URL] == ("accepted", "power_match")
    assert out["found"] == 1 and web.fetches == [hp245, P.Q3_MODEL_URL, P.Q3_CARTUBE_URL]
    assert ctx.counters["acq_il_version_pages_found"] == 1 and ctx.counters["acq_il_version_fetches"] == 3
    assert ctx.counters["acq_il_version_searches"] == out["searches"] == 4      # PR #47: max_searches 4
    assert pace.waits == ["cartube.co.il"] * 3


def test_accepted_version_page_binds_every_value_at_the_technical_variant(cache):
    decision, _, _ = _decide(cache, A.Q3_PAYLOAD, P.Q3_CARTUBE_URL, P.q3_cartube_html(synthetic_row=False),
                             "top_speed_kmh", 222, "מהירות מרבית | 222 קמ״ש")
    record = decision["record"]
    assert record["variant_match"] == "exact" and record["binding_basis"] == "il_version_page", record
    assert record["version_page"]["status"] == "accepted"


def test_a_page_listing_two_versions_is_not_accepted(cache):
    url = P.Q3_MODEL_URL + "/400-two-versions-2024"
    web = _Web(cache, {url: P.q3_two_versions_html()}, {"cartube.co.il": [url]})
    out, _, _ = _resolve(cache, web)
    assert out["pages"][0]["status"] == "rejected" and out["pages"][0]["reason"] == "several_versions"
    assert out["found"] == 0


def test_budget_caps_four_searches_and_six_fetches(cache):
    urls = [f"{P.Q3_MODEL_URL}/{500 + i}-two-versions-2024" for i in range(10)]
    pages = {u: P.q3_two_versions_html() for u in urls}
    web = _Web(cache, pages, {"cartube.co.il": urls, "icar.co.il": [], "auto.co.il": []})
    out, _, _ = _resolve(cache, web)
    # PR #47: max_searches 4 (carzone.co.il is the fourth il_version_sites entry)
    assert len(web.searches) == out["searches"] == 4 and len(web.fetches) == out["fetches"] == 6
    # PR #45 (R1): icar's `site:` search finds nothing, so its one domain-filter retry takes the third search
    assert [(q.startswith("site:"), d) for q, d in web.searches] == [
        (True, "cartube.co.il"), (True, "icar.co.il"), (False, "icar.co.il"), (True, "auto.co.il")]


def test_robots_disallow_is_respected(cache):
    web = _Web(cache, _q3_pages(), {"cartube.co.il": [P.Q3_CARTUBE_URL]}, disallow={P.Q3_CARTUBE_URL})
    out, _, _ = _resolve(cache, web)
    assert web.fetches == [] and out["skipped"] == [{"url": P.Q3_CARTUBE_URL, "reason": "robots_disallow"}]


def test_domain_pacer_keeps_one_request_per_second_per_domain():
    from src.il_version_pages import DomainPacer

    now, slept = [100.0], []
    pacer = DomainPacer(1.0, clock=lambda: now[0], sleep=lambda s: slept.append(s))
    pacer.wait("cartube.co.il")
    pacer.wait("auto.co.il")
    pacer.wait("cartube.co.il")
    assert slept == [1.0]


def test_search_templates_fill_the_target_terms():
    from src.document_binding import target_identity
    from src.il_version_pages import search_queries, target_terms

    terms = target_terms(A.Q3_PAYLOAD, target_identity(A.Q3_PAYLOAD))
    assert terms == {"make_he": "אודי", "make_en": "audi", "model": "Q3", "model_he": "Q3", "model_en": "q3",
                     "body_he": "", "body_en": "", "year": "2024", "engine_l": "2.0", "hp": "190"}
    assert search_queries(terms)[0] == ("cartube.co.il", "site:cartube.co.il אודי Q3 2024 2.0 190")


# --- P1: the government registry layer --------------------------------------------------------------------------------

def _ckan_page(records, fields=None):
    names = fields or ["_id", "mispar_rechev", "tozeret_cd", "degem_cd", "shnat_yitzur", "ramat_gimur", "zmig_kidmi",
                       "zmig_ahori"]
    return {"fields": [{"id": n} for n in names], "records": records, "total": len(records)}


def _vehicles(n, front, rear=None, *, degem=260, year=2025, trim="SLINE SUPER"):
    return [{"tozeret_cd": 21, "degem_cd": degem, "shnat_yitzur": year, "ramat_gimur": trim, "zmig_kidmi": front,
             "zmig_ahori": rear or front} for _ in range(n)]


def test_ckan_page_aggregates_majority_share_and_n():
    import build_gov_registry_index as B

    records = _vehicles(19, "285/45R21") + _vehicles(1, "285/40 R22 110Y") + _vehicles(5, "235/50R19", degem=261)
    counts = B.aggregate(_ckan_page(records)["records"])
    index = B.build_index(counts, source="fixture", resource_id=None, rows=len(records))
    entry = index["entries"]["21|260|2025|SLINE SUPER"]
    assert entry["front"] == {"majority": "285/45 R21", "share": 0.95, "n": 20,
                              "top": [{"size": "285/45 R21", "n": 19}, {"size": "285/40 R22", "n": 1}]}
    assert "21|261|2025|SLINE SUPER" not in index["entries"]                 # 5 vehicles: below 20, dropped
    assert index["complete"] is True and index["generated_at"] and index["source"] == "fixture"


def test_build_fails_closed_without_the_tyre_columns(tmp_path, capsys):
    import json

    import build_gov_registry_index as B

    page = tmp_path / "page.json"
    page.write_text(json.dumps(_ckan_page(_vehicles(30, "285/45R21"),
                                          fields=["tozeret_cd", "degem_cd", "shnat_yitzur", "ramat_gimur"])), "utf-8")
    out = tmp_path / "index.json"
    assert B.main(["--records-json", str(page), "--out", str(out)]) == 2
    assert not out.exists() and "zmig_kidmi" in capsys.readouterr().err


def test_paged_ckan_reads_check_columns_first_and_pause(monkeypatch):
    import build_gov_registry_index as B

    records = _vehicles(25, "285/45R21")
    calls, pauses = [], []

    def get(params):
        calls.append(dict(params))
        if params["limit"] == 1:
            return {**_ckan_page(records[:1]), "total": len(records)}
        start = params["offset"]
        return {"records": records[start:start + params["limit"]], "total": len(records)}

    counts, stats = B.read_ckan("rid", page=10, pause_s=1.0, get=get, sleep=pauses.append)
    assert calls[0] == {"resource_id": "rid", "limit": 1}
    assert [c["offset"] for c in calls[1:]] == [0, 10, 20] and all(c["fields"] == ",".join(B.COLUMNS) for c in calls[1:])
    assert pauses == [1.0, 1.0, 1.0] and stats["rows"] == 25
    assert sum(counts["21|260|2025|SLINE SUPER"]["front"].values()) == 25


def _registry(tmp_path, monkeypatch, entries, complete=True):
    import json

    path = tmp_path / "gov_registry_index.json"
    path.write_text(json.dumps({"complete": complete, "generated_at": "2026-10-04T00:00:00+00:00",
                                "source": "fixture", "entries": entries}), "utf-8")
    from src import gov_registry

    monkeypatch.setattr(gov_registry, "INDEX_PATH", path)


def _entry(front, front_share, n, rear=None, rear_share=None, top=None):
    def axle(size, share):
        return {"majority": size, "share": share, "n": n, "top": top or [{"size": size, "n": int(n * share)}]}
    return {"front": axle(front, front_share), "rear": axle(rear or front, rear_share or front_share)}


def _emit(cache, entries_ctx):
    from collections import Counter

    from src.gov_registry import emit
    from src.tools.evidence import EvidenceStore

    class Ctx:
        def __init__(self):
            self.cache, self.counters, self.evidence = cache, Counter(), EvidenceStore()
            self.admission, self.events, self.documents = _adm(A.Q8_PAYLOAD), [], []

        def emit(self, kind, **data):
            self.events.append((kind, data))

        def note_document(self, doc, cache_hit=None):
            self.documents.append(doc)

    ctx = Ctx()
    return emit(ctx, A.Q8_PAYLOAD), ctx


def test_registry_majority_emits_tyres_and_rim_at_the_market_trim(cache, tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"21|260|2025|SLINE SUPER": _entry("285/45 R21", 0.92, 120)})
    out, ctx = _emit(cache, None)
    items = {e["field"]: e for e in ctx.evidence.items}
    assert set(items) == {"tire_size_front", "tire_size_rear", "rim_diameter_in"} and out["stored"] == 3
    assert items["tire_size_front"]["value"] == "285/45 R21" and items["rim_diameter_in"]["value"] == 21
    for item in items.values():
        assert item["source_authority"] == "government_registry" and item["market"] == "IL"
        assert item["binding_level"] == "exact_market_trim" and item["variant_match"] == "exact"
        assert item["binding_basis"] == "gov_model_code"
    from src.field_recovery import evaluate_fields
    from src.fields import resolve_requested_fields

    specs = resolve_requested_fields(["tire_size_front", "rim_diameter_in"], propulsion="plug_in")
    states = {e["field"]: e["state"] for e in evaluate_fields(specs, [{"kind": k, **d} for k, d in ctx.events], "IL")}
    assert states == {"tire_size_front": "ok", "rim_diameter_in": "ok"}


def test_registry_low_share_gives_alternative_sizes_only(cache, tmp_path, monkeypatch):
    top = [{"size": "285/45 R21", "n": 60}, {"size": "285/40 R22", "n": 40}, {"size": "265/55 R20", "n": 20}]
    _registry(tmp_path, monkeypatch, {"21|260|2025|SLINE SUPER": _entry("285/45 R21", 0.5, 120, top=top)})
    _, ctx = _emit(cache, None)
    assert [(e["field"], e["value"]) for e in ctx.evidence.items] == [("alternative_tire_sizes",
                                                                       "285/45 R21, 285/40 R22")]


@pytest.mark.parametrize("entries, complete", [
    ({"21|260|2025|SLINE SUPER": _entry("285/45 R21", 0.95, 19)}, True),     # N < 20
    ({"21|260|2025|SLINE SUPER": _entry("285/45 R21", 0.95, 120)}, False),   # an index not marked complete
    ({"21|260|2024|SLINE SUPER": _entry("285/45 R21", 0.95, 120)}, True),    # another model year
])
def test_registry_emits_nothing_below_the_thresholds(cache, tmp_path, monkeypatch, entries, complete):
    _registry(tmp_path, monkeypatch, entries, complete)
    out, ctx = _emit(cache, None)
    assert ctx.evidence.items == [] and out["stored"] == 0


def test_registry_rims_that_disagree_give_no_rim(cache, tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"21|260|2025|SLINE SUPER": _entry("255/45 R20", 0.9, 50, "285/40 R21", 0.9)})
    _, ctx = _emit(cache, None)
    assert {e["field"] for e in ctx.evidence.items} == {"tire_size_front", "tire_size_rear"}


def test_registry_evidence_replays_to_the_same_binding(cache, tmp_path, monkeypatch):
    from src.binding_replay import replay_fact

    _registry(tmp_path, monkeypatch, {"21|260|2025|SLINE SUPER": _entry("285/45 R21", 0.92, 120)})
    _, ctx = _emit(cache, None)
    item = next(e for e in ctx.evidence.items if e["field"] == "tire_size_front")
    now = replay_fact(_adm(A.Q8_PAYLOAD), cache, field=item["field"], value=item["value"], quote=item["quote"],
                      document_id=item["document_id"], source_url=item["source_url"])
    assert now["binding_level_now"] == "exact_market_trim" and now["variant_match_now"] == "exact"
    other = dict(A.Q8_PAYLOAD, identity={**A.Q8_PAYLOAD["identity"], "trim": "SUPERIOR"})
    moved = replay_fact(_adm(other), cache, field=item["field"], value=item["value"], quote=item["quote"],
                        document_id=item["document_id"], source_url=item["source_url"])
    assert moved["variant_match_now"] == "different"


def test_shipped_registry_index_is_a_placeholder_or_a_well_formed_index():
    """The shipped file is either the empty fail-closed placeholder or a complete build (main carries the first build
    since the regenerated index was merged): 4-part keys with integer codes, every entry above MIN_KEEP vehicles."""
    import json
    from pathlib import Path

    data = json.loads((Path(__file__).resolve().parent.parent / "data" / "gov_registry_index.json").read_text("utf-8"))
    if data["complete"] is not True:
        assert data["entries"] == {}
        return
    assert data["entries"] and data["rows"] > 0
    for key, entry in data["entries"].items():
        parts = key.split("|")
        assert len(parts) == 4 and all(p.isdigit() for p in parts[:3]), key
        assert max(entry.get(a, {}).get("n", 0) for a in ("front", "rear")) >= 20, key


def test_q8_model_page_gearbox_stays_non_exact(cache):
    """The production replay's other lost source: the model page names three powertrain versions (50 TDI / 55 / 60
    TFSIe) and its year is 2026: the shared 8-speed statement stays unresolved for the 2025 plug-in record."""
    for field, value in (("gearbox_type", "automatic"), ("gear_count", 8)):
        decision, _, _ = _decide(cache, A.Q8_PAYLOAD, P.Q8_MODEL_URL, P.q8_model_html(), field, value,
                                 P.Q8_MODEL_QUOTE)
        assert decision["accepted"], decision
        record = decision["record"]
        assert record["variant_match"] != "exact", (field, record)
        assert "several_powertrain_versions" in record["binding_flags"]
        assert record["version_page"]["reason"] == "several_versions"


def test_diagnostics_report_the_playbook_counters():
    from src.diagnostics import operations_summary

    events = [{"kind": "il_version_pages", "searches": 3, "fetches": 4, "found": 1},
              {"kind": "domain_unreadable", "domain": "audi.co.il", "reason": "http_247_shell"},
              {"kind": "gov_registry", "stored": 3}]
    ops = operations_summary(events)
    assert (ops["il_version_searches"], ops["il_version_fetches"], ops["il_version_pages_found"]) == (3, 4, 1)
    assert ops["unreadable_reasons"] == {"audi.co.il": "http_247_shell"} and ops["gov_registry_evidence"] == 3
