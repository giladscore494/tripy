"""Per-resource provenance and the fail-safe checks.

Every resource of a build records: resource_id, source_url (the `resource_show` URL), downloaded_at,
source_last_modified, license (exactly as the package states it), row_count, file_size, sha256, schema_hash,
ingestion_version, access_method (+ format, the package title and organization). access_method `file_download`: the
resource file, sha256 / file_size of its bytes, schema_hash of its header. access_method `datastore_api` (the file
download failed and the resource is in the datastore): source_url is the datastore_search endpoint, sha256 is over the
canonical JSON lines of the projected rows in `_id` order, file_size is null and schema_hash is of the datastore fields;
`file_attempt` records why the file failed (HTTP status, host, redirect_host for a refused redirect, reason);
datastore_total, total_was_estimated and total_check (exact_total / exact_count / estimate_within_2pct) record how the
rows were checked against the server's count.

Any of these stops the whole dataset (DatasetFailed); the previous committed snapshot and its manifest entry stay,
and the build writes {"dataset", "status": "failed", "reason", "previous_snapshot_preserved": true}:

    resource_unavailable       resource_show / package_show failed, or the file could not be read
    licence_changed            the package's licence text differs from the one the previous build recorded
    format_changed             the resource format is not an accepted one or differs from the previous build's; or
                               the body is not a delimited table
    required_column_missing    a required column is not in the live header
    row_count_drop             fewer rows than min_row_ratio (70 %) of the previous build's rows of that resource
    html_body                  an HTML page / error body instead of the data
    datastore_incomplete       the datastore fallback read fewer / more rows than its first page's exact `total` (or
                               the datastore_search_sql COUNT(*)); an estimated total without an exact count: more
                               than 2 % off, or the last page was not short

The other datasets continue.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

FAIL_REASONS = ("resource_unavailable", "licence_changed", "format_changed", "required_column_missing",
                "row_count_drop", "html_body",
                "datastore_incomplete")


class DatasetFailed(RuntimeError):
    """A fail-safe stop. `lines`: report lines shown under the failed dataset (diagnostic tables: patterns, counts)."""

    def __init__(self, reason: str, detail: str = "", resource_id: str | None = None,
                 lines: list[str] | None = None, findings: list[dict] | None = None):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail, self.resource_id = reason, detail, resource_id
        self.lines, self.findings = list(lines or []), list(findings or [])


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def failure(dataset: str, exc: DatasetFailed, previous_exists: bool = True) -> dict:
    """The failure record of a stopped dataset (the previous snapshot is never touched by a failed build)."""
    out: dict[str, Any] = {"dataset": dataset, "status": "failed", "reason": exc.reason,
                           "previous_snapshot_preserved": True}
    if exc.detail:
        out["detail"] = exc.detail[:500]
    if exc.resource_id:
        out["resource_id"] = exc.resource_id
    if exc.findings:
        out["findings"] = exc.findings
    if not previous_exists:
        out["note"] = "no previous snapshot existed; nothing is served for this dataset"
    return out


def previous_resource(previous_entry: dict | None, resource_id: str) -> dict:
    for item in (previous_entry or {}).get("resources") or []:
        if isinstance(item, dict) and item.get("resource_id") == resource_id:
            return item
    return {}


def check_licence(previous: dict, licence: Any, resource_id: str) -> None:
    if previous.get("license") is not None and str(licence) != str(previous["license"]):
        raise DatasetFailed("licence_changed", f"was {previous['license']!r}, now {licence!r}", resource_id)


def check_format(previous: dict, fmt: str | None, accepted: list[str], resource_id: str) -> None:
    accepted_upper = {str(a).upper() for a in accepted or []}
    if fmt and accepted_upper and fmt.upper() not in accepted_upper:
        raise DatasetFailed("format_changed", f"resource format {fmt!r}, accepted {sorted(accepted_upper)}",
                            resource_id)
    if previous.get("format") and fmt and str(fmt).upper() != str(previous["format"]).upper():
        raise DatasetFailed("format_changed", f"was {previous['format']!r}, now {fmt!r}", resource_id)


def check_row_count(previous: dict, rows: int, ratio: float, resource_id: str) -> None:
    before = previous.get("row_count")
    if isinstance(before, int) and before > 0 and rows < ratio * before:
        raise DatasetFailed("row_count_drop", f"{rows} rows, previous build {before} (minimum {ratio:.0%})",
                            resource_id)


def record(*, resource_id: str, source_url: str, downloaded_at: str, source_last_modified: Any, license: Any,
           row_count: int, file_size: int | None, sha256: str, schema_hash: str, ingestion_version: str,
           **extra: Any) -> dict:
    out = {"resource_id": resource_id, "source_url": source_url, "downloaded_at": downloaded_at,
           "source_last_modified": source_last_modified, "license": license, "row_count": int(row_count),
           "file_size": None if file_size is None else int(file_size), "sha256": sha256, "schema_hash": schema_hash,
           "ingestion_version": ingestion_version}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out
