# -*- coding: utf-8 -*-
"""Shared fakes for the MILO R5 capture tests.

Every HTTP interaction in the suite goes through an injected fake transport.
No test performs a live network request or sleeps for real.
"""

import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import capture  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# Fake HTTP plumbing
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
    """Maps URL -> FakeResponse, a list of them, an exception, or a callable."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
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

    @property
    def urls(self):
        return [call["url"] for call in self.calls]


class RecordingSleeper(object):
    """Injected sleeper: records requested delays, never actually waits."""

    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


def fixed_clock():
    moments = iter(
        datetime.datetime(2026, 9, 14, 12, 0, 0, tzinfo=datetime.timezone.utc)
        + datetime.timedelta(seconds=step)
        for step in range(0, 100000)
    )
    last = {"value": None}

    def clock():
        try:
            last["value"] = next(moments)
        except StopIteration:  # pragma: no cover
            pass
        return last["value"]

    return clock


# --------------------------------------------------------------------------
# Response builders
# --------------------------------------------------------------------------


def json_response(payload, status=200, headers=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json; charset=utf-8"}
    merged.update(headers or {})
    return FakeResponse(status, merged, body)


def html_response(text, status=200, headers=None):
    merged = {"Content-Type": "text/html; charset=utf-8"}
    merged.update(headers or {})
    return FakeResponse(status, merged, text.encode("utf-8"))


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


def rav4_records(count, first_id=1000):
    return [
        {
            "_id": first_id + i,
            "tozeret_nm": "TOYOTA",
            "kinuy_mishari": "RAV4",
            "shnat_yitzur": 2023,
        }
        for i in range(count)
    ]


def paged_routes(resource_id, token, records, total=None, page_size=None,
                 stop_short=False):
    """Routes for every page the paginator will legitimately request."""
    page_size = page_size or capture.PAGE_SIZE
    reported_total = len(records) if total is None else total
    routes = {}
    offset = 0
    while True:
        chunk = records[offset:offset + page_size]
        url = capture.datastore_page_url(resource_id, token, page_size, offset)
        routes[url] = json_response(
            {"success": True, "result": {"records": chunk, "total": reported_total}}
        )
        if not chunk:
            break
        offset += len(chunk)
        if offset >= len(records):
            if stop_short or offset >= reported_total:
                break
            # The paginator will ask for one more page; serve an empty one.
            routes[capture.datastore_page_url(resource_id, token, page_size, offset)] = (
                json_response(
                    {"success": True, "result": {"records": [], "total": reported_total}}
                )
            )
            break
    return routes


# --------------------------------------------------------------------------
# Toyota HTML fixtures
# --------------------------------------------------------------------------

_FILLER = "<p>\u05ea\u05d5\u05db\u05df \u05e8\u05e9\u05de\u05d9 \u05e9\u05dc \u05d8\u05d5\u05d9\u05d5\u05d8\u05d4 \u05d9\u05e9\u05e8\u05d0\u05dc.</p>" * 45

# Third-party widgets a perfectly healthy Toyota page loads site-wide. Every
# occurrence of "captcha" below is in markup or script, never in visible text.
_RECAPTCHA_MARKUP = (
    '<script src="https://www.google.com/recaptcha/api.js" async defer></script>'
    '<div class="g-recaptcha" data-sitekey="6LcEXAMPLEKEY"></div>'
    '<input type="hidden" name="g-recaptcha-response">'
    '<script>window.___grecaptcha_cfg={};'
    'grecaptcha.ready(function(){grecaptcha.execute("6LcEXAMPLEKEY");});</script>'
)

# Generic error wording that lives only inside JavaScript on a healthy page.
_JS_ERROR_STRINGS = (
    '<script>var i18n={notFound:"Page Not Found",e404:"404 Not Found",'
    'e500:"500 Internal Server Error",unavailable:"Service Unavailable",'
    'signIn:"Please sign in",denied:"Access Denied"};</script>'
)

_ENDED_MARKETING_HE = (
    "\u05e9\u05d9\u05d5\u05d5\u05e7 \u05d4\u05d3\u05d2\u05dd \u05e8\u05d0\u05d14 "
    "\u05e4\u05dc\u05d0\u05d2-\u05d0\u05d9\u05df \u05d4\u05e1\u05ea\u05d9\u05d9\u05dd"
)

_ARCHIVE_SECTION_HE = (
    "\u05d0\u05e8\u05db\u05d9\u05d5\u05df \u05d3\u05d2\u05de\u05d9\u05dd - "
    "\u05d3\u05d2\u05de\u05d9 \u05e2\u05d1\u05e8"
)

TOYOTA_ARCHIVE_INDEX_HTML = (
    "<!DOCTYPE html><html lang=\"he\" dir=\"rtl\"><head>"
    "<title>\u05d0\u05e8\u05db\u05d9\u05d5\u05df \u05d3\u05d2\u05de\u05d9\u05dd | "
    "\u05d8\u05d5\u05d9\u05d5\u05d8\u05d4</title></head><body>"
    "<h1>TOYOTA \u05d8\u05d5\u05d9\u05d5\u05d8\u05d4</h1>"
    "<h2>" + _ARCHIVE_SECTION_HE + "</h2>"
    "<ul><li>RAV4 Plug-in Hybrid (PHEV)</li><li>Corolla</li><li>Yaris</li></ul>"
    + _FILLER + "</body></html>"
)

# Same legitimate page, plus the site-wide reCAPTCHA widget and JS error strings.
TOYOTA_ARCHIVE_INDEX_WITH_CAPTCHA_HTML = (
    "<!DOCTYPE html><html lang=\"he\" dir=\"rtl\"><head>"
    "<title>\u05d0\u05e8\u05db\u05d9\u05d5\u05df \u05d3\u05d2\u05de\u05d9\u05dd | "
    "\u05d8\u05d5\u05d9\u05d5\u05d8\u05d4</title>"
    + _RECAPTCHA_MARKUP + "</head><body>"
    "<h1>TOYOTA \u05d8\u05d5\u05d9\u05d5\u05d8\u05d4</h1>"
    "<h2>" + _ARCHIVE_SECTION_HE + "</h2>"
    "<ul><li>RAV4 Plug-in Hybrid (PHEV)</li></ul>"
    + _JS_ERROR_STRINGS + _FILLER + "</body></html>"
)

# Archive index that never establishes RAV4 Plug-in identity.
TOYOTA_ARCHIVE_INDEX_NO_PLUGIN_HTML = (
    "<!DOCTYPE html><html lang=\"he\" dir=\"rtl\"><head>"
    "<title>\u05d0\u05e8\u05db\u05d9\u05d5\u05df | \u05d8\u05d5\u05d9\u05d5\u05d8\u05d4"
    "</title></head><body>"
    "<h1>TOYOTA \u05d8\u05d5\u05d9\u05d5\u05d8\u05d4</h1>"
    "<h2>" + _ARCHIVE_SECTION_HE + "</h2>"
    "<ul><li>RAV4 Hybrid</li><li>Corolla</li></ul>"
    + _FILLER + "</body></html>"
)

TOYOTA_RAV4_PHEV_HTML = (
    "<!DOCTYPE html><html lang=\"he\" dir=\"rtl\"><head>"
    "<title>TOYOTA RAV4 Plug-in</title></head><body>"
    "<h1>TOYOTA RAV4 Plug-in Hybrid (PHEV)</h1>"
    "<p>" + _ENDED_MARKETING_HE + "</p>"
    + _FILLER + "</body></html>"
)

# Same legitimate model page, plus the site-wide reCAPTCHA widget.
TOYOTA_RAV4_PHEV_WITH_RECAPTCHA_HTML = (
    "<!DOCTYPE html><html lang=\"he\" dir=\"rtl\"><head>"
    "<title>TOYOTA RAV4 Plug-in</title>" + _RECAPTCHA_MARKUP + "</head><body>"
    "<h1>TOYOTA RAV4 Plug-in Hybrid (PHEV)</h1>"
    "<p>" + _ENDED_MARKETING_HE + "</p>"
    + _JS_ERROR_STRINGS + _FILLER + "</body></html>"
)

# Official RAV4 page with no Plug-in/PHEV identity: must be rejected.
TOYOTA_RAV4_HYBRID_HTML = (
    "<!DOCTYPE html><html lang=\"he\" dir=\"rtl\"><head>"
    "<title>TOYOTA RAV4 Hybrid</title></head><body>"
    "<h1>TOYOTA RAV4 Hybrid</h1>"
    "<p>\u05d4\u05e1\u05ea\u05d9\u05d9\u05dd \u05d4\u05e9\u05d9\u05d5\u05d5\u05e7.</p>"
    + _FILLER + "</body></html>"
)

# Plug-in page that never says the model is archived: must be rejected.
TOYOTA_RAV4_PHEV_NO_ARCHIVE_HTML = (
    "<!DOCTYPE html><html lang=\"he\" dir=\"rtl\"><head>"
    "<title>TOYOTA RAV4 Plug-in</title></head><body>"
    "<h1>TOYOTA RAV4 Plug-in Hybrid (PHEV)</h1>"
    "<p>\u05e7\u05d1\u05e2\u05d5 \u05e0\u05e1\u05d9\u05e2\u05ea "
    "\u05de\u05d1\u05d7\u05df \u05e2\u05d5\u05d3 \u05d4\u05d9\u05d5\u05dd.</p>"
    + _FILLER + "</body></html>"
)

# Plug-in archive page for another model: must be rejected on RAV4 identity.
TOYOTA_NO_RAV4_HTML = (
    "<!DOCTYPE html><html lang=\"he\" dir=\"rtl\"><head>"
    "<title>TOYOTA Prius Plug-in</title></head><body>"
    "<h1>TOYOTA Prius Plug-in Hybrid (PHEV)</h1>"
    "<p>\u05d4\u05e1\u05ea\u05d9\u05d9\u05dd \u05d4\u05e9\u05d9\u05d5\u05d5\u05e7.</p>"
    + _FILLER + "</body></html>"
)

TOYOTA_BLOCKED_HTML = (
    "<!DOCTYPE html><html><head><title>Access Denied</title></head><body>"
    "<h1>Access Denied</h1><p>TOYOTA RAV4 Plug-in PHEV</p>"
    "<p>\u05d4\u05e1\u05ea\u05d9\u05d9\u05dd \u05d4\u05e9\u05d9\u05d5\u05d5\u05e7.</p>"
    + "<p>You do not have permission to access this resource.</p>" * 45
    + "</body></html>"
)

# A real challenge page: the CAPTCHA wording is VISIBLE, not just a script.
TOYOTA_VISIBLE_CAPTCHA_HTML = (
    "<!DOCTYPE html><html><head><title>Attention Required</title>"
    + _RECAPTCHA_MARKUP + "</head><body>"
    "<h1>Please complete the CAPTCHA to continue</h1>"
    "<p>TOYOTA \u05d8\u05d5\u05d9\u05d5\u05d8\u05d4 RAV4 Plug-in</p>"
    "<p>Cloudflare Ray ID: 0123456789abcdef</p>"
    "</body></html>"
)

TOYOTA_CAPTCHA_HTML = TOYOTA_VISIBLE_CAPTCHA_HTML

# 404 content served with HTTP 200 ("soft 404"): must still fail.
TOYOTA_404_HTTP200_HTML = (
    "<!DOCTYPE html><html lang=\"he\" dir=\"rtl\"><head>"
    "<title>404 - Page Not Found | TOYOTA</title></head><body>"
    "<h1>Page Not Found</h1>"
    "<p>TOYOTA \u05d8\u05d5\u05d9\u05d5\u05d8\u05d4 RAV4 Plug-in PHEV</p>"
    "<p>" + _ENDED_MARKETING_HE + "</p>"
    + _FILLER + "</body></html>"
)

TOYOTA_404_HTML = (
    "<!DOCTYPE html><html><head><title>404 - Page Not Found</title></head><body>"
    "<h1>TOYOTA</h1><p>404 Not Found</p>"
    + "<p>The page you requested does not exist.</p>" * 20
    + "</body></html>"
)


def full_routes(wltp_records=None, additional_records=None,
                wltp_alt_records=None, additional_alt_records=None,
                index_html=TOYOTA_ARCHIVE_INDEX_HTML,
                index_status=200,
                model_html=TOYOTA_RAV4_PHEV_HTML,
                model_status=200):
    """Routes covering every URL the fixed plan can request."""
    if wltp_records is None:
        wltp_records = rav4_records(3, first_id=1000)
    if additional_records is None:
        additional_records = rav4_records(2, first_id=2000)

    routes = {capture.package_show_url(): json_response(package_payload())}
    per_resource = {
        "wltp": (capture.WLTP_RESOURCE_ID, wltp_records, wltp_alt_records),
        "additional": (capture.ADDITIONAL_RESOURCE_ID, additional_records,
                       additional_alt_records),
    }
    for _slug, (resource_id, records, alt_records) in per_resource.items():
        routes[capture.resource_schema_url(resource_id)] = json_response(schema_payload())
        routes.update(paged_routes(resource_id, capture.PRIMARY_QUERY_TOKEN, records))
        if not records:
            for token in capture.ALTERNATE_QUERY_TOKENS:
                routes.update(paged_routes(resource_id, token, alt_records or []))

    routes[capture.TOYOTA_ARCHIVE_INDEX_URL] = html_response(index_html, status=index_status)
    routes[capture.TOYOTA_RAV4_PHEV_URL] = html_response(model_html, status=model_status)
    return routes


def run_capture(routes, **kwargs):
    """Execute a full capture against fake routes. Returns (result, transport)."""
    transport = FakeTransport(routes)
    result = capture.run_and_package(
        getter=transport, clock=fixed_clock(), sleeper=RecordingSleeper(), **kwargs
    )
    return result, transport


def zip_names(result):
    """Archive member paths, relative to the archive root directory."""
    import io as _io
    import zipfile as _zipfile
    with _zipfile.ZipFile(_io.BytesIO(result["archive_bytes"])) as archive:
        root = result["capture_id"] + "/"
        return [n[len(root):] for n in archive.namelist() if n.startswith(root)]


def zip_read(result, name):
    import io as _io
    import zipfile as _zipfile
    with _zipfile.ZipFile(_io.BytesIO(result["archive_bytes"])) as archive:
        return archive.read("{0}/{1}".format(result["capture_id"], name))


def entry_by_id(result, source_id):
    for entry in result["entries"]:
        if entry["source_id"] == source_id:
            return entry
    raise AssertionError("no entry {0}".format(source_id))


@pytest.fixture
def transport_factory():
    return FakeTransport


@pytest.fixture
def sleeper():
    return RecordingSleeper()
