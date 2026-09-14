"""Mocked tests for the MILO R5 bounded source capture.

No test in this module performs a live network request. Every HTTP interaction
goes through an injected fake transport.
"""

import datetime
import hashlib
import io
import json
import os
import re
import sys
import tokenize
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capture  # noqa: E402


# Patterns that indicate a real credential VALUE, as opposed to prose naming a
# header the app deliberately never sends.
SECRET_VALUE_PATTERNS = (
    rb"bearer\s+[a-z0-9._\-]{8,}",
    rb"authorization\s*[:=]\s*[\"']?[a-z0-9._\-]{8,}",
    rb"api[_-]?key\s*[:=]\s*[\"']?[a-z0-9._\-]{8,}",
    rb"access[_-]?token\s*[:=]\s*[\"']?[a-z0-9._\-]{8,}",
    rb"password\s*[:=]\s*[\"']?\S",
    rb"set-cookie\s*:",
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeResponse(object):
    def __init__(self, status_code, headers=None, body=b""):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.body), max(chunk_size, 1)):
            yield self.body[start:start + chunk_size]

    def close(self):
        self.closed = True


class FakeTransport(object):
    """Routes URL -> FakeResponse, a list of them, an exception, or a callable."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if url not in self.routes:
            raise AssertionError("unexpected URL requested: {0}".format(url))
        handler = self.routes[url]
        if isinstance(handler, list):
            handler = handler.pop(0)
        if isinstance(handler, Exception):
            raise handler
        if callable(handler):
            return handler(url)
        return handler


def json_response(payload, status=200, headers=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json; charset=utf-8"}
    merged.update(headers or {})
    return FakeResponse(status, merged, body)


def html_response(body_text, status=200, headers=None):
    merged = {"Content-Type": "text/html; charset=utf-8"}
    merged.update(headers or {})
    return FakeResponse(status, merged, body_text.encode("utf-8"))


def package_payload(resource_count=2):
    return {
        "success": True,
        "result": {
            "id": capture.CKAN_PACKAGE_ID,
            "resources": [{"id": "r{0}".format(i)} for i in range(resource_count)],
        },
    }


def schema_payload(field_count=3):
    return {
        "success": True,
        "result": {"fields": [{"id": "f{0}".format(i)} for i in range(field_count)]},
    }


def query_payload(records):
    return {"success": True, "result": {"records": records, "total": len(records)}}


def rav4_records(count=2, first_id=1001):
    return [
        {"_id": first_id + i, "tozeret_nm": "TOYOTA", "kinuy_mishari": "RAV4"}
        for i in range(count)
    ]


GOOD_TOYOTA_HTML = (
    "<!DOCTYPE html><html lang=\"he\"><head><title>Toyota RAV4 Plug-in</title></head>"
    "<body><h1>TOYOTA RAV4 Plug-in Hybrid</h1>"
    "<section>מפרט טכני</section>"
    "<p>סוללה 18.1 kWh</p>"
    "<p>WLTP</p>"
    + ("<p>filler content to exceed the minimum plausible html size.</p>" * 20)
    + "</body></html>"
)

BARE_TOYOTA_HTML = (
    "<!DOCTYPE html><html lang=\"he\"><head><title>Toyota RAV4</title></head>"
    "<body><h1>TOYOTA RAV4 Plug-in</h1><p>Book a test drive.</p>"
    + ("<p>marketing copy without any specification numbers.</p>" * 20)
    + "</body></html>"
)

BLOCKED_TOYOTA_HTML = (
    "<!DOCTYPE html><html><head><title>Access Denied</title></head>"
    "<body><h1>Access Denied</h1><p>Toyota RAV4</p>"
    + ("<p>You do not have permission to access this resource.</p>" * 20)
    + "</body></html>"
)


def full_routes(
    primary_records=None,
    alt_records=None,
    toyota_html=GOOD_TOYOTA_HTML,
    toyota_status=200,
):
    """Routes covering every URL the fixed plan can request."""
    if primary_records is None:
        primary_records = rav4_records(2)
    routes = {capture.package_show_url(): json_response(package_payload())}
    for _slug, resource_id in capture.GOVERNMENT_RESOURCES:
        routes[capture.resource_schema_url(resource_id)] = json_response(schema_payload())
        routes[capture.resource_query_url(resource_id, capture.PRIMARY_QUERY_TOKEN)] = (
            json_response(query_payload(list(primary_records)))
        )
        for token in capture.ALTERNATE_QUERY_TOKENS:
            routes[capture.resource_query_url(resource_id, token)] = json_response(
                query_payload(list(alt_records or []))
            )
    routes[capture.TOYOTA_URL] = html_response(toyota_html, status=toyota_status)
    return routes


def fixed_clock():
    moments = iter(
        datetime.datetime(2026, 9, 14, 12, 0, second, tzinfo=datetime.timezone.utc)
        for second in range(0, 60)
    )
    last = {"value": None}

    def clock():
        try:
            last["value"] = next(moments)
        except StopIteration:
            pass
        return last["value"]

    return clock


def run(routes, **kwargs):
    transport = FakeTransport(routes)
    result = capture.run_and_package(getter=transport, clock=fixed_clock(), **kwargs)
    return result, transport


def entry_by_id(result, source_id):
    for entry in result["entries"]:
        if entry["source_id"] == source_id:
            return entry
    raise AssertionError("no entry {0}".format(source_id))


# --------------------------------------------------------------------------
# Constants and URL construction
# --------------------------------------------------------------------------


class ConstantsTests(unittest.TestCase):
    def test_exact_constants(self):
        self.assertEqual(capture.CKAN_API_BASE, "https://data.gov.il/api/3/action")
        self.assertEqual(capture.CKAN_API_VERSION, 3)
        self.assertEqual(capture.CKAN_PACKAGE_ID, "degem-rechev-wltp")
        self.assertEqual(capture.WLTP_RESOURCE_ID, "142afde2-6228-49f9-8a29-9b6c3a0cbe40")
        self.assertEqual(
            capture.ADDITIONAL_RESOURCE_ID, "5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6"
        )
        self.assertEqual(capture.TOYOTA_URL, "https://www.toyota.co.il/models/rav4-plugin")
        self.assertEqual(capture.USER_AGENT, "MILO-R5-streamlit-evidence-capture/1.0")

    def test_exact_fixed_urls(self):
        self.assertEqual(
            capture.package_show_url(),
            "https://data.gov.il/api/3/action/package_show?id=degem-rechev-wltp",
        )
        self.assertEqual(
            capture.resource_schema_url(capture.WLTP_RESOURCE_ID),
            "https://data.gov.il/api/3/action/datastore_search"
            "?resource_id=142afde2-6228-49f9-8a29-9b6c3a0cbe40&limit=0",
        )
        self.assertEqual(
            capture.resource_query_url(capture.WLTP_RESOURCE_ID, capture.PRIMARY_QUERY_TOKEN),
            "https://data.gov.il/api/3/action/datastore_search"
            "?resource_id=142afde2-6228-49f9-8a29-9b6c3a0cbe40&limit=100&q=RAV4",
        )
        self.assertEqual(
            capture.resource_schema_url(capture.ADDITIONAL_RESOURCE_ID),
            "https://data.gov.il/api/3/action/datastore_search"
            "?resource_id=5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6&limit=0",
        )
        self.assertEqual(
            capture.resource_query_url(
                capture.ADDITIONAL_RESOURCE_ID, capture.PRIMARY_QUERY_TOKEN
            ),
            "https://data.gov.il/api/3/action/datastore_search"
            "?resource_id=5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6&limit=100&q=RAV4",
        )

    def test_alternate_query_tokens_are_exact(self):
        self.assertEqual(capture.ALTERNATE_QUERY_TOKENS, ("RAV%204", "%D7%A8%D7%90%D7%91"))
        alt = capture.build_alternate_sources("142afde2", capture.WLTP_RESOURCE_ID)
        self.assertEqual(
            alt[0].url,
            "https://data.gov.il/api/3/action/datastore_search"
            "?resource_id=142afde2-6228-49f9-8a29-9b6c3a0cbe40&limit=100&q=RAV%204",
        )
        self.assertEqual(
            alt[1].url,
            "https://data.gov.il/api/3/action/datastore_search"
            "?resource_id=142afde2-6228-49f9-8a29-9b6c3a0cbe40&limit=100&q=%D7%A8%D7%90%D7%91",
        )

    def test_no_full_dataset_download(self):
        """Every datastore_search URL carries an explicit bounded limit."""
        for source in capture.build_base_plan():
            if "datastore_search" in source.url:
                self.assertIn("limit=", source.url)
                limit = int(re.search(r"limit=(\d+)", source.url).group(1))
                self.assertLessEqual(limit, capture.QUERY_LIMIT)

    def test_base_plan_is_the_six_mandatory_sources(self):
        ids = [s.source_id for s in capture.build_base_plan()]
        self.assertEqual(
            ids,
            [
                "package_show_degem_rechev_wltp",
                "resource_142afde2_schema",
                "resource_142afde2_rav4",
                "resource_5e87a7a1_schema",
                "resource_5e87a7a1_rav4",
                "toyota_rav4_plugin",
            ],
        )


# --------------------------------------------------------------------------
# Request policy
# --------------------------------------------------------------------------


class RequestPolicyTests(unittest.TestCase):
    def test_no_authorization_header_and_no_api_key(self):
        _result, transport = run(full_routes())
        self.assertTrue(transport.calls)
        forbidden_header_names = ("authorization", "cookie", "x-api-key", "api-key", "token")
        for call in transport.calls:
            lowered = {k.lower(): v for k, v in call["headers"].items()}
            for name in forbidden_header_names:
                self.assertNotIn(name, lowered)
            self.assertEqual(set(lowered), {"user-agent", "accept", "accept-encoding"})
            self.assertEqual(lowered["user-agent"], capture.USER_AGENT)
            self.assertEqual(lowered["accept-encoding"], "identity")

    def test_no_api_key_in_any_url(self):
        _result, transport = run(full_routes())
        for call in transport.calls:
            lowered = call["url"].lower()
            for token in ("api_key", "apikey", "access_token", "token=", "key=", "secret"):
                self.assertNotIn(token, lowered)

    def test_only_get_is_used(self):
        """The transport is the sole network entrypoint and has no verb parameter."""
        _result, transport = run(full_routes())
        self.assertTrue(transport.calls)
        for call in transport.calls:
            self.assertNotIn("method", call)
        source = open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "capture.py"),
            encoding="utf-8",
        ).read()
        self.assertIn("requests.get(", source)
        for verb in ("requests.post", "requests.put", "requests.patch", "requests.delete"):
            self.assertNotIn(verb, source)

    def test_accept_headers_match_source_type(self):
        _result, transport = run(full_routes())
        by_url = {c["url"]: c["headers"]["Accept"] for c in transport.calls}
        self.assertEqual(by_url[capture.package_show_url()], "application/json")
        self.assertEqual(by_url[capture.TOYOTA_URL], "text/html,application/xhtml+xml")

    def test_timeouts_are_connect_10_read_45(self):
        _result, transport = run(full_routes())
        for call in transport.calls:
            self.assertEqual(call["timeout"], (10, 45))

    def test_serial_execution_in_plan_order(self):
        _result, transport = run(full_routes())
        urls = [c["url"] for c in transport.calls]
        self.assertEqual(urls[0], capture.package_show_url())
        self.assertEqual(urls[-1], capture.TOYOTA_URL)


# --------------------------------------------------------------------------
# Headers, cookies, size and redirects
# --------------------------------------------------------------------------


class TransportSafetyTests(unittest.TestCase):
    def test_set_cookie_and_other_headers_are_removed(self):
        raw = {
            "Date": "Mon, 14 Sep 2026 12:00:00 GMT",
            "Content-Type": "application/json",
            "ETag": "\"abc\"",
            "Set-Cookie": "session=supersecret; Path=/",
            "Authorization": "Bearer leaked",
            "Server": "nginx/1.2.3",
            "X-Powered-By": "internal-host-01",
        }
        kept = capture.sanitize_headers(raw)
        self.assertEqual(
            set(kept), {"Date", "Content-Type", "ETag"}
        )
        self.assertNotIn("Set-Cookie", kept)
        serialized = json.dumps(kept)
        self.assertNotIn("supersecret", serialized)
        self.assertNotIn("nginx", serialized)

    def test_set_cookie_never_reaches_the_manifest(self):
        routes = full_routes()
        routes[capture.package_show_url()] = json_response(
            package_payload(), headers={"Set-Cookie": "tracker=leakme; Path=/"}
        )
        result, _transport = run(routes)
        blob = json.dumps(result["entries"], ensure_ascii=False)
        self.assertNotIn("leakme", blob)
        self.assertNotIn("Set-Cookie", blob)

    def test_cookie_from_a_redirect_is_not_replayed(self):
        target = capture.package_show_url()
        first = FakeResponse(
            302,
            {"Location": target, "Set-Cookie": "hop=1; Path=/"},
            b"",
        )
        transport = FakeTransport(
            {
                "https://data.gov.il/redirect-start": first,
                target: json_response(package_payload()),
            }
        )
        fetch = capture.perform_get(
            "https://data.gov.il/redirect-start",
            capture.ACCEPT_JSON,
            getter=transport,
            clock=fixed_clock(),
        )
        self.assertIsNone(fetch["error"])
        self.assertEqual(len(fetch["redirect_chain"]), 1)
        second_call = transport.calls[1]
        self.assertNotIn("Cookie", second_call["headers"])
        self.assertEqual(
            set(k.lower() for k in second_call["headers"]),
            {"user-agent", "accept", "accept-encoding"},
        )

    def test_relative_redirect_is_resolved_and_followed(self):
        transport = FakeTransport(
            {
                "https://data.gov.il/api/3/action/start": FakeResponse(
                    301, {"Location": "/api/3/action/package_show?id=degem-rechev-wltp"}, b""
                ),
                capture.package_show_url(): json_response(package_payload()),
            }
        )
        fetch = capture.perform_get(
            "https://data.gov.il/api/3/action/start",
            capture.ACCEPT_JSON,
            getter=transport,
            clock=fixed_clock(),
        )
        self.assertIsNone(fetch["error"])
        self.assertEqual(fetch["final_url"], capture.package_show_url())
        self.assertEqual(fetch["redirect_chain"][0]["to"], capture.package_show_url())

    def test_redirect_to_disallowed_host_is_rejected(self):
        transport = FakeTransport(
            {
                capture.package_show_url(): FakeResponse(
                    302, {"Location": "https://evil.example.com/steal"}, b""
                )
            }
        )
        fetch = capture.perform_get(
            capture.package_show_url(), capture.ACCEPT_JSON, getter=transport, clock=fixed_clock()
        )
        self.assertIsNotNone(fetch["error"])
        self.assertIn("redirect_host_not_allowed", fetch["error"])
        self.assertEqual(len(transport.calls), 1, "must not request the disallowed host")

    def test_redirect_to_http_is_rejected(self):
        transport = FakeTransport(
            {
                capture.package_show_url(): FakeResponse(
                    302, {"Location": "http://data.gov.il/api/3/action/package_show"}, b""
                )
            }
        )
        fetch = capture.perform_get(
            capture.package_show_url(), capture.ACCEPT_JSON, getter=transport, clock=fixed_clock()
        )
        self.assertIn("redirect_not_https", fetch["error"])

    def test_allowed_hosts_are_exactly_the_four_public_hosts(self):
        self.assertEqual(
            set(capture.ALLOWED_HOSTS),
            {"data.gov.il", "www.data.gov.il", "toyota.co.il", "www.toyota.co.il"},
        )

    def test_more_than_five_redirects_is_rejected(self):
        routes = {}
        for index in range(8):
            routes["https://data.gov.il/hop{0}".format(index)] = FakeResponse(
                302, {"Location": "https://data.gov.il/hop{0}".format(index + 1)}, b""
            )
        transport = FakeTransport(routes)
        fetch = capture.perform_get(
            "https://data.gov.il/hop0", capture.ACCEPT_JSON, getter=transport, clock=fixed_clock()
        )
        self.assertIn("too_many_redirects", fetch["error"])
        self.assertEqual(len(fetch["redirect_chain"]), capture.MAX_REDIRECTS + 1)

    def test_exactly_five_redirects_is_accepted(self):
        routes = {}
        for index in range(capture.MAX_REDIRECTS):
            routes["https://data.gov.il/hop{0}".format(index)] = FakeResponse(
                302, {"Location": "https://data.gov.il/hop{0}".format(index + 1)}, b""
            )
        routes["https://data.gov.il/hop5"] = json_response(package_payload())
        transport = FakeTransport(routes)
        fetch = capture.perform_get(
            "https://data.gov.il/hop0", capture.ACCEPT_JSON, getter=transport, clock=fixed_clock()
        )
        self.assertIsNone(fetch["error"])
        self.assertEqual(len(fetch["redirect_chain"]), capture.MAX_REDIRECTS)

    def test_redirect_without_location_is_rejected(self):
        transport = FakeTransport({capture.package_show_url(): FakeResponse(302, {}, b"")})
        fetch = capture.perform_get(
            capture.package_show_url(), capture.ACCEPT_JSON, getter=transport, clock=fixed_clock()
        )
        self.assertIn("redirect_missing_location", fetch["error"])

    def test_oversized_streamed_body_is_rejected(self):
        big = b"x" * 2048
        transport = FakeTransport(
            {capture.package_show_url(): FakeResponse(200, {"Content-Type": "application/json"}, big)}
        )
        fetch = capture.perform_get(
            capture.package_show_url(),
            capture.ACCEPT_JSON,
            getter=transport,
            clock=fixed_clock(),
            max_bytes=1024,
        )
        self.assertIn("response_too_large", fetch["error"])
        self.assertEqual(fetch["body"], b"")

    def test_oversized_content_length_is_rejected_before_reading(self):
        transport = FakeTransport(
            {
                capture.package_show_url(): FakeResponse(
                    200,
                    {"Content-Type": "application/json", "Content-Length": str(20 * 1024 * 1024)},
                    b"{}",
                )
            }
        )
        fetch = capture.perform_get(
            capture.package_show_url(), capture.ACCEPT_JSON, getter=transport, clock=fixed_clock()
        )
        self.assertIn("response_too_large", fetch["error"])

    def test_default_size_limit_is_15_mib(self):
        self.assertEqual(capture.MAX_RESPONSE_BYTES, 15 * 1024 * 1024)

    def test_transport_errors_retry_three_times_then_fail_sanitized(self):
        transport = FakeTransport(
            {capture.package_show_url(): [IOError("proxy http://10.0.0.1:8080 refused")] * 3}
        )
        fetch = capture.perform_get(
            capture.package_show_url(), capture.ACCEPT_JSON, getter=transport, clock=fixed_clock()
        )
        self.assertEqual(len(transport.calls), capture.MAX_ATTEMPTS)
        self.assertEqual(fetch["attempts"], capture.MAX_ATTEMPTS)
        self.assertIn("transport_error", fetch["error"])
        self.assertNotIn("10.0.0.1", fetch["error"])
        self.assertNotIn("proxy", fetch["error"].lower())

    def test_http_500_and_429_are_retried_then_a_success_is_used(self):
        for retryable in (500, 503, 429):
            transport = FakeTransport(
                {
                    capture.package_show_url(): [
                        FakeResponse(retryable, {}, b""),
                        json_response(package_payload()),
                    ]
                }
            )
            fetch = capture.perform_get(
                capture.package_show_url(),
                capture.ACCEPT_JSON,
                getter=transport,
                clock=fixed_clock(),
            )
            self.assertIsNone(fetch["error"], retryable)
            self.assertEqual(fetch["attempts"], 2, retryable)

    def test_http_404_is_not_retried(self):
        transport = FakeTransport(
            {capture.package_show_url(): FakeResponse(404, {"Content-Type": "text/html"}, b"nope")}
        )
        fetch = capture.perform_get(
            capture.package_show_url(), capture.ACCEPT_JSON, getter=transport, clock=fixed_clock()
        )
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(fetch["http_status"], 404)


# --------------------------------------------------------------------------
# Government validation
# --------------------------------------------------------------------------


class GovernmentValidationTests(unittest.TestCase):
    def _fetch(self, response, url=None):
        url = url or capture.package_show_url()
        return capture.perform_get(
            url, capture.ACCEPT_JSON, getter=FakeTransport({url: response}), clock=fixed_clock()
        )

    def test_package_show_success(self):
        outcome = capture.validate_government(
            self._fetch(json_response(package_payload(3))), capture.SOURCE_TYPE_PACKAGE
        )
        self.assertEqual(outcome["result"], "passed")

    def test_package_show_without_resources_fails(self):
        payload = {"success": True, "result": {"id": "x"}}
        outcome = capture.validate_government(
            self._fetch(json_response(payload)), capture.SOURCE_TYPE_PACKAGE
        )
        self.assertEqual(outcome["result"], "failed_missing_resources")

    def test_schema_success_and_empty_fields_failure(self):
        ok = capture.validate_government(
            self._fetch(json_response(schema_payload(4))), capture.SOURCE_TYPE_SCHEMA
        )
        self.assertEqual(ok["result"], "passed")
        bad = capture.validate_government(
            self._fetch(json_response({"success": True, "result": {"fields": []}})),
            capture.SOURCE_TYPE_SCHEMA,
        )
        self.assertEqual(bad["result"], "failed_missing_fields")

    def test_query_success_preserves_raw_id_values(self):
        records = [
            {"_id": 4211, "kinuy_mishari": "RAV4"},
            {"_id": "A-77", "kinuy_mishari": "RAV4 PRIME"},
        ]
        outcome = capture.validate_government(
            self._fetch(json_response(query_payload(records))), capture.SOURCE_TYPE_QUERY
        )
        self.assertEqual(outcome["result"], "passed")
        self.assertEqual(outcome["record_count"], 2)
        self.assertEqual(outcome["record_ids"], [4211, "A-77"])

    def test_query_with_zero_records_is_valid_but_empty(self):
        outcome = capture.validate_government(
            self._fetch(json_response(query_payload([]))), capture.SOURCE_TYPE_QUERY
        )
        self.assertEqual(outcome["result"], "passed_no_records")
        self.assertEqual(outcome["record_count"], 0)
        self.assertEqual(outcome["record_ids"], [])

    def test_record_without_id_fails(self):
        outcome = capture.validate_government(
            self._fetch(json_response(query_payload([{"kinuy_mishari": "RAV4"}]))),
            capture.SOURCE_TYPE_QUERY,
        )
        self.assertEqual(outcome["result"], "failed_missing_record_id")

    def test_missing_records_array_fails(self):
        outcome = capture.validate_government(
            self._fetch(json_response({"success": True, "result": {"total": 0}})),
            capture.SOURCE_TYPE_QUERY,
        )
        self.assertEqual(outcome["result"], "failed_missing_records")

    def test_success_false_fails(self):
        outcome = capture.validate_government(
            self._fetch(json_response({"success": False, "error": {"message": "nope"}})),
            capture.SOURCE_TYPE_QUERY,
        )
        self.assertEqual(outcome["result"], "failed_success_flag")

    def test_missing_result_object_fails(self):
        outcome = capture.validate_government(
            self._fetch(json_response({"success": True})), capture.SOURCE_TYPE_QUERY
        )
        self.assertEqual(outcome["result"], "failed_missing_result")

    def test_non_json_content_type_fails(self):
        response = FakeResponse(200, {"Content-Type": "text/html"}, b"{\"success\": true}")
        outcome = capture.validate_government(self._fetch(response), capture.SOURCE_TYPE_QUERY)
        self.assertEqual(outcome["result"], "failed_content_type")

    def test_invalid_json_fails(self):
        response = FakeResponse(200, {"Content-Type": "application/json"}, b"{not json")
        outcome = capture.validate_government(self._fetch(response), capture.SOURCE_TYPE_QUERY)
        self.assertEqual(outcome["result"], "failed_invalid_json")

    def test_empty_body_fails(self):
        response = FakeResponse(200, {"Content-Type": "application/json"}, b"   ")
        outcome = capture.validate_government(self._fetch(response), capture.SOURCE_TYPE_QUERY)
        self.assertEqual(outcome["result"], "failed_empty_body")

    def test_non_2xx_fails(self):
        response = FakeResponse(404, {"Content-Type": "application/json"}, b"{}")
        outcome = capture.validate_government(self._fetch(response), capture.SOURCE_TYPE_QUERY)
        self.assertEqual(outcome["result"], "failed_http_status")


# --------------------------------------------------------------------------
# Toyota validation
# --------------------------------------------------------------------------


class ToyotaValidationTests(unittest.TestCase):
    def _fetch(self, response):
        return capture.perform_get(
            capture.TOYOTA_URL,
            capture.ACCEPT_HTML,
            getter=FakeTransport({capture.TOYOTA_URL: response}),
            clock=fixed_clock(),
        )

    def test_page_with_technical_markers_is_a_human_fact_check_candidate(self):
        outcome = capture.validate_toyota(self._fetch(html_response(GOOD_TOYOTA_HTML)))
        self.assertEqual(outcome["result"], capture.TOYOTA_RESULT_CANDIDATE)
        self.assertEqual(outcome["result"], "passed_candidate_requires_human_fact_check")
        self.assertGreaterEqual(
            len(outcome["technical_markers"]), capture.MIN_TOYOTA_TECHNICAL_MARKERS
        )

    def test_page_without_specifications_is_insufficient(self):
        outcome = capture.validate_toyota(self._fetch(html_response(BARE_TOYOTA_HTML)))
        self.assertEqual(outcome["result"], "insufficient_official_content")
        self.assertEqual(outcome["result"], capture.TOYOTA_RESULT_INSUFFICIENT)

    def test_access_denied_page_is_a_blocking_page(self):
        outcome = capture.validate_toyota(self._fetch(html_response(BLOCKED_TOYOTA_HTML)))
        self.assertEqual(outcome["result"], "failed_blocking_page")

    def test_captcha_page_is_a_blocking_page(self):
        body = (
            "<!DOCTYPE html><html><body><h1>Toyota RAV4</h1>"
            "<p>Please complete the CAPTCHA to continue.</p>"
            + ("<p>padding padding padding padding.</p>" * 20)
            + "</body></html>"
        )
        outcome = capture.validate_toyota(self._fetch(html_response(body)))
        self.assertEqual(outcome["result"], "failed_blocking_page")

    def test_non_html_content_type_fails(self):
        response = FakeResponse(
            200, {"Content-Type": "application/json"}, GOOD_TOYOTA_HTML.encode("utf-8")
        )
        outcome = capture.validate_toyota(self._fetch(response))
        self.assertEqual(outcome["result"], "failed_content_type")

    def test_missing_model_identifier_fails(self):
        body = (
            "<!DOCTYPE html><html><body><h1>Toyota Corolla</h1><p>מפרט</p>"
            + ("<p>padding padding padding padding.</p>" * 20)
            + "</body></html>"
        )
        outcome = capture.validate_toyota(self._fetch(html_response(body)))
        self.assertEqual(outcome["result"], "failed_missing_model_identifier")

    def test_tiny_body_fails(self):
        outcome = capture.validate_toyota(self._fetch(html_response("<html>hi</html>")))
        self.assertEqual(outcome["result"], "failed_empty_body")

    def test_no_fact_is_inferred_from_url_or_filename(self):
        """A page naming the model but carrying no specs must not pass on the URL."""
        outcome = capture.validate_toyota(self._fetch(html_response(BARE_TOYOTA_HTML)))
        self.assertNotEqual(outcome["result"], capture.TOYOTA_RESULT_CANDIDATE)


# --------------------------------------------------------------------------
# Zero-record fallback queries
# --------------------------------------------------------------------------


class AlternateQueryTests(unittest.TestCase):
    def test_zero_records_triggers_both_alternate_queries_per_resource(self):
        result, transport = run(full_routes(primary_records=[], alt_records=[]))
        urls = [c["url"] for c in transport.calls]
        for _slug, resource_id in capture.GOVERNMENT_RESOURCES:
            for token in capture.ALTERNATE_QUERY_TOKENS:
                self.assertIn(capture.resource_query_url(resource_id, token), urls)
        self.assertEqual(result["overall_status"], "no_matching_government_records")

    def test_alternates_get_their_own_body_headers_and_manifest_entry(self):
        result, _transport = run(full_routes(primary_records=[], alt_records=[]))
        ids = [e["source_id"] for e in result["entries"]]
        self.assertIn("resource_142afde2_rav4_alt1", ids)
        self.assertIn("resource_142afde2_rav4_alt2", ids)
        self.assertIn("resource_5e87a7a1_rav4_alt1", ids)
        self.assertIn("resource_5e87a7a1_rav4_alt2", ids)

        names = zip_names(result)
        for slug in ("142afde2", "5e87a7a1"):
            for index in (1, 2):
                base = "government/resource_{0}_rav4_alt{1}".format(slug, index)
                self.assertIn(base + ".json", names)
                self.assertIn(base + ".headers.json", names)

        alt = entry_by_id(result, "resource_142afde2_rav4_alt1")
        self.assertEqual(alt["query_token"], "RAV%204")
        alt2 = entry_by_id(result, "resource_142afde2_rav4_alt2")
        self.assertEqual(alt2["query_token"], "%D7%A8%D7%90%D7%91")

    def test_no_alternates_when_the_primary_query_returns_records(self):
        _result, transport = run(full_routes())
        urls = [c["url"] for c in transport.calls]
        for _slug, resource_id in capture.GOVERNMENT_RESOURCES:
            for token in capture.ALTERNATE_QUERY_TOKENS:
                self.assertNotIn(capture.resource_query_url(resource_id, token), urls)
        self.assertEqual(len(transport.calls), 6)

    def test_alternate_with_records_reaches_ready(self):
        result, _transport = run(
            full_routes(primary_records=[], alt_records=rav4_records(1))
        )
        self.assertEqual(result["overall_status"], "ready_for_r5_bundle_review")


# --------------------------------------------------------------------------
# Preservation, hashes and archive structure
# --------------------------------------------------------------------------


def zip_names(result):
    with zipfile.ZipFile(io.BytesIO(result["archive_bytes"])) as archive:
        root = result["capture_id"] + "/"
        return [n[len(root):] for n in archive.namelist() if n.startswith(root)]


def zip_read(result, name):
    with zipfile.ZipFile(io.BytesIO(result["archive_bytes"])) as archive:
        return archive.read("{0}/{1}".format(result["capture_id"], name))


class ArchiveTests(unittest.TestCase):
    def test_archive_structure_contains_every_required_path(self):
        result, _transport = run(full_routes())
        names = zip_names(result)
        required = [
            "manifest.json",
            "SHA256SUMS.txt",
            "README.txt",
            "government/package_show_degem_rechev_wltp.json",
            "government/package_show_degem_rechev_wltp.headers.json",
            "government/resource_142afde2_schema.json",
            "government/resource_142afde2_schema.headers.json",
            "government/resource_142afde2_rav4.json",
            "government/resource_142afde2_rav4.headers.json",
            "government/resource_5e87a7a1_schema.json",
            "government/resource_5e87a7a1_schema.headers.json",
            "government/resource_5e87a7a1_rav4.json",
            "government/resource_5e87a7a1_rav4.headers.json",
            "web/toyota_rav4_plugin.html",
            "web/toyota_rav4_plugin.headers.json",
        ]
        for name in required:
            self.assertIn(name, names)

    def test_archive_root_directory_is_the_capture_id(self):
        result, _transport = run(full_routes())
        self.assertTrue(
            re.match(r"^milo-r5-source-capture-\d{8}T\d{6}Z$", result["capture_id"]),
            result["capture_id"],
        )
        with zipfile.ZipFile(io.BytesIO(result["archive_bytes"])) as archive:
            for name in archive.namelist():
                self.assertTrue(name.startswith(result["capture_id"] + "/"), name)

    def test_bodies_are_preserved_as_exact_bytes(self):
        raw = b'{"success":true,"result":{"records":[{"_id":7,"x":"\\u05e8"}]}}'
        routes = full_routes()
        routes[
            capture.resource_query_url(capture.WLTP_RESOURCE_ID, capture.PRIMARY_QUERY_TOKEN)
        ] = json_response(None, raw=raw)
        result, _transport = run(routes)
        stored = zip_read(result, "government/resource_142afde2_rav4.json")
        self.assertEqual(stored, raw, "body must not be reformatted")

    def test_manifest_hashes_match_the_stored_bodies(self):
        result, _transport = run(full_routes())
        manifest = json.loads(zip_read(result, "manifest.json").decode("utf-8"))
        for entry in manifest["entries"]:
            body = zip_read(result, entry["body_filename"])
            self.assertEqual(len(body), entry["byte_count"], entry["source_id"])
            self.assertEqual(
                hashlib.sha256(body).hexdigest(), entry["sha256"], entry["source_id"]
            )

    def test_sha256sums_verifies_against_every_archive_member(self):
        result, _transport = run(full_routes())
        sums = zip_read(result, "SHA256SUMS.txt").decode("utf-8")
        listed = []
        for line in sums.splitlines():
            digest, _, name = line.partition("  ")
            listed.append(name)
            self.assertEqual(hashlib.sha256(zip_read(result, name)).hexdigest(), digest, name)
        self.assertIn("manifest.json", listed)
        self.assertIn("README.txt", listed)
        self.assertNotIn("SHA256SUMS.txt", listed, "cannot list its own digest")
        for name in zip_names(result):
            if name != "SHA256SUMS.txt":
                self.assertIn(name, listed)

    def test_manifest_sha256_is_reported_and_correct(self):
        result, _transport = run(full_routes())
        self.assertEqual(
            hashlib.sha256(zip_read(result, "manifest.json")).hexdigest(),
            result["manifest_sha256"],
        )

    def test_manifest_records_provenance_for_every_entry(self):
        result, _transport = run(full_routes())
        manifest = json.loads(zip_read(result, "manifest.json").decode("utf-8"))
        self.assertFalse(manifest["credentials_used"])
        self.assertFalse(manifest["api_key_used"])
        self.assertFalse(manifest["cookies_supplied"])
        required_keys = {
            "source_id", "source_type", "requested_url", "final_url", "redirect_chain",
            "started_utc", "finished_utc", "http_status", "headers", "body_filename",
            "byte_count", "sha256", "attempts", "credentials_used", "api_key_used",
            "cookies_supplied", "validation_result", "error", "record_count", "record_ids",
        }
        for entry in manifest["entries"]:
            self.assertTrue(required_keys.issubset(set(entry)), entry["source_id"])
            self.assertFalse(entry["credentials_used"])
            self.assertFalse(entry["api_key_used"])
            self.assertFalse(entry["cookies_supplied"])

    def test_manifest_preserves_original_record_ids(self):
        records = [{"_id": 31337, "kinuy_mishari": "RAV4"}, {"_id": 42, "kinuy_mishari": "RAV4"}]
        result, _transport = run(full_routes(primary_records=records))
        entry = entry_by_id(result, "resource_142afde2_rav4")
        self.assertEqual(entry["record_count"], 2)
        self.assertEqual(entry["record_ids"], [31337, 42])

    def test_header_files_carry_only_allowlisted_headers(self):
        routes = full_routes()
        routes[capture.package_show_url()] = json_response(
            package_payload(),
            headers={
                "Set-Cookie": "leak=1",
                "Server": "secret-host",
                "ETag": "\"v1\"",
                "Cache-Control": "max-age=60",
            },
        )
        result, _transport = run(routes)
        document = json.loads(
            zip_read(result, "government/package_show_degem_rechev_wltp.headers.json").decode("utf-8")
        )
        self.assertEqual(
            set(document["preserved_headers"]), {"Content-Type", "ETag", "Cache-Control"}
        )
        raw = zip_read(result, "government/package_show_degem_rechev_wltp.headers.json")
        self.assertNotIn(b"leak=1", raw)
        self.assertNotIn(b"secret-host", raw)

    def test_no_credential_values_anywhere_in_the_archive(self):
        """Prose naming a header is fine; an actual credential value is not."""
        result, _transport = run(full_routes())
        for name in zip_names(result):
            blob = zip_read(result, name).lower()
            for pattern in SECRET_VALUE_PATTERNS:
                self.assertIsNone(
                    re.search(pattern, blob),
                    "{0} matches credential pattern {1!r}".format(name, pattern),
                )

    def test_every_header_file_is_restricted_to_the_allowlist(self):
        result, _transport = run(full_routes())
        allowed = set(capture.PRESERVED_HEADERS)
        for name in zip_names(result):
            if not name.endswith(".headers.json"):
                continue
            document = json.loads(zip_read(result, name).decode("utf-8"))
            self.assertTrue(
                set(document["preserved_headers"]).issubset(allowed),
                "{0}: {1}".format(name, sorted(document["preserved_headers"])),
            )


# --------------------------------------------------------------------------
# Overall status and archive filenames
# --------------------------------------------------------------------------


class OverallStatusTests(unittest.TestCase):
    def test_ready_status_and_normal_filename(self):
        result, _transport = run(full_routes())
        self.assertEqual(result["overall_status"], "ready_for_r5_bundle_review")
        self.assertTrue(result["integrity_verified"])
        self.assertTrue(
            re.match(r"^milo-r5-source-capture-\d{8}T\d{6}Z\.zip$", result["archive_filename"]),
            result["archive_filename"],
        )
        self.assertNotIn("INCOMPLETE", result["archive_filename"])

    def test_zero_records_status_and_incomplete_filename(self):
        result, _transport = run(full_routes(primary_records=[], alt_records=[]))
        self.assertEqual(result["overall_status"], "no_matching_government_records")
        self.assertTrue(result["archive_filename"].startswith("milo-r5-source-capture-INCOMPLETE-"))

    def test_toyota_without_specs_status_and_incomplete_filename(self):
        result, _transport = run(full_routes(toyota_html=BARE_TOYOTA_HTML))
        self.assertEqual(result["overall_status"], "incomplete_official_web_source")
        self.assertIn("INCOMPLETE", result["archive_filename"])

    def test_blocked_toyota_page_is_capture_failed(self):
        result, _transport = run(full_routes(toyota_html=BLOCKED_TOYOTA_HTML))
        self.assertEqual(result["overall_status"], "capture_failed")
        self.assertIn("INCOMPLETE", result["archive_filename"])

    def test_government_failure_is_capture_failed(self):
        routes = full_routes()
        routes[capture.package_show_url()] = json_response({"success": False}, status=200)
        result, _transport = run(routes)
        self.assertEqual(result["overall_status"], "capture_failed")
        self.assertIn("INCOMPLETE", result["archive_filename"])

    def test_network_failure_still_produces_a_diagnostic_archive(self):
        routes = full_routes()
        routes[capture.package_show_url()] = [IOError("boom")] * 3
        result, _transport = run(routes)
        self.assertEqual(result["overall_status"], "capture_failed")
        self.assertIn("INCOMPLETE", result["archive_filename"])
        self.assertGreater(len(result["archive_bytes"]), 0)
        self.assertIn("manifest.json", zip_names(result))
        entry = entry_by_id(result, "package_show_degem_rechev_wltp")
        self.assertEqual(entry["validation_result"], "failed_request")

    def test_an_incomplete_archive_is_never_labelled_ready(self):
        for routes in (
            full_routes(primary_records=[], alt_records=[]),
            full_routes(toyota_html=BARE_TOYOTA_HTML),
            full_routes(toyota_html=BLOCKED_TOYOTA_HTML),
        ):
            result, _transport = run(routes)
            self.assertNotEqual(result["overall_status"], "ready_for_r5_bundle_review")
            self.assertIn("INCOMPLETE", result["archive_filename"])
            manifest = json.loads(zip_read(result, "manifest.json").decode("utf-8"))
            self.assertEqual(manifest["overall_status"], result["overall_status"])
            self.assertNotEqual(manifest["overall_status"], "ready_for_r5_bundle_review")

    def test_integrity_failure_forces_capture_failed(self):
        transport = FakeTransport(full_routes())
        raw = capture.run_capture(getter=transport, clock=fixed_clock())
        self.assertEqual(capture.determine_status(raw["entries"]), "ready_for_r5_bundle_review")
        # Corrupt one preserved body after capture; verification must catch it.
        raw["files"]["government/resource_142afde2_rav4.json"] += b"tampered"
        result = capture.package_capture(raw)
        self.assertEqual(result["overall_status"], "capture_failed")
        self.assertFalse(result["integrity_verified"])
        self.assertTrue(
            any("sha256 mismatch" in p for p in result["integrity_problems"]),
            result["integrity_problems"],
        )
        self.assertIn("INCOMPLETE", result["archive_filename"])

    def test_archive_filename_helper(self):
        capture_id = "milo-r5-source-capture-20260914T120000Z"
        self.assertEqual(
            capture.archive_filename(capture_id, capture.STATUS_READY),
            "milo-r5-source-capture-20260914T120000Z.zip",
        )
        for status in (
            capture.STATUS_NO_RECORDS,
            capture.STATUS_INCOMPLETE_WEB,
            capture.STATUS_FAILED,
        ):
            self.assertEqual(
                capture.archive_filename(capture_id, status),
                "milo-r5-source-capture-INCOMPLETE-20260914T120000Z.zip",
            )


# --------------------------------------------------------------------------
# Source-code audit (kept as a permanent regression guard)
# --------------------------------------------------------------------------


class SourceAuditTests(unittest.TestCase):
    def _sources(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("capture.py", "app.py"):
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                yield name, handle.read()

    def _code_only(self):
        """Source with comments and string literals removed.

        The modules document what they deliberately do NOT do ("no
        requests.Session", "no Authorization header"). Auditing raw text would
        flag that prose, so structural checks run against executable code only.
        """
        for name, source in self._sources():
            pieces = []
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                pieces.append(token.string)
            yield name, " ".join(pieces)

    def test_no_mutating_http_verbs(self):
        for name, source in self._code_only():
            for verb in ("requests.post", "requests.put", "requests.patch", "requests.delete"):
                self.assertNotIn(verb, source, name)

    def test_no_requests_session(self):
        for name, code in self._code_only():
            self.assertNotIn("requests.Session", code, name)
            self.assertNotIn("Session (", code, name)

    def test_no_secret_lookup_in_code(self):
        """No code path reads a secret, an env var or a credential store."""
        forbidden = ("st.secrets", "os.environ", "getenv", "dotenv", "keyring", "netrc")
        for name, code in self._code_only():
            for pattern in forbidden:
                self.assertNotIn(pattern, code, "{0} contains {1!r}".format(name, pattern))

    def test_no_credential_values_in_source(self):
        """Prose naming a header is fine; an actual credential value is not."""
        for name, source in self._sources():
            lowered = source.lower().encode("utf-8")
            for pattern in SECRET_VALUE_PATTERNS:
                self.assertIsNone(
                    re.search(pattern, lowered),
                    "{0} matches credential pattern {1!r}".format(name, pattern),
                )

    def test_no_auth_header_is_ever_constructed(self):
        """Catch a real header dict entry, which _code_only would strip."""
        for name, source in self._sources():
            for pattern in (
                r"[\"']Authorization[\"']\s*:",
                r"[\"']Cookie[\"']\s*:",
                r"\bauth\s*=",
                r"\bheaders\[[\"']Authorization",
            ):
                self.assertIsNone(
                    re.search(pattern, source), "{0} matches {1!r}".format(name, pattern)
                )

    def test_no_provider_client_libraries_are_imported(self):
        for name, code in self._code_only():
            for pattern in ("openai", "anthropic", "openrouter", "kimi", "httpx", "aiohttp"):
                self.assertNotIn(pattern, code.lower(), "{0} imports {1!r}".format(name, pattern))

    def test_no_generic_url_input_in_the_ui(self):
        for name, source in self._code_only():
            for widget in ("st.text_input", "st.text_area", "st.file_uploader", "st.chat_input"):
                self.assertNotIn(widget, source, name)

    def test_no_browser_automation_or_model_providers(self):
        patterns = ("playwright", "selenium", "webdriver", "openai", "anthropic", "openrouter")
        for name, source in self._code_only():
            lowered = source.lower()
            for pattern in patterns:
                self.assertNotIn(pattern, lowered, "{0} contains {1!r}".format(name, pattern))

    def test_no_writes_to_milo_supabase_or_gcp(self):
        patterns = ("supabase", "bigquery", "google.cloud", "milo-agent-workspace")
        for name, source in self._code_only():
            lowered = source.lower()
            for pattern in patterns:
                self.assertNotIn(pattern, lowered, "{0} contains {1!r}".format(name, pattern))

    def test_no_global_network_caching(self):
        for name, source in self._code_only():
            self.assertNotIn("cache_data", source, name)
            self.assertNotIn("cache_resource", source, name)
            self.assertNotIn("st.cache", source, name)

    def test_no_database_or_persistence_layer(self):
        patterns = ("sqlite3", "psycopg", "sqlalchemy", "redis", "pymongo")
        for name, source in self._code_only():
            lowered = source.lower()
            for pattern in patterns:
                self.assertNotIn(pattern, lowered, name)


if __name__ == "__main__":
    unittest.main()
