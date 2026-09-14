# -*- coding: utf-8 -*-
"""Official Toyota Israel archive index and archived RAV4 Plug-in model page.

The central regression guarded here: a third-party CAPTCHA script reference is
not evidence that a page is a CAPTCHA challenge.
"""

import capture
from conftest import (
    FakeResponse, FakeTransport, RecordingSleeper, fixed_clock, html_response,
    full_routes, run_capture, entry_by_id, zip_read,
    TOYOTA_ARCHIVE_INDEX_HTML, TOYOTA_ARCHIVE_INDEX_WITH_CAPTCHA_HTML,
    TOYOTA_ARCHIVE_INDEX_NO_PLUGIN_HTML, TOYOTA_RAV4_PHEV_HTML,
    TOYOTA_RAV4_PHEV_WITH_RECAPTCHA_HTML, TOYOTA_RAV4_HYBRID_HTML,
    TOYOTA_RAV4_PHEV_NO_ARCHIVE_HTML, TOYOTA_NO_RAV4_HTML, TOYOTA_BLOCKED_HTML,
    TOYOTA_VISIBLE_CAPTCHA_HTML, TOYOTA_404_HTML, TOYOTA_404_HTTP200_HTML,
)

ENDED_MARKETING_HE = (
    "שיווק הדגם ראב4 "
    "פלאג-אין הסתיים"
)


def web_fetch(response, url=None):
    url = url or capture.TOYOTA_RAV4_PHEV_URL
    return capture.perform_get(
        url, capture.ACCEPT_HTML, getter=FakeTransport({url: response}),
        clock=fixed_clock(), sleeper=RecordingSleeper(),
    )


def index_fetch(html, status=200):
    return web_fetch(html_response(html, status=status), capture.TOYOTA_ARCHIVE_INDEX_URL)


def model_fetch(html, status=200):
    return web_fetch(html_response(html, status=status), capture.TOYOTA_RAV4_PHEV_URL)


# --------------------------------------------------------------------------
# Visible-text extraction
# --------------------------------------------------------------------------


def test_script_and_style_text_is_not_visible():
    body = (
        "<html><head><title>Hello</title>"
        "<style>.g-recaptcha{display:none}</style>"
        "<script>var x='captcha';</script></head>"
        "<body><p>Real content</p>"
        "<template><p>templated captcha</p></template>"
        "<svg><text>svg captcha</text></svg>"
        "<noscript>noscript captcha</noscript></body></html>"
    ).encode("utf-8")
    visible, title = capture.extract_visible_text(body)
    assert "real content" in visible
    assert title == "hello"
    assert "captcha" not in visible


def test_attribute_values_are_never_visible_text():
    """class="g-recaptcha" and src=".../recaptcha/api.js" must not be scanned."""
    body = (
        '<html><body><div class="g-recaptcha" data-sitekey="6Lc"></div>'
        '<script src="https://www.google.com/recaptcha/api.js"></script>'
        '<p>Toyota</p></body></html>'
    ).encode("utf-8")
    visible, _title = capture.extract_visible_text(body)
    assert "captcha" not in visible
    assert "toyota" in visible


def test_whitespace_is_normalized_and_entities_decoded():
    body = b"<html><body><p>a  \n\t b</p><p>&amp;&nbsp;c</p></body></html>"
    visible, _title = capture.extract_visible_text(body)
    assert "a b" in visible
    assert "  " not in visible
    assert "&amp;" not in visible


def test_malformed_html_does_not_raise():
    for body in (b"<html><body><p>unclosed", b"<<>><html", b"<html><script>x"):
        visible, title = capture.extract_visible_text(body)
        assert isinstance(visible, str) and isinstance(title, str)


# --------------------------------------------------------------------------
# The false positive this branch fixes
# --------------------------------------------------------------------------


def test_legitimate_archive_html_with_captcha_in_script_still_passes():
    assert "captcha" in TOYOTA_ARCHIVE_INDEX_WITH_CAPTCHA_HTML.lower()
    outcome = capture.validate_toyota_archive_index(
        index_fetch(TOYOTA_ARCHIVE_INDEX_WITH_CAPTCHA_HTML)
    )
    assert outcome["result"] == capture.RESULT_ARCHIVE_INDEX


def test_legitimate_model_html_with_recaptcha_script_still_passes():
    assert "recaptcha" in TOYOTA_RAV4_PHEV_WITH_RECAPTCHA_HTML.lower()
    outcome = capture.validate_toyota_archived_model(
        model_fetch(TOYOTA_RAV4_PHEV_WITH_RECAPTCHA_HTML)
    )
    assert outcome["result"] == capture.RESULT_ARCHIVED_MODEL


def test_generic_error_strings_inside_javascript_do_not_fail_a_valid_page():
    raw = TOYOTA_RAV4_PHEV_WITH_RECAPTCHA_HTML.lower()
    for phrase in ("page not found", "404 not found", "500 internal server error",
                   "service unavailable", "please sign in", "access denied"):
        assert phrase in raw, phrase
    outcome = capture.validate_toyota_archived_model(
        model_fetch(TOYOTA_RAV4_PHEV_WITH_RECAPTCHA_HTML)
    )
    assert outcome["result"] == capture.RESULT_ARCHIVED_MODEL


def test_whole_capture_is_ready_despite_site_wide_captcha_widgets():
    result, _transport = run_capture(
        full_routes(
            index_html=TOYOTA_ARCHIVE_INDEX_WITH_CAPTCHA_HTML,
            model_html=TOYOTA_RAV4_PHEV_WITH_RECAPTCHA_HTML,
        )
    )
    assert result["overall_status"] == capture.STATUS_READY
    assert result["web_failures"] == []


# --------------------------------------------------------------------------
# Real blocking pages must still fail
# --------------------------------------------------------------------------


def test_visible_captcha_challenge_fails():
    outcome = capture.validate_toyota_archived_model(
        model_fetch(TOYOTA_VISIBLE_CAPTCHA_HTML)
    )
    assert outcome["result"] == "failed_blocking_page"


def test_visible_access_denied_page_fails():
    outcome = capture.validate_toyota_archived_model(model_fetch(TOYOTA_BLOCKED_HTML))
    assert outcome["result"] == "failed_blocking_page"


def test_visible_404_page_with_http_200_fails():
    """A soft 404: HTTP 200, but the visible page is an error page."""
    fetch = model_fetch(TOYOTA_404_HTTP200_HTML)
    assert fetch["http_status"] == 200
    outcome = capture.validate_toyota_archived_model(fetch)
    assert outcome["result"] == "failed_blocking_page"


def test_404_page_served_with_404_fails_on_status_first():
    outcome = capture.validate_toyota_archived_model(model_fetch(TOYOTA_404_HTML, status=404))
    assert outcome["result"] == "failed_http_status"


def test_strong_block_wording_fails_even_on_a_content_rich_page():
    body = (
        "<html><head><title>TOYOTA</title></head><body>"
        "<h1>Request Blocked</h1><p>TOYOTA RAV4 Plug-in PHEV</p>"
        + "<p>lots of additional visible content on this page.</p>" * 60
        + "</body></html>"
    )
    outcome = capture.validate_toyota_archived_model(model_fetch(body))
    assert outcome["result"] == "failed_blocking_page"


def test_http_status_must_be_exactly_200():
    for status in (201, 202, 204, 301, 403, 404, 500):
        outcome = capture.validate_toyota_archived_model(
            model_fetch(TOYOTA_RAV4_PHEV_HTML, status=status)
        )
        assert outcome["result"] != capture.RESULT_ARCHIVED_MODEL, status


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
    assert capture.TOYOTA_PREVIOUS_OBSOLETE_URL in manifest


# --------------------------------------------------------------------------
# Archive index positive validation
# --------------------------------------------------------------------------


def test_archive_index_passes_with_full_identity():
    outcome = capture.validate_toyota_archive_index(index_fetch(TOYOTA_ARCHIVE_INDEX_HTML))
    assert outcome["result"] == capture.RESULT_ARCHIVE_INDEX
    assert outcome["archive_sections"]
    assert outcome["plugin_markers"]


def test_archive_index_without_rav4_plugin_identity_fails():
    outcome = capture.validate_toyota_archive_index(
        index_fetch(TOYOTA_ARCHIVE_INDEX_NO_PLUGIN_HTML)
    )
    assert outcome["result"] == "failed_missing_plugin_identifier"


def test_archive_index_without_an_archive_section_fails():
    body = (
        "<html><head><title>TOYOTA</title></head><body>"
        "<h1>TOYOTA</h1><ul><li>RAV4 Plug-in PHEV</li></ul>"
        + "<p>ordinary marketing content.</p>" * 60
        + "</body></html>"
    )
    outcome = capture.validate_toyota_archive_index(index_fetch(body))
    assert outcome["result"] == "failed_missing_archive_section"


def test_archive_index_404_fails():
    outcome = capture.validate_toyota_archive_index(index_fetch(TOYOTA_404_HTML, status=404))
    assert outcome["result"] == "failed_http_status"


# --------------------------------------------------------------------------
# Archived model positive validation
# --------------------------------------------------------------------------


def test_archived_rav4_phev_page_passes_as_identity_evidence():
    outcome = capture.validate_toyota_archived_model(model_fetch(TOYOTA_RAV4_PHEV_HTML))
    assert outcome["result"] == capture.RESULT_ARCHIVED_MODEL
    assert outcome["result"] == "passed_official_archived_model_identity_requires_human_fact_check"
    assert outcome["plugin_markers"]


def test_real_world_ended_marketing_wording_is_recognized():
    """'שיווק הדגם ראב4 פלאג-אין הסתיים' has the model name between the words."""
    assert capture.has_ended_marketing_statement(ENDED_MARKETING_HE)
    body = (
        "<html><head><title>TOYOTA RAV4</title></head><body>"
        "<h1>TOYOTA RAV4 Plug-in</h1><p>" + ENDED_MARKETING_HE + "</p>"
        + "<p>content.</p>" * 60 + "</body></html>"
    )
    outcome = capture.validate_toyota_archived_model(model_fetch(body))
    assert outcome["result"] == capture.RESULT_ARCHIVED_MODEL


def test_ended_marketing_pair_requires_proximity():
    far_apart = (
        "שיווק " + ("x" * 400) + " הסתיים"
    )
    assert not capture.has_ended_marketing_statement(far_apart)


def test_rejected_when_rav4_identity_is_absent():
    outcome = capture.validate_toyota_archived_model(model_fetch(TOYOTA_NO_RAV4_HTML))
    assert outcome["result"] == "failed_missing_model_identifier"


def test_generic_rav4_hybrid_page_is_rejected():
    outcome = capture.validate_toyota_archived_model(model_fetch(TOYOTA_RAV4_HYBRID_HTML))
    assert outcome["result"] == "failed_missing_plugin_identifier"


def test_model_page_without_ended_marketing_wording_fails():
    outcome = capture.validate_toyota_archived_model(
        model_fetch(TOYOTA_RAV4_PHEV_NO_ARCHIVE_HTML)
    )
    assert outcome["result"] == "failed_missing_archive_indication"


def test_non_html_content_type_is_rejected():
    response = FakeResponse(
        200, {"Content-Type": "application/json"}, TOYOTA_RAV4_PHEV_HTML.encode("utf-8")
    )
    assert capture.validate_toyota_archived_model(web_fetch(response))["result"] == \
        "failed_content_type"


def test_tiny_body_is_rejected():
    outcome = capture.validate_toyota_archived_model(model_fetch("<html>hi</html>"))
    assert outcome["result"] == "failed_empty_body"


def test_plugin_identity_accepts_each_documented_marker():
    for marker in ("plug-in", "plugin", "phev", "פלאג"):
        body = (
            "<html><head><title>TOYOTA RAV4</title></head><body>"
            "<h1>TOYOTA RAV4 {0}</h1><p>{1}</p>".format(marker, ENDED_MARKETING_HE)
            + "<p>content.</p>" * 60 + "</body></html>"
        )
        outcome = capture.validate_toyota_archived_model(model_fetch(body))
        assert outcome["result"] == capture.RESULT_ARCHIVED_MODEL, marker


def test_no_fact_is_inferred_from_url_or_filename():
    """The URL says RAV4-PHEV, but a body lacking the markers must still fail."""
    body = (
        "<html><head><title>TOYOTA</title></head><body><h1>TOYOTA</h1>"
        + "<p>welcome content.</p>" * 60 + "</body></html>"
    )
    outcome = capture.validate_toyota_archived_model(model_fetch(body))
    assert outcome["result"] == "failed_missing_model_identifier"


def test_final_host_outside_toyota_is_rejected():
    fetch = model_fetch(TOYOTA_RAV4_PHEV_HTML)
    fetch["final_url"] = "https://data.gov.il/cars/RAV4-PHEV"
    assert capture.validate_toyota_archived_model(fetch)["result"] == "failed_final_host"


# --------------------------------------------------------------------------
# Raw preservation
# --------------------------------------------------------------------------


def test_raw_html_is_preserved_at_the_required_paths():
    result, _transport = run_capture(full_routes())
    assert zip_read(result, "raw/toyota/archive-index.html") == \
        TOYOTA_ARCHIVE_INDEX_HTML.encode("utf-8")
    assert zip_read(result, "raw/toyota/rav4-phev.html") == \
        TOYOTA_RAV4_PHEV_HTML.encode("utf-8")


def test_raw_html_is_byte_exact_after_visible_text_validation():
    """Visible-text extraction is validation-only: it must not rewrite evidence."""
    result, _transport = run_capture(
        full_routes(
            index_html=TOYOTA_ARCHIVE_INDEX_WITH_CAPTCHA_HTML,
            model_html=TOYOTA_RAV4_PHEV_WITH_RECAPTCHA_HTML,
        )
    )
    stored_index = zip_read(result, "raw/toyota/archive-index.html")
    stored_model = zip_read(result, "raw/toyota/rav4-phev.html")
    assert stored_index == TOYOTA_ARCHIVE_INDEX_WITH_CAPTCHA_HTML.encode("utf-8")
    assert stored_model == TOYOTA_RAV4_PHEV_WITH_RECAPTCHA_HTML.encode("utf-8")
    # The script content the validator ignored is still present in the evidence.
    assert b"recaptcha/api.js" in stored_index
    assert b"grecaptcha.ready" in stored_model
    assert b"Page Not Found" in stored_model


def test_hash_matches_the_raw_bytes_not_the_visible_text():
    import hashlib
    result, _transport = run_capture(
        full_routes(model_html=TOYOTA_RAV4_PHEV_WITH_RECAPTCHA_HTML)
    )
    entry = entry_by_id(result, "toyota_rav4_phev")
    raw = TOYOTA_RAV4_PHEV_WITH_RECAPTCHA_HTML.encode("utf-8")
    assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert entry["byte_count"] == len(raw)


# --------------------------------------------------------------------------
# End-to-end status
# --------------------------------------------------------------------------


def test_toyota_404_yields_incomplete_official_web_source_not_capture_failed():
    result, _transport = run_capture(full_routes(model_html=TOYOTA_404_HTML, model_status=404))
    assert result["overall_status"] == capture.STATUS_INCOMPLETE_WEB
    assert "INCOMPLETE" in result["archive_filename"]
    assert result["government_failures"] == []


def test_either_toyota_page_failing_marks_the_web_source_incomplete():
    for kwargs in (
        {"index_html": TOYOTA_404_HTML, "index_status": 404},
        {"model_html": TOYOTA_RAV4_HYBRID_HTML},
        {"index_html": TOYOTA_ARCHIVE_INDEX_NO_PLUGIN_HTML},
    ):
        result, _transport = run_capture(full_routes(**kwargs))
        assert result["overall_status"] == capture.STATUS_INCOMPLETE_WEB, kwargs
        assert result["web_failures"], kwargs
