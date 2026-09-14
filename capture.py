"""Bounded, read-only public source capture for MILO R5.

Captures a fixed set of public Israeli Government CKAN endpoints and two
official Toyota Israel pages, preserves the exact response bytes, validates
them, and packages everything into a verifiable in-memory evidence archive.

Design constraints enforced here:

* Every URL is built from the fixed constants below. No code path fetches a
  caller-supplied or user-supplied URL.
* GET over HTTPS only. No POST/PUT/PATCH/DELETE, no ``requests.Session``.
* No credentials, API keys, tokens or cookies are read, stored or transmitted.
  ``Authorization``, ``Cookie`` and ``Proxy-Authorization`` are never sent, and
  ``Set-Cookie`` is never replayed.
* TLS verification stays enabled.
* Response bodies are preserved as exact bytes and never reformatted.
* Only an allowlist of response headers is retained.

This module holds all networking and is importable without Streamlit.
"""

import datetime
import hashlib
import io
import json
import time
import urllib.parse
import zipfile
from collections import namedtuple

# --------------------------------------------------------------------------
# Public connection identifiers (public resource IDs, not secrets)
# --------------------------------------------------------------------------

CKAN_API_BASE = "https://data.gov.il/api/3/action"
CKAN_API_VERSION = 3
CKAN_PACKAGE_ID = "degem-rechev-wltp"
WLTP_RESOURCE_ID = "142afde2-6228-49f9-8a29-9b6c3a0cbe40"
ADDITIONAL_RESOURCE_ID = "5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6"

# Current official Toyota Israel pages.
TOYOTA_ARCHIVE_INDEX_URL = "https://www.toyota.co.il/cars"
TOYOTA_RAV4_PHEV_URL = "https://www.toyota.co.il/cars/RAV4-PHEV"

# Retained for provenance only. Returns HTTP 404 and is never requested.
TOYOTA_PREVIOUS_OBSOLETE_URL = "https://www.toyota.co.il/models/rav4-plugin"

# --------------------------------------------------------------------------
# Request policy
# --------------------------------------------------------------------------

USER_AGENT = "MILO-R5-streamlit-evidence-capture/2.0"
ACCEPT_JSON = "application/json"
ACCEPT_HTML = "text/html,application/xhtml+xml"
ACCEPT_ENCODING = "identity"

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 45
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 15 * 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 30.0

# Only these statuses are retried. Ordinary 4xx, including 404, are not.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

ALLOWED_HOSTS = frozenset(
    {"data.gov.il", "www.data.gov.il", "toyota.co.il", "www.toyota.co.il"}
)
TOYOTA_HOSTS = frozenset({"toyota.co.il", "www.toyota.co.il"})

PRESERVED_HEADERS = (
    "Date",
    "Content-Type",
    "Content-Length",
    "Content-Encoding",
    "ETag",
    "Last-Modified",
    "Location",
    "Cache-Control",
    "Retry-After",
)

# Never retained, even if an allowlist entry were added by mistake.
FORBIDDEN_HEADERS = frozenset(
    {"set-cookie", "cookie", "authorization", "proxy-authorization",
     "www-authenticate", "proxy-authenticate"}
)

# --------------------------------------------------------------------------
# Government query and pagination policy
# --------------------------------------------------------------------------

PRIMARY_QUERY_TOKEN = "RAV4"
ALTERNATE_QUERY_TOKENS = ("RAV%204", "%D7%A8%D7%90%D7%91")
PAGE_SIZE = 100
MAX_PAGES_PER_QUERY = 200
SCHEMA_LIMIT = 0

GOVERNMENT_RESOURCES = (
    ("wltp", WLTP_RESOURCE_ID),
    ("additional", ADDITIONAL_RESOURCE_ID),
)

SOURCE_TYPE_PACKAGE = "government_package_metadata"
SOURCE_TYPE_SCHEMA = "government_resource_schema"
SOURCE_TYPE_PAGE = "government_datastore_page"
SOURCE_TYPE_WEB_INDEX = "official_web_archive_index"
SOURCE_TYPE_WEB_MODEL = "official_web_archived_model_page"

STATUS_READY = "ready_for_r5_bundle_review"
STATUS_NO_RECORDS = "no_matching_government_records"
STATUS_INCOMPLETE_WEB = "incomplete_official_web_source"
STATUS_FAILED = "capture_failed"

# --------------------------------------------------------------------------
# Content validation vocabulary
# --------------------------------------------------------------------------

JSON_CONTENT_TYPE_MARKERS = ("application/json", "text/json", "+json")
HTML_CONTENT_TYPE_MARKERS = ("text/html", "application/xhtml+xml")
MIN_HTML_BYTES = 512

TOYOTA_BRAND_MARKERS = ("toyota", "טויוטה")
TOYOTA_MODEL_MARKERS = ("rav4", "rav 4", "rav-4")
TOYOTA_PLUGIN_MARKERS = ("plug-in", "plugin", "phev", "פלאג")

TOYOTA_BLOCK_MARKERS = (
    "access denied",
    "captcha",
    "are you a human",
    "attention required",
    "request blocked",
    "web application firewall",
    "incapsula incident",
    "cloudflare ray id",
    "403 forbidden",
    "error 403",
    "404 not found",
    "page not found",
    "500 internal server error",
    "service unavailable",
    "please sign in",
    "please log in",
    "הדף לא נמצא",       # page not found
    "הגישה נדחתה",   # access denied
)

# Credible indications that the model is archived / no longer marketed.
TOYOTA_ARCHIVE_MARKERS = (
    "הסתיים השיווק",       # marketing has ended
    "השיווק הסתיים",       # the marketing ended
    "הופסק השיווק",             # marketing was stopped
    "שיווק הדגם הסתיים",  # model marketing ended
    "אינו משווק",                         # is not marketed
    "אינם משווקים",             # are not marketed
    "לא משווק",                                     # not marketed
    "דגמי עבר",                                     # past models
    "ארכיון",                                            # archive
    "discontinued",
    "no longer marketed",
    "no longer available",
    "end of production",
)

RESULT_ARCHIVE_INDEX = "passed_official_archive_index"
RESULT_ARCHIVED_MODEL = "passed_official_archived_model_identity_requires_human_fact_check"

SourceSpec = namedtuple(
    "SourceSpec",
    "source_id source_type url accept raw_path resource_id query_token offset limit",
)


def make_source(source_id, source_type, url, accept, raw_path,
                resource_id=None, query_token=None, offset=None, limit=None):
    return SourceSpec(
        source_id, source_type, url, accept, raw_path,
        resource_id, query_token, offset, limit,
    )


class CaptureFailure(Exception):
    """Internal signal carrying a sanitized failure code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.attempts = 0
        self.retries = 0
        self.status = None
        self.delays = []

    def sanitized(self):
        return self.code if not self.detail else "{0}: {1}".format(self.code, self.detail)


# --------------------------------------------------------------------------
# URL construction
# --------------------------------------------------------------------------


def package_show_url():
    return "{0}/package_show?id={1}".format(CKAN_API_BASE, CKAN_PACKAGE_ID)


def resource_schema_url(resource_id):
    """Bounded schema probe: limit=0 returns field definitions, never rows."""
    return "{0}/datastore_search?resource_id={1}&limit={2}".format(
        CKAN_API_BASE, resource_id, SCHEMA_LIMIT
    )


def datastore_page_url(resource_id, encoded_query_token, limit, offset):
    """Bounded, targeted page query. ``encoded_query_token`` is pre-encoded."""
    return "{0}/datastore_search?resource_id={1}&limit={2}&offset={3}&q={4}".format(
        CKAN_API_BASE, resource_id, limit, offset, encoded_query_token
    )


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _stamp(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_hex(payload):
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(document):
    text = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False, allow_nan=False)
    return (text + "\n").encode("utf-8")


def sanitized_query_params(url):
    """Parsed query string. All parameters here are public identifiers."""
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return {}
    params = {}
    for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items():
        if key.lower() in ("api_key", "apikey", "token", "access_token", "key", "secret"):
            continue  # defensive: such a parameter is never constructed
        params[key] = values[0] if len(values) == 1 else values
    return params


# --------------------------------------------------------------------------
# Header handling
# --------------------------------------------------------------------------


def _header_value(raw_headers, name):
    try:
        items = list(raw_headers.items())
    except AttributeError:
        return None
    wanted = name.lower()
    for key, value in items:
        if str(key).lower() == wanted:
            return str(value)
    return None


def sanitize_headers(raw_headers):
    """Keep only allowlisted headers; forbidden ones can never survive."""
    kept = {}
    for name in PRESERVED_HEADERS:
        if name.lower() in FORBIDDEN_HEADERS:
            continue
        value = _header_value(raw_headers, name)
        if value is not None:
            kept[name] = value
    return kept


def _sanitize_exception(exc):
    """Exception type only: messages can carry proxy URLs and local paths."""
    return type(exc).__name__


def _close(response):
    closer = getattr(response, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # pragma: no cover - defensive only
            pass


# --------------------------------------------------------------------------
# Low level HTTP: GET only, no Session, manual redirects, bounded body
# --------------------------------------------------------------------------


def _request_headers(accept):
    """Fresh header dict per request. No Authorization/Cookie/Proxy-Auth ever."""
    return {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Encoding": ACCEPT_ENCODING,
    }


def _requests_get(url, headers, timeout):
    """Single bounded GET. Imported lazily so tests need no dependency."""
    import requests

    return requests.get(
        url,
        headers=headers,
        timeout=timeout,
        allow_redirects=False,
        stream=True,
        cookies={},
        verify=True,
    )


def require_allowed_url(url, redirect=False):
    """HTTPS, allowlisted host, and no embedded credentials."""
    prefix = "redirect_" if redirect else ""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise CaptureFailure(prefix + "not_https", "scheme=" + (parsed.scheme or "<empty>"))
    if parsed.username or parsed.password:
        raise CaptureFailure(prefix + "url_contains_credentials")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise CaptureFailure(prefix + "host_not_allowed", "host=" + (host or "<empty>"))


def parse_retry_after(value):
    """Return a capped delay for a valid, reasonable Retry-After, else None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None  # HTTP-date form is ignored rather than guessed at
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def backoff_delay(attempt):
    """Bounded exponential backoff for the Nth attempt (1-based)."""
    return min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)


def _read_bounded_body(response, max_bytes):
    declared = _header_value(response.headers, "Content-Length")
    if declared:
        try:
            declared_length = int(declared)
        except (TypeError, ValueError):
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            _close(response)
            raise CaptureFailure("response_too_large", "content-length={0}".format(declared_length))

    chunks = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise CaptureFailure("response_too_large", "exceeded {0} bytes".format(max_bytes))
            chunks.append(chunk)
    finally:
        _close(response)
    return b"".join(chunks)


def _get_with_retries(url, accept, getter, sleeper):
    """Up to MAX_ATTEMPTS GETs. Retries transport errors, 429 and 500/502/503/504."""
    attempts = 0
    delays = []
    failure = None
    while attempts < MAX_ATTEMPTS:
        attempts += 1
        retry_after = None
        try:
            response = getter(
                url,
                _request_headers(accept),
                (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
        except Exception as exc:  # transport level only
            failure = CaptureFailure("transport_error", _sanitize_exception(exc))
        else:
            status = int(response.status_code)
            if status not in RETRY_STATUSES:
                return response, attempts, delays
            retry_after = parse_retry_after(_header_value(response.headers, "Retry-After"))
            _close(response)
            failure = CaptureFailure("retryable_http_status", "HTTP {0}".format(status))
            failure.status = status

        if attempts < MAX_ATTEMPTS:
            delay = retry_after if retry_after is not None else backoff_delay(attempts)
            delays.append(round(float(delay), 3))
            sleeper(delay)

    failure.attempts = attempts
    failure.retries = attempts - 1
    failure.delays = delays
    raise failure


def perform_get(url, accept, getter=None, clock=None, sleeper=None,
                max_bytes=MAX_RESPONSE_BYTES):
    """Fetch one fixed URL under the full bounded policy.

    Never raises: transport, HTTP, redirect and size failures are returned as a
    sanitized ``error`` code on the result dict.
    """
    getter = getter or _requests_get
    clock = clock or _utc_now
    sleeper = sleeper or time.sleep

    started = clock()
    redirect_chain = []
    attempts = 0
    retries = 0
    delays = []
    status = None
    headers = {}
    body = b""
    error = None
    current_url = url
    final_url = url

    try:
        while True:
            final_url = current_url
            require_allowed_url(current_url, redirect=bool(redirect_chain))
            response, used, used_delays = _get_with_retries(
                current_url, accept, getter, sleeper
            )
            attempts += used
            retries += used - 1
            delays.extend(used_delays)
            status = int(response.status_code)
            headers = sanitize_headers(response.headers)

            if status in REDIRECT_STATUSES:
                location = _header_value(response.headers, "Location")
                _close(response)
                if not location or not location.strip():
                    raise CaptureFailure("redirect_missing_location")
                target = urllib.parse.urljoin(current_url, location.strip())
                if any(hop["to"] == target for hop in redirect_chain) or target == url:
                    raise CaptureFailure("redirect_loop", "repeated target")
                redirect_chain.append({"status": status, "from": current_url, "to": target})
                if len(redirect_chain) > MAX_REDIRECTS:
                    raise CaptureFailure("too_many_redirects", "limit={0}".format(MAX_REDIRECTS))
                # A fresh header dict is built for the next hop, so no cookie
                # from this response is ever replayed onward.
                current_url = target
                continue

            body = _read_bounded_body(response, max_bytes)
            break
    except CaptureFailure as exc:
        error = exc.sanitized()
        attempts += exc.attempts
        retries += exc.retries
        delays.extend(exc.delays)
        if status is None:
            status = exc.status
        body = b""

    finished = clock()
    declared = headers.get("Content-Length")
    try:
        declared_content_length = int(declared) if declared is not None else None
    except (TypeError, ValueError):
        declared_content_length = None

    return {
        "requested_url": url,
        "query_params": sanitized_query_params(url),
        "final_url": final_url,
        "redirect_chain": redirect_chain,
        "started_utc": _iso(started),
        "finished_utc": _iso(finished),
        "http_status": status,
        "headers": headers,
        "content_type": headers.get("Content-Type"),
        "declared_content_length": declared_content_length,
        "body": body,
        "byte_count": len(body),
        "sha256": sha256_hex(body),
        "attempts": attempts,
        "retries": retries,
        "retry_delays_seconds": delays,
        "error": error,
    }


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _content_type_matches(content_type, markers):
    lowered = (content_type or "").lower()
    return any(marker in lowered for marker in markers)


def _outcome(result, detail="", **extra):
    outcome = {"result": result, "detail": detail}
    outcome.update(extra)
    return outcome


def decode_government_payload(fetch):
    """Shared Government preconditions. Returns (payload, failure_outcome)."""
    if fetch["error"]:
        return None, _outcome("failed_request", fetch["error"])
    if fetch["http_status"] != 200:
        return None, _outcome("failed_http_status", "HTTP {0}".format(fetch["http_status"]))
    if not _content_type_matches(fetch["content_type"], JSON_CONTENT_TYPE_MARKERS):
        return None, _outcome("failed_content_type", fetch["content_type"] or "<missing>")
    body = fetch["body"]
    if not body.strip():
        return None, _outcome("failed_empty_body", "0 usable bytes")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return None, _outcome("failed_invalid_json", _sanitize_exception(exc))
    if not isinstance(payload, dict):
        return None, _outcome("failed_invalid_json", "top level is not a JSON object")
    if payload.get("success") is not True:
        return None, _outcome("failed_success_flag", "success is not exactly true")
    if not isinstance(payload.get("result"), dict):
        return None, _outcome("failed_missing_result", "result object absent")
    return payload, None


def validate_package_show(fetch):
    payload, failure = decode_government_payload(fetch)
    if failure:
        return failure
    resources = payload["result"].get("resources")
    if not isinstance(resources, list) or not resources:
        return _outcome("failed_missing_resources", "no resources in package")
    return _outcome("passed", "{0} resources".format(len(resources)))


def validate_resource_schema(fetch):
    payload, failure = decode_government_payload(fetch)
    if failure:
        return failure
    fields = payload["result"].get("fields")
    if not isinstance(fields, list) or not fields:
        return _outcome("failed_missing_fields", "no fields in schema")
    return _outcome("passed", "{0} fields".format(len(fields)))


def validate_datastore_page(fetch):
    """Validate one datastore page and surface its records, total and _ids."""
    payload, failure = decode_government_payload(fetch)
    if failure:
        return failure
    result = payload["result"]
    records = result.get("records")
    if not isinstance(records, list):
        return _outcome("failed_missing_records", "records is not a list")

    record_ids = []
    for item in records:
        if not isinstance(item, dict):
            return _outcome("failed_record_not_object", "record is not an object")
        if "_id" not in item:
            return _outcome("failed_missing_record_id", "record without _id")
        record_ids.append(item["_id"])  # preserved raw, never renamed

    reported_total = result.get("total")
    if reported_total is not None and not isinstance(reported_total, int):
        try:
            reported_total = int(reported_total)
        except (TypeError, ValueError):
            return _outcome("failed_invalid_total", "total is not an integer")

    return _outcome(
        "passed" if records else "passed_no_records",
        "{0} records".format(len(records)),
        records=records,
        record_ids=record_ids,
        reported_total=reported_total,
    )


def _web_preconditions(fetch):
    """Shared Toyota checks: 200, HTTPS, allowed final host, plausible HTML."""
    if fetch["error"]:
        return _outcome("failed_request", fetch["error"])
    if fetch["http_status"] != 200:
        return _outcome("failed_http_status", "HTTP {0}".format(fetch["http_status"]))

    parsed = urllib.parse.urlsplit(fetch["final_url"])
    if parsed.scheme.lower() != "https":
        return _outcome("failed_not_https", "final URL is not HTTPS")
    host = (parsed.hostname or "").lower()
    if host not in TOYOTA_HOSTS:
        return _outcome("failed_final_host", "final host: {0}".format(host or "<empty>"))

    if not _content_type_matches(fetch["content_type"], HTML_CONTENT_TYPE_MARKERS):
        return _outcome("failed_content_type", fetch["content_type"] or "<missing>")

    body = fetch["body"]
    if len(body.strip()) < MIN_HTML_BYTES:
        return _outcome("failed_empty_body", "{0} bytes".format(len(body)))

    text = body.decode("utf-8", "replace").lower()
    if "<html" not in text and "<!doctype html" not in text:
        return _outcome("failed_implausible_html", "no html document element")

    for marker in TOYOTA_BLOCK_MARKERS:
        if marker in text:
            return _outcome("failed_blocking_page", "marker: {0}".format(marker))

    if not any(marker in text for marker in TOYOTA_BRAND_MARKERS):
        return _outcome("failed_missing_brand_identifier", "no Toyota identity in body")

    return None


def validate_toyota_archive_index(fetch):
    """The archive index establishes that the official archive section exists."""
    failure = _web_preconditions(fetch)
    if failure:
        return failure
    text = fetch["body"].decode("utf-8", "replace").lower()
    mentions_rav4 = any(marker in text for marker in TOYOTA_MODEL_MARKERS)
    return _outcome(
        RESULT_ARCHIVE_INDEX,
        "official archive index captured",
        mentions_rav4=mentions_rav4,
    )


def validate_toyota_archived_model(fetch):
    """Model identity and archived status only. No technical fact is inferred.

    Nothing is read from the URL or the filename: every marker below must be
    present in the saved response body.
    """
    failure = _web_preconditions(fetch)
    if failure:
        return failure

    text = fetch["body"].decode("utf-8", "replace").lower()

    if not any(marker in text for marker in TOYOTA_MODEL_MARKERS):
        return _outcome("failed_missing_model_identifier", "no RAV4 identity in body")

    plugin_markers = sorted({m for m in TOYOTA_PLUGIN_MARKERS if m in text})
    if not plugin_markers:
        return _outcome("failed_missing_plugin_identifier", "no Plug-in/PHEV identity in body")

    archive_markers = sorted({m for m in TOYOTA_ARCHIVE_MARKERS if m in text})
    if not archive_markers:
        return _outcome(
            "failed_missing_archive_indication",
            "no ended-marketing or archive indication in body",
        )

    return _outcome(
        RESULT_ARCHIVED_MODEL,
        "model identity and archived status corroborated",
        plugin_markers=plugin_markers,
        archive_markers=archive_markers,
    )


VALIDATORS = {
    SOURCE_TYPE_PACKAGE: validate_package_show,
    SOURCE_TYPE_SCHEMA: validate_resource_schema,
    SOURCE_TYPE_PAGE: validate_datastore_page,
    SOURCE_TYPE_WEB_INDEX: validate_toyota_archive_index,
    SOURCE_TYPE_WEB_MODEL: validate_toyota_archived_model,
}


def validate_fetch(source, fetch):
    return VALIDATORS[source.source_type](fetch)


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def page_raw_path(slug, query_index, page_number):
    """Primary query pages live in pages/; each alternate gets its own dir."""
    base = "raw/government/{0}".format(slug)
    if query_index == 0:
        folder = "{0}/pages".format(base)
    else:
        folder = "{0}/alt{1}/pages".format(base, query_index)
    return "{0}/page_{1:06d}.json".format(folder, page_number)


class PaginationResult(object):
    def __init__(self, query_token, query_index):
        self.query_token = query_token
        self.query_index = query_index
        self.entries = []          # manifest entries for each page
        self.records = []          # records in first-seen order
        self.record_pages = []     # parallel raw_path for each record
        self.reported_total = None
        self.failure = None        # sanitized failure code, or None

    @property
    def page_count(self):
        return len(self.entries)

    @property
    def captured(self):
        return len(self.records)

    @property
    def complete(self):
        return self.failure is None


def paginate_query(slug, resource_id, query_token, query_index, run_source):
    """Walk every page of one fixed targeted query.

    ``run_source`` executes a SourceSpec and returns (entry, outcome). Raises
    nothing: failures are recorded on the returned PaginationResult.
    """
    outcome_state = PaginationResult(query_token, query_index)
    offset = 0
    seen_offsets = set()
    seen_page_hashes = set()

    while True:
        page_number = outcome_state.page_count + 1
        if page_number > MAX_PAGES_PER_QUERY:
            outcome_state.failure = "page_limit_exceeded: max {0} pages".format(
                MAX_PAGES_PER_QUERY
            )
            return outcome_state

        if offset in seen_offsets:
            outcome_state.failure = "pagination_offset_repeated: offset={0}".format(offset)
            return outcome_state
        seen_offsets.add(offset)

        source = make_source(
            "government_{0}_q{1}_page_{2:06d}".format(slug, query_index, page_number),
            SOURCE_TYPE_PAGE,
            datastore_page_url(resource_id, query_token, PAGE_SIZE, offset),
            ACCEPT_JSON,
            page_raw_path(slug, query_index, page_number),
            resource_id=resource_id,
            query_token=query_token,
            offset=offset,
            limit=PAGE_SIZE,
        )
        entry, outcome, body_sha = run_source(source)
        outcome_state.entries.append(entry)

        if str(outcome["result"]).startswith("failed_"):
            outcome_state.failure = "page_validation_failed: {0}".format(outcome["result"])
            return outcome_state

        if body_sha in seen_page_hashes:
            outcome_state.failure = "pagination_repeated_page: identical body at offset {0}".format(
                offset
            )
            return outcome_state
        seen_page_hashes.add(body_sha)

        records = outcome.get("records") or []
        reported_total = outcome.get("reported_total")
        if reported_total is not None:
            if outcome_state.reported_total is None:
                outcome_state.reported_total = reported_total
            elif reported_total != outcome_state.reported_total:
                outcome_state.failure = (
                    "pagination_inconsistent_total: {0} then {1}".format(
                        outcome_state.reported_total, reported_total
                    )
                )
                return outcome_state

        for record in records:
            outcome_state.records.append(record)
            outcome_state.record_pages.append(source.raw_path)

        total = outcome_state.reported_total
        captured = outcome_state.captured

        if not records:
            # An empty page is only a legitimate stop when nothing is outstanding.
            if total is not None and captured < total:
                outcome_state.failure = (
                    "pagination_incomplete: captured {0} of reported {1}".format(captured, total)
                )
            return outcome_state

        if total is not None:
            if captured > total:
                outcome_state.failure = (
                    "pagination_inconsistent_total: captured {0} exceeds reported {1}".format(
                        captured, total
                    )
                )
                return outcome_state
            if captured == total:
                return outcome_state

        next_offset = offset + len(records)
        if next_offset <= offset:
            outcome_state.failure = "pagination_offset_not_advancing: offset={0}".format(offset)
            return outcome_state
        offset = next_offset


# --------------------------------------------------------------------------
# Capture orchestration
# --------------------------------------------------------------------------


def _build_entry(source, fetch, outcome):
    entry = {
        "source_id": source.source_id,
        "source_type": source.source_type,
        "requested_url": source.url,
        "query_params": fetch["query_params"],
        "started_utc": fetch["started_utc"],
        "finished_utc": fetch["finished_utc"],
        "http_status": fetch["http_status"],
        "redirect_chain": fetch["redirect_chain"],
        "final_url": fetch["final_url"],
        "headers": fetch["headers"],
        "content_type": fetch["content_type"],
        "declared_content_length": fetch["declared_content_length"],
        "byte_count": fetch["byte_count"],
        "sha256": fetch["sha256"],
        "attempts": fetch["attempts"],
        "retries": fetch["retries"],
        "retry_delays_seconds": fetch["retry_delays_seconds"],
        "validation_result": outcome["result"],
        "validation_detail": outcome["detail"],
        "error": fetch["error"],
        "raw_path": source.raw_path,
        "api_key_used": False,
        "credentials_used": False,
        "cookies_supplied": False,
    }
    if source.source_type == SOURCE_TYPE_PAGE:
        entry.update(
            {
                "resource_id": source.resource_id,
                "query_token": source.query_token,
                "requested_offset": source.offset,
                "requested_limit": source.limit,
                "returned_record_count": len(outcome.get("records") or []),
                "reported_total": outcome.get("reported_total"),
                "record_ids": outcome.get("record_ids"),
            }
        )
    elif source.source_type in (SOURCE_TYPE_SCHEMA,):
        entry["resource_id"] = source.resource_id
    elif source.source_type == SOURCE_TYPE_WEB_MODEL:
        entry["plugin_markers"] = outcome.get("plugin_markers")
        entry["archive_markers"] = outcome.get("archive_markers")
        entry["previous_obsolete_url"] = TOYOTA_PREVIOUS_OBSOLETE_URL
    elif source.source_type == SOURCE_TYPE_WEB_INDEX:
        entry["mentions_rav4"] = outcome.get("mentions_rav4")
    return entry


def build_derived_records(slug, resource_id, pagination_results):
    """Consolidate every captured record, deduplicating only by original _id."""
    seen = {}
    order = []
    duplicates = 0
    provenance = []

    for result in pagination_results:
        for index, record in enumerate(result.records):
            key = json.dumps(record["_id"], sort_keys=True, ensure_ascii=False)
            page_path = result.record_pages[index]
            if key in seen:
                duplicates += 1
                provenance[seen[key]]["also_seen_in"].append(
                    {"query_token": result.query_token, "raw_path": page_path}
                )
                continue
            seen[key] = len(order)
            order.append(record)
            provenance.append(
                {
                    "_id": record["_id"],
                    "query_token": result.query_token,
                    "raw_path": page_path,
                    "also_seen_in": [],
                }
            )

    return {
        "resource_id": resource_id,
        "resource_slug": slug,
        "queries": [
            {
                "query_token": r.query_token,
                "query_index": r.query_index,
                "page_count": r.page_count,
                "reported_total": r.reported_total,
                "captured_record_count": r.captured,
                "complete": r.complete,
                "failure": r.failure,
                "page_paths": [e["raw_path"] for e in r.entries],
            }
            for r in pagination_results
        ],
        "record_count": len(order),
        "duplicate_ids_collapsed": duplicates,
        "note": (
            "Records are the exact objects returned by CKAN, in first-seen order. "
            "Field names and values are preserved; nothing is normalized, renamed "
            "or inferred. Deduplication is by original _id only."
        ),
        "provenance": provenance,
        "records": order,
    }


def build_plan_preview():
    """The fixed URLs the capture will request, for display only."""
    preview = [(("package_show"), SOURCE_TYPE_PACKAGE, package_show_url())]
    for slug, resource_id in GOVERNMENT_RESOURCES:
        preview.append(
            ("{0}_schema".format(slug), SOURCE_TYPE_SCHEMA, resource_schema_url(resource_id))
        )
        preview.append(
            (
                "{0}_query".format(slug),
                SOURCE_TYPE_PAGE,
                datastore_page_url(resource_id, PRIMARY_QUERY_TOKEN, PAGE_SIZE, 0),
            )
        )
    preview.append(("toyota_archive_index", SOURCE_TYPE_WEB_INDEX, TOYOTA_ARCHIVE_INDEX_URL))
    preview.append(("toyota_rav4_phev", SOURCE_TYPE_WEB_MODEL, TOYOTA_RAV4_PHEV_URL))
    return preview


def run_capture(getter=None, clock=None, sleeper=None, progress=None,
                max_bytes=MAX_RESPONSE_BYTES, max_bundle_bytes=MAX_BUNDLE_BYTES):
    """Execute the fixed capture plan serially. Returns entries, files, summary."""
    clock = clock or _utc_now
    started = clock()

    entries = []
    files = {}
    state = {"bundle_bytes": 0, "over_budget": False, "completed": 0}

    def run_source(source):
        if progress is not None:
            progress(source.source_id, state["completed"])
        fetch = perform_get(
            source.url, source.accept, getter=getter, clock=clock,
            sleeper=sleeper, max_bytes=max_bytes,
        )
        outcome = validate_fetch(source, fetch)
        entry = _build_entry(source, fetch, outcome)
        entries.append(entry)
        files[source.raw_path] = fetch["body"]
        state["bundle_bytes"] += fetch["byte_count"]
        if state["bundle_bytes"] > max_bundle_bytes:
            state["over_budget"] = True
        state["completed"] += 1
        return entry, outcome, fetch["sha256"]

    # 1. Package metadata
    package_source = make_source(
        "government_package_show", SOURCE_TYPE_PACKAGE, package_show_url(),
        ACCEPT_JSON, "raw/government/package_show.json",
    )
    _entry, package_outcome, _sha = run_source(package_source)
    government_failures = []
    if str(package_outcome["result"]).startswith("failed_"):
        government_failures.append("package_show: " + package_outcome["result"])

    # 2. Schemas and 3. targeted paginated queries, per resource
    derived = {}
    total_records = 0
    for slug, resource_id in GOVERNMENT_RESOURCES:
        schema_source = make_source(
            "government_{0}_schema".format(slug), SOURCE_TYPE_SCHEMA,
            resource_schema_url(resource_id), ACCEPT_JSON,
            "raw/government/{0}/schema.json".format(slug), resource_id=resource_id,
        )
        _entry, schema_outcome, _sha = run_source(schema_source)
        if str(schema_outcome["result"]).startswith("failed_"):
            government_failures.append("{0}_schema: {1}".format(slug, schema_outcome["result"]))

        results = []
        primary = paginate_query(slug, resource_id, PRIMARY_QUERY_TOKEN, 0, run_source)
        results.append(primary)
        if primary.failure:
            government_failures.append("{0} q={1}: {2}".format(
                slug, PRIMARY_QUERY_TOKEN, primary.failure))
        elif primary.captured == 0:
            for index, token in enumerate(ALTERNATE_QUERY_TOKENS, start=1):
                alternate = paginate_query(slug, resource_id, token, index, run_source)
                results.append(alternate)
                if alternate.failure:
                    government_failures.append("{0} q={1}: {2}".format(
                        slug, token, alternate.failure))

        consolidated = build_derived_records(slug, resource_id, results)
        derived[slug] = consolidated
        total_records += consolidated["record_count"]
        files["derived/government/{0}_all_records.json".format(slug)] = _json_bytes(consolidated)

    # 4. Official Toyota pages
    index_source = make_source(
        "toyota_archive_index", SOURCE_TYPE_WEB_INDEX, TOYOTA_ARCHIVE_INDEX_URL,
        ACCEPT_HTML, "raw/toyota/archive-index.html",
    )
    _entry, index_outcome, _sha = run_source(index_source)

    model_source = make_source(
        "toyota_rav4_phev", SOURCE_TYPE_WEB_MODEL, TOYOTA_RAV4_PHEV_URL,
        ACCEPT_HTML, "raw/toyota/rav4-phev.html",
    )
    _entry, model_outcome, _sha = run_source(model_source)

    web_failures = []
    if index_outcome["result"] != RESULT_ARCHIVE_INDEX:
        web_failures.append("archive_index: " + index_outcome["result"])
    if model_outcome["result"] != RESULT_ARCHIVED_MODEL:
        web_failures.append("rav4_phev: " + model_outcome["result"])

    if state["over_budget"]:
        government_failures.append(
            "bundle_size_exceeded: over {0} bytes".format(max_bundle_bytes)
        )

    finished = clock()
    return {
        "capture_id": "milo-r5-source-capture-" + _stamp(started),
        "started": started,
        "started_utc": _iso(started),
        "finished_utc": _iso(finished),
        "entries": entries,
        "files": files,
        "derived": derived,
        "government_failures": government_failures,
        "web_failures": web_failures,
        "total_records": total_records,
        "bundle_bytes": state["bundle_bytes"],
    }


def determine_status(capture, integrity_problems=()):
    """Map a completed capture onto one of the four defined overall statuses."""
    if capture["government_failures"] or integrity_problems:
        return STATUS_FAILED
    if capture["total_records"] == 0:
        return STATUS_NO_RECORDS
    if capture["web_failures"]:
        return STATUS_INCOMPLETE_WEB
    return STATUS_READY


def archive_filename(capture_id, overall_status):
    stamp = capture_id.rsplit("-", 1)[-1]
    if overall_status == STATUS_READY:
        return "milo-r5-source-capture-{0}.zip".format(stamp)
    return "milo-r5-source-capture-INCOMPLETE-{0}.zip".format(stamp)


# --------------------------------------------------------------------------
# Manifest, checksums and archive
# --------------------------------------------------------------------------


def build_manifest(capture, overall_status, integrity_problems):
    return {
        "schema": "milo-r5-source-capture/2",
        "capture_id": capture["capture_id"],
        "tool": USER_AGENT,
        "user_agent": USER_AGENT,
        "started_utc": capture["started_utc"],
        "finished_utc": capture["finished_utc"],
        "overall_status": overall_status,
        "api_key_used": False,
        "credentials_used": False,
        "cookies_supplied": False,
        "authentication": "none; all sources are public read-only endpoints",
        "method": "GET",
        "sources": {
            "ckan_api_base": CKAN_API_BASE,
            "ckan_api_version": CKAN_API_VERSION,
            "ckan_package_id": CKAN_PACKAGE_ID,
            "wltp_resource_id": WLTP_RESOURCE_ID,
            "additional_resource_id": ADDITIONAL_RESOURCE_ID,
            "toyota_archive_index_url": TOYOTA_ARCHIVE_INDEX_URL,
            "toyota_rav4_phev_url": TOYOTA_RAV4_PHEV_URL,
            "previous_obsolete_url": TOYOTA_PREVIOUS_OBSOLETE_URL,
            "previous_obsolete_url_note": (
                "Returned HTTP 404 and was replaced by the official Toyota Israel "
                "archive pages above. It is recorded for provenance only and is "
                "never requested."
            ),
        },
        "limits": {
            "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
            "read_timeout_seconds": READ_TIMEOUT_SECONDS,
            "max_attempts": MAX_ATTEMPTS,
            "max_redirects": MAX_REDIRECTS,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "max_bundle_bytes": MAX_BUNDLE_BYTES,
            "page_size": PAGE_SIZE,
            "max_pages_per_query": MAX_PAGES_PER_QUERY,
            "retry_statuses": sorted(RETRY_STATUSES),
            "backoff_base_seconds": BACKOFF_BASE_SECONDS,
            "backoff_max_seconds": BACKOFF_MAX_SECONDS,
            "max_retry_after_seconds": MAX_RETRY_AFTER_SECONDS,
            "allowed_hosts": sorted(ALLOWED_HOSTS),
            "preserved_headers": list(PRESERVED_HEADERS),
        },
        "totals": {
            "request_count": len(capture["entries"]),
            "government_record_count": capture["total_records"],
            "raw_bundle_bytes": capture["bundle_bytes"],
        },
        "government_failures": list(capture["government_failures"]),
        "web_failures": list(capture["web_failures"]),
        "integrity_problems": list(integrity_problems),
        "entries": capture["entries"],
    }


def verify_raw_files(entries, files):
    """Recalculate byte counts and hashes and compare against the entries."""
    problems = []
    for entry in entries:
        path = entry["raw_path"]
        data = files.get(path)
        if data is None:
            problems.append("missing raw file: {0}".format(path))
            continue
        if len(data) != entry["byte_count"]:
            problems.append("byte count mismatch: {0}".format(path))
        if sha256_hex(data) != entry["sha256"]:
            problems.append("sha256 mismatch: {0}".format(path))
    return problems


def build_checksums(files):
    """Deterministic SHA256SUMS body covering every file except itself."""
    lines = []
    for name in sorted(files):
        if name == "SHA256SUMS.txt":
            continue
        lines.append("{0}  {1}\n".format(sha256_hex(files[name]), name))
    return "".join(lines).encode("utf-8")


def verify_checksums(sums_bytes, files):
    problems = []
    listed = set()
    for line in sums_bytes.decode("utf-8").splitlines():
        digest, _, name = line.partition("  ")
        listed.add(name)
        if name not in files:
            problems.append("SHA256SUMS references missing file: {0}".format(name))
        elif sha256_hex(files[name]) != digest:
            problems.append("SHA256SUMS digest mismatch: {0}".format(name))
    for name in files:
        if name != "SHA256SUMS.txt" and name not in listed:
            problems.append("SHA256SUMS omits file: {0}".format(name))
    return problems


def _readme_text(capture, overall_status, manifest_sha256):
    derived_lines = []
    for slug, document in sorted(capture["derived"].items()):
        for query in document["queries"]:
            derived_lines.append(
                "  {0:<12} q={1:<22} pages={2:<4} reported_total={3:<8} captured={4}".format(
                    slug, query["query_token"], query["page_count"],
                    str(query["reported_total"]), query["captured_record_count"],
                )
            )
    return (
        "MILO R5 source capture\n"
        "======================\n\n"
        "Capture id     : {capture_id}\n"
        "Overall status : {status}\n"
        "Requests       : {requests}\n"
        "Gov records    : {records}\n"
        "manifest.json  : sha256 {manifest_sha}\n\n"
        "What this archive is\n"
        "--------------------\n"
        "Byte-exact bodies of a fixed, bounded set of public read-only HTTP GET\n"
        "requests, captured server-side by the MILO R5 Streamlit capture app.\n"
        "Government JSON and Toyota HTML under raw/ are stored exactly as received.\n"
        "Nothing there was pretty-printed, normalized, renamed or rewritten.\n\n"
        "raw/ versus derived/\n"
        "--------------------\n"
        "raw/      Exact response bytes, one file per HTTP request. This is the\n"
        "          evidence. Every hash in manifest.json and SHA256SUMS.txt is\n"
        "          taken over these bytes.\n"
        "derived/  Deterministic consolidations built by this tool from the raw\n"
        "          pages: all records for one resource, deduplicated by original\n"
        "          _id only, in first-seen order, with per-record provenance\n"
        "          naming the query and raw page it came from. Field names and\n"
        "          values are preserved verbatim. No vehicle fact is invented,\n"
        "          normalized or inferred.\n\n"
        "Pagination\n"
        "----------\n"
        "{pagination}\n\n"
        "Authentication\n"
        "--------------\n"
        "None. Every source is a public endpoint. No API key, access token,\n"
        "username, password, cookie or Authorization header was used or stored.\n\n"
        "Preserved headers\n"
        "-----------------\n"
        "Only these response headers are retained: {headers}.\n"
        "Set-Cookie, Cookie, Authorization and Proxy-Authorization are never\n"
        "retained, and no cookie is ever replayed between requests.\n\n"
        "Verifying this archive\n"
        "----------------------\n"
        "From the archive root directory:\n\n"
        "    sha256sum -c SHA256SUMS.txt\n\n"
        "SHA256SUMS.txt covers manifest.json, README.txt and every raw and derived\n"
        "file. It cannot list its own digest; compare manifest.json against the\n"
        "sha256 printed above instead.\n\n"
        "Toyota evidence: what it does and does not establish\n"
        "---------------------------------------------------\n"
        "The previous URL {obsolete}\n"
        "returned HTTP 404 and was replaced by the official Toyota Israel archive\n"
        "pages. A result of\n"
        "'{model_result}'\n"
        "means the saved HTML corroborates MODEL IDENTITY (Toyota, RAV4, Plug-in /\n"
        "PHEV) and ARCHIVED STATUS (marketing of the model has ended). It is NOT a\n"
        "technical specification and NOT a vehicle fact. No specification value was\n"
        "extracted, and nothing was inferred from the URL or the filename.\n"
        "Government CKAN data under raw/government/ remains the structured\n"
        "technical source. A human must read the preserved HTML to confirm any\n"
        "claim about the model.\n\n"
        "Status meanings\n"
        "---------------\n"
        "{ready}\n"
        "    All mandatory Government requests passed, targeted Government records\n"
        "    are present, both official Toyota pages passed, and integrity verified.\n"
        "{no_records}\n"
        "    Government requests were valid but the targeted searches found no\n"
        "    RAV4 candidates.\n"
        "{incomplete_web}\n"
        "    Government capture succeeded but a Toyota page was unavailable or\n"
        "    failed validation.\n"
        "{failed}\n"
        "    A network, HTTP, redirect, size, JSON, pagination or integrity failure\n"
        "    prevented a valid capture.\n".format(
            capture_id=capture["capture_id"],
            status=overall_status,
            requests=len(capture["entries"]),
            records=capture["total_records"],
            manifest_sha=manifest_sha256,
            pagination="\n".join(derived_lines) if derived_lines else "  (none captured)",
            headers=", ".join(PRESERVED_HEADERS),
            obsolete=TOYOTA_PREVIOUS_OBSOLETE_URL,
            model_result=RESULT_ARCHIVED_MODEL,
            ready=STATUS_READY,
            no_records=STATUS_NO_RECORDS,
            incomplete_web=STATUS_INCOMPLETE_WEB,
            failed=STATUS_FAILED,
        )
    )


def _archive_order(names):
    head = [n for n in ("manifest.json", "README.txt", "SHA256SUMS.txt") if n in names]
    rest = sorted(n for n in names if n not in head)
    return head + rest


def _build_zip(capture_id, files, moment):
    buffer = io.BytesIO()
    date_time = moment.astimezone(datetime.timezone.utc).timetuple()[:6]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in _archive_order(files):
            info = zipfile.ZipInfo("{0}/{1}".format(capture_id, name), date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[name])
    return buffer.getvalue()


def package_capture(capture):
    """Verify integrity, then build the ZIP from the verified bytes.

    Order: recalculate raw byte counts and hashes -> verify against
    manifest.json -> hash the final manifest.json -> generate and verify
    SHA256SUMS.txt -> build the ZIP. Fails closed on any mismatch.
    """
    entries = capture["entries"]
    raw_files = capture["files"]

    integrity_problems = verify_raw_files(entries, raw_files)
    overall_status = determine_status(capture, integrity_problems)

    manifest = build_manifest(capture, overall_status, integrity_problems)
    manifest_bytes = _json_bytes(manifest)

    # Verify the recalculated digests against the serialized manifest.json.
    round_trip = verify_raw_files(
        json.loads(manifest_bytes.decode("utf-8"))["entries"], raw_files
    )
    if round_trip and overall_status != STATUS_FAILED:
        integrity_problems = integrity_problems + round_trip
        overall_status = STATUS_FAILED
        manifest = build_manifest(capture, overall_status, integrity_problems)
        manifest_bytes = _json_bytes(manifest)
        round_trip = verify_raw_files(
            json.loads(manifest_bytes.decode("utf-8"))["entries"], raw_files
        )
    for problem in round_trip:
        if problem not in integrity_problems:
            integrity_problems = integrity_problems + [problem]

    manifest_sha256 = sha256_hex(manifest_bytes)

    files = dict(raw_files)
    files["manifest.json"] = manifest_bytes
    files["README.txt"] = _readme_text(capture, overall_status, manifest_sha256).encode("utf-8")

    sums_bytes = build_checksums(files)
    sums_problems = verify_checksums(sums_bytes, files)
    if sums_problems:
        integrity_problems = integrity_problems + sums_problems
        overall_status = STATUS_FAILED
    files["SHA256SUMS.txt"] = sums_bytes

    archive_bytes = _build_zip(capture["capture_id"], files, capture["started"])

    return {
        "capture_id": capture["capture_id"],
        "overall_status": overall_status,
        "entries": entries,
        "derived": capture["derived"],
        "government_failures": capture["government_failures"],
        "web_failures": capture["web_failures"],
        "total_records": capture["total_records"],
        "archive_filename": archive_filename(capture["capture_id"], overall_status),
        "archive_bytes": archive_bytes,
        "archive_byte_count": len(archive_bytes),
        "archive_sha256": sha256_hex(archive_bytes),
        "manifest_sha256": manifest_sha256,
        "integrity_problems": integrity_problems,
        "integrity_verified": not integrity_problems,
        "started_utc": capture["started_utc"],
        "finished_utc": capture["finished_utc"],
    }


def run_and_package(getter=None, clock=None, sleeper=None, progress=None,
                    max_bytes=MAX_RESPONSE_BYTES, max_bundle_bytes=MAX_BUNDLE_BYTES):
    """Full capture run. Always returns a downloadable archive, even on failure."""
    capture = run_capture(
        getter=getter, clock=clock, sleeper=sleeper, progress=progress,
        max_bytes=max_bytes, max_bundle_bytes=max_bundle_bytes,
    )
    return package_capture(capture)
