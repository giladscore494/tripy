"""One resource, end to end: resource_show -> package_show (the licence) -> the fail-safe checks -> the streamed,
projected rows (each passed to the dataset's aggregation) -> the provenance record. Any failure raises
provenance.DatasetFailed; the caller discards the dataset's aggregation and keeps its previous snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import ckan
from . import provenance as P
from . import schemas as S
from .download import CsvDownload, DownloadRefused, project


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
        raise P.DatasetFailed("resource_unavailable", f"{type(exc).__name__}: {str(exc)[:200]}", resource_id) from None
    licence = package.get("license")
    P.check_licence(previous, licence, resource_id)
    P.check_format(previous, meta.get("format"), ctx.accepted_formats, resource_id)
    downloaded_at = ctx.now()
    tee = None
    if spec.get("to_disk") and ctx.work_dir is not None:
        ctx.work_dir.mkdir(parents=True, exist_ok=True)
        tee = ctx.work_dir / f"{dataset}-{resource_id}.raw"
    try:
        source = ctx.http.open(meta["url"])
    except Exception as exc:  # noqa: BLE001
        raise P.DatasetFailed("resource_unavailable", f"download: {type(exc).__name__}", resource_id) from None
    try:
        try:
            body = CsvDownload(source, tee)
        except DownloadRefused as exc:
            raise P.DatasetFailed(exc.reason, exc.detail, resource_id) from None
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
    P.check_row_count(previous, rows, ctx.min_row_ratio, resource_id)
    if spec.get("qa_sample"):
        try:
            ctx.samples[resource_id] = ckan.datastore_sample(ctx.http, ctx.base, resource_id, list(columns)[:8])
        except Exception as exc:  # noqa: BLE001 - a sample never costs the build
            ctx.samples[resource_id] = [{"sample_error": type(exc).__name__}]
    return P.record(resource_id=resource_id, source_url=meta["url"], downloaded_at=downloaded_at,
                    source_last_modified=meta.get("last_modified"), license=licence, row_count=rows,
                    file_size=body.file_size, sha256=body.sha256, schema_hash=S.schema_hash(body.header),
                    ingestion_version=ctx.ingestion_version, role=role, format=meta.get("format"),
                    package_title=package.get("title"), organization=package.get("organization"),
                    schema=[S.clean_name(h) for h in body.header], encoding=body.encoding,
                    delimiter=body.delimiter, malformed_rows=body.malformed or None)
