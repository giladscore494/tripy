"""data.gov.il CKAN actions. The file URL always comes from `resource_show(resource_id)` (`result.url`), never from a
hard-coded URL; the licence is the package's own text (`package_show`: `license_title`, else `license_id`), recorded
exactly as stated. `datastore_search` is used only for QA samples (a few rows, projected to the kept columns).

Every call goes through an `Http` object (tests pass a fake one: no network in the test suite).
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, BinaryIO

USER_AGENT = "tripy-gov-datasets/1 (+https://github.com/giladscore494/tripy)"


class CkanError(RuntimeError):
    """A CKAN action failed or returned success: false."""


class Http:
    """The network side: JSON GETs and binary streams (urllib, a polite pause and retries)."""

    def __init__(self, timeout: float = 120.0, retries: int = 3, pause_s: float = 1.0):
        self.timeout, self.retries, self.pause_s = timeout, retries, pause_s

    def _request(self, url: str):
        return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
                                      timeout=self.timeout)

    def get_json(self, url: str) -> dict:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                with self._request(url) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                last = exc
                time.sleep(self.pause_s * (2 ** attempt))
        raise CkanError(f"{type(last).__name__}: {str(last)[:200]}")

    def open(self, url: str) -> BinaryIO:
        return self._request(url)


def _action(http: Http, base: str, action: str, params: dict) -> Any:
    url = f"{base.rstrip('/')}/{action}?{urllib.parse.urlencode(params)}"
    body = http.get_json(url)
    if not isinstance(body, dict) or not body.get("success"):
        error = body.get("error") if isinstance(body, dict) else body
        raise CkanError(f"{action} refused: {json.dumps(error, ensure_ascii=False)[:300]}")
    return body.get("result")


def resource_show(http: Http, base: str, resource_id: str) -> dict:
    """{resource_id, url, format, last_modified, package_id, name} of a resource (CkanError when unavailable or
    without a URL)."""
    result = _action(http, base, "resource_show", {"id": resource_id})
    if not isinstance(result, dict) or not str(result.get("url") or "").strip():
        raise CkanError("resource_show returned no url")
    return {"resource_id": resource_id, "url": str(result["url"]).strip(),
            "format": str(result.get("format") or "").strip().upper() or None,
            "last_modified": result.get("last_modified") or result.get("metadata_modified") or result.get("created"),
            "package_id": result.get("package_id"), "name": result.get("name")}


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
