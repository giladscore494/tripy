# -*- coding: utf-8 -*-
"""Government CKAN validation, pagination and record consolidation."""

import json

import capture
from conftest import (
    FakeResponse, FakeTransport, RecordingSleeper, fixed_clock, json_response,
    package_payload, schema_payload, rav4_records, paged_routes, full_routes,
    run_capture, entry_by_id, zip_names, zip_read,
)


def gov_fetch(response, url=None):
    url = url or capture.package_show_url()
    return capture.perform_get(
        url, capture.ACCEPT_JSON, getter=FakeTransport({url: response}),
        clock=fixed_clock(), sleeper=RecordingSleeper(),
    )


# --------------------------------------------------------------------------
# Identifiers and URLs
# --------------------------------------------------------------------------


def test_exact_government_identifiers():
    assert capture.CKAN_API_BASE == "https://data.gov.il/api/3/action"
    assert capture.CKAN_PACKAGE_ID == "degem-rechev-wltp"
    assert capture.WLTP_RESOURCE_ID == "142afde2-6228-49f9-8a29-9b6c3a0cbe40"
    assert capture.ADDITIONAL_RESOURCE_ID == "5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6"


def test_exact_fixed_urls():
    assert capture.package_show_url() == (
        "https://data.gov.il/api/3/action/package_show?id=degem-rechev-wltp"
    )
    assert capture.resource_schema_url(capture.WLTP_RESOURCE_ID) == (
        "https://data.gov.il/api/3/action/datastore_search"
        "?resource_id=142afde2-6228-49f9-8a29-9b6c3a0cbe40&limit=0"
    )
    assert capture.datastore_page_url(capture.WLTP_RESOURCE_ID, "RAV4", 100, 0) == (
        "https://data.gov.il/api/3/action/datastore_search"
        "?resource_id=142afde2-6228-49f9-8a29-9b6c3a0cbe40&limit=100&offset=0&q=RAV4"
    )


def test_alternate_query_tokens_are_exact():
    assert capture.ALTERNATE_QUERY_TOKENS == ("RAV%204", "%D7%A8%D7%90%D7%91")


def test_queries_stay_targeted_and_bounded():
    """No plan URL requests an unbounded or untargeted dataset."""
    for _sid, _stype, url in capture.build_plan_preview():
        if "datastore_search" in url:
            assert "limit=" in url
            if "q=" in url:
                assert "q=RAV4" in url or "q=RAV%204" in url or "q=%D7%A8%D7%90%D7%91" in url


# --------------------------------------------------------------------------
# Single-response validation
# --------------------------------------------------------------------------


def test_package_show_success():
    outcome = capture.validate_package_show(gov_fetch(json_response(package_payload(3))))
    assert outcome["result"] == "passed"


def test_package_show_without_resources_fails():
    outcome = capture.validate_package_show(
        gov_fetch(json_response({"success": True, "result": {"id": "x"}}))
    )
    assert outcome["result"] == "failed_missing_resources"


def test_malformed_json_fails():
    response = FakeResponse(200, {"Content-Type": "application/json"}, b"{not json")
    assert capture.validate_package_show(gov_fetch(response))["result"] == "failed_invalid_json"


def test_success_false_fails():
    outcome = capture.validate_package_show(
        gov_fetch(json_response({"success": False, "error": {"message": "no"}}))
    )
    assert outcome["result"] == "failed_success_flag"


def test_success_truthy_but_not_exactly_true_fails():
    for value in (1, "true", "yes"):
        outcome = capture.validate_package_show(
            gov_fetch(json_response({"success": value, "result": {"resources": [{"id": "a"}]}}))
        )
        assert outcome["result"] == "failed_success_flag", value


def test_missing_result_fails():
    outcome = capture.validate_package_show(gov_fetch(json_response({"success": True})))
    assert outcome["result"] == "failed_missing_result"


def test_non_200_fails_even_within_2xx():
    response = FakeResponse(204, {"Content-Type": "application/json"}, b"{}")
    assert capture.validate_package_show(gov_fetch(response))["result"] == "failed_http_status"


def test_schema_success_and_empty_fields_failure():
    assert capture.validate_resource_schema(
        gov_fetch(json_response(schema_payload(4)))
    )["result"] == "passed"
    assert capture.validate_resource_schema(
        gov_fetch(json_response({"success": True, "result": {"fields": []}}))
    )["result"] == "failed_missing_fields"


def test_page_records_must_be_objects_with_id():
    assert capture.validate_datastore_page(
        gov_fetch(json_response({"success": True, "result": {"records": ["x"], "total": 1}}))
    )["result"] == "failed_record_not_object"
    assert capture.validate_datastore_page(
        gov_fetch(json_response({"success": True, "result": {"records": [{"a": 1}], "total": 1}}))
    )["result"] == "failed_missing_record_id"
    assert capture.validate_datastore_page(
        gov_fetch(json_response({"success": True, "result": {"total": 0}}))
    )["result"] == "failed_missing_records"


def test_empty_query_is_valid_but_empty():
    outcome = capture.validate_datastore_page(
        gov_fetch(json_response({"success": True, "result": {"records": [], "total": 0}}))
    )
    assert outcome["result"] == "passed_no_records"
    assert outcome["records"] == []


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def test_multi_page_query_captures_every_page_and_record():
    records = rav4_records(250, first_id=5000)
    result, transport = run_capture(full_routes(wltp_records=records))
    pages = [e for e in result["entries"]
             if e["source_type"] == capture.SOURCE_TYPE_PAGE
             and e["resource_id"] == capture.WLTP_RESOURCE_ID]
    assert len(pages) == 3
    assert [e["requested_offset"] for e in pages] == [0, 100, 200]
    assert [e["returned_record_count"] for e in pages] == [100, 100, 50]
    assert all(e["reported_total"] == 250 for e in pages)
    derived = result["derived"]["wltp"]
    assert derived["record_count"] == 250
    assert [r["_id"] for r in derived["records"]] == [5000 + i for i in range(250)]


def test_each_page_is_preserved_as_its_own_raw_file():
    records = rav4_records(150, first_id=7000)
    result, _transport = run_capture(full_routes(wltp_records=records))
    names = zip_names(result)
    assert "raw/government/wltp/pages/page_000001.json" in names
    assert "raw/government/wltp/pages/page_000002.json" in names
    assert "raw/government/package_show.json" in names
    assert "raw/government/wltp/schema.json" in names


def test_raw_page_bytes_are_preserved_exactly():
    raw = b'{"success":true,"result":{"records":[{"_id":9,"x":"\\u05e8"}],"total":1}}'
    routes = full_routes(wltp_records=rav4_records(1, first_id=9))
    routes[capture.datastore_page_url(capture.WLTP_RESOURCE_ID, "RAV4", 100, 0)] = (
        json_response(None, raw=raw)
    )
    result, _transport = run_capture(routes)
    assert zip_read(result, "raw/government/wltp/pages/page_000001.json") == raw


def test_offset_that_does_not_advance_is_rejected():
    """A page whose records never move the offset forward must fail closed."""
    url0 = capture.datastore_page_url(capture.WLTP_RESOURCE_ID, "RAV4", 100, 0)
    routes = full_routes(wltp_records=rav4_records(1, first_id=1))
    # total claims 50 but the page returns zero rows: cannot complete safely.
    routes[url0] = json_response({"success": True, "result": {"records": [], "total": 50}})
    result, _transport = run_capture(routes)
    assert result["overall_status"] == capture.STATUS_FAILED
    assert any("pagination_incomplete" in f for f in result["government_failures"])


def test_repeated_identical_page_is_detected():
    records = rav4_records(100, first_id=3000)
    routes = full_routes(wltp_records=records)
    identical = json_response({"success": True, "result": {"records": records, "total": 300}})
    for offset in (0, 100, 200):
        routes[capture.datastore_page_url(capture.WLTP_RESOURCE_ID, "RAV4", 100, offset)] = (
            json_response({"success": True, "result": {"records": records, "total": 300}})
        )
    result, _transport = run_capture(routes)
    assert result["overall_status"] == capture.STATUS_FAILED
    assert any("pagination_repeated_page" in f for f in result["government_failures"])


def test_inconsistent_total_between_pages_is_rejected():
    routes = full_routes(wltp_records=rav4_records(150, first_id=100))
    routes[capture.datastore_page_url(capture.WLTP_RESOURCE_ID, "RAV4", 100, 100)] = (
        json_response({
            "success": True,
            "result": {"records": rav4_records(50, first_id=200), "total": 999},
        })
    )
    result, _transport = run_capture(routes)
    assert result["overall_status"] == capture.STATUS_FAILED
    assert any("pagination_inconsistent_total" in f for f in result["government_failures"])


def test_captured_exceeding_reported_total_is_rejected():
    routes = full_routes(wltp_records=rav4_records(10, first_id=1))
    routes[capture.datastore_page_url(capture.WLTP_RESOURCE_ID, "RAV4", 100, 0)] = (
        json_response({"success": True, "result": {"records": rav4_records(10), "total": 3}})
    )
    result, _transport = run_capture(routes)
    assert result["overall_status"] == capture.STATUS_FAILED
    assert any("pagination_inconsistent_total" in f for f in result["government_failures"])


def test_defensive_page_limit_is_enforced():
    """A server that never finishes must not loop forever."""
    routes = full_routes(wltp_records=rav4_records(1, first_id=1))
    page = rav4_records(100, first_id=1)
    for index in range(capture.MAX_PAGES_PER_QUERY + 2):
        offset = index * 100
        routes[capture.datastore_page_url(capture.WLTP_RESOURCE_ID, "RAV4", 100, offset)] = (
            json_response({
                "success": True,
                "result": {"records": rav4_records(100, first_id=offset + 1), "total": 10 ** 9},
            })
        )
    result, _transport = run_capture(routes)
    assert result["overall_status"] == capture.STATUS_FAILED
    assert any("page_limit_exceeded" in f for f in result["government_failures"])
    pages = [e for e in result["entries"]
             if e["source_type"] == capture.SOURCE_TYPE_PAGE
             and e["resource_id"] == capture.WLTP_RESOURCE_ID]
    assert len(pages) == capture.MAX_PAGES_PER_QUERY


def test_pagination_never_silently_truncates():
    """Reported total larger than what is served must fail, not truncate."""
    routes = full_routes(wltp_records=rav4_records(100, first_id=1))
    routes[capture.datastore_page_url(capture.WLTP_RESOURCE_ID, "RAV4", 100, 0)] = (
        json_response({"success": True, "result": {"records": rav4_records(100), "total": 500}})
    )
    routes[capture.datastore_page_url(capture.WLTP_RESOURCE_ID, "RAV4", 100, 100)] = (
        json_response({"success": True, "result": {"records": [], "total": 500}})
    )
    result, _transport = run_capture(routes)
    assert result["overall_status"] == capture.STATUS_FAILED
    assert any("pagination_incomplete" in f for f in result["government_failures"])


# --------------------------------------------------------------------------
# Alternate queries
# --------------------------------------------------------------------------


def test_zero_records_triggers_both_alternate_queries():
    result, transport = run_capture(
        full_routes(wltp_records=[], additional_records=[])
    )
    for resource_id in (capture.WLTP_RESOURCE_ID, capture.ADDITIONAL_RESOURCE_ID):
        for token in capture.ALTERNATE_QUERY_TOKENS:
            assert capture.datastore_page_url(resource_id, token, 100, 0) in transport.urls
    assert result["overall_status"] == capture.STATUS_NO_RECORDS


def test_alternate_pages_live_in_separate_directories():
    result, _transport = run_capture(full_routes(wltp_records=[], additional_records=[]))
    names = zip_names(result)
    assert "raw/government/wltp/pages/page_000001.json" in names
    assert "raw/government/wltp/alt1/pages/page_000001.json" in names
    assert "raw/government/wltp/alt2/pages/page_000001.json" in names


def test_alternate_query_that_finds_records_is_recorded():
    result, _transport = run_capture(
        full_routes(
            wltp_records=[], additional_records=[],
            wltp_alt_records=rav4_records(2, first_id=42),
            additional_alt_records=[],
        )
    )
    derived = result["derived"]["wltp"]
    assert derived["record_count"] == 2
    tokens = [q["query_token"] for q in derived["queries"]]
    assert tokens == ["RAV4", "RAV%204", "%D7%A8%D7%90%D7%91"]
    assert derived["provenance"][0]["query_token"] == "RAV%204"
    assert result["overall_status"] == capture.STATUS_READY


def test_no_alternates_when_the_primary_query_returns_records():
    _result, transport = run_capture(full_routes())
    for resource_id in (capture.WLTP_RESOURCE_ID, capture.ADDITIONAL_RESOURCE_ID):
        for token in capture.ALTERNATE_QUERY_TOKENS:
            assert capture.datastore_page_url(resource_id, token, 100, 0) not in transport.urls


# --------------------------------------------------------------------------
# Consolidation and _id handling
# --------------------------------------------------------------------------


def test_original_ids_are_preserved_verbatim():
    records = [
        {"_id": 31337, "kinuy_mishari": "RAV4"},
        {"_id": "A-77", "kinuy_mishari": "RAV4 PRIME"},
    ]
    routes = full_routes(wltp_records=records)
    result, _transport = run_capture(routes)
    entry = [e for e in result["entries"]
             if e["source_type"] == capture.SOURCE_TYPE_PAGE
             and e["resource_id"] == capture.WLTP_RESOURCE_ID][0]
    assert entry["record_ids"] == [31337, "A-77"]
    derived = result["derived"]["wltp"]
    assert [r["_id"] for r in derived["records"]] == [31337, "A-77"]
    assert derived["records"][1]["kinuy_mishari"] == "RAV4 PRIME"


def test_duplicate_ids_are_collapsed_only_by_id_with_provenance():
    shared = rav4_records(2, first_id=500)
    result, _transport = run_capture(
        full_routes(
            wltp_records=[], additional_records=[],
            wltp_alt_records=shared, additional_alt_records=[],
        )
    )
    derived = result["derived"]["wltp"]
    # Both alternate queries return the same two records.
    assert derived["record_count"] == 2
    assert derived["duplicate_ids_collapsed"] == 2
    provenance = derived["provenance"][0]
    assert provenance["query_token"] == "RAV%204"
    assert provenance["also_seen_in"][0]["query_token"] == "%D7%A8%D7%90%D7%91"


def test_derived_files_are_deterministic():
    routes = full_routes(wltp_records=rav4_records(5, first_id=11))
    first, _t1 = run_capture(routes)
    second, _t2 = run_capture(full_routes(wltp_records=rav4_records(5, first_id=11)))
    assert zip_read(first, "derived/government/wltp_all_records.json") == \
        zip_read(second, "derived/government/wltp_all_records.json")


def test_derived_file_reports_resource_query_pages_and_total():
    result, _transport = run_capture(full_routes(wltp_records=rav4_records(120, first_id=1)))
    document = json.loads(
        zip_read(result, "derived/government/wltp_all_records.json").decode("utf-8")
    )
    assert document["resource_id"] == capture.WLTP_RESOURCE_ID
    query = document["queries"][0]
    assert query["query_token"] == "RAV4"
    assert query["page_count"] == 2
    assert query["reported_total"] == 120
    assert query["captured_record_count"] == 120
    assert document["record_count"] == 120


def test_both_derived_files_are_produced():
    result, _transport = run_capture(full_routes())
    names = zip_names(result)
    assert "derived/government/wltp_all_records.json" in names
    assert "derived/government/additional_all_records.json" in names
