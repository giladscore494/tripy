# -*- coding: utf-8 -*-
"""Network policy: hosts, redirects, retries, backoff, size limits, headers."""

import json

import capture
from conftest import (
    FakeResponse, FakeTransport, RecordingSleeper, fixed_clock,
    json_response, package_payload, full_routes, run_capture,
)

URL = capture.package_show_url()


def fetch(routes, url=URL, accept=capture.ACCEPT_JSON, sleeper=None, **kwargs):
    transport = FakeTransport(routes)
    sleeper = sleeper or RecordingSleeper()
    result = capture.perform_get(
        url, accept, getter=transport, clock=fixed_clock(), sleeper=sleeper, **kwargs
    )
    return result, transport, sleeper


# --------------------------------------------------------------------------
# Hosts, scheme and credentials
# --------------------------------------------------------------------------


def test_allowed_hosts_are_exactly_the_four_public_hosts():
    assert set(capture.ALLOWED_HOSTS) == {
        "data.gov.il", "www.data.gov.il", "toyota.co.il", "www.toyota.co.il",
    }


def test_disallowed_hostname_is_rejected_without_a_request():
    result, transport, _ = fetch({}, url="https://evil.example.com/x")
    assert "host_not_allowed" in result["error"]
    assert transport.calls == []


def test_http_downgrade_is_rejected():
    result, transport, _ = fetch({}, url="http://data.gov.il/api/3/action/package_show")
    assert "not_https" in result["error"]
    assert transport.calls == []


def test_url_with_embedded_credentials_is_rejected():
    result, transport, _ = fetch({}, url="https://user:pass@data.gov.il/api/3/action/x")
    assert "url_contains_credentials" in result["error"]
    assert transport.calls == []


def test_redirect_to_disallowed_host_is_rejected():
    result, transport, _ = fetch(
        {URL: FakeResponse(302, {"Location": "https://evil.example.com/steal"}, b"")}
    )
    assert "redirect_host_not_allowed" in result["error"]
    assert len(transport.calls) == 1


def test_redirect_http_downgrade_is_rejected():
    result, _transport, _ = fetch(
        {URL: FakeResponse(302, {"Location": "http://data.gov.il/x"}, b"")}
    )
    assert "redirect_not_https" in result["error"]


# --------------------------------------------------------------------------
# Redirects
# --------------------------------------------------------------------------


def test_relative_redirect_is_resolved_and_followed():
    routes = {
        "https://data.gov.il/api/3/action/start": FakeResponse(
            301, {"Location": "/api/3/action/package_show?id=degem-rechev-wltp"}, b""
        ),
        URL: json_response(package_payload()),
    }
    result, _t, _s = fetch(routes, url="https://data.gov.il/api/3/action/start")
    assert result["error"] is None
    assert result["final_url"] == URL
    assert result["redirect_chain"][0]["to"] == URL


def test_redirect_loop_is_detected():
    routes = {
        "https://data.gov.il/a": FakeResponse(302, {"Location": "https://data.gov.il/b"}, b""),
        "https://data.gov.il/b": FakeResponse(302, {"Location": "https://data.gov.il/a"}, b""),
    }
    result, _t, _s = fetch(routes, url="https://data.gov.il/a")
    assert "redirect_loop" in result["error"]


def test_more_than_five_redirects_is_rejected():
    routes = {
        "https://data.gov.il/hop{0}".format(i): FakeResponse(
            302, {"Location": "https://data.gov.il/hop{0}".format(i + 1)}, b""
        )
        for i in range(8)
    }
    result, _t, _s = fetch(routes, url="https://data.gov.il/hop0")
    assert "too_many_redirects" in result["error"]
    assert len(result["redirect_chain"]) == capture.MAX_REDIRECTS + 1


def test_exactly_five_redirects_is_accepted():
    routes = {
        "https://data.gov.il/hop{0}".format(i): FakeResponse(
            302, {"Location": "https://data.gov.il/hop{0}".format(i + 1)}, b""
        )
        for i in range(capture.MAX_REDIRECTS)
    }
    routes["https://data.gov.il/hop5"] = json_response(package_payload())
    result, _t, _s = fetch(routes, url="https://data.gov.il/hop0")
    assert result["error"] is None
    assert len(result["redirect_chain"]) == capture.MAX_REDIRECTS


def test_redirect_without_location_is_rejected():
    result, _t, _s = fetch({URL: FakeResponse(302, {}, b"")})
    assert "redirect_missing_location" in result["error"]


# --------------------------------------------------------------------------
# Retries, backoff and Retry-After
# --------------------------------------------------------------------------


def test_transport_timeout_retries_then_fails_sanitized():
    class FakeTimeout(Exception):
        pass

    result, transport, sleeper = fetch({URL: [FakeTimeout("connect to 10.0.0.1:8080")] * 3})
    assert len(transport.calls) == capture.MAX_ATTEMPTS
    assert result["attempts"] == capture.MAX_ATTEMPTS
    assert result["retries"] == capture.MAX_ATTEMPTS - 1
    assert "transport_error" in result["error"]
    assert "10.0.0.1" not in result["error"]
    assert len(sleeper.delays) == capture.MAX_ATTEMPTS - 1


def test_retryable_statuses_are_retried_then_success_is_used():
    for status in sorted(capture.RETRY_STATUSES):
        result, transport, _s = fetch(
            {URL: [FakeResponse(status, {}, b""), json_response(package_payload())]}
        )
        assert result["error"] is None, status
        assert result["attempts"] == 2, status
        assert result["retries"] == 1, status


def test_non_retryable_4xx_is_not_retried():
    for status in (400, 401, 403, 404, 410, 422):
        result, transport, _s = fetch(
            {URL: FakeResponse(status, {"Content-Type": "application/json"}, b"{}")}
        )
        assert len(transport.calls) == 1, status
        assert result["http_status"] == status
        assert result["retries"] == 0


def test_404_is_never_retried():
    result, transport, sleeper = fetch(
        {URL: FakeResponse(404, {"Content-Type": "text/html"}, b"nope")}
    )
    assert len(transport.calls) == 1
    assert sleeper.delays == []


def test_backoff_is_bounded_and_exponential():
    assert capture.backoff_delay(1) == 1.0
    assert capture.backoff_delay(2) == 2.0
    assert capture.backoff_delay(3) == 4.0
    assert capture.backoff_delay(10) == capture.BACKOFF_MAX_SECONDS


def test_backoff_delays_are_recorded_and_never_actually_slept():
    result, _t, sleeper = fetch({URL: [FakeResponse(503, {}, b"")] * 3})
    assert sleeper.delays == [1.0, 2.0]
    assert result["retry_delays_seconds"] == [1.0, 2.0]


def test_retry_after_is_respected_and_capped():
    assert capture.parse_retry_after("5") == 5.0
    assert capture.parse_retry_after("9999") == capture.MAX_RETRY_AFTER_SECONDS
    assert capture.parse_retry_after("-1") is None
    assert capture.parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert capture.parse_retry_after(None) is None

    result, _t, sleeper = fetch(
        {URL: [FakeResponse(429, {"Retry-After": "7"}, b""), json_response(package_payload())]}
    )
    assert sleeper.delays == [7.0]
    assert result["retry_delays_seconds"] == [7.0]


def test_unreasonable_retry_after_is_capped_not_obeyed():
    result, _t, sleeper = fetch(
        {URL: [FakeResponse(503, {"Retry-After": "3600"}, b""), json_response(package_payload())]}
    )
    assert sleeper.delays == [capture.MAX_RETRY_AFTER_SECONDS]


# --------------------------------------------------------------------------
# Size limits
# --------------------------------------------------------------------------


def test_default_size_limit_is_15_mib():
    assert capture.MAX_RESPONSE_BYTES == 15 * 1024 * 1024


def test_oversized_streamed_response_is_rejected():
    result, _t, _s = fetch(
        {URL: FakeResponse(200, {"Content-Type": "application/json"}, b"x" * 2048)},
        max_bytes=1024,
    )
    assert "response_too_large" in result["error"]
    assert result["body"] == b""


def test_oversized_content_length_is_rejected_before_reading():
    result, _t, _s = fetch(
        {URL: FakeResponse(
            200,
            {"Content-Type": "application/json", "Content-Length": str(20 * 1024 * 1024)},
            b"{}",
        )}
    )
    assert "response_too_large" in result["error"]


def test_bundle_byte_ceiling_fails_the_capture():
    result, _transport = run_capture(full_routes(), max_bundle_bytes=10)
    assert result["overall_status"] == capture.STATUS_FAILED
    assert any("bundle_size_exceeded" in f for f in result["government_failures"])


# --------------------------------------------------------------------------
# Headers and cookies
# --------------------------------------------------------------------------


def test_no_sensitive_request_headers_are_ever_sent():
    _result, transport = run_capture(full_routes())
    assert transport.calls
    for call in transport.calls:
        lowered = {k.lower() for k in call["headers"]}
        assert lowered == {"user-agent", "accept", "accept-encoding"}
        assert "authorization" not in lowered
        assert "cookie" not in lowered
        assert "proxy-authorization" not in lowered


def test_timeouts_are_connect_10_read_45():
    _result, transport = run_capture(full_routes())
    for call in transport.calls:
        assert call["timeout"] == (10, 45)


def test_sensitive_response_headers_are_discarded():
    kept = capture.sanitize_headers(
        {
            "Date": "Mon, 14 Sep 2026 12:00:00 GMT",
            "Content-Type": "application/json",
            "Set-Cookie": "session=supersecret; Path=/",
            "Cookie": "a=b",
            "Authorization": "Bearer leaked",
            "Proxy-Authorization": "Basic leaked",
            "WWW-Authenticate": "Basic realm=x",
            "Server": "nginx/1.2.3",
        }
    )
    assert set(kept) == {"Date", "Content-Type"}
    blob = json.dumps(kept)
    assert "supersecret" not in blob
    assert "leaked" not in blob
    assert "nginx" not in blob


def test_set_cookie_never_reaches_the_manifest():
    routes = full_routes()
    routes[capture.package_show_url()] = json_response(
        package_payload(), headers={"Set-Cookie": "tracker=leakme; Path=/"}
    )
    result, _transport = run_capture(routes)
    blob = json.dumps(result["entries"], ensure_ascii=False)
    assert "leakme" not in blob
    assert "Set-Cookie" not in blob


def test_cookie_from_a_redirect_is_not_replayed():
    routes = {
        "https://data.gov.il/start": FakeResponse(
            302, {"Location": URL, "Set-Cookie": "hop=1; Path=/"}, b""
        ),
        URL: json_response(package_payload()),
    }
    result, transport, _s = fetch(routes, url="https://data.gov.il/start")
    assert result["error"] is None
    second = transport.calls[1]["headers"]
    assert {k.lower() for k in second} == {"user-agent", "accept", "accept-encoding"}


def test_tls_verification_is_not_disabled_anywhere():
    source = open(capture.__file__.replace(".pyc", ".py"), encoding="utf-8").read()
    assert "verify=False" not in source
    assert "verify=True" in source
