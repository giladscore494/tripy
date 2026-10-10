"""The build-gov-datasets GitHub Action: data.gov.il government datasets -> committed, aggregated snapshots in data/gov/.

    road_survival    data/gov/road_survival.sqlite.gz    (G3; built first: G1's report and G2 read its registry names)
    new_car_prices   data/gov/new_car_prices.sqlite.gz   (G1)
    recall_notices   data/gov/recall_notices.sqlite.gz   (G2) + data/gov/recall_model_map.json (proposals)

plus data/gov/manifest.json (per dataset: file, sha256, bytes, rows, provenance per resource, stats, last_attempt),
data/gov/build_status.json (one {dataset, status, ..., resources: [{resource_id, status, access_method,
file_http_status, rows, ...}]} per selected dataset) and data/gov/report.md (the pull-request body). Each dataset is
all-or-nothing: any fail-safe stop (src/gov_data/provenance.py) keeps the previous snapshot and manifest entry and
records {"dataset", "status": "failed", "reason", "previous_snapshot_preserved": true}; the other
datasets continue. Raw files only ever live in --work (the runner's disk), never in git. Never run on the server.

    python scripts/build_gov_datasets.py [--datasets all|new_car_prices,...] [--out data/gov] [--work DIR]
                                         [--report data/gov/report.md] [--body gov-pr-body.md]
    python scripts/build_gov_datasets.py --summarize-status data/gov/build_status.json

Exit 0 when at least one dataset was built, 1 when every selected dataset failed (the report is written either way).
--summarize-status (the workflow's step after the build) reads build_status.json and writes `create_pr=true|false`
to $GITHUB_OUTPUT (false when nothing was built: every dataset failed, or no status file) and the per-dataset outcome
with the failure reasons to $GITHUB_STEP_SUMMARY; it always exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.gov_data import INGESTION_VERSION, SOURCES  # noqa: E402
from src.gov_data import prices as G1  # noqa: E402
from src.gov_data import provenance as P  # noqa: E402
from src.gov_data import recalls as G2  # noqa: E402
from src.gov_data import schemas as S  # noqa: E402
from src.gov_data import snapshot as SN  # noqa: E402
from src.gov_data import survival as G3  # noqa: E402
from src.gov_data.ckan import Http  # noqa: E402
from src.gov_data.ingest import Context, read_resource  # noqa: E402
from src.gov_data.names import ym_of  # noqa: E402

ORDER = ("road_survival", "new_car_prices", "recall_notices")
REGISTRY_INDEX = ROOT / "data" / "gov_registry_index.json"
BODY_LIMIT = 55000


def registry_keys(path: Path) -> set[str] | None:
    data = SN.read_json(path)
    years = data.get("model_years")
    return set(years) if isinstance(years, dict) and years else None


def _committed(out: Path, previous: dict, dataset: str, work: Path) -> Path | None:
    """The committed snapshot of a dataset decompressed into the work folder (the inputs of a later dataset when that
    one is not rebuilt in this run)."""
    import gzip
    import shutil

    entry = (previous.get("datasets") or {}).get(dataset) or {}
    source = out / str(entry.get("file") or "")
    if not entry.get("file") or not source.is_file() or SN.sha256_file(source) != entry.get("sha256"):
        return None
    target = work / f"committed-{dataset}.sqlite"
    with gzip.open(source, "rb") as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    return target


def _ingest(ctx: Context, name: str, spec: dict, previous_entry: dict, sink) -> list[dict]:
    """Every resource is tried, even after one failed, so each one's outcome is reported (ctx.attempts); the dataset
    is built only when all of them succeed (the aggregation of a failed dataset is discarded)."""
    records, failures = [], []
    for resource in spec["resources"]:
        try:
            records.append(read_resource(ctx, name, spec, resource, previous_entry, sink))
        except P.DatasetFailed as exc:
            failures.append(exc)
    if not failures:
        return records
    if len(failures) == 1 and len(spec["resources"]) == 1:
        raise failures[0]
    first = failures[0]
    detail = "; ".join(f"[{f.resource_id}] {f.reason}: {f.detail}" for f in failures)
    raise P.DatasetFailed(first.reason, f"{len(failures)} of {len(spec['resources'])} resources failed: {detail}",
                          first.resource_id)


def _record_checks(ctx: Context, name: str, checks: dict[str, dict]) -> None:
    """Attach the per-resource checks (date shares, unkeyed rows, ages) to the resource's build_status entry."""
    for attempt in ctx.attempts:
        if attempt["dataset"] == name and attempt["resource_id"] in checks:
            attempt["checks"] = checks[attempt["resource_id"]]


def _write(out: Path, work: Path, name: str, tables: dict, meta: dict) -> dict:
    raw = SN.write_sqlite(work / f"{name}.sqlite", tables, meta)
    target = out / f"{name}.sqlite.gz"
    SN.gzip_file(raw, target)
    raw.unlink()
    return {"file": target.name, "sha256": SN.sha256_file(target), "bytes": target.stat().st_size}


def _licence_and_attribution(spec: dict, resources: list[dict], date: str) -> dict:
    licences = sorted({str(r.get("license")) for r in resources if r.get("license") is not None})
    first = resources[0]["resource_id"] if resources else ""
    attribution = str(spec.get("attribution") or "").format(title_he=spec.get("title_he") or "", resource_id=first,
                                                             date=date[:10])
    return {"licence": " / ".join(licences) or None, "stated_licence": spec.get("stated_licence"),
            "attribution": attribution, "title": spec.get("title"), "title_he": spec.get("title_he"),
            "ministry": spec.get("ministry")}


def build_prices(ctx, name, spec, prev, out, work, state) -> tuple[dict, list[str]]:
    agg = G1.Prices()
    resources = _ingest(ctx, name, spec, prev, agg.add)
    state["prices"] = agg
    survival = state.get("survival")
    key_names = survival.key_names() if survival is not None else None
    lines, stats = G1.report(agg, registry_keys(state["registry_index"]), key_names)
    failure = agg.check()
    if failure:
        rid = spec["resources"][0]["resource_id"]
        _record_checks(ctx, name, {rid: {"unkeyed": agg.unkeyed_codes, "unkeyed_examples": agg.unkeyed_examples}})
        raise P.DatasetFailed("unkeyed_rows", f"[{rid}] {failure}", rid, report_lines=lines)
    written = _write(out, work, name, G1.tables(agg), {"dataset": name, "rows": agg.rows, "keys": stats["keys"],
                                                       "unkeyed_rows": agg.unkeyed, "source": SOURCES[name]})
    return {**written, "rows": agg.rows, "resources": resources, "stats": stats}, lines


def build_survival(ctx, name, spec, prev, out, work, state) -> tuple[dict, list[str]]:
    agg = G3.Survival()
    resources = _ingest(ctx, name, spec, prev, agg.add)
    agg.resolve()                               # S2: the degem_nm fallback, once every row was read
    state["survival"] = agg                     # the registry's names (G1 report, G2) even when a check fails below
    checks = {rid: s.summary() for rid, s in agg.resources.items()}
    _record_checks(ctx, name, checks)
    reason, problems = agg.check()
    if reason:
        failed = next((rid for rid in agg.resources if any(p.startswith(f"[{rid}]") for p in problems)), None)
        raise P.DatasetFailed(reason, "; ".join(problems), failed, report_lines=G3.resource_lines(agg))
    coverage = agg.year_coverage()
    active = next((r for r in resources if r.get("role") == "active"), {})
    ref = ym_of(active.get("source_last_modified"))
    if ref:
        ref_year = ref[0]
    else:
        dated = [k[3] for k in agg.cancelled if k[3] is not None]
        ref_year = max(dated) if dated else 0
    low, high = (int(a) for a in spec.get("ages") or (3, 20))
    min_size = int(spec.get("min_cohort_size", 200))
    rows, stats = G3.cohorts(agg, ref_year, min_size, (low, high))
    map_rows = G3.model_year_map(agg)
    lines, summary = G3.report(agg, coverage, rows, stats, map_rows, min_size, ref_year)
    meta = {"dataset": name, "cohort_basis": G3.BASIS_MODEL_YEAR, "shnat_yitzur_coverage": coverage,
            "reference_year": ref_year, "reference_month": f"{ref[0]}-{ref[1]:02d}" if ref else str(ref_year),
            "age_unit": "whole_years", "min_cohort_size": min_size, "ages": [low, high], "exclusion": G3.EXCLUSION,
            "coverage_start": stats.get("coverage_start"),
            "definition_he": G3.DEFINITION_HE[G3.BASIS_MODEL_YEAR], "source": SOURCES[name]}
    written = _write(out, work, name, G3.table_payload(rows, map_rows, agg), meta)
    return {**written, "rows": sum(agg.rows.values()), "resources": resources, "stats": summary}, lines


def _registry_inputs(state: dict, out: Path, previous: dict, work: Path) -> tuple[dict[int, Counter], Counter, list[str]]:
    """{tozeret_cd: Counter(tozeret_nm)} and Counter((tozeret_nm, kinuy_mishari)) from this run's G1 / G3, else from
    their committed snapshots; the sources used are listed."""
    names: dict[int, Counter] = {}
    models: Counter = Counter()
    used = []
    survival = state.get("survival")
    if survival is not None:
        for code, counter in survival.makes().items():
            names.setdefault(code, Counter()).update(counter)
        models.update(survival.model_names())
        used.append("road_survival (this build: the active registry)")
    else:
        path = _committed(out, previous, "road_survival", work)
        if path:
            for r in SN.query(path, "SELECT tozeret_cd, tozeret_nm, kinuy_mishari, n FROM models"):
                names.setdefault(int(r["tozeret_cd"]), Counter())[r["tozeret_nm"]] += int(r["n"])
                models[(r["tozeret_nm"], r["kinuy_mishari"])] += int(r["n"])
            used.append("road_survival (committed snapshot: the active registry)")
    prices = state.get("prices")
    if prices is not None:
        for code, counter in prices.makes().items():
            names.setdefault(code, Counter()).update(counter)
        models.update(prices.models())
        used.append("new_car_prices (this build)")
    else:
        path = _committed(out, previous, "new_car_prices", work)
        if path:
            for r in SN.query(path, "SELECT tozeret_cd, tozeret_nm, kinuy_mishari, SUM(rows) AS n FROM prices "
                                    "GROUP BY tozeret_cd, tozeret_nm, kinuy_mishari"):
                if r["tozeret_nm"]:
                    names.setdefault(int(r["tozeret_cd"]), Counter())[r["tozeret_nm"]] += int(r["n"])
                    if r["kinuy_mishari"]:
                        models[(r["tozeret_nm"], r["kinuy_mishari"])] += int(r["n"])
            used.append("new_car_prices (committed snapshot)")
    return names, models, used


def build_recalls(ctx, name, spec, prev, out, work, state) -> tuple[dict, list[str]]:
    agg = G2.Recalls()
    resources = _ingest(ctx, name, spec, prev, agg.add)
    names, models, used = _registry_inputs(state, out, state["previous"], work)
    agreement = G2.code_agreement(agg, names)
    min_rate = float(spec.get("tozar_cd_key_min_agreement", 0.98))
    code_key = bool(agreement["codes"]) and agreement["rate"] >= min_rate
    map_path = out / SN.MAP_NAME
    new_map = G2.propose_map(agg, G2.load_map(map_path), G2.catalogue_models(models))
    map_path.write_text(json.dumps(new_map, ensure_ascii=False, indent=1) + "\n", "utf-8")
    pairs = G2.map_pairs(new_map)
    lines, stats = G2.report(agg, agreement, code_key, new_map, min_rate)
    lines.insert(0, f"- registry names / catalogue models read from: {', '.join(used) or 'nothing (no G1 / G3 data)'}")
    stats["tozar_cd_disagreeing_sample"] = agreement["disagreeing"][:20]
    meta = {"dataset": name, "tozar_cd_agreement": agreement["rate"], "tozar_cd_codes": agreement["codes"],
            "tozar_cd_used_as_key": code_key, "min_agreement": min_rate, "source": SOURCES[name]}
    written = _write(out, work, name, {"recalls": (G2.COLUMNS, G2.table_rows(agg, pairs, code_key))}, meta)
    return {**written, "rows": agg.count, "resources": resources, "stats": stats}, lines


BUILDERS = {"new_car_prices": build_prices, "road_survival": build_survival, "recall_notices": build_recalls}


def run(names: list[str], out: Path, work: Path, *, http=None, now=P.utc_now, run_url: str | None = None,
        registry_index: Path = REGISTRY_INDEX, cfg: dict | None = None) -> dict:
    """Build the selected datasets. {results: {name: {status, ...}}, manifest, status: [...], sections: {name: lines}}."""
    cfg = S.config() if cfg is None else cfg
    specs = S.datasets(cfg)
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    previous = SN.manifest(out)
    entries = dict(previous.get("datasets") or {})
    ctx = Context(http=http or Http(), base=str(cfg.get("ckan_base")), accepted_formats=list(cfg.get("formats") or []),
                  min_row_ratio=float(cfg.get("min_row_ratio", 0.7)),
                  ingestion_version=str(cfg.get("ingestion_version") or INGESTION_VERSION), work_dir=work / "raw",
                  now=now)
    state: dict[str, Any] = {"previous": previous, "registry_index": registry_index}
    results, statuses, sections = {}, [], {}

    def attempts(dataset: str) -> list[dict]:
        return [{k: v for k, v in a.items() if k != "dataset"} for a in ctx.attempts if a["dataset"] == dataset]

    for name in [n for n in ORDER if n in names]:
        spec = specs[name]
        prev_entry = dict(entries.get(name) or {})
        attempt_at = now()
        try:
            built, lines = BUILDERS[name](ctx, name, spec, prev_entry, out, work, state)
        except P.DatasetFailed as exc:
            record = P.failure(name, exc, previous_exists=bool(prev_entry.get("file")))
            record["resources"] = attempts(name)
            statuses.append(record)
            results[name] = record
            sections[name] = [f"- **failed**: {exc.reason}" + (f" ({exc.detail})" if exc.detail else "")
                              + (f" [resource {exc.resource_id}]" if exc.resource_id else ""),
                              "- the previous snapshot and its manifest entry are kept", *exc.report_lines]
            if prev_entry:
                prev_entry["build_status"] = "failed"
            prev_entry["last_attempt"] = {"at": attempt_at, "status": "failed", "reason": exc.reason,
                                          "detail": exc.detail[:300] or None, "run_url": run_url}
            entries[name] = prev_entry
            continue
        finally:
            for leftover in (work / "raw").glob(f"{name}-*.raw") if (work / "raw").is_dir() else []:
                leftover.unlink()                       # raw files never outlive their dataset's build
        entry = {**built, **_licence_and_attribution(spec, built["resources"], attempt_at), "built_at": attempt_at,
                 "run_url": run_url, "build_status": "built", "source": SOURCES[name],
                 "last_attempt": {"at": attempt_at, "status": "built", "run_url": run_url}}
        entries[name] = entry
        statuses.append({"dataset": name, "status": "built", "rows": built["rows"], "sha256": built["sha256"],
                         "resources": attempts(name)})
        results[name] = {"status": "built", **entry}
        sections[name] = lines
    manifest = {"version": SN.MANIFEST_VERSION, "built_at": now(), "run_url": run_url,
                "config_version": cfg.get("version"), "ingestion_version": ctx.ingestion_version,
                "datasets": {k: entries[k] for k in sorted(entries)}}
    (out / SN.MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=1, default=str) + "\n", "utf-8")
    (out / "build_status.json").write_text(json.dumps(statuses, ensure_ascii=False, indent=1) + "\n", "utf-8")
    return {"results": results, "manifest": manifest, "status": statuses, "sections": sections,
            "samples": ctx.samples}


def report(outcome: dict, cfg: dict | None = None) -> str:
    """data/gov/report.md and the pull-request body: per dataset its status, row counts and schema per resource, and
    its section (G1 multi-price stats and the registry join, G2 code agreement and unresolved models, G3 year coverage,
    cohort basis and the largest cohorts)."""
    specs = S.datasets(S.config() if cfg is None else cfg)
    manifest = outcome["manifest"]
    lines = ["## Government datasets build", "",
             "Aggregates only (model / model-year level); no per-vehicle record is stored. Raw files never enter git.",
             "", "| dataset | status | rows | file | sha256 |", "|---|---|---|---|---|"]
    for name, result in outcome["results"].items():
        entry = (manifest.get("datasets") or {}).get(name) or {}
        status = result.get("status")
        if status == "failed":
            status = f"failed: {result.get('reason')} (previous snapshot kept)"
        lines.append(f"| {name} | {status} | {entry.get('rows', '—') if result.get('status') == 'built' else '—'} | "
                     f"{entry.get('file') or '—'} | {str(entry.get('sha256') or '—')[:12]} |")
    lines += ["", "| dataset | resource | status | access method | file HTTP status | rows | total check |",
              "|---|---|---|---|---|---|---|"]
    for item in outcome["status"]:
        for res in item.get("resources") or []:
            status = res.get("status") if res.get("status") != "failed" else f"failed: {res.get('reason')}"
            lines.append(f"| {item['dataset']} | `{res['resource_id']}` ({res.get('role')}) | {status} | "
                         f"{res.get('access_method')} | {res.get('file_http_status') or '—'} | "
                         f"{res.get('rows') if res.get('rows') is not None else '—'} | "
                         f"{res.get('total_check') or '—'} |")
    for name, section in outcome["sections"].items():
        spec = specs.get(name) or {}
        entry = (manifest.get("datasets") or {}).get(name) or {}
        lines += ["", f"### {name} — {spec.get('title')}", ""]
        if (outcome["results"].get(name) or {}).get("status") == "built":
            lines.append(f"- licence as stated by the package: {entry.get('licence')!r} (brief: "
                         f"{spec.get('stated_licence')!r})")
            for res in entry.get("resources") or []:
                size = f"{res['file_size']} bytes" if res.get("file_size") is not None else "no file"
                lines.append(f"- resource `{res['resource_id']}` ({res.get('role')}): {res['row_count']} rows via "
                             f"{res.get('access_method') or 'file_download'}, {size}, sha256 {res['sha256'][:12]}, "
                             f"last modified {res.get('source_last_modified')}, schema hash {res['schema_hash'][:12]}")
                if res.get("file_attempt"):
                    lines.append(f"  - file attempt: {res['file_attempt'].get('detail')}")
                if res.get("access_method") == "datastore_api":
                    lines.append(f"  - datastore total {res.get('datastore_total')} (estimated: "
                                 f"{bool(res.get('total_was_estimated'))}), total check {res.get('total_check')}"
                                 + (f", exact count {res['datastore_exact_count']}"
                                    if res.get("datastore_exact_count") is not None else ""))
                lines.append(f"  - schema: `{' | '.join(res.get('schema') or [])}`")
        lines += section
        samples = [s for rid, s in (outcome.get("samples") or {}).items()
                   if any(r["resource_id"] == rid for r in spec.get("resources") or [])]
        if samples and (outcome["results"].get(name) or {}).get("status") == "built":
            lines += ["", "<details><summary>QA sample (datastore_search, projected)</summary>", "", "```json",
                      json.dumps(samples, ensure_ascii=False, indent=1)[:4000], "```", "", "</details>"]
    text = "\n".join(lines) + "\n"
    return text if len(text) <= BODY_LIMIT else text[:BODY_LIMIT - 80] + "\n\n_(cut at the body limit: see data/gov/report.md)_\n"


def exit_summary(statuses: list[dict] | None) -> dict:
    """{create_pr, built, failed, text}: the pull request is opened only when at least one dataset was built; the text
    lists every dataset's outcome, the failure reasons verbatim and each resource's access method."""
    statuses = [s for s in statuses or [] if isinstance(s, dict)]
    built = [s for s in statuses if s.get("status") == "built"]
    failed = [s for s in statuses if s.get("status") != "built"]
    if not statuses:
        head = "no build status: nothing was built; the pull-request step is skipped"
    elif not built:
        head = "every dataset failed; the pull-request step is skipped"
    else:
        head = f"{len(built)} built, {len(failed)} failed; the pull request is opened"
    lines = [f"## build-gov-datasets: {head}", ""]
    for item in statuses:
        if item.get("status") == "built":
            lines.append(f"- {item.get('dataset')}: built, {item.get('rows')} rows")
        else:
            lines.append(f"- {item.get('dataset')}: failed: {item.get('reason')}"
                         + (f" ({item.get('detail')})" if item.get("detail") else "")
                         + (f" [resource {item.get('resource_id')}]" if item.get("resource_id") else ""))
        for res in item.get("resources") or []:
            lines.append(f"  - `{res.get('resource_id')}` ({res.get('role')}): {res.get('status')} via "
                         f"{res.get('access_method')}, file HTTP status {res.get('file_http_status') or '—'}, "
                         f"rows {res.get('rows') if res.get('rows') is not None else '—'}"
                         + (f", total check {res['total_check']}" if res.get("total_check") else "")
                         + (f", redirect to {res['file_redirect_host']}" if res.get("file_redirect_host") else "")
                         + (f": {res.get('reason')} ({res.get('detail')})" if res.get("status") == "failed" else ""))
    return {"create_pr": bool(built), "built": len(built), "failed": len(failed), "text": "\n".join(lines) + "\n"}


def summarize_status(path: Path) -> dict:
    """The workflow step after the build: build_status.json -> $GITHUB_OUTPUT (create_pr, built, failed) and the
    step summary."""
    try:
        statuses = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        statuses = []
    summary = exit_summary(statuses if isinstance(statuses, list) else [])
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write(f"create_pr={'true' if summary['create_pr'] else 'false'}\n"
                         f"built={summary['built']}\nfailed={summary['failed']}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(summary["text"] + "\n")
    print(summary["text"])
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--datasets", default="all")
    parser.add_argument("--out", default=str(ROOT / "data" / "gov"))
    parser.add_argument("--work", default="")
    parser.add_argument("--report", default="")
    parser.add_argument("--body", default="")
    parser.add_argument("--summarize-status", default="", metavar="BUILD_STATUS_JSON")
    args = parser.parse_args(argv)
    if args.summarize_status:
        summarize_status(Path(args.summarize_status))
        return 0
    names = S.selected(args.datasets)
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="gov-data-"))
    outcome = run(names, Path(args.out), work, run_url=os.environ.get("GOV_DATA_RUN_URL") or None)
    full = report(outcome)
    if args.report:
        Path(args.report).write_text(full, "utf-8")
    if args.body:
        Path(args.body).write_text(full, "utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(full)
    print(full)
    for status in outcome["status"]:
        if status["status"] == "failed":
            print(f"::warning::{status['dataset']}: {status['reason']} ({status.get('detail') or ''}); the previous "
                  "snapshot is kept", file=sys.stderr)
    built = sum(1 for s in outcome["status"] if s["status"] == "built")
    return 0 if built else 1


if __name__ == "__main__":
    sys.exit(main())
