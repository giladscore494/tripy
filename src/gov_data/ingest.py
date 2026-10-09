"""One resource, end to end: resource_show -> package_show (the licence) -> the fail-safe checks -> the streamed,
projected rows (each passed to the dataset's aggregation) -> the provenance record. Any failure raises
provenance.DatasetFailed; the caller discards the dataset's aggregation and keeps its previous snapshot.

The rows come from the resource file (`file_download`). When the file cannot be had (an HTTP error status, or an HTML /
error body instead of the data) and resource_show reports `datastore_active: true`, they come from the CKAN datastore
instead (`datastore_api`): `datastore_search` pages of 32000 rows sorted by `_id`, only the projected fields requested
(a `never` column is never asked for), at most 4 requests per second. The datastore keeps every fail-safe: a required
field missing, the row-count drop and `total` != the rows read (`datastore_incomplete`). When the first page's
`total` is an estimate (`total_was_estimated`), one exact `datastore_search_sql` COUNT(*) is tried; without it the read
is accepted only when the last page was short, no page failed and the rows are within 2 % of the estimate
(`total_check: estimate_within_2pct`). No raw file is written for it. A redirect off *.gov.il or a download with
no HTTP answer stops the resource without the fallback.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import ckan
from . import provenance as P
from . import schemas as S
from .download import CsvDownload, DownloadRefused, project

FILE_DOWNLOAD, DATASTORE_API = "file_download", "datastore_api"


@dataclass
class Context:
    http: Any
    base: str
    accepted_formats: list[str]
    min_row_ratio: float
    ingestion_version: str
    work_dir: Path | None = None
    now: Callable[[], str] = P.utc_now
    samples: dict = field(default_factory=dict)
    attempts: list = field(default_factory=list)          # one {dataset, resource_id, access_method, ...} per resource
    rate: Any = field(default_factory=ckan.RateLimit)     # datastore requests: at most 4 per second, build-wide
    datastore_page: int = ckan.DATASTORE_PAGE


class _FileFailed(Exception):
    """The file path failed before any row reached the aggregation; `fallback` when the datastore may take over."""

    def __init__(self, failure: P.DatasetFailed, fallback: bool, status: int | None = None, host: str | None = None):
        super().__init__(failure.detail)
        self.failure, self.fallback, self.status, self.host = failure, fallback, status, host


def read_resource(ctx: Context, dataset: str, spec: dict, resource: dict, previous_entry: dict | None,
                  on_row: Callable[[dict], None]) -> dict:
    """Stream one resource into `on_row` (projected rows) and return its provenance record."""
    resource_id = str(resource["resource_id"])
    role = resource.get("role")
    previous = P.previous_resource(previous_entry, resource_id)
    try:
        meta = ckan.resource_show(ctx.http, ctx.base, resource_id)
        package = ckan.package_show(ctx.http, ctx.base, meta["package_id"]) if meta.get("package_id") else {}
    except Exception as exc:  # noqa: BLE001 - any CKAN problem stops the dataset
        raise P.DatasetFailed("resource_unavailable", f"{type(exc).__name__}: {str(exc)[:300]}", resource_id) from None
    licence = package.get("license")
    P.check_licence(previous, licence, resource_id)
    P.check_format(previous, meta.get("format"), ctx.accepted_formats, resource_id)
    attempt: dict[str, Any] = {"dataset": dataset, "resource_id": resource_id, "role": role, "status": "failed",
                               "access_method": FILE_DOWNLOAD, "file_http_status": None, "file_error": None,
                               "datastore_active": meta.get("datastore_active"), "rows": None}
    ctx.attempts.append(attempt)
    downloaded_at = ctx.now()
    common = {"resource_id": resource_id, "downloaded_at": downloaded_at,
              "source_last_modified": meta.get("last_modified"), "license": licence,
              "ingestion_version": ctx.ingestion_version, "role": role, "format": meta.get("format"),
              "package_title": package.get("title"), "organization": package.get("organization")}
    try:
        fields, columns = _from_file(ctx, dataset, spec, resource_id, role, meta, on_row, attempt)
    except _FileFailed as failed:
        attempt["file_http_status"], attempt["file_error"] = failed.status, failed.failure.detail
        if not failed.fallback:
            raise failed.failure from None
        if not meta.get("datastore_active"):
            raise P.DatasetFailed(failed.failure.reason, f"{failed.failure.detail}; no datastore fallback "
                                  "(datastore_active is not true)", resource_id) from None
        attempt["access_method"] = DATASTORE_API
        file_attempt = {"http_status": failed.status, "host": failed.host, "reason": failed.failure.reason,
                        "detail": failed.failure.detail[:500]}
        try:
            fields, columns = _from_datastore(ctx, spec, resource_id, role, on_row, attempt)
            P.check_row_count(previous, fields["row_count"], ctx.min_row_ratio, resource_id)
        except P.DatasetFailed as exc:
            raise P.DatasetFailed(exc.reason, f"datastore: {exc.detail}; file: {failed.failure.detail}",
                                  resource_id) from None
        fields["file_attempt"] = file_attempt
    else:
        P.check_row_count(previous, fields["row_count"], ctx.min_row_ratio, resource_id)
    attempt["status"], attempt["rows"] = "ok", fields["row_count"]
    if spec.get("qa_sample"):
        try:
            ctx.samples[resource_id] = ckan.datastore_sample(ctx.http, ctx.base, resource_id, list(columns)[:8])
        except Exception as exc:  # noqa: BLE001 - a sample never costs the build
            ctx.samples[resource_id] = [{"sample_error": type(exc).__name__}]
    return P.record(**common, **fields)


def _from_file(ctx: Context, dataset: str, spec: dict, resource_id: str, role: str | None, meta: dict,
               on_row: Callable[[dict], None], attempt: dict) -> tuple[dict, list[str]]:
    """The resource file, streamed: (provenance fields, the live names of the projected columns)."""
    tee = None
    if spec.get("to_disk") and ctx.work_dir is not None:
        ctx.work_dir.mkdir(parents=True, exist_ok=True)
        tee = ctx.work_dir / f"{dataset}-{resource_id}.raw"
    try:
        source = ctx.http.open(meta["url"])
    except ckan.DownloadError as exc:
        failure = P.DatasetFailed("resource_unavailable", f"download: {exc}", resource_id)
        raise _FileFailed(failure, exc.kind == "http", exc.status, exc.host) from None
    except Exception as exc:  # noqa: BLE001
        failure = P.DatasetFailed("resource_unavailable", f"download: {type(exc).__name__} from "
                                  f"{ckan.host_of(meta['url'])}: {ckan.body_snippet(str(exc))}", resource_id)
        raise _FileFailed(failure, False, None, ckan.host_of(meta["url"])) from None
    status = getattr(source, "status", None)
    attempt["file_http_status"] = status if isinstance(status, int) else None
    try:
        try:
            body = CsvDownload(source, tee)
        except DownloadRefused as exc:
            detail = exc.detail
            if exc.snippet:
                detail = f"{detail} from {ckan.host_of(meta['url'])}: {exc.snippet!r}"
            failure = P.DatasetFailed(exc.reason, detail, resource_id)
            raise _FileFailed(failure, exc.reason == "html_body", attempt["file_http_status"],
                              ckan.host_of(meta["url"])) from None
        columns, missing = S.projection(body.header, spec, role)
        if missing:
            body.close()
            raise P.DatasetFailed("required_column_missing", f"missing {missing}", resource_id)
        rows = 0
        try:
            for raw in body.rows():
                rows += 1
                on_row({**project(raw, columns), "_role": role, "_resource_id": resource_id})
        except P.DatasetFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken stream stops the dataset (no partial snapshot)
            raise P.DatasetFailed("resource_unavailable", f"read: {type(exc).__name__}: {str(exc)[:200]}",
                                  resource_id) from None
        finally:
            body.close()
    finally:
        try:
            source.close()
        except Exception:  # noqa: BLE001
            pass
    fields = {"source_url": meta["url"], "access_method": FILE_DOWNLOAD, "row_count": rows,
              "file_size": body.file_size, "sha256": body.sha256, "schema_hash": S.schema_hash(body.header),
              "schema": [S.clean_name(h) for h in body.header], "encoding": body.encoding,
              "delimiter": body.delimiter, "malformed_rows": body.malformed or None}
    return fields, list(columns)


def cell(value: Any) -> str | None:
    """A datastore value as the CSV reader would give it: text, stripped, blank as None (2017.0 -> "2017")."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


def canonical_line(row: dict) -> bytes:
    return (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _from_datastore(ctx: Context, spec: dict, resource_id: str, role: str | None, on_row: Callable[[dict], None],
                    attempt: dict) -> tuple[dict, list[str]]:
    """The resource through datastore_search: (provenance fields, the live names of the projected fields)."""
    try:
        live = ckan.datastore_schema(ctx.http, ctx.base, resource_id, ctx.rate)
    except Exception as exc:  # noqa: BLE001
        raise P.DatasetFailed("resource_unavailable", f"schema: {type(exc).__name__}: {str(exc)[:300]}",
                              resource_id) from None
    if not live:
        raise P.DatasetFailed("resource_unavailable", "schema: the datastore lists no fields", resource_id)
    columns, missing = S.projection(live, spec, role)
    if missing:
        raise P.DatasetFailed("required_column_missing", f"missing {missing} in the datastore fields", resource_id)
    never = {str(c).lower() for c in (spec.get("columns") or {}).get("never") or []}
    wanted = {canon: live[index] for canon, index in columns.items() if live[index].lower() not in never}
    digest, rows, total, estimated, exact_count, ended_short = hashlib.sha256(), 0, None, False, None, False
    try:
        pages = ckan.datastore_pages(ctx.http, ctx.base, resource_id, list(wanted.values()), ctx.rate,
                                     ctx.datastore_page)
        for page, (page_total, records, page_estimated) in enumerate(pages):
            if page == 0:
                total, estimated = page_total, page_estimated
                if estimated:
                    exact_count = ckan.datastore_count(ctx.http, ctx.base, resource_id, ctx.rate)
            ended_short = len(records) < ctx.datastore_page
            for record in records:
                projected = {canon: cell(record.get(field_id)) for canon, field_id in wanted.items()}
                digest.update(canonical_line(projected))
                rows += 1
                on_row({**projected, "_role": role, "_resource_id": resource_id})
    except P.DatasetFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed page stops the dataset (no partial snapshot)
        raise P.DatasetFailed("resource_unavailable", f"page at row {rows}: {type(exc).__name__}: "
                              f"{str(exc)[:300]}", resource_id) from None
    attempt["rows"] = rows
    check = _total_check(rows, total, estimated, exact_count, ended_short, resource_id)
    attempt["total_check"] = check
    fields = {"source_url": ckan.datastore_endpoint(ctx.base, resource_id), "access_method": DATASTORE_API,
              "row_count": rows, "file_size": None, "sha256": digest.hexdigest(), "schema_hash": S.schema_hash(live),
              "schema": [S.clean_name(h) for h in live], "datastore_total": total,
              "total_was_estimated": estimated, "total_check": check}
    if exact_count is not None:
        fields["datastore_exact_count"] = exact_count
    return fields, list(wanted.values())


def _total_check(rows: int, total: int | None, estimated: bool, exact_count: int | None, ended_short: bool,
                 resource_id: str) -> str:
    """How the rows read were checked against the server's count (DatasetFailed datastore_incomplete otherwise):
    exact_total (an exact `total`, equal), exact_count (an estimated `total`, the datastore_search_sql COUNT(*) equal)
    or estimate_within_2pct (no exact count: the last page short and the rows within 2 % of the estimate)."""
    if not estimated:
        if total is None or total != rows:
            raise P.DatasetFailed("datastore_incomplete", f"total {total}, {rows} rows read", resource_id)
        return "exact_total"
    if exact_count is not None:
        if exact_count != rows:
            raise P.DatasetFailed("datastore_incomplete", f"exact count {exact_count} (datastore_search_sql; total "
                                  f"{total} estimated), {rows} rows read", resource_id)
        return "exact_count"
    if not total:
        raise P.DatasetFailed("datastore_incomplete", f"total {total} (estimated), no exact count, {rows} rows read",
                              resource_id)
    deviation = abs(rows - total) / total
    if ended_short and deviation <= ckan.ESTIMATE_TOLERANCE:
        return "estimate_within_2pct"
    raise P.DatasetFailed("datastore_incomplete", f"total {total} (estimated), no exact count, {rows} rows read "
                          f"({deviation:.2%} off; tolerance {ckan.ESTIMATE_TOLERANCE:.0%}"
                          f"{'' if ended_short else ', the last page was not short'})", resource_id)
