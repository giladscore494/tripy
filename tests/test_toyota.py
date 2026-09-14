# -*- coding: utf-8 -*-
"""Official Toyota Israel archive index and archived RAV4 Plug-in model page."""

import capture
from conftest import (
    FakeTransport, RecordingSleeper, fixed_clock, html_response, full_routes,
    run_capture, entry_by_id, zip_read,
    TOYOTA_ARCHIVE_INDEX_HTML, TOYOTA_RAV4_PHEV_HTML, TOYOTA_RAV4_HYBRID_HTML,
    TOYOTA_RAV4_PHEV_NO_ARCHIVE_HTML, TOYOTA_NO_RAV4_HTML, TOYOTA_BLOCKED_HTML,
    TOYOTA_CAPTCHA_HTML, TOYOTA_404_HTML,
)


def web_fetch(response, url=None):
    url = url or capture.TOYOTA_RAV4_PHEV_URL
    return capture.perform_get(
        url, capture.ACCEPT_HTML, getter=FakeTransport({url: response}),
        clock=fixed_clock(), sleeper=RecordingSleeper(),
    )


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------


def test_exact_new_toyota_urls():
    assert capture.TOYOTA_ARCHIVE_INDEX_URL == "https://www.toyota.co.il/cars"
    assert capture.TOYOTA_RAV4_PHEV_URL == "https://www.toyota.co.il/cars/RAV4-PHEV"


def test_obsolete_url_is_recorded_but_never_requested():
    assert capture.TOYOTA_PREVIOUS_OBSOLETE_URL == "https://www.toyota.co.il/models/rav4-plugin"
    _result, transport = run_capture(full_routes())
    assert capture.TOYOTA_PREVIOUS_OBSOLETE_URL not in transport.urls
    for _sid, _stype, url in capture.build_plan_preview():
        assert url != capture.TOYOTA_PREVIOUS_OBSOLETE_URL


def test_obsolete_url_is_retained_in_provenance():
    result, _transport = run_capture(full_routes())
    entry = entry_by_id(result, "toyota_rav4_phev")
    assert entry["previous_obsolete_url"] == capture.TOYOTA_PREVIOUS_OBSOLETE_URL
    manifest = zip_read(result, "manifest.json").decode("utf-8")
    assert "previous_obsolete_url" in manifest
    assert capture.TOYOTA_PREVIOUS_OBSOLETE_URL in manifest


# --------------------------------------------------------------------------
# Archive index
# --------------------------------------------------------------------------


def test_archive_index_passes():
    outcome = capture.validate_toyota_archive_index(
        web_fetch(html_response(TOYOTA_ARCHIVE_INDEX_HTML), capture.TOYOTA_ARCHIVE_INDEX_URL)
    )
    assert outcome["result"] == capture.RESULT_ARCHIVE_INDEX
    assert outcome["mentions_rav4"] is True


def test_archive_index_404_fails():
    outcome = capture.validate_toyota_archive_index(
        web_fetch(
            html_response(TOYOTA_404_HTML, status=404), capture.TOYOTA_ARCHIVE_INDEX_URL
        )
    )
    assert outcome["result"] == "failed_http_status"


# --------------------------------------------------------------------------
# Archived model page
# --------------------------------------------------------------------------


def test_archived_rav4_phev_page_passes_as_identity_evidence():
    outcome = capture.validate_toyota_archived_model(
        web_fetch(html_response(TOYOTA_RAV4_PHEV_HTML))
    )
    assert outcome["result"] == capture.RESULT_ARCHIVED_MODEL
    assert outcome["result"] == "passed_official_archived_model_identity_requires_human_fact_check"
    assert outcome["plugin_markers"]
    assert outcome["archive_markers"]


def test_rejected_when_rav4_identity_is_absent():
    outcome = capture.validate_toyota_archived_model(
        web_fetch(html_response(TOYOTA_NO_RAV4_HTML))
    )
    assert outcome["result"] == "failed_missing_model_identifier"


def test_generic_rav4_hybrid_page_is_rejected():
    """A RAV4 page with no Plug-in/PHEV identity is not the archived PHEV page."""
    outcome = capture.validate_toyota_archived_model(
        web_fetch(html_response(TOYOTA_RAV4_HYBRID_HTML))
    )
    assert outcome["result"] == "failed_missing_plugin_identifier"


def test_rejected_when_archive_indication_is_absent():
    outcome = capture.validate_toyota_archived_model(
        web_fetch(html_response(TOYOTA_RAV4_PHEV_NO_ARCHIVE_HTML))
    )
    assert outcome["result"] == "failed_missing_archive_indication"


def test_404_page_is_rejected():
    outcome = capture.validate_toyota_archived_model(
        web_fetch(html_response(TOYOTA_404_HTML, status=404))
    )
    assert outcome["result"] == "failed_http_status"


def test_access_denied_page_is_rejected():
    outcome = capture.validate_toyota_archived_model(
        web_fetch(html_response(TOYOTA_BLOCKED_HTML))
    )
    assert outcome["result"] == "failed_blocking_page"


def test_captcha_and_waf_page_is_rejected():
    outcome = capture.validate_toyota_archived_model(
        web_fetch(html_response(TOYOTA_CAPTCHA_HTML))
    )
    assert outcome["result"] == "failed_blocking_page"


def test_non_html_content_type_is_rejected():
    from conftest import FakeResponse
    response = FakeResponse(
        200, {"Content-Type": "application/json"}, TOYOTA_RAV4_PHEV_HTML.encode("utf-8")
    )
    assert capture.validate_toyota_archived_model(web_fetch(response))["result"] == \
        "failed_content_type"


def test_tiny_body_is_rejected():
    outcome = capture.validate_toyota_archived_model(web_fetch(html_response("<html>hi</html>")))
    assert outcome["result"] == "failed_empty_body"


def test_plugin_identity_accepts_each_documented_marker():
    for marker in ("plug-in", "plugin", "phev", "פלאג"):
        body = (
            "<!DOCTYPE html><html><body><h1>TOYOTA RAV4 {0}</h1>"
            "<p>הסתיים השיווק</p>"
            .format(marker)
            + "<p>filler content for plausible html size.</p>" * 20
            + "</body></html>"
        )
        outcome = capture.validate_toyota_archived_model(web_fetch(html_response(body)))
        assert outcome["result"] == capture.RESULT_ARCHIVED_MODEL, marker


def test_no_fact_is_inferred_from_url_or_filename():
    """The URL says RAV4-PHEV, but a body lacking the markers must still fail."""
    body = (
        "<!DOCTYPE html><html><body><h1>TOYOTA</h1><p>Welcome.</p>"
        + "<p>filler content for plausible html size.</p>" * 20
        + "</body></html>"
    )
    fetch = web_fetch(html_response(body), capture.TOYOTA_RAV4_PHEV_URL)
    outcome = capture.validate_toyota_archived_model(fetch)
    assert outcome["result"] == "failed_missing_model_identifier"


def test_final_host_outside_toyota_is_rejected():
    fetch = web_fetch(html_response(TOYOTA_RAV4_PHEV_HTML))
    fetch["final_url"] = "https://data.gov.il/cars/RAV4-PHEV"
    assert capture.validate_toyota_archived_model(fetch)["result"] == "failed_final_host"


# --------------------------------------------------------------------------
# Raw preservation and end-to-end placement
# --------------------------------------------------------------------------


def test_raw_html_is_preserved_at_the_required_paths():
    result, _transport = run_capture(full_routes())
    assert zip_read(result, "raw/toyota/archive-index.html") == \
        TOYOTA_ARCHIVE_INDEX_HTML.encode("utf-8")
    assert zip_read(result, "raw/toyota/rav4-phev.html") == \
        TOYOTA_RAV4_PHEV_HTML.encode("utf-8")


def test_toyota_404_yields_incomplete_official_web_source_not_capture_failed():
    result, _transport = run_capture(
        full_routes(model_html=TOYOTA_404_HTML, model_status=404)
    )
    assert result["overall_status"] == capture.STATUS_INCOMPLETE_WEB
    assert "INCOMPLETE" in result["archive_filename"]
    assert result["government_failures"] == []


def test_either_toyota_page_failing_marks_the_web_source_incomplete():
    for kwargs in (
        {"index_html": TOYOTA_404_HTML, "index_status": 404},
        {"model_html": TOYOTA_RAV4_HYBRID_HTML},
    ):
        result, _transport = run_capture(full_routes(**kwargs))
        assert result["overall_status"] == capture.STATUS_INCOMPLETE_WEB, kwargs
        assert result["web_failures"], kwargs
