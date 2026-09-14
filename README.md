# MILO R5 Source Capture

A Streamlit evidence-capture app. It makes a fixed, bounded set of public
read-only `GET` requests, preserves the returned source bodies byte-for-byte,
validates them, and produces a verified ZIP for MILO R5 review.

The interface is in Hebrew (RTL). Networking lives entirely in `capture.py`,
which is importable and testable without Streamlit.

## No API key is required

Every source is a **public** endpoint. There is nothing to configure.

The app does **not** use — and has no code path for — API keys, access tokens,
usernames, passwords, `Authorization` / `Cookie` / `Proxy-Authorization`
headers, CKAN API tokens, Streamlit secrets, or cookies copied from a browser.
`Set-Cookie` is never retained and never replayed. TLS verification stays on.

## Sources

### Government (data.gov.il CKAN)

| Identifier | Value |
| --- | --- |
| `CKAN_API_BASE` | `https://data.gov.il/api/3/action` |
| `CKAN_PACKAGE_ID` | `degem-rechev-wltp` |
| `WLTP_RESOURCE_ID` | `142afde2-6228-49f9-8a29-9b6c3a0cbe40` |
| `ADDITIONAL_RESOURCE_ID` | `5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6` |

Captured: package metadata (`package_show`), the resource schema for both
resource IDs (`limit=0`), and the targeted RAV4 query results for both
resources, fully paginated.

The primary query uses `q=RAV4`. If a **valid** primary query returns zero
records, two bounded alternatives run for that resource: `q=RAV%204` and
`q=%D7%A8%D7%90%D7%91`. The manifest and the derived files record exactly which
query produced each page and each record. Unrelated full datasets are never
downloaded — every query stays scoped to RAV4 candidates.

### Official Toyota Israel

| Page | URL | Raw path |
| --- | --- | --- |
| Archive index | `https://www.toyota.co.il/cars` | `raw/toyota/archive-index.html` |
| Archived RAV4 Plug-in | `https://www.toyota.co.il/cars/RAV4-PHEV` | `raw/toyota/rav4-phev.html` |

**Obsolete URL, replaced:** `https://www.toyota.co.il/models/rav4-plugin`

That URL returns **HTTP 404**. The Toyota Israel site moved its model pages
from `/models/<slug>` to `/cars/<MODEL>`, and the RAV4 Plug-in is now an
archived model whose marketing has ended. Because a 404 is an ordinary 4xx it
is correctly never retried, so every capture using the old URL failed the
Toyota source outright. The obsolete URL is retained in `README`, in the
manifest (`sources.previous_obsolete_url`) and on the model page's manifest
entry (`previous_obsolete_url`) **for provenance only** — it is never
requested during a capture.

## What the Toyota evidence does and does not establish

The archived model page states that marketing of the RAV4 Plug-in has ended.
It is an official **model-identity and archived-status** source, not a complete
technical specification source.

Model page validation requires all of: HTTP 200, HTTPS, a final hostname still
on `toyota.co.il` / `www.toyota.co.il`, non-empty plausible HTML, no CAPTCHA /
WAF / access-denied / login / error page, Toyota identity, RAV4 identity, at
least one Plug-in identity marker (`plug-in`, `plugin`, `phev`, `פלאג`), and a
credible ended-marketing or archive indication. Every marker must appear in the
saved response body; nothing is read from the URL or the filename.

A passing model page yields:

```
passed_official_archived_model_identity_requires_human_fact_check
```

That result confirms **identity and archived status only**. It is not a vehicle
fact and not a technical specification. No specification value is extracted and
none is inferred. **Government CKAN data remains the structured technical
source**; Toyota provides official importer corroboration of model identity and
archived status. A human must read the preserved HTML to establish any claim
about the model.

The archive index has its own, lighter validation
(`passed_official_archive_index`) — it establishes that the official archive
section exists and was reachable.

## Install and run

Requires Python 3.11 (matching Streamlit Community Cloud).

```bash
pip install -r requirements.txt
streamlit run app.py
```

Entrypoint: **`app.py`**.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall app.py capture.py
```

All tests use mocked or injected HTTP behaviour and never reach the network.
The retry sleeper is injected, so tests never actually wait.
`tests/test_audit.py` is a permanent source-code audit asserting the absence of
mutating HTTP verbs, `requests.Session`, secret lookups, credential values,
generic URL inputs, browser automation, provider clients, response caching,
databases and any MILO/Supabase/GCP write path.

## Network policy

| Control | Value |
| --- | --- |
| Method | `GET` only |
| Scheme | HTTPS only; HTTP downgrades rejected |
| URLs | fixed in code; no user-supplied targets; embedded credentials rejected |
| Allowed hosts | `data.gov.il`, `www.data.gov.il`, `toyota.co.il`, `www.toyota.co.il` |
| Session | none — `requests.get`, never `requests.Session` |
| Redirects | `allow_redirects=False`; at most 5, each validated manually; loops detected |
| Attempts | 3; retries only transport errors, 429 and 500/502/503/504 |
| Retries excluded | ordinary 4xx, including 404 |
| Backoff | bounded exponential (1s, 2s, … capped at 8s); `Retry-After` honoured, capped at 30s |
| Timeouts | connect 10 s, read 45 s |
| Body | streamed; max 15 MiB per response; max 64 MiB per bundle |
| TLS | verification enabled |

Retained response headers: `Date`, `Content-Type`, `Content-Length`,
`Content-Encoding`, `ETag`, `Last-Modified`, `Location`, `Cache-Control`,
`Retry-After`. Everything else — including `Set-Cookie`, `Cookie`,
`Authorization` and `Proxy-Authorization` — is discarded at capture time.
Errors are sanitized to the exception type only, so proxy URLs and local paths
cannot leak into the manifest. Complete response bodies are never logged.

## Pagination

Every Government query uses CKAN `result.total` with `offset` and `limit`
(page size 100) and continues until all reported results for that query are
captured. The paginator requires the offset to move forward, detects repeated
offsets and byte-identical repeated pages, rejects internally inconsistent
totals, and enforces a defensive maximum of 200 pages per query plus a bundle
byte ceiling.

**Results are never silently truncated.** If safe completion cannot be
established — a reported total larger than what the server serves, a
non-advancing offset, an inconsistent total — the capture fails closed with a
`pagination_*` reason rather than returning a partial record set.

## ZIP structure

Built entirely in memory. Archive root:
`milo-r5-source-capture-YYYYMMDDTHHMMSSZ/`

```
manifest.json
README.txt
SHA256SUMS.txt
raw/
  government/
    package_show.json
    wltp/
      schema.json
      pages/page_000001.json …
      alt1/pages/…            (only if the primary query returned zero records)
      alt2/pages/…
    additional/
      schema.json
      pages/page_000001.json …
  toyota/
    archive-index.html
    rav4-phev.html
derived/
  government/
    wltp_all_records.json
    additional_all_records.json
```

### raw/ versus derived/

- **`raw/`** — exact response bytes, one file per HTTP request. This is the
  evidence. Every hash in `manifest.json` and `SHA256SUMS.txt` is taken over
  these bytes. Nothing here is pretty-printed, normalized or rewritten.
- **`derived/`** — deterministic consolidations this tool builds *from* the raw
  pages: all records for one resource, deduplicated **by original `_id` only**,
  in first-seen order, with per-record provenance naming the query and the raw
  page it came from. Original field names and values are preserved verbatim. No
  vehicle fact is invented, normalized or inferred.

## Provenance and integrity

For every request the manifest records: logical source ID, source type,
requested URL, sanitized query parameters, UTC start and finish, HTTP status,
redirect chain, final URL, sanitized retained headers, `Content-Type`, declared
`Content-Length`, actual byte count, SHA-256, attempt count, retry count and
sanitized retry delays, validation result, sanitized error, raw output path,
and `api_key_used` / `credentials_used` / `cookies_supplied`, all `false`.

Government pages additionally record: resource ID, query token, requested
offset, requested limit, returned record count, reported total, and the
original `_id` values.

`SHA256SUMS.txt` covers every raw and derived file plus `manifest.json` and
`README.txt`, in deterministic sorted path order. It cannot list its own digest.

Before the download is exposed the app recalculates every byte count and hash,
verifies them against `manifest.json`, hashes the final `manifest.json`,
generates and verifies `SHA256SUMS.txt`, and only then builds the ZIP from the
verified bytes. Any mismatch **fails closed** to `capture_failed`. Verify a
downloaded archive with `sha256sum -c SHA256SUMS.txt`.

## Overall statuses

| Status | Meaning |
| --- | --- |
| `ready_for_r5_bundle_review` | All mandatory Government requests passed, targeted records are present, both official Toyota pages passed, integrity verified. |
| `no_matching_government_records` | Government requests valid, but the targeted searches found no RAV4 candidates. |
| `incomplete_official_web_source` | Government capture succeeded but a Toyota page was unavailable or failed validation. |
| `capture_failed` | Network, HTTP, redirect, size, JSON, pagination or integrity failure prevented a valid capture. |

A diagnostic ZIP is always downloadable. Only `ready_for_r5_bundle_review` uses
the plain filename `milo-r5-source-capture-<timestamp>.zip`; every other status
produces `milo-r5-source-capture-INCOMPLETE-<timestamp>.zip`. An incomplete
archive is never labelled R5-ready, and validation is never weakened to turn a
partial capture into a success.

## Deploying to Streamlit Community Cloud

1. Push to `main` on `giladscore494/tripy`.
2. At <https://share.streamlit.io>, create an app pointing at this repository,
   branch `main`, main file `app.py`.
3. Set the Python version to **3.11**.
4. Leave the secrets box **empty** — this app reads no secrets.
5. Deploy. Existing deployments tracking `main` redeploy automatically on push;
   because `requirements.txt` is pinned, the environment rebuilds on change.

To re-run a capture on the deployed app: open it, press **התחל לכידה**
("Start capture"), wait for the progress bar, then use the download button. The
generated ZIP is held in `st.session_state`, so downloading it does not repeat
the network calls. **איפוס לכידה** ("Reset capture") clears it.

## Outbound-network troubleshooting

The capture runs **server-side**, so the Streamlit host must be able to reach
`data.gov.il` and `toyota.co.il` over HTTPS on port 443.

| Symptom | Meaning |
| --- | --- |
| `transport_error: ProxyError` / `ConnectionError` | The host could not egress. Sandboxes and corporate proxies commonly block these domains — check the egress allowlist. |
| `transport_error: ConnectTimeout` | DNS or firewall is dropping the connection. |
| `failed_http_status` | The source returned a non-200 code. If an official URL moved again, update the constants at the top of `capture.py`. |
| `failed_blocking_page` | A CAPTCHA / WAF / access-denied page was served instead of content. |
| `failed_missing_archive_indication` | The page was captured but contained no ended-marketing or archive wording; adjust `TOYOTA_ARCHIVE_MARKERS` if Toyota rewords it. |
| `pagination_*` | Pagination did not advance or totals were inconsistent. The capture fails closed rather than truncating. |
| `host_not_allowed` / `redirect_host_not_allowed` | A redirect pointed off the allowlist and was refused by design. |
