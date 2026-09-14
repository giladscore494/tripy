"""Bounded, read-only public source capture for MILO R5.

This module performs a small, fixed set of public HTTP GET requests against
Israeli Government CKAN endpoints and one official Toyota product page, then
packages the byte-exact response bodies into a verifiable evidence archive.

Design constraints enforced here:

* Every URL is built from the fixed constants below. There is no code path
  that fetches a caller-supplied or user-supplied URL.
* GET only. No POST/PUT/PATCH/DELETE, no ``requests.Session``.
* No credentials, API keys, tokens, cookies or secrets are read, stored or
  transmitted. The Government CKAN endpoints and the Toyota page are public.
* Responses are preserved as exact bytes; they are never reformatted.
* Only an allowlist of response headers is retained. ``Set-Cookie`` and every
  other header is discarded.
"""

import datetime
import hashlib
import io
import json
import urllib.parse
import zipfile
from collections import namedtuple

# --------------------------------------------------------------------------
# Fixed public connection identifiers (public resource IDs, not secrets)
# --------------------------------------------------------------------------

CKAN_API_BASE = "https://data.gov.il/api/3/action"
CKAN_API_VERSION = 3
CKAN_PACKAGE_ID = "degem-rechev-wltp"
WLTP_RESOURCE_ID = "142afde2-6228-49f9-8a29-9b6c3a0cbe40"
ADDITIONAL_RESOURCE_ID = "5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6"
TOYOTA_URL = "https://www.toyota.co.il/models/rav4-plugin"

# --------------------------------------------------------------------------
# Request policy
# --------------------------------------------------------------------------

USER_AGENT = "MILO-R5-streamlit-evidence-capture/1.0"
ACCEPT_JSON = "application/json"
ACCEPT_HTML = "text/html,application/xhtml+xml"
ACCEPT_ENCODING = "identity"

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 45
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 15 * 1024 * 1024
QUERY_LIMIT = 100
SCHEMA_LIMIT = 0

RETRY_STATUSES = frozenset({429})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

ALLOWED_HOSTS = frozenset(
    {"data.gov.il", "www.data.gov.il", "toyota.co.il", "www.toyota.co.il"}
)

PRESERVED_HEADERS = (
    "Date",
    "Content-Type",
    "Content-Length",
    "Content-Encoding",
    "ETag",
    "Last-Modified",
    "Location",
    "Cache-Control",
)

# Percent-encoded exactly as required; these strings go into the URL verbatim.
PRIMARY_QUERY_TOKEN = "RAV4"
ALTERNATE_QUERY_TOKENS = ("RAV%204", "%D7%A8%D7%90%D7%91")

GOVERNMENT_RESOURCES = (
    ("142afde2", WLTP_RESOURCE_ID),
    ("5e87a7a1", ADDITIONAL_RESOURCE_ID),
)

SOURCE_TYPE_PACKAGE = "government_package_metadata"
SOURCE_TYPE_SCHEMA = "government_resource_schema"
SOURCE_TYPE_QUERY = "government_resource_query"
SOURCE_TYPE_WEB = "official_web_page"

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

TOYOTA_BRAND_MARKERS = ("toyota",)
TOYOTA_MODEL_MARKERS = ("rav4", "rav 4", "rav-4")

TOYOTA_BLOCK_MARKERS = (
    "access denied",
    "captcha",
    "are you a human",
    "attention required",
    "request blocked",
    "web application firewall",
    "incapsula incident",
    "cloudflare ray id",
    "error 403",
    "http 403",
    "404 not found",
    "page not found",
)

TOYOTA_TECHNICAL_MARKERS = (
    "wltp",
    "kwh",
    "co2",
    "מפרט",                      # specification
    "נתונים טכניים",  # technical data
    "סוללה",                 # battery
    "טווח",                      # range
    "צריכת דלק",  # fuel consumption
    "מנוע",                      # engine
    "הספק",                      # power output
    "תאוצה",                 # acceleration
    "זיהום",                 # pollution
)
MIN_TOYOTA_TECHNICAL_MARKERS = 2

TOYOTA_RESULT_CANDIDATE = "passed_candidate_requires_human_fact_check"
TOYOTA_RESULT_INSUFFICIENT = "insufficient_official_content"

Source = namedtuple("Source", "source_id source_type url accept body_filename query_token")


class _CaptureFailure(Exception):
    """Internal signal carrying a sanitized failure code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.attempts = 0
        self.status = None

    def sanitized(self):
        return self.code if not self.detail else "{0}: {1}".format(self.code, self.detail)


# --------------------------------------------------------------------------
# URL construction
# --------------------------------------------------------------------------


def package_show_url():
    """Public CKAN package metadata URL."""
    return "{0}/package_show?id={1}".format(CKAN_API_BASE, CKAN_PACKAGE_ID)


def resource_schema_url(resource_id):
    """Bounded schema probe: limit=0 returns field definitions, never rows."""
    return "{0}/datastore_search?resource_id={1}&limit={2}".format(
        CKAN_API_BASE, resource_id, SCHEMA_LIMIT
    )


def resource_query_url(resource_id, encoded_query_token):
    """Bounded record query. ``encoded_query_token`` is already percent-encoded."""
    return "{0}/datastore_search?resource_id={1}&limit={2}&q={3}".format(
        CKAN_API_BASE, resource_id, QUERY_LIMIT, encoded_query_token
    )


def headers_filename_for(body_filename):
    base = body_filename.rsplit(".", 1)[0]
    return base + ".headers.json"


def build_base_plan():
    """The fixed mandatory source plan, in evidence order."""
    plan = [
        Source(
            "package_show_degem_rechev_wltp",
            SOURCE_TYPE_PACKAGE,
            package_show_url(),
            ACCEPT_JSON,
            "government/package_show_degem_rechev_wltp.json",
            None,
        )
    ]
    for slug, resource_id in GOVERNMENT_RESOURCES:
        plan.append(
            Source(
                "resource_{0}_schema".format(slug),
                SOURCE_TYPE_SCHEMA,
                resource_schema_url(resource_id),
                ACCEPT_JSON,
                "government/resource_{0}_schema.json".format(slug),
                None,
            )
        )
        plan.append(
            Source(
                "resource_{0}_rav4".format(slug),
                SOURCE_TYPE_QUERY,
                resource_query_url(resource_id, PRIMARY_QUERY_TOKEN),
                ACCEPT_JSON,
                "government/resource_{0}_rav4.json".format(slug),
                PRIMARY_QUERY_TOKEN,
            )
        )
    plan.append(
        Source(
            "toyota_rav4_plugin",
            SOURCE_TYPE_WEB,
            TOYOTA_URL,
            ACCEPT_HTML,
            "web/toyota_rav4_plugin.html",
            None,
        )
    )
    return plan


def build_alternate_sources(slug, resource_id):
    """The two extra bounded queries used only when a valid query returns zero rows."""
    alternates = []
    for index, token in enumerate(ALTERNATE_QUERY_TOKENS, start=1):
        alternates.append(
            Source(
                "resource_{0}_rav4_alt{1}".format(slug, index),
                SOURCE_TYPE_QUERY,
                resource_query_url(resource_id, token),
                ACCEPT_JSON,
                "government/resource_{0}_rav4_alt{1}.json".format(slug, index),
                token,
            )
        )
    return alternates


# --------------------------------------------------------------------------
# Low level HTTP (GET only, no Session, manual redirects, bounded body)
# --------------------------------------------------------------------------


def _request_headers(accept):
    """Fresh header dict per request. No Authorization, no Cookie, ever."""
    return {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Encoding": ACCEPT_ENCODING,
    }


def _requests_get(url, headers, timeout):
    """Single bounded GET. Imported lazily so tests never need the dependency."""
    import requests

    return requests.get(
        url,
        headers=headers,
        timeout=timeout,
        allow_redirects=False,
        stream=True,
        cookies={},
    )


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _stamp(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256_hex(payload):
    return hashlib.sha256(payload).hexdigest()


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
    """Keep only allowlisted headers. Set-Cookie and friends are dropped."""
    kept = {}
    for name in PRESERVED_HEADERS:
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


def _require_allowed_url(url, redirect=False):
    prefix = "redirect_" if redirect else ""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise _CaptureFailure(prefix + "not_https", "scheme=" + (parsed.scheme or "<empty>"))
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise _CaptureFailure(prefix + "host_not_allowed", "host=" + (host or "<empty>"))


def _read_bounded_body(response, max_bytes):
    declared = _header_value(response.headers, "Content-Length")
    if declared:
        try:
            declared_length = int(declared)
        except (TypeError, ValueError):
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            _close(response)
            raise _CaptureFailure("response_too_large", "content-length={0}".format(declared_length))

    chunks = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise _CaptureFailure(
                    "response_too_large", "exceeded {0} bytes".format(max_bytes)
                )
            chunks.append(chunk)
    finally:
        _close(response)
    return b"".join(chunks)


def _get_with_retries(url, accept, getter):
    """Up to MAX_ATTEMPTS GETs. Retries transport errors, 429 and 5xx only."""
    attempts = 0
    failure = None
    while attempts < MAX_ATTEMPTS:
        attempts += 1
        try:
            response = getter(
                url,
                _request_headers(accept),
                (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            )
        except Exception as exc:  # transport level only
            failure = _CaptureFailure("transport_error", _sanitize_exception(exc))
            continue
        status = int(response.status_code)
        if status in RETRY_STATUSES or 500 <= status <= 599:
            _close(response)
            failure = _CaptureFailure("retryable_http_status", "HTTP {0}".format(status))
            failure.status = status
            continue
        return response, attempts
    failure.attempts = attempts
    raise failure


def perform_get(url, accept, getter=None, max_bytes=MAX_RESPONSE_BYTES, clock=None):
    """Fetch one fixed URL with the full bounded policy applied.

    Never raises: transport, HTTP, redirect and size failures are returned as a
    sanitized ``error`` code on the result dict.
    """
    getter = getter or _requests_get
    clock = clock or _utc_now

    started = clock()
    redirect_chain = []
    attempts = 0
    status = None
    headers = {}
    body = b""
    error = None
    current_url = url
    final_url = url

    try:
        while True:
            final_url = current_url
            _require_allowed_url(current_url, redirect=bool(redirect_chain))
            response, used = _get_with_retries(current_url, accept, getter)
            attempts += used
            status = int(response.status_code)
            headers = sanitize_headers(response.headers)

            if status in REDIRECT_STATUSES:
                location = _header_value(response.headers, "Location")
                _close(response)
                if not location or not location.strip():
                    raise _CaptureFailure("redirect_missing_location")
                target = urllib.parse.urljoin(current_url, location.strip())
                redirect_chain.append(
                    {"status": status, "from": current_url, "to": target}
                )
                if len(redirect_chain) > MAX_REDIRECTS:
                    raise _CaptureFailure(
                        "too_many_redirects", "limit={0}".format(MAX_REDIRECTS)
                    )
                # A fresh header dict is built for the next hop, so no cookie
                # received from this response is ever replayed onward.
                current_url = target
                continue

            body = _read_bounded_body(response, max_bytes)
            break
    except _CaptureFailure as exc:
        error = exc.sanitized()
        attempts += exc.attempts
        if status is None:
            status = exc.status
        body = b""

    finished = clock()
    return {
        "requested_url": url,
        "final_url": final_url,
        "redirect_chain": redirect_chain,
        "started_utc": _iso(started),
        "finished_utc": _iso(finished),
        "http_status": status,
        "headers": headers,
        "body": body,
        "byte_count": len(body),
        "sha256": sha256_hex(body),
        "attempts": attempts,
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


def validate_government(fetch, source_type):
    """Validate a CKAN response without normalizing or renaming any field."""
    if fetch["error"]:
        return _outcome("failed_request", fetch["error"])

    status = fetch["http_status"]
    if status is None or not 200 <= status < 300:
        return _outcome("failed_http_status", "HTTP {0}".format(status))

    content_type = fetch["headers"].get("Content-Type", "")
    if not _content_type_matches(content_type, JSON_CONTENT_TYPE_MARKERS):
        return _outcome("failed_content_type", content_type or "<missing>")

    body = fetch["body"]
    if not body.strip():
        return _outcome("failed_empty_body", "0 usable bytes")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return _outcome("failed_invalid_json", _sanitize_exception(exc))

    if not isinstance(payload, dict):
        return _outcome("failed_invalid_json", "top level is not a JSON object")
    if payload.get("success") is not True:
        return _outcome("failed_success_flag", "success is not true")

    result = payload.get("result")
    if not isinstance(result, dict):
        return _outcome("failed_missing_result", "result object absent")

    if source_type == SOURCE_TYPE_PACKAGE:
        resources = result.get("resources")
        if not isinstance(resources, list) or not resources:
            return _outcome("failed_missing_resources", "no resources in package")
        return _outcome("passed", "{0} resources".format(len(resources)))

    if source_type == SOURCE_TYPE_SCHEMA:
        fields = result.get("fields")
        if not isinstance(fields, list) or not fields:
            return _outcome("failed_missing_fields", "no fields in schema")
        return _outcome("passed", "{0} fields".format(len(fields)))

    records = result.get("records")
    if not isinstance(records, list):
        return _outcome("failed_missing_records", "records array absent")

    record_ids = []
    for item in records:
        if not isinstance(item, dict) or "_id" not in item:
            return _outcome("failed_missing_record_id", "record without _id")
        record_ids.append(item["_id"])  # preserved raw, never renamed

    if not records:
        return _outcome("passed_no_records", "0 records", record_count=0, record_ids=[])
    return _outcome(
        "passed",
        "{0} records".format(len(records)),
        record_count=len(records),
        record_ids=record_ids,
    )


def validate_toyota(fetch):
    """Validate the official page. No vehicle fact is inferred from URL or name."""
    if fetch["error"]:
        return _outcome("failed_request", fetch["error"])

    status = fetch["http_status"]
    if status is None or not 200 <= status < 300:
        return _outcome("failed_http_status", "HTTP {0}".format(status))

    content_type = fetch["headers"].get("Content-Type", "")
    if not _content_type_matches(content_type, HTML_CONTENT_TYPE_MARKERS):
        return _outcome("failed_content_type", content_type or "<missing>")

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
        return _outcome("failed_missing_brand_identifier", "no Toyota identifier in body")
    if not any(marker in text for marker in TOYOTA_MODEL_MARKERS):
        return _outcome("failed_missing_model_identifier", "no RAV4 identifier in body")

    markers = sorted({m for m in TOYOTA_TECHNICAL_MARKERS if m in text})
    if len(markers) < MIN_TOYOTA_TECHNICAL_MARKERS:
        return _outcome(
            TOYOTA_RESULT_INSUFFICIENT,
            "{0} technical markers found".format(len(markers)),
            technical_markers=markers,
        )
    return _outcome(
        TOYOTA_RESULT_CANDIDATE,
        "{0} technical markers found".format(len(markers)),
        technical_markers=markers,
    )


def validate_fetch(source, fetch):
    if source.source_type == SOURCE_TYPE_WEB:
        return validate_toyota(fetch)
    return validate_government(fetch, source.source_type)


# --------------------------------------------------------------------------
# Capture orchestration
# --------------------------------------------------------------------------


def _build_entry(source, fetch, validation):
    entry = {
        "source_id": source.source_id,
        "source_type": source.source_type,
        "requested_url": source.url,
        "final_url": fetch["final_url"],
        "redirect_chain": fetch["redirect_chain"],
        "started_utc": fetch["started_utc"],
        "finished_utc": fetch["finished_utc"],
        "http_status": fetch["http_status"],
        "headers": fetch["headers"],
        "body_filename": source.body_filename,
        "headers_filename": headers_filename_for(source.body_filename),
        "byte_count": fetch["byte_count"],
        "sha256": fetch["sha256"],
        "attempts": fetch["attempts"],
        "credentials_used": False,
        "api_key_used": False,
        "cookies_supplied": False,
        "validation_result": validation["result"],
        "validation_detail": validation["detail"],
        "error": fetch["error"],
        "query_token": source.query_token,
        "record_count": validation.get("record_count"),
        "record_ids": validation.get("record_ids"),
    }
    if source.source_type == SOURCE_TYPE_WEB:
        entry["technical_markers"] = validation.get("technical_markers")
    return entry


def _headers_document(entry):
    document = {
        "source_id": entry["source_id"],
        "requested_url": entry["requested_url"],
        "final_url": entry["final_url"],
        "http_status": entry["http_status"],
        "redirect_chain": entry["redirect_chain"],
        "body_filename": entry["body_filename"],
        "preserved_headers": entry["headers"],
        "note": (
            "Only allowlisted response headers are preserved. Set-Cookie, "
            "Authorization and all other headers are discarded at capture time."
        ),
    }
    return _json_bytes(document)


def _json_bytes(document):
    text = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False, allow_nan=False)
    return (text + "\n").encode("utf-8")


def run_capture(getter=None, clock=None, progress=None, max_bytes=MAX_RESPONSE_BYTES):
    """Execute the fixed capture plan serially and return entries plus bodies."""
    clock = clock or _utc_now
    started = clock()

    entries = []
    files = {}
    state = {"completed": 0, "planned": len(build_base_plan())}

    def execute(source):
        if progress is not None:
            progress(source.source_id, state["completed"], state["planned"])
        fetch = perform_get(
            source.url, source.accept, getter=getter, max_bytes=max_bytes, clock=clock
        )
        validation = validate_fetch(source, fetch)
        entry = _build_entry(source, fetch, validation)
        entries.append(entry)
        files[source.body_filename] = fetch["body"]
        files[entry["headers_filename"]] = _headers_document(entry)
        state["completed"] += 1
        return entry

    plan = build_base_plan()
    by_id = {source.source_id: source for source in plan}

    execute(by_id["package_show_degem_rechev_wltp"])
    for slug, resource_id in GOVERNMENT_RESOURCES:
        execute(by_id["resource_{0}_schema".format(slug)])
        primary = execute(by_id["resource_{0}_rav4".format(slug)])
        if primary["validation_result"] == "passed_no_records":
            alternates = build_alternate_sources(slug, resource_id)
            state["planned"] += len(alternates)
            for alternate in alternates:
                execute(alternate)
    execute(by_id["toyota_rav4_plugin"])

    finished = clock()
    return {
        "capture_id": "milo-r5-source-capture-" + _stamp(started),
        "started": started,
        "started_utc": _iso(started),
        "finished_utc": _iso(finished),
        "entries": entries,
        "files": files,
    }


def determine_status(entries):
    """Map validated entries onto one of the four defined overall statuses."""
    if any(str(entry["validation_result"]).startswith("failed_") for entry in entries):
        return STATUS_FAILED

    queries = [e for e in entries if e["source_type"] == SOURCE_TYPE_QUERY]
    total_records = sum(int(e.get("record_count") or 0) for e in queries)
    if total_records == 0:
        return STATUS_NO_RECORDS

    web = [e for e in entries if e["source_type"] == SOURCE_TYPE_WEB]
    if any(e["validation_result"] == TOYOTA_RESULT_INSUFFICIENT for e in web):
        return STATUS_INCOMPLETE_WEB

    return STATUS_READY


def archive_filename(capture_id, overall_status):
    stamp = capture_id.rsplit("-", 1)[-1]
    if overall_status == STATUS_READY:
        return "milo-r5-source-capture-{0}.zip".format(stamp)
    return "milo-r5-source-capture-INCOMPLETE-{0}.zip".format(stamp)


def build_manifest(capture, overall_status, integrity_problems):
    return {
        "schema": "milo-r5-source-capture/1",
        "capture_id": capture["capture_id"],
        "tool": USER_AGENT,
        "user_agent": USER_AGENT,
        "started_utc": capture["started_utc"],
        "finished_utc": capture["finished_utc"],
        "overall_status": overall_status,
        "credentials_used": False,
        "api_key_used": False,
        "cookies_supplied": False,
        "authentication": "none; all sources are public read-only endpoints",
        "method": "GET",
        "constants": {
            "ckan_api_base": CKAN_API_BASE,
            "ckan_api_version": CKAN_API_VERSION,
            "ckan_package_id": CKAN_PACKAGE_ID,
            "wltp_resource_id": WLTP_RESOURCE_ID,
            "additional_resource_id": ADDITIONAL_RESOURCE_ID,
            "toyota_url": TOYOTA_URL,
        },
        "limits": {
            "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS,
            "read_timeout_seconds": READ_TIMEOUT_SECONDS,
            "max_attempts": MAX_ATTEMPTS,
            "max_redirects": MAX_REDIRECTS,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "query_limit": QUERY_LIMIT,
            "allowed_hosts": sorted(ALLOWED_HOSTS),
            "preserved_headers": list(PRESERVED_HEADERS),
        },
        "integrity_problems": list(integrity_problems),
        "entries": capture["entries"],
    }


def _verify_bodies(entries, files):
    """Recalculate byte counts and hashes and compare against the entries."""
    problems = []
    for entry in entries:
        name = entry["body_filename"]
        data = files.get(name)
        if data is None:
            problems.append("missing body file: {0}".format(name))
            continue
        if len(data) != entry["byte_count"]:
            problems.append("byte count mismatch: {0}".format(name))
        if sha256_hex(data) != entry["sha256"]:
            problems.append("sha256 mismatch: {0}".format(name))
    return problems


def _readme_text(capture_id, overall_status, manifest_sha256, entry_count):
    return (
        "MILO R5 source capture\n"
        "======================\n\n"
        "Capture id     : {capture_id}\n"
        "Overall status : {status}\n"
        "Entries        : {entries}\n"
        "manifest.json  : sha256 {manifest_sha}\n\n"
        "What this archive is\n"
        "--------------------\n"
        "Byte-exact bodies of a fixed, bounded set of public read-only HTTP GET\n"
        "requests, captured server-side by the MILO R5 Streamlit capture app.\n"
        "Government JSON and Toyota HTML are stored exactly as received. Nothing\n"
        "was pretty-printed, normalized, renamed or otherwise rewritten.\n\n"
        "Authentication\n"
        "--------------\n"
        "None. Every source is a public endpoint. No API key, access token,\n"
        "username, password, cookie or Authorization header was used or stored.\n\n"
        "Preserved headers\n"
        "-----------------\n"
        "Only these response headers are retained: {headers}.\n"
        "Set-Cookie and every other response header was discarded at capture time.\n\n"
        "Verifying this archive\n"
        "----------------------\n"
        "From the archive root directory:\n\n"
        "    sha256sum -c SHA256SUMS.txt\n\n"
        "SHA256SUMS.txt covers manifest.json, README.txt and every captured body\n"
        "and header file. It cannot list its own digest; compare manifest.json\n"
        "against the sha256 printed above instead.\n\n"
        "Status meanings\n"
        "---------------\n"
        "{ready}\n"
        "    All mandatory sources passed, at least one Government query returned\n"
        "    records, and the Toyota HTML contains technical markers.\n"
        "{no_records}\n"
        "    All Government calls were valid but every bounded query returned zero\n"
        "    records.\n"
        "{incomplete_web}\n"
        "    Government evidence passed but the Toyota HTML lacked usable technical\n"
        "    content.\n"
        "{failed}\n"
        "    A network, HTTP, redirect, size, JSON, blocking-page or integrity\n"
        "    failure occurred.\n\n"
        "Human fact-check required\n"
        "-------------------------\n"
        "A Toyota result of '{candidate}' means technical markers\n"
        "are present in the saved HTML. It is not a vehicle fact. No specification\n"
        "value was extracted, and nothing was inferred from the URL or filename.\n"
        "A human must read the preserved HTML to establish any vehicle fact.\n".format(
            capture_id=capture_id,
            status=overall_status,
            entries=entry_count,
            manifest_sha=manifest_sha256,
            headers=", ".join(PRESERVED_HEADERS),
            ready=STATUS_READY,
            no_records=STATUS_NO_RECORDS,
            incomplete_web=STATUS_INCOMPLETE_WEB,
            failed=STATUS_FAILED,
            candidate=TOYOTA_RESULT_CANDIDATE,
        )
    )


def _archive_order(names):
    head = [n for n in ("manifest.json", "SHA256SUMS.txt", "README.txt") if n in names]
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

    Order: recalculate body hashes -> verify against manifest.json -> hash the
    final manifest.json -> generate and verify SHA256SUMS.txt -> build the ZIP.
    """
    entries = capture["entries"]
    bodies = capture["files"]

    integrity_problems = _verify_bodies(entries, bodies)
    overall_status = determine_status(entries)
    if integrity_problems:
        overall_status = STATUS_FAILED

    manifest = build_manifest(capture, overall_status, integrity_problems)
    manifest_bytes = _json_bytes(manifest)

    # Verify the recalculated body digests against the serialized manifest.json.
    round_trip = _verify_bodies(json.loads(manifest_bytes.decode("utf-8"))["entries"], bodies)
    if round_trip and overall_status != STATUS_FAILED:
        integrity_problems = integrity_problems + round_trip
        overall_status = STATUS_FAILED
        manifest = build_manifest(capture, overall_status, integrity_problems)
        manifest_bytes = _json_bytes(manifest)
        round_trip = _verify_bodies(
            json.loads(manifest_bytes.decode("utf-8"))["entries"], bodies
        )
    integrity_problems = integrity_problems + [p for p in round_trip if p not in integrity_problems]

    manifest_sha256 = sha256_hex(manifest_bytes)

    files = dict(bodies)
    files["manifest.json"] = manifest_bytes
    files["README.txt"] = _readme_text(
        capture["capture_id"], overall_status, manifest_sha256, len(entries)
    ).encode("utf-8")

    sums_text = "".join(
        "{0}  {1}\n".format(sha256_hex(files[name]), name) for name in sorted(files)
    )
    sums_bytes = sums_text.encode("utf-8")

    sums_problems = []
    for line in sums_text.splitlines():
        digest, _, name = line.partition("  ")
        if name not in files:
            sums_problems.append("SHA256SUMS references missing file: {0}".format(name))
        elif sha256_hex(files[name]) != digest:
            sums_problems.append("SHA256SUMS digest mismatch: {0}".format(name))
    if sums_problems:
        integrity_problems = integrity_problems + sums_problems
        overall_status = STATUS_FAILED

    files["SHA256SUMS.txt"] = sums_bytes

    archive_bytes = _build_zip(capture["capture_id"], files, capture["started"])

    return {
        "capture_id": capture["capture_id"],
        "overall_status": overall_status,
        "entries": entries,
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


def run_and_package(getter=None, clock=None, progress=None, max_bytes=MAX_RESPONSE_BYTES):
    """Full capture run. Always returns a downloadable archive, even on failure."""
    capture = run_capture(
        getter=getter, clock=clock, progress=progress, max_bytes=max_bytes
    )
    return package_capture(capture)
