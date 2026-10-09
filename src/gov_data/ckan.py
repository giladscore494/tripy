"""data.gov.il CKAN actions. The file URL always comes from `resource_show(resource_id)` (`result.url`), never from a
hard-coded URL; the licence is the package's own text (`package_show`: `license_title`, else `license_id`), recorded
exactly as stated. `datastore_search` serves the QA samples (a few rows, projected to the kept columns) and the
datastore fallback of a resource whose file cannot be downloaded (`datastore_schema` + `datastore_pages`: paged, sorted
by `_id`, only the projected fields requested).

Every call goes through an `Http` object (tests pass a fake one, or a real one over a fake transport: no network in
the test suite). A failed request names `HTTP <status>`, the URL host (never the query string) and the first 200
characters of the response body, control characters stripped (`describe_http_error`).

The file download (`Http.open`): `User-Agent: datagov-external-client`, `Accept: text/csv,*/*`; one retry after 10 s
on 429 / 5xx; redirects are followed by hand and only to `*.gov.il` hosts (`DownloadError`, kind `redirect`, otherwise).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, BinaryIO, Callable, Iterator

USER_AGENT = "tripy-gov-datasets/1 (+https://github.com/giladscore494/tripy)"
DOWNLOAD_HEADERS = {"User-Agent": "datagov-external-client", "Accept": "text/csv,*/*"}
REDIRECT_CODES = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 5
DOWNLOAD_RETRY_WAIT_S = 10.0
DATASTORE_PAGE = 32000
DATASTORE_RATE = 4.0                    # requests per second, at most
DATASTORE_RETRIES = 3                   # per page, on 429 / 5xx
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


class CkanError(RuntimeError):
    """A CKAN action failed or returned success: false."""


class DownloadError(RuntimeError):
    """The file download failed. `kind`: http (an HTTP error status: the datastore fallback may take over), redirect
    (a redirect off *.gov.il, or too many) or network (no HTTP answer)."""

    def __init__(self, kind: str, message: str, status: int | None = None, host: str | None = None):
        super().__init__(message)
        self.kind, self.status, self.host = kind, status, host


def host_of(url: str) -> str:
    """The URL's host (no scheme, path or query string)."""
    return urllib.parse.urlsplit(str(url or "")).hostname or "?"


def gov_host(host: str | None) -> bool:
    host = str(host or "").lower().rstrip(".")
    return host == "gov.il" or host.endswith(".gov.il")


def body_snippet(data: bytes | str | None, limit: int = 200) -> str:
    """The first `limit` characters of a response body, control characters stripped."""
    text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data or "")
    return " ".join(_CONTROL.sub(" ", text).split())[:limit]


def _read_error_body(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read(4096) or b""
    except Exception:  # noqa: BLE001 - a body that cannot be read is reported empty
        return b""


def describe_http_error(exc: urllib.error.HTTPError, url: str) -> str:
    return f"HTTP {exc.code} from {host_of(url)}: {body_snippet(_read_error_body(exc))!r}"


def retryable(status: int | None) -> bool:
    return status is not None and (status == 429 or 500 <= status <= 599)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx answers surface as HTTPError: Http.open follows them by hand, after checking the target host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None


def _urllib_transport() -> Callable[[urllib.request.Request, float], Any]:
    opener = urllib.request.build_opener(_NoRedirect)
    return lambda request, timeout: opener.open(request, timeout=timeout)


class Http:
    """The network side: JSON GETs (retried on 429 / 5xx / no answer, with backoff) and the binary file stream.
    `transport(request, timeout)` and `sleep` are injectable (tests)."""

    def __init__(self, timeout: float = 120.0, retries: int = 3, pause_s: float = 1.0,
                 transport: Callable[[urllib.request.Request, float], Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.timeout, self.retries, self.pause_s = timeout, retries, pause_s
        self.transport = transport or _urllib_transport()
        self.sleep = sleep

    def get_json(self, url: str, retries: int | None = None) -> dict:
        """The decoded JSON body. 429 / 5xx and network errors are retried `retries` times (backoff 1, 2, 4 s, or the
        server's Retry-After, at most 60 s); any other HTTP status is a CkanError at once."""
        retries = self.retries if retries is None else retries
        last = ""
        for attempt in range(retries + 1):
            wait = self.pause_s * (2 ** attempt)
            try:
                response = self.transport(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
                                          self.timeout)
                with response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last = describe_http_error(exc, url)
                if not retryable(exc.code):
                    break
                try:
                    wait = max(wait, min(60.0, float(exc.headers.get("Retry-After") or 0)))
                except (TypeError, ValueError, AttributeError):
                    pass
            except Exception as exc:  # noqa: BLE001 - no answer / a broken body: retried, then reported
                last = f"{type(exc).__name__} from {host_of(url)}: {body_snippet(str(exc))}"
            if attempt < retries:
                self.sleep(wait)
        raise CkanError(last)

    def open(self, url: str) -> BinaryIO:
        """The file body as a stream (DownloadError when it cannot be had)."""
        current, hops, retried = url, 0, False
        while True:
            try:
                return self.transport(urllib.request.Request(current, headers=dict(DOWNLOAD_HEADERS)), self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in REDIRECT_CODES:
                    location = (exc.headers.get("Location") if exc.headers is not None else None) or ""
                    target = urllib.parse.urljoin(current, location.strip())
                    if not location.strip():
                        raise DownloadError("redirect", f"HTTP {exc.code} from {host_of(current)} without a "
                                            "Location", exc.code, host_of(current)) from None
                    if not gov_host(host_of(target)):
                        raise DownloadError("redirect", f"HTTP {exc.code} from {host_of(current)}: redirect to a "
                                            f"non-gov host {host_of(target)} refused", exc.code,
                                            host_of(target)) from None
                    hops += 1
                    if hops > MAX_REDIRECTS:
                        raise DownloadError("redirect", f"more than {MAX_REDIRECTS} redirects (last host "
                                            f"{host_of(target)})", exc.code, host_of(target)) from None
                    current = target
                    continue
                if retryable(exc.code) and not retried:
                    retried = True
                    _read_error_body(exc)
                    self.sleep(DOWNLOAD_RETRY_WAIT_S)
                    continue
                raise DownloadError("http", describe_http_error(exc, current), exc.code, host_of(current)) from None
            except DownloadError:
                raise
            except Exception as exc:  # noqa: BLE001 - no HTTP answer at all
                raise DownloadError("network", f"{type(exc).__name__} from {host_of(current)}: "
                                    f"{body_snippet(str(exc))}", None, host_of(current)) from None


def action_url(base: str, action: str, params: dict) -> str:
    return f"{base.rstrip('/')}/{action}?{urllib.parse.urlencode(params)}"


def _action(http: Http, base: str, action: str, params: dict, **kwargs: Any) -> Any:
    body = http.get_json(action_url(base, action, params), **kwargs)
    if not isinstance(body, dict) or not body.get("success"):
        error = body.get("error") if isinstance(body, dict) else body
        raise CkanError(f"{action} refused: {json.dumps(error, ensure_ascii=False)[:300]}")
    return body.get("result")


def resource_show(http: Http, base: str, resource_id: str) -> dict:
    """{resource_id, url, format, last_modified, package_id, name, datastore_active} of a resource (CkanError when
    unavailable or without a URL)."""
    result = _action(http, base, "resource_show", {"id": resource_id})
    if not isinstance(result, dict) or not str(result.get("url") or "").strip():
        raise CkanError("resource_show returned no url")
    active = result.get("datastore_active")
    return {"resource_id": resource_id, "url": str(result["url"]).strip(),
            "format": str(result.get("format") or "").strip().upper() or None,
            "last_modified": result.get("last_modified") or result.get("metadata_modified") or result.get("created"),
            "package_id": result.get("package_id"), "name": result.get("name"),
            "datastore_active": active is True or str(active).strip().lower() == "true"}


def package_show(http: Http, base: str, package_id: str) -> dict:
    """{license, title, organization} of the resource's package; `license` exactly as stated (license_title, else
    license_id)."""
    result = _action(http, base, "package_show", {"id": package_id})
    result = result if isinstance(result, dict) else {}
    org = result.get("organization") if isinstance(result.get("organization"), dict) else {}
    return {"license": result.get("license_title") or result.get("license_id"), "title": result.get("title"),
            "organization": org.get("title") or org.get("name")}


def datastore_sample(http: Http, base: str, resource_id: str, fields: list[str], limit: int = 3) -> list[dict]:
    """QA sample: `limit` rows of the datastore, only the given fields (never a whole row)."""
    result = _action(http, base, "datastore_search",
                     {"resource_id": resource_id, "limit": int(limit), "fields": ",".join(fields)})
    records = (result or {}).get("records") if isinstance(result, dict) else None
    return [{k: r.get(k) for k in fields if k in r} for r in records or [] if isinstance(r, dict)]


def datastore_endpoint(base: str, resource_id: str) -> str:
    return action_url(base, "datastore_search", {"resource_id": resource_id})


class RateLimit:
    """At most `per_second` calls per second (the caller waits before each call)."""

    def __init__(self, per_second: float = DATASTORE_RATE, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval, self.clock, self.sleep = 1.0 / per_second, clock, sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self._last is not None and now - self._last < self.interval:
            self.sleep(self.interval - (now - self._last))
            now = self.clock()
        self._last = now


def datastore_schema(http: Http, base: str, resource_id: str, rate: RateLimit) -> list[str]:
    """The datastore's field ids in order (`limit=0`: no record is read; `_id` / `_full_text` left out)."""
    rate.wait()
    result = _action(http, base, "datastore_search", {"resource_id": resource_id, "limit": 0},
                     retries=DATASTORE_RETRIES)
    fields = (result or {}).get("fields") if isinstance(result, dict) else None
    ids = [str(f.get("id")) for f in fields or [] if isinstance(f, dict) and f.get("id") is not None]
    return [i for i in ids if i not in ("_id", "_full_text")]


def datastore_pages(http: Http, base: str, resource_id: str, fields: list[str], rate: RateLimit,
                    limit: int = DATASTORE_PAGE) -> Iterator[tuple[int | None, list[dict], bool]]:
    """(total, records, total_was_estimated) per page: `fields` only, sorted by `_id`, `offset` paging; stops after a
    page shorter than `limit`. `total` is the server's count (the first page's is the one checked)."""
    offset = 0
    while True:
        rate.wait()
        result = _action(http, base, "datastore_search",
                         {"resource_id": resource_id, "limit": int(limit), "offset": offset,
                          "fields": ",".join(fields), "sort": "_id"}, retries=DATASTORE_RETRIES)
        result = result if isinstance(result, dict) else {}
        records = [r for r in result.get("records") or [] if isinstance(r, dict)]
        total = result.get("total")
        total = int(total) if isinstance(total, (int, str)) and str(total).strip().isdigit() else None
        yield total, records, bool(result.get("total_was_estimated"))
        if len(records) < limit:
            return
        offset += len(records)
