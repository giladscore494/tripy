# MILO R5 Source Capture

A **temporary** Streamlit evidence-capture app. It makes a fixed, bounded set of
public read-only `GET` requests from the Streamlit server, preserves the returned
source bodies byte-for-byte, validates them, and produces a verified ZIP for
MILO R5 review.

This repository previously held a different application. The prior contents are
preserved on the backup branch created before the replacement.

## No authentication is required or used

Every source is a **public** endpoint. There is nothing to configure.

The app does **not** use — and has no code path for — API keys, access tokens,
usernames, passwords, `Authorization` headers, CKAN API tokens, Streamlit
secrets, cookies copied from a browser, or any other credential. The resource
identifiers below are public resource IDs, not secrets.

| Identifier | Value |
| --- | --- |
| `CKAN_API_BASE` | `https://data.gov.il/api/3/action` |
| `CKAN_API_VERSION` | `3` |
| `CKAN_PACKAGE_ID` | `degem-rechev-wltp` |
| `WLTP_RESOURCE_ID` | `142afde2-6228-49f9-8a29-9b6c3a0cbe40` |
| `ADDITIONAL_RESOURCE_ID` | `5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6` |
| `TOYOTA_URL` | `https://www.toyota.co.il/models/rav4-plugin` |

## Run it

```bash
pip install -r requirements.txt
streamlit run app.py
```

The Streamlit entrypoint is **`app.py`**.

## What it captures

Six mandatory requests, executed serially in this order:

1. Package metadata — `package_show?id=degem-rechev-wltp`
2. WLTP resource schema — `datastore_search?resource_id=142afde2…&limit=0`
3. WLTP RAV4 query — `datastore_search?resource_id=142afde2…&limit=100&q=RAV4`
4. Additional resource schema — `datastore_search?resource_id=5e87a7a1…&limit=0`
5. Additional resource RAV4 query — `datastore_search?resource_id=5e87a7a1…&limit=100&q=RAV4`
6. Official Toyota page — `https://www.toyota.co.il/models/rav4-plugin`

If a **valid** RAV4 query returns zero records, two extra bounded queries run for
that resource: `q=RAV%204` and `q=%D7%A8%D7%90%D7%91`. Each alternative gets its
own body file, header file and manifest entry.

The full dataset is never requested. Schema probes use `limit=0`; record queries
use `limit=100`.

## Request policy

| Control | Value |
| --- | --- |
| Method | `GET` only |
| Session | none — `requests.get`, never `requests.Session` |
| Execution | serial |
| Redirects | `allow_redirects=False`, at most 5 processed manually |
| Redirect targets | must be HTTPS and one of `data.gov.il`, `www.data.gov.il`, `toyota.co.il`, `www.toyota.co.il` |
| Attempts | 3 total; retries only transport errors, HTTP 429 and HTTP 5xx |
| Timeouts | connect 10 s, read 45 s |
| Max response | 15 MiB |
| `User-Agent` | `MILO-R5-streamlit-evidence-capture/1.0` |
| `Accept-Encoding` | `identity` |
| `Accept` | `application/json` (Government), `text/html,application/xhtml+xml` (Toyota) |

Cookies received from one response are never sent to the next request: a fresh
header dict is built for every hop and no session object carries state.

There is **no URL input in the UI**. Every URL is constructed in code from the
constants above.

## Preservation and provenance

Response bodies are stored as exact bytes. Government JSON and Toyota HTML are
never pretty-printed, normalized, renamed or otherwise rewritten.

Only these response headers are preserved: `Date`, `Content-Type`,
`Content-Length`, `Content-Encoding`, `ETag`, `Last-Modified`, `Location`,
`Cache-Control`. `Set-Cookie`, `Authorization`, environment variables, proxy
details, local paths and server identifiers are never saved.

Each request records: logical source ID, source type, requested URL, final URL,
redirect chain, UTC start/finish, final HTTP status, sanitized headers, body
filename, exact byte count, SHA-256, attempt count,
`credentials_used=false`, `api_key_used=false`, `cookies_supplied=false`,
validation result, sanitized error, and — for Government queries — the record
count and the original `_id` values.

Errors are sanitized to the exception type only, so proxy URLs and local paths
cannot leak into the manifest.

## Archive

Built entirely in memory. Archive root: `milo-r5-source-capture-YYYYMMDDTHHMMSSZ/`

```
manifest.json
SHA256SUMS.txt
README.txt
government/
  package_show_degem_rechev_wltp.json          + .headers.json
  resource_142afde2_schema.json                + .headers.json
  resource_142afde2_rav4.json                  + .headers.json
  resource_5e87a7a1_schema.json                + .headers.json
  resource_5e87a7a1_rav4.json                  + .headers.json
web/
  toyota_rav4_plugin.html                      + .headers.json
```

Before the download is exposed, the app recalculates every byte count and body
hash, verifies them against `manifest.json`, hashes the final `manifest.json`,
generates and verifies `SHA256SUMS.txt`, and only then builds the ZIP from the
verified bytes. Verify a downloaded archive with `sha256sum -c SHA256SUMS.txt`.

## Overall statuses

| Status | Meaning |
| --- | --- |
| `ready_for_r5_bundle_review` | All mandatory sources passed, at least one Government query returned records, and the Toyota HTML contains technical markers. |
| `no_matching_government_records` | All Government calls were valid but all bounded queries returned zero records. |
| `incomplete_official_web_source` | Government evidence passed but the Toyota HTML lacked usable technical content. |
| `capture_failed` | Network, HTTP, redirect, size, JSON, blocking-page or integrity failure. |

A diagnostic ZIP is always downloadable. Only a `ready_for_r5_bundle_review`
capture uses the plain filename `milo-r5-source-capture-<timestamp>.zip`; every
other status produces `milo-r5-source-capture-INCOMPLETE-<timestamp>.zip`. An
incomplete archive is never labelled R5-ready.

## Toyota validation

The saved HTML must be 2xx, HTML-typed, non-empty and plausible, must contain
Toyota and RAV4 identifiers, and must not be an Access Denied, CAPTCHA, WAF or
error page.

When technical specification markers are present the result is
`passed_candidate_requires_human_fact_check` — **a candidate, not a fact**. No
specification value is extracted and nothing is inferred from the URL or the
filename; a human must read the preserved HTML to establish any vehicle fact.
When the official page is captured but its saved HTML lacks technical content
the result is `insufficient_official_content`.

## Not used

No databases, model providers, AI APIs, user accounts, secrets, production
services, generic user-supplied URLs, browser automation, Playwright, Selenium,
or full Government dataset downloads.

## Tests

```bash
python -m compileall app.py capture.py tests
python -m unittest discover -s tests -v
```

All tests use mocked responses through an injected fake transport; none reaches
a live source. `tests/test_capture.py` also contains a permanent source-code
audit (`SourceAuditTests`) asserting the absence of mutating HTTP verbs,
`requests.Session`, secret lookups, credential values, generic URL inputs,
browser automation, provider clients, response caching and any database or
MILO/Supabase/GCP write path.
