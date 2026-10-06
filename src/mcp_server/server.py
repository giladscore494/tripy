"""The FastMCP server: tool registration, worker threads and the audit log line of every call.

Tools run in worker threads (the event loop is the web server's: a slow replay must never stall the API), at most
MAX_CONCURRENT_CALLS at a time. Each call logs exactly one line to the `tripy.mcp` logger (stdout + the rotating
server log): tool, ids, response size, milliseconds, outcome. Neither the URL nor the token ever reaches this module.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import anyio

from ..server_logging import get_logger
from . import safety
from .tools import Observer

MAX_CONCURRENT_CALLS = 2
INSTRUCTIONS = (
    "TRIPY read-only observation channel. Tools read the runs, events, results, diagnostics, binding replay, "
    "candidates, fetched documents and the server log of the TRIPY deployment; none of them starts, cancels, deletes "
    "or writes anything. Start with list_runs, then run_status / run_events (tail with the returned `next` cursor). "
    "Every response is JSON, redacted and capped at 60,000 characters; long outputs page with offset / limit (or "
    "since / next for events) and report what remains.")
_AUDIT_KEYS = ("run_id", "record_id", "run_ids", "doc_id", "field", "since", "offset", "limit", "lines")

log = get_logger("mcp")


def _ids(args: dict) -> str:
    parts = []
    for key in _AUDIT_KEYS:
        value = args.get(key)
        if value not in (None, "", []):
            parts.append(f"{key}={','.join(map(str, value)) if isinstance(value, list) else value}")
    return " ".join(parts) or "-"


class _Slots:
    """At most MAX_CONCURRENT_CALLS tools run at once (created lazily inside the server's event loop)."""

    def __init__(self) -> None:
        self.limiter: anyio.CapacityLimiter | None = None

    def get(self) -> anyio.CapacityLimiter:
        if self.limiter is None:
            self.limiter = anyio.CapacityLimiter(MAX_CONCURRENT_CALLS)
        return self.limiter


async def call(tool: str, args: dict, fn: Callable[[], dict], slots: _Slots) -> str:
    """Run one tool in a worker thread; return its redacted, capped JSON; log the audit line."""
    started, outcome, size = time.perf_counter(), "ok", 0
    try:
        def work() -> str:
            return safety.finish(fn(), safety.Redactor())

        text = await anyio.to_thread.run_sync(work, limiter=slots.get())
        size = len(text)
        return text
    except safety.ToolInputError as exc:
        outcome = "rejected"
        raise ValueError(safety.Redactor().text(str(exc))) from None
    except Exception as exc:  # noqa: BLE001 - a failing tool returns an error result, never a traceback
        outcome = f"error:{type(exc).__name__}"
        logging.getLogger("tripy.mcp").debug("tool %s failed", tool, exc_info=True)
        raise RuntimeError(f"{tool} failed: {safety.Redactor().text(f'{type(exc).__name__}: {exc}')[:500]}") from None
    finally:
        log.info("mcp call tool=%s %s chars=%d ms=%d outcome=%s", tool, safety.Redactor().text(_ids(args))[:300],
                 size, round((time.perf_counter() - started) * 1000), outcome)


def build(observer_factory: Callable[[], Observer] = Observer):
    """The FastMCP instance with the fourteen read-only tools (stateless Streamable HTTP, JSON responses)."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import ToolAnnotations

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    server = FastMCP("tripy", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
                     # the secret path is the credential; the Host header is Railway's public domain
                     transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
                     log_level="WARNING")
    root.handlers[:], root.level = handlers, level     # FastMCP's basicConfig must not change the server's logging
    slots = _Slots()

    async def run(name: str, args: dict, fn: Callable[[], dict]) -> str:
        return await call(name, args, fn, slots)

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    def tool(fn):
        return server.tool(annotations=read_only, structured_output=False)(fn)

    @tool
    async def list_runs(limit: int = 20, offset: int = 0) -> str:
        """Runs newest first: run_id, label, record_ids, status, executing, profile, started/finished, cost, wall
        time. Pages with offset / limit (max 200)."""
        args = {"limit": limit, "offset": offset}
        return await run("list_runs", args, lambda: observer_factory().list_runs(limit, offset))

    @tool
    async def run_status(run_id: str) -> str:
        """Status of one run and of each vehicle: current pipeline stage, stage states and durations, counters,
        last event time, and whether it is active (executing in this server process)."""
        return await run("run_status", {"run_id": run_id}, lambda: observer_factory().run_status(run_id))

    @tool
    async def run_events(run_id: str, record_id: str, since: int = 0, limit: int = 200,
                         kinds: list[str] | None = None) -> str:
        """events.jsonl rows of one vehicle after line `since` (1-based line numbers), optionally only some
        `kinds`. Returns `next`: pass it as `since` to get only newer events (live tail). limit max 1000."""
        args = {"run_id": run_id, "record_id": record_id, "since": since, "limit": limit}
        return await run("run_events", args,
                          lambda: observer_factory().run_events(run_id, record_id, since, limit, kinds))

    @tool
    async def run_result(run_id: str, record_id: str, offset: int = 0, limit: int = 200) -> str:
        """Compact result of one vehicle: run status, then per field its state (current evaluation) and final
        value / unit / market / provenance from result.json (or the events when the run wrote no result)."""
        args = {"run_id": run_id, "record_id": record_id, "offset": offset, "limit": limit}
        return await run("run_result", args,
                          lambda: observer_factory().run_result(run_id, record_id, offset, limit))

    @tool
    async def run_diagnostics(run_ids: list[str], offset: int = 0, limit: int = 50) -> str:
        """The per-vehicle rows of the benchmark export (diagnostics.vehicle_row), including the Binding Replay
        columns, for up to 20 runs."""
        args = {"run_ids": run_ids, "offset": offset, "limit": limit}
        return await run("run_diagnostics", args,
                          lambda: observer_factory().run_diagnostics(run_ids, offset, limit))

    @tool
    async def binding_replay(run_id: str, record_id: str, field: str | None = None, offset: int = 0,
                             limit: int = 100) -> str:
        """Binding Replay of one vehicle with the current binding code: summary plus items (optionally one
        field), each with variant_map_region / binding_basis. Computed in memory when no current replay is stored;
        never stored from here."""
        args = {"run_id": run_id, "record_id": record_id, "field": field, "offset": offset, "limit": limit}
        return await run("binding_replay", args,
                          lambda: observer_factory().binding_replay(run_id, record_id, field, offset, limit))

    @tool
    async def candidates(run_id: str, record_id: str, field: str | None = None, offset: int = 0,
                         limit: int = 100) -> str:
        """The candidate table of one vehicle (the rows of candidates.csv: values found, sources, origin,
        rejection, verified evidence, state), optionally one field."""
        args = {"run_id": run_id, "record_id": record_id, "field": field, "offset": offset, "limit": limit}
        return await run("candidates", args,
                          lambda: observer_factory().candidates(run_id, record_id, field, offset, limit))

    @tool
    async def documents(run_id: str, record_id: str, offset: int = 0, limit: int = 50) -> str:
        """Documents one vehicle run touched: doc_id, url, title, source authority, market, content type, size,
        fetched_at and the fields admitted evidence from it was used for."""
        args = {"run_id": run_id, "record_id": record_id, "offset": offset, "limit": limit}
        return await run("documents", args,
                          lambda: observer_factory().documents(run_id, record_id, offset, limit))

    @tool
    async def document_text(doc_id: str, offset: int = 0, limit: int = 20000) -> str:
        """Cached extracted text of a fetched document, paged by character offset (limit max 50,000)."""
        args = {"doc_id": doc_id, "offset": offset, "limit": limit}
        return await run("document_text", args, lambda: observer_factory().document_text(doc_id, offset, limit))

    @tool
    async def document_structure(doc_id: str, run_id: str | None = None, record_id: str | None = None,
                                 offset: int = 0, limit: int = 20) -> str:
        """Structural harvest of a document: tables with headers and column_identity, DOM pair groups with
        headers, and the Document Variant Map (regions, identity vectors, catalog candidates). Give run_id +
        record_id to compute the map for that run's target (in memory); tables page with offset / limit."""
        args = {"doc_id": doc_id, "run_id": run_id, "record_id": record_id, "offset": offset, "limit": limit}
        return await run("document_structure", args, lambda: observer_factory().document_structure(
            doc_id, run_id, record_id, offset, limit))

    @tool
    async def target_identity(record_id: str, run_id: str | None = None) -> str:
        """The Level 1.5 payload a run used for this record (newest run unless run_id is given), the derived
        target identity, and the catalog entries for its manufacturer / family / model year."""
        args = {"record_id": record_id, "run_id": run_id}
        return await run("target_identity", args, lambda: observer_factory().target_identity(record_id, run_id))

    @tool
    async def open_data_match(record_id: str, run_id: str | None = None) -> str:
        """The open structured data match of a record (identity anchors PR): the approval route, the per-source
        candidates and vetoes, the international variant and the field offers its newest run (or run_id) recorded,
        plus the match recomputed now from the local snapshots when they exist. Read-only."""
        args = {"record_id": record_id, "run_id": run_id}
        return await run("open_data_match", args, lambda: observer_factory().open_data_match(record_id, run_id))

    @tool
    async def open_data_coverage(run_id: str) -> str:
        """Open-data coverage of a run, per vehicle: route, level, designation, match status and vetoes per source,
        offers per field (offered / admitted / reference_only / alternative_definition ...), skipped fields."""
        return await run("open_data_coverage", {"run_id": run_id},
                         lambda: observer_factory().open_data_coverage(run_id))

    @tool
    async def open_data_status() -> str:
        """The open-data snapshot builds: the last build per dataset (status, reason, missing / ambiguous keys, live
        header, per-year / per-file report) and each snapshot's meta (built_at, rows, years, absent_columns, size).
        Read-only."""
        return await run("open_data_status", {}, lambda: observer_factory().open_data_status())

    @tool
    async def list_bakeoffs(limit: int = 20, offset: int = 0) -> str:
        """Search bake-offs newest first (the web UI's "Search bake-off" jobs): id, status, backends, progress and
        each backend's version_url_hit / accepted pages / USD per record."""
        args = {"limit": limit, "offset": offset}
        return await run("list_bakeoffs", args, lambda: observer_factory().list_bakeoffs(limit, offset))

    @tool
    async def bakeoff_result(bakeoff_id: str, offset: int = 0, limit: int = 100) -> str:
        """One search bake-off: its job status, the metrics per backend (full_path_ratio, on_site_ratio,
        version_url_hit, first_version_rank, israeli_domain_share, accepted pages, latency, cost) and the per-record
        rows (paged)."""
        args = {"bakeoff_id": bakeoff_id, "offset": offset, "limit": limit}
        return await run("bakeoff_result", args,
                          lambda: observer_factory().bakeoff_result(bakeoff_id, offset, limit))

    @tool
    async def catalog_search(manufacturer: str | None = None, model: str | None = None, year: int | None = None,
                             trim: str | None = None, q: str | None = None, limit: int = 50) -> str:
        """The live MILO catalog (public.catalog_variants_current over DATABASE_URL, read-only), filtered like the
        web UI's catalog browser: manufacturer + model [+ year [+ trim]], manufacturer + year [+ trim / a degem_cd in
        q], or q = an upstream record id. limit max 200. Without DATABASE_URL: {available: false}."""
        args = {"manufacturer": manufacturer, "model": model, "year": year, "trim": trim, "q": q, "limit": limit}
        return await run("catalog_search", args,
                         lambda: observer_factory().catalog_search(manufacturer, model, year, trim, q, limit))

    @tool
    async def server_log_tail(lines: int = 200, grep: str | None = None) -> str:
        """The last lines of the server log (the rotating data/logs/tripy.log), optionally only lines containing
        `grep` (case-insensitive substring). lines max 2000."""
        return await run("server_log_tail", {"lines": lines},
                          lambda: observer_factory().server_log_tail(lines, grep))

    return server


TOOL_NAMES = ("list_runs", "run_status", "run_events", "run_result", "run_diagnostics", "binding_replay",
              "candidates", "documents", "document_text", "document_structure", "target_identity", "open_data_match",
              "open_data_coverage", "open_data_status", "list_bakeoffs", "bakeoff_result", "catalog_search", "server_log_tail")
