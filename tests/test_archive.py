# -*- coding: utf-8 -*-
"""Manifest, checksums, archive structure, integrity and overall status."""

import hashlib
import io
import json
import re
import zipfile

import capture
from conftest import (
    full_routes, run_capture, zip_names, zip_read, entry_by_id, rav4_records,
    json_response, package_payload, TOYOTA_404_HTML, TOYOTA_RAV4_HYBRID_HTML,
)


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


def test_archive_contains_every_required_path():
    result, _transport = run_capture(full_routes())
    names = zip_names(result)
    for required in (
        "manifest.json",
        "README.txt",
        "SHA256SUMS.txt",
        "raw/government/package_show.json",
        "raw/government/wltp/schema.json",
        "raw/government/wltp/pages/page_000001.json",
        "raw/government/additional/schema.json",
        "raw/government/additional/pages/page_000001.json",
        "raw/toyota/archive-index.html",
        "raw/toyota/rav4-phev.html",
        "derived/government/wltp_all_records.json",
        "derived/government/additional_all_records.json",
    ):
        assert required in names, required


def test_archive_root_is_the_capture_id():
    result, _transport = run_capture(full_routes())
    assert re.match(r"^milo-r5-source-capture-\d{8}T\d{6}Z$", result["capture_id"])
    with zipfile.ZipFile(io.BytesIO(result["archive_bytes"])) as archive:
        for name in archive.namelist():
            assert name.startswith(result["capture_id"] + "/"), name


def test_archive_paths_are_deterministic():
    first, _t1 = run_capture(full_routes())
    second, _t2 = run_capture(full_routes())
    assert zip_names(first) == zip_names(second)


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


def test_manifest_records_every_required_field():
    result, _transport = run_capture(full_routes())
    manifest = json.loads(zip_read(result, "manifest.json").decode("utf-8"))
    assert manifest["api_key_used"] is False
    assert manifest["credentials_used"] is False
    assert manifest["cookies_supplied"] is False
    assert manifest["method"] == "GET"

    required = {
        "source_id", "source_type", "requested_url", "query_params", "started_utc",
        "finished_utc", "http_status", "redirect_chain", "final_url", "headers",
        "content_type", "declared_content_length", "byte_count", "sha256",
        "attempts", "retries", "validation_result", "error", "raw_path",
        "api_key_used", "credentials_used", "cookies_supplied",
    }
    for entry in manifest["entries"]:
        assert required.issubset(set(entry)), entry["source_id"]
        assert entry["api_key_used"] is False
        assert entry["credentials_used"] is False
        assert entry["cookies_supplied"] is False


def test_manifest_records_pagination_fields_for_government_pages():
    result, _transport = run_capture(full_routes(wltp_records=rav4_records(120, first_id=1)))
    manifest = json.loads(zip_read(result, "manifest.json").decode("utf-8"))
    pages = [e for e in manifest["entries"] if e["source_type"] == capture.SOURCE_TYPE_PAGE]
    assert pages
    for entry in pages:
        for field in ("resource_id", "query_token", "requested_offset", "requested_limit",
                      "returned_record_count", "reported_total", "record_ids"):
            assert field in entry, field
    wltp = [e for e in pages if e["resource_id"] == capture.WLTP_RESOURCE_ID]
    assert [e["requested_offset"] for e in wltp] == [0, 100]
    assert wltp[0]["record_ids"] == list(range(1, 101))


def test_manifest_query_params_are_parsed_and_public():
    result, _transport = run_capture(full_routes())
    entry = entry_by_id(result, "government_package_show")
    assert entry["query_params"] == {"id": capture.CKAN_PACKAGE_ID}


# --------------------------------------------------------------------------
# Hashes and checksums
# --------------------------------------------------------------------------


def test_manifest_hashes_match_the_stored_raw_files():
    result, _transport = run_capture(full_routes())
    manifest = json.loads(zip_read(result, "manifest.json").decode("utf-8"))
    for entry in manifest["entries"]:
        body = zip_read(result, entry["raw_path"])
        assert len(body) == entry["byte_count"], entry["source_id"]
        assert hashlib.sha256(body).hexdigest() == entry["sha256"], entry["source_id"]


def test_sha256sums_covers_every_file_except_itself_and_verifies():
    result, _transport = run_capture(full_routes())
    sums = zip_read(result, "SHA256SUMS.txt").decode("utf-8")
    listed = []
    for line in sums.splitlines():
        digest, _, name = line.partition("  ")
        listed.append(name)
        assert hashlib.sha256(zip_read(result, name)).hexdigest() == digest, name
    assert "manifest.json" in listed
    assert "README.txt" in listed
    assert "SHA256SUMS.txt" not in listed
    for name in zip_names(result):
        if name != "SHA256SUMS.txt":
            assert name in listed, name


def test_checksum_ordering_is_deterministic_and_sorted():
    result, _transport = run_capture(full_routes())
    sums = zip_read(result, "SHA256SUMS.txt").decode("utf-8")
    names = [line.partition("  ")[2] for line in sums.splitlines()]
    assert names == sorted(names)


def test_manifest_sha256_is_reported_and_correct():
    result, _transport = run_capture(full_routes())
    assert hashlib.sha256(zip_read(result, "manifest.json")).hexdigest() == \
        result["manifest_sha256"]


def test_integrity_mismatch_fails_closed():
    from conftest import FakeTransport, fixed_clock, RecordingSleeper
    transport = FakeTransport(full_routes())
    raw = capture.run_capture(
        getter=transport, clock=fixed_clock(), sleeper=RecordingSleeper()
    )
    assert capture.determine_status(raw) == capture.STATUS_READY
    raw["files"]["raw/government/wltp/pages/page_000001.json"] += b"tampered"
    result = capture.package_capture(raw)
    assert result["overall_status"] == capture.STATUS_FAILED
    assert result["integrity_verified"] is False
    assert any("sha256 mismatch" in p for p in result["integrity_problems"])
    assert "INCOMPLETE" in result["archive_filename"]


def test_checksum_verification_detects_a_missing_file():
    files = {"a.txt": b"alpha", "b.txt": b"beta"}
    sums = capture.build_checksums(files)
    assert capture.verify_checksums(sums, files) == []
    assert capture.verify_checksums(sums, {"a.txt": b"alpha"}) != []
    assert capture.verify_checksums(sums, {"a.txt": b"CHANGED", "b.txt": b"beta"}) != []


# --------------------------------------------------------------------------
# Overall status and filenames
# --------------------------------------------------------------------------


def test_complete_bundle_is_ready_and_uses_the_plain_filename():
    result, _transport = run_capture(full_routes())
    assert result["overall_status"] == capture.STATUS_READY
    assert result["integrity_verified"] is True
    assert re.match(
        r"^milo-r5-source-capture-\d{8}T\d{6}Z\.zip$", result["archive_filename"]
    ), result["archive_filename"]
    assert "INCOMPLETE" not in result["archive_filename"]


def test_no_matching_records_is_incomplete():
    result, _transport = run_capture(full_routes(wltp_records=[], additional_records=[]))
    assert result["overall_status"] == capture.STATUS_NO_RECORDS
    assert result["archive_filename"].startswith("milo-r5-source-capture-INCOMPLETE-")


def test_government_failure_is_capture_failed():
    routes = full_routes()
    routes[capture.package_show_url()] = json_response({"success": False})
    result, _transport = run_capture(routes)
    assert result["overall_status"] == capture.STATUS_FAILED
    assert "INCOMPLETE" in result["archive_filename"]


def test_network_failure_still_produces_a_diagnostic_bundle():
    routes = full_routes()
    routes[capture.package_show_url()] = [IOError("boom")] * 3
    result, _transport = run_capture(routes)
    assert result["overall_status"] == capture.STATUS_FAILED
    assert "INCOMPLETE" in result["archive_filename"]
    assert len(result["archive_bytes"]) > 0
    names = zip_names(result)
    assert "manifest.json" in names
    assert "README.txt" in names
    assert "SHA256SUMS.txt" in names
    assert entry_by_id(result, "government_package_show")["validation_result"] == "failed_request"


def test_incomplete_bundle_is_never_labelled_ready():
    for routes in (
        full_routes(wltp_records=[], additional_records=[]),
        full_routes(model_html=TOYOTA_RAV4_HYBRID_HTML),
        full_routes(model_html=TOYOTA_404_HTML, model_status=404),
    ):
        result, _transport = run_capture(routes)
        assert result["overall_status"] != capture.STATUS_READY
        assert "INCOMPLETE" in result["archive_filename"]
        manifest = json.loads(zip_read(result, "manifest.json").decode("utf-8"))
        assert manifest["overall_status"] == result["overall_status"]
        assert manifest["overall_status"] != capture.STATUS_READY


def test_archive_filename_helper():
    capture_id = "milo-r5-source-capture-20260914T120000Z"
    assert capture.archive_filename(capture_id, capture.STATUS_READY) == \
        "milo-r5-source-capture-20260914T120000Z.zip"
    for status in (capture.STATUS_NO_RECORDS, capture.STATUS_INCOMPLETE_WEB,
                   capture.STATUS_FAILED):
        assert capture.archive_filename(capture_id, status) == \
            "milo-r5-source-capture-INCOMPLETE-20260914T120000Z.zip"


def test_readme_explains_raw_versus_derived_and_the_toyota_caveat():
    result, _transport = run_capture(full_routes())
    readme = zip_read(result, "README.txt").decode("utf-8")
    assert "raw/" in readme and "derived/" in readme
    assert capture.TOYOTA_PREVIOUS_OBSOLETE_URL in readme
    assert "NOT a" in readme
    assert capture.RESULT_ARCHIVED_MODEL in readme
