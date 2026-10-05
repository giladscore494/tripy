"""Search bake-off (PR #46, S4): which search backend finds the Israeli version pages, measured, not argued.

The shared implementation behind the "Search bake-off" page of the web UI (a background job of the RunManager,
src/jobs/bakeoffs.py; results under <TRIPY_DATA_DIR>/bakeoffs/<id>/) and the CLI (scripts/search_bakeoff.py). Nothing
here enters a run's evidence: the bake-off only searches, ranks candidates and (optionally) fetches one page per
record and backend to read its deterministic version-page verdict.

Query set per benchmark Level 1.5 record (deterministic):

    resolver   the IL version-page resolver's queries, unchanged (il_version_pages.search_queries: the first
               `il_version_sites.search_templates` template on every site, `max_searches` of them), site-restricted
    generic    "{make_he} {model} {year} מפרט טכני" and "{make_en} {model} {year} {engine_l} specifications"

Metrics per backend (overall and per record), from the RAW provider results (before the engine's sanity rules):

    full_path_ratio        results whose URL path is not "/"
    on_site_ratio          results of site-restricted queries on that site
    version_url_hit        records with >= 1 result matching the site's il_version_sites.version_url_patterns
    first_version_rank     the best (lowest) rank of a version URL per record (median over the records with a hit)
    israeli_domain_share   results on an Israeli host (.il)
    unique_urls            distinct result URLs
    latency p50 / p95      per query
    cost                   per 1,000 queries and per record (data/search_backends.json prices; Gemini: tokens + billed
                           search queries)
    candidates             records whose resolver results give a ranked version candidate (il_version_pages
                           classify_url + slug_score: the resolver's own candidate selection, no fetch)
    accepted / rejected    with fetch_top: the top candidate fetched through the normal fetch path (robots.txt, the
                           per-domain pace, the shared document cache) and judged by version_page_verdict
"""

from __future__ import annotations

import csv
import io
import json
import re
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

SCHEMA = "tripy-search-bakeoff/1"
DEFAULT_BACKENDS = ("glm", "serper", "gemini")
GENERIC_TEMPLATES = ("{make_he} {model} {year} מפרט טכני", "{make_en} {model} {year} {engine_l} specifications")
RESULTS_PER_QUERY = 10
METRIC_COLUMNS = ("backend", "queries", "errors", "full_path_ratio", "on_site_ratio", "version_url_hit",
                  "version_url_hit_ratio", "first_version_rank", "israeli_domain_share", "unique_urls", "candidates",
                  "fetched", "accepted", "rejected", "latency_p50_ms", "latency_p95_ms", "usd", "usd_per_1000_queries",
                  "usd_per_record", "accepted_per_usd")
RECORD_COLUMNS = ("record_id", "vehicle", "backend", "queries", "results", "version_url_hit", "first_version_rank",
                  "candidate_url", "candidate_score", "verdict", "verdict_reason", "usd")


class Cancelled(RuntimeError):
    pass


@dataclass
class BakeoffConfig:
    backends: list[str] = field(default_factory=lambda: list(DEFAULT_BACKENDS))
    records: list[str] = field(default_factory=list)          # empty: every benchmark Level 1.5 record
    fetch_top: int = 1                                        # 0: no fetch (candidate selection only)
    label: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# --- records and queries ------------------------------------------------------------------------------------------------

def benchmark_records(ids: list[str] | None = None) -> list[dict]:
    """[{record_id, label, payload}] of the benchmark Level 1.5 records (the frozen snapshot), in benchmark order."""
    from .benchmark import benchmark_vehicles, vehicle_label
    from .db import build_level15_payload, load_snapshot

    vehicles = benchmark_vehicles()
    wanted = [str(i) for i in ids] if ids else [str(v["upstream_record_id"]) for v in vehicles]
    rows = {str(r["upstream_record_id"]): r for r in load_snapshot(wanted)}
    labels = {str(v["upstream_record_id"]): vehicle_label(v) for v in vehicles}
    return [{"record_id": rid, "label": labels.get(rid, rid), "payload": build_level15_payload(rows[rid])}
            for rid in wanted if rid in rows]


def record_queries(payload: dict) -> list[dict]:
    """[{kind, site, query}]: the resolver's site-restricted queries, then the two generic research queries."""
    from .document_binding import target_identity
    from .il_version_pages import search_queries, target_terms

    identity = target_identity(payload)
    terms = target_terms(payload, identity)
    out = [{"kind": "resolver", "site": domain, "query": query} for domain, query in search_queries(terms)]
    for template in GENERIC_TEMPLATES:
        query = template
        for key, value in terms.items():
            query = query.replace("{" + key + "}", str(value or ""))
        out.append({"kind": "generic", "site": None, "query": re.sub(r"\s+", " ", query).strip()})
    return out


# --- one query ----------------------------------------------------------------------------------------------------------

def _israeli(url: str) -> bool:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host.endswith(".il")


def result_flags(url: str, site: str | None) -> dict:
    from .il_version_pages import classify_url
    from .tools.search_backends import on_site

    path = unquote(urlparse(url).path or "")
    found_site, kind = classify_url(url)
    return {"full_path": path.strip() not in ("", "/"), "on_site": bool(site) and on_site(url, site),
            "version": kind == "version" and (site is None or found_site == site), "version_site": found_site
            if kind == "version" else None, "israeli": _israeli(url)}


def run_query(backend, name: str, item: dict) -> dict:
    """One provider search: {backend, kind, site, query, results: [{url, title, rank, flags}], latency_ms, usd,
    error}. The query goes as written (a resolver query carries its own `site:`); no engine cache."""
    row = {"backend": name, **item, "results": [], "latency_ms": None, "usd": 0.0, "error": None}
    t0 = time.monotonic()
    try:
        results, info = backend.query(item["query"], site=None, count=RESULTS_PER_QUERY)
        row["latency_ms"] = info.latency_ms or int((time.monotonic() - t0) * 1000)
        row["usd"] = float(info.usd or 0.0)
        if info.queries and name == "gemini":
            row["web_search_queries"] = info.queries
            row["redirects_resolved"], row["redirects_failed"] = info.redirects_resolved, info.redirects_failed
    except Exception as exc:  # noqa: BLE001 - a failed query is a measured error, never the bake-off's end
        row["latency_ms"] = int((time.monotonic() - t0) * 1000)
        row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return row
    for n, r in enumerate(results, start=1):
        url = r.url
        row["results"].append({"url": url, "title": (r.title or "")[:160], "rank": r.rank or n,
                               "raw_url": r.raw_url if r.raw_url != url else None, **result_flags(url, item["site"])})
    return row


# --- the resolver's candidate selection (dry run) and the optional fetch ------------------------------------------------

def candidates(payload: dict, rows: list[dict]) -> list[dict]:
    """The resolver's ranked version candidates of a backend's resolver results (R1: a site's version URL, scored by
    slug_score; no fetch), best first: [{url, score, site, rank}]."""
    from .document_binding import target_identity
    from .il_version_pages import classify_url, slug_score, target_terms

    identity = target_identity(payload)
    terms = target_terms(payload, identity)
    out, seen = [], set()
    for row in rows:
        if row.get("kind") != "resolver":
            continue
        for r in row.get("results") or []:
            url = str(r["url"]).split("#")[0]
            site, kind = classify_url(url)
            if kind != "version" or site != row.get("site") or url in seen:
                continue
            seen.add(url)
            scored = slug_score(url, r.get("title"), terms, identity, payload)
            out.append({"url": url, "score": scored["score"], "site": site, "rank": r.get("rank"),
                        "order": len(out)})
    out.sort(key=lambda c: (-c["score"], c["order"]))
    return out


def fetch_verdict(payload: dict, url: str, fetch: Callable[[str], dict], cache) -> dict:
    """{status: accepted | rejected | not_a_version_page | fetch_failed | robots_disallow, reason} of one candidate."""
    from .evidence_admission import AdmissionContext
    from .fields import resolve_requested_fields

    try:
        result = fetch(url) or {}
    except Exception as exc:  # noqa: BLE001
        return {"status": "fetch_failed", "reason": type(exc).__name__}
    if result.get("robots_disallow"):
        return {"status": "robots_disallow", "reason": "robots_disallow"}
    doc, status = result.get("document_id"), result.get("status")
    if not doc or result.get("error") or (isinstance(status, int) and not 200 <= status < 300):
        return {"status": "fetch_failed", "reason": str(result.get("error") or f"status_{status}")[:80]}
    propulsion = ((payload.get("engine_drivetrain") or {}).get("propulsion_normalized"))
    adm = AdmissionContext.for_run(payload, None, resolve_requested_fields(None, propulsion=propulsion), "IL")
    material = adm.material(cache, doc, None, [doc])
    verdict = getattr(material, "version_page", None) if material is not None else None
    if not verdict:
        return {"status": "not_a_version_page", "reason": None, "document_id": doc}
    return {"status": verdict.get("status"), "reason": verdict.get("reason"), "document_id": doc}


# --- metrics ------------------------------------------------------------------------------------------------------------

def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[k]


def record_summary(record: dict, backend: str, rows: list[dict], cands: list[dict], verdict: dict | None) -> dict:
    """One record x backend row (per-record metrics)."""
    results = [r for row in rows for r in row.get("results") or []]
    version_ranks = [r["rank"] for row in rows for r in row.get("results") or [] if r.get("version")]
    top = cands[0] if cands else None
    return {"record_id": record["record_id"], "vehicle": record.get("label"), "backend": backend,
            "queries": len(rows), "errors": sum(1 for row in rows if row.get("error")), "results": len(results),
            "version_url_hit": bool(version_ranks), "first_version_rank": min(version_ranks) if version_ranks else None,
            "candidate_url": top["url"] if top else None, "candidate_score": top["score"] if top else None,
            "candidates": len(cands), "verdict": (verdict or {}).get("status"),
            "verdict_reason": (verdict or {}).get("reason"), "usd": round(sum(row.get("usd") or 0 for row in rows), 6)}


def backend_metrics(backend: str, queries: list[dict], records: list[dict]) -> dict:
    """The overall metrics of one backend (module doc) over its query rows and per-record rows."""
    rows = [q for q in queries if q["backend"] == backend]
    recs = [r for r in records if r["backend"] == backend]
    results = [r for row in rows for r in row.get("results") or []]
    sited = [r for row in rows if row.get("site") for r in row.get("results") or []]
    latencies = [row["latency_ms"] for row in rows if row.get("latency_ms") is not None and not row.get("error")]
    usd = round(sum(row.get("usd") or 0 for row in rows), 6)
    hits = [r for r in recs if r["version_url_hit"]]
    accepted = sum(1 for r in recs if r.get("verdict") == "accepted")
    fetched = sum(1 for r in recs if r.get("verdict") is not None)
    reasons: dict[str, int] = {}
    for r in recs:
        if r.get("verdict") not in (None, "accepted"):
            key = f"{r['verdict']}:{r.get('verdict_reason')}" if r.get("verdict_reason") else r["verdict"]
            reasons[key] = reasons.get(key, 0) + 1
    return {"backend": backend, "queries": len(rows), "errors": sum(1 for row in rows if row.get("error")),
            "results": len(results),
            "full_path_ratio": _ratio(sum(1 for r in results if r["full_path"]), len(results)),
            "on_site_ratio": _ratio(sum(1 for r in sited if r["on_site"]), len(sited)),
            "version_url_hit": len(hits), "version_url_hit_ratio": _ratio(len(hits), len(recs)),
            "first_version_rank": statistics.median([r["first_version_rank"] for r in hits]) if hits else None,
            "israeli_domain_share": _ratio(sum(1 for r in results if r["israeli"]), len(results)),
            "unique_urls": len({r["url"] for r in results}),
            "candidates": sum(1 for r in recs if r.get("candidate_url")), "fetched": fetched, "accepted": accepted,
            "rejected": fetched - accepted, "rejected_by_reason": dict(sorted(reasons.items())),
            "latency_p50_ms": _percentile(latencies, 50), "latency_p95_ms": _percentile(latencies, 95),
            "usd": usd, "usd_per_1000_queries": round(usd / len(rows) * 1000, 4) if rows else None,
            "usd_per_record": round(usd / len(recs), 6) if recs else None,
            "accepted_per_usd": round(accepted / usd, 2) if usd else None}


def summarize(config: BakeoffConfig, queries: list[dict], records: list[dict], *, status: str = "completed",
              started_at: str | None = None, finished_at: str | None = None) -> dict:
    backends = list(dict.fromkeys(config.backends))
    return {"schema": SCHEMA, "status": status, "config": config.as_dict(), "started_at": started_at,
            "finished_at": finished_at, "records": len({r["record_id"] for r in records}),
            "queries": len(queries), "metrics": [backend_metrics(b, queries, records) for b in backends],
            "note": "Bake-off results never enter a run's evidence."}


def markdown_table(summary: dict) -> str:
    cols = ("backend", "queries", "full_path_ratio", "on_site_ratio", "version_url_hit", "first_version_rank",
            "israeli_domain_share", "unique_urls", "candidates", "accepted", "latency_p50_ms", "latency_p95_ms",
            "usd_per_1000_queries", "usd_per_record", "accepted_per_usd")
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for m in summary.get("metrics") or []:
        lines.append("| " + " | ".join("" if m.get(c) is None else str(m.get(c)) for c in cols) + " |")
    return "\n".join(lines)


def metrics_csv(summary: dict) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(METRIC_COLUMNS), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for m in summary.get("metrics") or []:
        writer.writerow(m)
    return out.getvalue()


def records_csv(records: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(RECORD_COLUMNS), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for r in records:
        writer.writerow(r)
    return out.getvalue()


# --- the run ------------------------------------------------------------------------------------------------------------

def new_id() -> str:
    return time.strftime("bakeoff-%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{uuid.uuid4().hex[:6]}"


def _utc() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, value: Any) -> None:
    from .storage.atomic import atomic_write_json

    atomic_write_json(path, value, durable=True)


def run_bakeoff(config: BakeoffConfig, out_dir: Path | str, *, make_backend: Callable[[str], Any],
                fetch: Callable[[str], dict] | None = None, cache=None, cancel: threading.Event | None = None,
                progress: Callable[[dict], None] | None = None,
                records: list[dict] | None = None) -> dict:
    """Run the bake-off and write <out_dir>/summary.json, records.jsonl (per record x backend) and queries.jsonl (every
    query with its raw results). `make_backend(name)` returns a search backend (search_backends.make_backend);
    `fetch(url)` the normal fetch path (fetch_top > 0 only). Cancellable between records: a cancelled bake-off keeps
    what it measured (status `cancelled`). Returns the summary."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started = _utc()
    records_in = records if records is not None else benchmark_records(config.records or None)
    backends = {name: make_backend(name) for name in dict.fromkeys(config.backends)}
    queries: list[dict] = []
    rows: list[dict] = []
    status = "completed"
    lock = threading.Lock()
    with open(out / "queries.jsonl", "w", encoding="utf-8") as qf, \
            open(out / "records.jsonl", "w", encoding="utf-8") as rf, ThreadPoolExecutor(len(backends) or 1) as pool:
        for index, record in enumerate(records_in):
            if cancel is not None and cancel.is_set():
                status = "cancelled"
                break
            items = record_queries(record["payload"])

            def per_backend(name: str, record=record, items=items) -> tuple[list[dict], dict]:
                done = [run_query(backends[name], name, item) for item in items]
                cands = candidates(record["payload"], done)
                verdict = None
                if config.fetch_top and fetch is not None and cands:
                    verdict = fetch_verdict(record["payload"], cands[0]["url"], fetch, cache)
                return done, record_summary(record, name, done, cands, verdict)

            for name, (done, summary) in zip(backends, pool.map(per_backend, list(backends))):
                with lock:
                    for row in done:
                        row = {"record_id": record["record_id"], **row}
                        queries.append(row)
                        qf.write(json.dumps(row, ensure_ascii=False) + "\n")
                    rows.append(summary)
                    rf.write(json.dumps(summary, ensure_ascii=False) + "\n")
            qf.flush()
            rf.flush()
            if progress is not None:
                progress({"done": index + 1, "total": len(records_in), "record_id": record["record_id"]})
    summary = summarize(config, queries, rows, status=status, started_at=started, finished_at=_utc())
    _write_json(out / "summary.json", summary)
    return summary


def live_dependencies(cache, lookup: Callable[[str], str | None] | None = None):
    """(make_backend, fetch) over the real providers and the normal fetch path: search_backends.make_backend with the
    server's keys (a GLM client for glm), and fetch_url through a tool context on the shared document `cache` with
    robots.txt respected and at most one request per il_version_sites.min_interval_s per domain (the resolver's own
    pace)."""
    import os

    import requests

    from .glm_client import GLMClient, GLMSettings
    from .il_version_pages import _default_robots, pacer, site_of
    from .source_authority import host_of
    from .tools import ToolConfig, ToolContext
    from .tools.evidence import EvidenceStore
    from .tools.fetch import fetch_url
    from .tools.search_backends import make_backend as build

    lookup = lookup or os.environ.get
    session = requests.Session()
    glm_client = None

    def make_backend(name: str):
        nonlocal glm_client
        if name == "glm" and glm_client is None:
            settings = GLMSettings.from_env()
            settings.model = settings.model or "search-only"      # search needs no model; the client requires one
            glm_client = GLMClient(settings)
        return build(name, session=session, glm=glm_client, lookup=lookup)

    ctx = ToolContext(cache=cache, evidence=EvidenceStore(), config=ToolConfig(), session=session)
    fetch_lock = threading.Lock()

    def fetch(url: str) -> dict:
        if not _default_robots(ctx, url):
            return {"robots_disallow": True}
        pacer().wait(site_of(url) or host_of(url))
        with fetch_lock:                     # the tool context's counters are not shared across threads
            return fetch_url(ctx, url)
    return make_backend, fetch


def read_rows(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in Path(path).read_text("utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []
