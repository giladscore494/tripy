"""The read-only MCP tools: plain functions over the same storage and loaders the HTTP API reads.

Nothing here writes: documents are read through binding_replay.ReadOnlyCache (a derived extraction missing from disk
is computed in memory only), the RunManager is only looked up (never created: creating one reconciles run state on
disk), diagnostics are never rebuilt onto disk and a Binding Replay computed on demand is not persisted.

Every function returns a JSON-like dict; server.py redacts and caps it (safety.finish) and writes the audit line.
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any

from .. import diagnostics as diag_mod
from ..binding_replay import ReadOnlyCache, _run_inputs, load_or_replay
from ..document_binding import catalog_family_entries, single_catalog_trim, target_identity as build_identity
from ..fields import normalize_field_name
from ..jobs.manager import existing_manager
from ..runstate.pipeline import PipelineCache
from ..runstate.repository import FileRunRepository
from ..server_logging import log_file_path
from ..source_authority import classify_source, source_market
from ..storage import trace
from ..storage.paths import DataPaths, resolve_paths
from ..storage.run_loader import document_text as run_document_text, load_run, run_document_metas
from ..storage.run_log import read_events
from . import safety
from .safety import ToolInputError, clamp, page

MAX_RUN_IDS = 20
_PIPELINES = PipelineCache()          # in-memory incremental event readers (never written anywhere)


class Observer:
    """The tools over one data root (TRIPY_DATA_DIR / MILO_RUNS_DIR / MILO_CACHE_DIR, as the application resolves)."""

    def __init__(self, paths: DataPaths | None = None, catalog=None):
        self.paths = paths or resolve_paths()
        self._catalog = catalog                 # PR #47 (B3): a src.catalog.CatalogBrowser (tests); default DATABASE_URL
        self.runs_dir = Path(self.paths.runs_dir)
        self.cache_dir = Path(self.paths.cache_dir)
        self.repository = FileRunRepository(self.runs_dir)

    # -- shared helpers ------------------------------------------------------------------------------------------
    def _cache(self, vehicle: Path | None = None) -> ReadOnlyCache:
        return ReadOnlyCache(self.cache_dir, vehicle)

    def _record(self, run_id: Any):
        folder = safety.run_dir(self.runs_dir, run_id)
        record = self.repository.get(folder.name)
        if record is None:
            raise ToolInputError(f"unknown run_id: {folder.name!r}")
        return record

    def _active_ids(self) -> set[str] | None:
        """run ids executing in this process (RunManager.active_runs semantics); None: no manager exists yet, so
        nothing executes here."""
        manager = existing_manager(self.paths)
        return None if manager is None else {r.run_id for r in manager.active_runs()}

    def _pipeline(self, vehicle: Path):
        return _PIPELINES.get(vehicle / "events.jsonl", vehicle.name)

    def _vehicle_dirs(self, run_folder: Path, record_ids: list[str]) -> list[Path]:
        names = list(dict.fromkeys([*record_ids, *(c.name for c in sorted(run_folder.iterdir()) if c.is_dir())]))
        out = []
        for name in names:
            try:
                out.append(safety.vehicle_dir(self.runs_dir, run_folder.name, name))
            except ToolInputError:
                continue
        return out

    def _document_dir(self, document_id: str) -> Path | None:
        """The cached copy of a document: the shared cache first, else a run's exported documents/ copy."""
        roots = [self.cache_dir, self.runs_dir]
        shared = self.cache_dir / "documents" / document_id
        if safety.contained_document_dir(shared, roots):
            return shared
        if self.runs_dir.is_dir():
            for folder in sorted(self.runs_dir.glob(f"*/*/documents/{document_id}")):
                if safety.contained_document_dir(folder, roots):
                    return folder
        return None

    def _document(self, document_id: Any) -> tuple[str, Path, ReadOnlyCache, dict]:
        doc = safety.doc_id(document_id)
        folder = self._document_dir(doc)
        if folder is None:
            raise ToolInputError(f"unknown doc_id: {doc!r}")
        # a run's exported copy sits at <run>/<record>/documents/<doc>; ReadOnlyCache wants the vehicle folder
        cache = self._cache(None if folder.parent.parent == self.cache_dir else folder.parent.parent)
        return doc, folder, cache, cache.get(doc) or {}

    # -- runs ----------------------------------------------------------------------------------------------------
    # -- search bake-offs (PR #46, S4): <data_dir>/bakeoffs/<id>/, read from disk only ------------------------------
    def _bakeoffs_root(self) -> Path:
        return Path(self.paths.data_dir) / "bakeoffs"

    def _bakeoff_dir(self, bakeoff_id: Any) -> Path:
        name = str(bakeoff_id or "").strip()
        if not name or "/" in name or "\\" in name or name.startswith(".") \
                or not (self._bakeoffs_root() / name / "status.json").is_file():
            raise ToolInputError(f"unknown bakeoff id: {name[:80]!r}")
        return self._bakeoffs_root() / name

    @staticmethod
    def _json_file(path: Path) -> dict | None:
        import json

        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def list_bakeoffs(self, limit: Any = 20, offset: Any = 0) -> dict:
        root = self._bakeoffs_root()
        states = [s for s in (self._json_file(p / "status.json") for p in root.iterdir() if p.is_dir())
                  if s] if root.is_dir() else []
        states.sort(key=lambda s: (s.get("created_at") or "", s.get("bakeoff_id") or ""), reverse=True)

        def row(state: dict) -> dict:
            summary = self._json_file(root / str(state.get("bakeoff_id")) / "summary.json") or {}
            return {k: state.get(k) for k in ("bakeoff_id", "label", "status", "created_at", "finished_at",
                                               "progress", "error")} | {
                "backends": (state.get("config") or {}).get("backends"),
                "fetch_top": (state.get("config") or {}).get("fetch_top"),
                "metrics": [{k: m.get(k) for k in ("backend", "version_url_hit", "accepted", "usd_per_record")}
                            for m in summary.get("metrics") or []]}
        return page(states, offset, limit, lambda chunk: {"bakeoffs": [row(s) for s in chunk]}, max_limit=200,
                    default_limit=20)

    def bakeoff_result(self, bakeoff_id: Any, offset: Any = 0, limit: Any = 100) -> dict:
        from ..search_bakeoff import read_rows

        folder = self._bakeoff_dir(bakeoff_id)
        rows = read_rows(folder / "records.jsonl")
        return page(rows, offset, limit, lambda chunk: {
            "bakeoff": self._json_file(folder / "status.json"), "summary": self._json_file(folder / "summary.json"),
            "records": chunk}, max_limit=500, default_limit=100)

    # -- the live MILO catalog (PR #47, B3): read-only, the same queries as GET /api/catalog -------------------------
    def _catalog_browser(self):
        if self._catalog is None:
            from ..catalog import CatalogBrowser, database_query
            from ..db import database_url

            dsn = database_url()
            self._catalog = CatalogBrowser(database_query(dsn) if dsn else None)
        return self._catalog

    def catalog_search(self, manufacturer: Any = None, model: Any = None, year: Any = None, trim: Any = None,
                       q: Any = None, limit: Any = 50) -> dict:
        from ..catalog import MAX_LIMIT, CatalogQueryRefused, CatalogUnavailable

        browser = self._catalog_browser()
        try:
            year_value = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            raise ToolInputError("year must be a number") from None
        try:
            return browser.search(manufacturer=str(manufacturer) if manufacturer else None,
                                  model=str(model) if model else None, year=year_value,
                                  trim=str(trim) if trim else None, q=str(q)[:60] if q else None,
                                  limit=clamp(limit, 1, MAX_LIMIT, 50), offset=0)
        except CatalogUnavailable as exc:
            return {"available": False, "error": str(exc)}
        except CatalogQueryRefused as exc:
            raise ToolInputError(str(exc)) from None

    def list_runs(self, limit: Any = 20, offset: Any = 0) -> dict:
        from ..runstate.report import elapsed_s

        records = self.repository.list_runs()
        active = self._active_ids()

        def row(record) -> dict:
            vehicles = (record.report or {}).get("vehicles") or {}
            costs = [v.get("cost_usd") for v in vehicles.values()]
            if not vehicles:
                try:
                    folder = safety.run_dir(self.runs_dir, record.run_id)
                    costs = [self._pipeline(v).live.cost_usd() for v in self._vehicle_dirs(folder, record.record_ids)]
                except ToolInputError:
                    costs = []
            return {"run_id": record.run_id, "label": record.label, "record_ids": record.record_ids,
                    "status": record.status, "executing": bool(active and record.run_id in active),
                    "profile": (record.request or {}).get("run_profile"), "stage": record.stage,
                    "created_at": record.created_at, "started_at": record.started_at,
                    "finished_at": record.finished_at,
                    "cost_usd": round(sum(costs), 6) if costs and all(c is not None for c in costs) else None,
                    "wall_time_s": round(elapsed_s(record), 1) if elapsed_s(record) is not None else None,
                    "legacy": record.legacy}

        return page(records, offset, limit, lambda chunk: {"runs_dir": str(self.runs_dir),
                                                            "runs": [row(r) for r in chunk]},
                    max_limit=200, default_limit=20)

    def run_status(self, run_id: Any) -> dict:
        record = self._record(run_id)
        folder = safety.run_dir(self.runs_dir, record.run_id)
        active = self._active_ids()
        vehicles = []
        for vehicle in self._vehicle_dirs(folder, record.record_ids):
            pipeline = self._pipeline(vehicle)
            view = pipeline.view()
            vehicles.append({
                "record_id": vehicle.name, **(record.vehicles.get(vehicle.name) or {}),
                "current_stage": view["current_stage"], "activity": view.get("activity"),
                "engine_status": view.get("engine_status"), "stop_reason": view.get("stop_reason"),
                "started_at": view.get("started_at"), "last_event_at": view.get("last_event_at"),
                "finished_at": view.get("finished_at"), "events_seen": view.get("events_seen"),
                "stages": [{k: s.get(k) for k in ("key", "state", "duration_s", "note")} for s in view["stages"]],
                "counters": view.get("counters"), "errors": view.get("errors"),
                "cost_usd": pipeline.live.cost_usd()})
        return {"run_id": record.run_id, "label": record.label, "status": record.status, "stage": record.stage,
                "durable_active": record.active,
                "active": bool(active and record.run_id in active),
                "active_basis": ("RunManager.active_runs() of this process" if active is not None else
                                 "no RunManager exists in this process yet, so nothing executes here"),
                "cancel_requested": record.cancel_requested, "heartbeat_at": record.heartbeat_at,
                "created_at": record.created_at, "started_at": record.started_at, "finished_at": record.finished_at,
                "updated_at": record.updated_at, "error": record.error, "profile": (record.request or {}).get(
                    "run_profile"), "legacy": record.legacy, "vehicles": vehicles}

    def run_events(self, run_id: Any, record_id: Any, since: Any = 0, limit: Any = 200,
                   kinds: list[str] | None = None) -> dict:
        """events.jsonl rows after line `since` (1-based line numbers of complete lines; a torn last line waits)."""
        import json

        vehicle = safety.vehicle_dir(self.runs_dir, run_id, record_id)
        since = clamp(since, 0, 10**9, 0)
        limit = clamp(limit, 1, 1000, 200)
        wanted = {str(k) for k in kinds} if kinds else None
        budget = safety.MAX_RESPONSE_CHARS - 4_000
        rows, used, line_no, last, more = [], 0, 0, since, False
        path = vehicle / "events.jsonl"
        if path.is_file():
            with path.open("rb") as fh:
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break                     # a line still being written: the next call returns it
                    line_no += 1
                    if line_no <= since:
                        continue
                    if len(rows) >= limit:
                        more = True
                        break
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        last = line_no            # a line cut off by a hard kill is skipped, as read_events does
                        continue
                    if not isinstance(event, dict) or (wanted is not None and event.get("kind") not in wanted):
                        last = line_no
                        continue
                    row = safety.clip({"line": line_no, **event})
                    size = len(safety.dumps(row))
                    if rows and used + size > budget:
                        more = True
                        break
                    rows.append(row)
                    used += size
                    last = line_no
        return {"run_id": vehicle.parent.name, "record_id": vehicle.name, "since": since, "next": last,
                "returned": len(rows), "more": more, "events": rows,
                "note": "pass `next` as `since` to tail new events"}

    def run_result(self, run_id: Any, record_id: Any, offset: Any = 0, limit: Any = 200) -> dict:
        from ..field_recovery import current_evaluation
        from ..schemas import iter_fields

        vehicle = safety.vehicle_dir(self.runs_dir, run_id, record_id)
        result = load_run(self.runs_dir, vehicle.parent.name, vehicle.name, cache=self._cache(vehicle)) or {}
        events = read_events(vehicle / "events.jsonl")
        started = trace.first_event(events, "run_started") or {}
        specs = started.get("requested_field_specs") or []
        values = {normalize_field_name(name): entry for name, entry in iter_fields(result.get("output"))}
        states = {e["field"]: e for e in current_evaluation(events, specs, started.get("target_market"))} \
            if specs else {}
        names = list(dict.fromkeys([*states, *values]))
        rows = []
        for name in names:
            state, entry = states.get(name) or {}, values.get(name) or {}
            rows.append({"field": name, "state": state.get("state"), "info": state.get("info") or None,
                         "evidence_ids": state.get("evidence_ids") or entry.get("evidence_ids") or None,
                         "value": entry.get("value", entry.get("values")), "unit": entry.get("unit"),
                         "market": entry.get("market"), "provenance": entry.get("provenance")})
        head = {"run_id": vehicle.parent.name, "record_id": vehicle.name, "status": result.get("status"),
                "result_source": result.get("result_source"), "synthesized": bool(result.get("synthesized")),
                "output_source": result.get("output_source"), "stop_reason": result.get("stop_reason"),
                "finalization": result.get("finalization"), "error": result.get("error"),
                "duration_s": result.get("duration_s"), "cost_usd": result.get("cost"),
                "target_market": result.get("target_market") or started.get("target_market"),
                "state_counts": {s: sum(1 for r in rows if r["state"] == s) for s in
                                 sorted({str(r["state"]) for r in rows})}}
        return page(rows, offset, limit, lambda chunk: {**head, "fields": chunk}, max_limit=500, default_limit=200)

    def run_diagnostics(self, run_ids: Any, offset: Any = 0, limit: Any = 50) -> dict:
        ids = [run_ids] if isinstance(run_ids, str) else list(run_ids or [])
        if not ids:
            raise ToolInputError("run_ids is empty")
        if len(ids) > MAX_RUN_IDS:
            raise ToolInputError(f"at most {MAX_RUN_IDS} run_ids per call")
        folders = [safety.run_dir(self.runs_dir, r).name for r in ids]
        rows = []
        for vehicle in diag_mod.vehicle_dirs(self.runs_dir, folders):
            diag = diag_mod.diagnostics_from_dir(vehicle, persist_replay=False)
            if diag:
                rows.append(diag_mod.vehicle_row(diag))
        return page(rows, offset, limit, lambda chunk: {"run_ids": folders, "per_vehicle": chunk,
                                                         "note": "the per_vehicle rows of the benchmark export"},
                    max_limit=200, default_limit=50)

    def binding_replay(self, run_id: Any, record_id: Any, field: str | None = None, offset: Any = 0,
                       limit: Any = 100) -> dict:
        vehicle = safety.vehicle_dir(self.runs_dir, run_id, record_id)
        if not (vehicle / "events.jsonl").is_file():
            raise ToolInputError("this vehicle run has no events.jsonl to replay")
        replay = load_or_replay(vehicle, self.cache_dir if (self.cache_dir / "documents").is_dir() else None,
                                persist=False)
        summary = dict(replay.get("summary") or {})
        items = list(replay.get("items") or [])
        if field:
            name = normalize_field_name(field)
            items = [i for i in items if normalize_field_name(i.get("field")) == name]
            summary["fields"] = {k: v for k, v in (summary.get("fields") or {}).items() if k == name}
        return page(items, offset, limit, lambda chunk: {"run_id": vehicle.parent.name, "record_id": vehicle.name,
                                                          "field": field, "summary": safety.clip(summary, 2_000),
                                                          "items": chunk},
                    max_limit=500, default_limit=100)

    def candidates(self, run_id: Any, record_id: Any, field: str | None = None, offset: Any = 0,
                   limit: Any = 100) -> dict:
        from ..runstate.live_state import candidate_table_rows

        vehicle = safety.vehicle_dir(self.runs_dir, run_id, record_id)
        events = read_events(vehicle / "events.jsonl")
        started = trace.first_event(events, "run_started") or {}
        specs = started.get("requested_field_specs") or []
        rows = candidate_table_rows(events, specs, started.get("vehicle_label")) if specs else []
        names = [s["name"] for s in specs if s.get("applicable", True)]
        rows = [{"field": name, **row} for name, row in zip(names, rows)]
        if field:
            wanted = normalize_field_name(field)
            rows = [r for r in rows if r["field"] == wanted]
        return page(rows, offset, limit, lambda chunk: {"run_id": vehicle.parent.name, "record_id": vehicle.name,
                                                         "rows": chunk, "note": "the rows of candidates.csv"},
                    max_limit=500, default_limit=100)

    # -- documents -----------------------------------------------------------------------------------------------
    def documents(self, run_id: Any, record_id: Any, offset: Any = 0, limit: Any = 50) -> dict:
        vehicle = safety.vehicle_dir(self.runs_dir, run_id, record_id)
        cache = self._cache(vehicle)
        result = load_run(self.runs_dir, vehicle.parent.name, vehicle.name, cache=cache) or {}
        events = read_events(vehicle / "events.jsonl")
        started = trace.first_event(events, "run_started") or {}
        manufacturer = (started.get("vehicle_label") or {}).get("manufacturer")
        used: dict[str, list[str]] = {}
        for item in trace.evidence_items(events):
            if item.get("admission_status", "accepted") == "accepted" and item.get("document_id"):
                name = normalize_field_name(item.get("field"))
                if name and name not in used.setdefault(str(item["document_id"]), []):
                    used[str(item["document_id"])].append(name)
        metas = run_document_metas(result, self.runs_dir, cache)

        def row(meta: dict) -> dict:
            doc = str(meta.get("document_id"))
            url = meta.get("final_url") or meta.get("url")
            market, basis = source_market(url, cache.read_text(doc)[:20_000] if safety.is_doc_id(doc)
                                          else "")
            return {"doc_id": doc, "url": url, "title": meta.get("title"),
                    **classify_source(url, manufacturer), "market": market, "market_basis": basis,
                    "kind": meta.get("kind"), "doc_type": meta.get("doc_type"),
                    "content_type": meta.get("content_type"), "size_bytes": meta.get("bytes"),
                    "text_chars": meta.get("text_chars"), "fetched_at": meta.get("fetched_at"),
                    "status": meta.get("status"), "used_for_fields": used.get(doc, []),
                    "meta_source": cache.sources.get(doc) or meta.get("_meta_source")}

        return page(metas, offset, limit, lambda chunk: {"run_id": vehicle.parent.name, "record_id": vehicle.name,
                                                          "documents": [row(m) for m in chunk]},
                    max_limit=200, default_limit=50)

    def document_text(self, doc_id: Any, offset: Any = 0, limit: Any = 20_000) -> dict:
        doc, folder, cache, meta = self._document(doc_id)
        owner = None if folder.parent.parent == self.cache_dir else {"batch_id": folder.parent.parent.parent.name,
                                                                     "record_id": folder.parent.parent.name}
        text = run_document_text(doc, owner, self.runs_dir, cache)
        total = len(text)
        offset = clamp(offset, 0, total, 0)
        limit = clamp(limit, 1, 50_000, 20_000)
        chunk = text[offset:offset + limit]
        remaining = max(0, total - offset - len(chunk))
        return {"doc_id": doc, "url": meta.get("final_url") or meta.get("url"), "title": meta.get("title"),
                "offset": offset, "returned_chars": len(chunk), "total_chars": total, "remaining_chars": remaining,
                "next_offset": offset + len(chunk) if remaining else None, "text": chunk}

    def document_structure(self, doc_id: Any, run_id: Any = None, record_id: Any = None, offset: Any = 0,
                           limit: Any = 20) -> dict:
        """Tables (with column_identity), DOM pair groups (with headers / column_identity) and the Document Variant
        Map. With run_id + record_id the map is computed (in memory) for that run's target; without, the maps
        already cached with the document are listed."""
        import json

        from ..evidence_admission import AdmissionContext, document_text as admission_text
        from ..structure_harvest import html_pairs
        from ..tools.extract import document_tables

        doc, folder, cache, meta = self._document(doc_id)
        if run_id or record_id:
            vehicle = safety.vehicle_dir(self.runs_dir, run_id, record_id)
            cache = ReadOnlyCache(self.cache_dir, vehicle)
            events, payload, vehicle_meta, specs, market = _run_inputs(vehicle)
            adm = AdmissionContext.for_run(payload, vehicle_meta, specs, market)
            from ..document_binding import apply_fingerprint
            from ..gov_registry import fingerprint_from_events

            apply_fingerprint(adm.identity, fingerprint_from_events(events))
        else:
            vehicle, adm = None, AdmissionContext.default(None)
        is_html = meta.get("doc_type") == "html" or meta.get("kind") == "rendered"
        html = cache.read_body(doc).decode("utf-8", errors="replace") if is_html else None
        tables = document_tables(cache, doc, meta, html)
        d = adm.dictionary
        groups: dict[str, list[dict]] = {}
        if html is not None:
            for pair in html_pairs(html, cache.read_text(doc), alias_pattern=d.any_alias, trim_header=d.trim_header,
                                   identity_rows=d.column_identity_rows):
                groups.setdefault(pair.get("header") or "", []).append(pair)
        dom_groups = [{"header": header or None, "column_identity": next((p.get("column_identity") for p in pairs
                                                                          if p.get("column_identity")), None),
                       "pairs": [{k: p.get(k) for k in ("label", "value", "kind")} for p in pairs]}
                      for header, pairs in groups.items()]
        if vehicle is not None:
            material = admission_text(cache, {**meta, "document_id": doc}, remember=False)
            variant_maps = {"computed_for": {"run_id": vehicle.parent.name, "record_id": vehicle.name,
                                             "identity": adm.identity.as_dict()},
                            "map": adm.variant_map(cache, material)}
        else:
            cached = {}
            for path in sorted(folder.glob("derived_variant_map_*.json")):
                try:
                    cached[path.stem.removeprefix("derived_")] = json.loads(path.read_text("utf-8"))
                except (OSError, ValueError):
                    continue
            variant_maps = {"cached": cached, "note": "pass run_id and record_id to compute the map for that "
                                                      "run's target (in memory, not stored)"}
        table_rows = [{"index": i, "header": t.get("header"), "column_identity": t.get("column_identity"),
                       "caption": t.get("caption"), "rows": t.get("rows")} for i, t in enumerate(tables)]
        return page(table_rows, offset, limit, lambda chunk: {
            "doc_id": doc, "url": meta.get("final_url") or meta.get("url"), "title": meta.get("title"),
            "doc_type": meta.get("doc_type"), "tables": chunk, "dom_groups": safety.clip(dom_groups, 500),
            "variant_map": safety.clip(variant_maps, 2_000), "paging_applies_to": "tables"},
            max_limit=100, default_limit=20)

    def target_identity(self, record_id: Any, run_id: Any = None) -> dict:
        record = safety._check_id(record_id, "record_id")
        if run_id:
            vehicle = safety.vehicle_dir(self.runs_dir, run_id, record)
            runs = [vehicle.parent.name]
        else:
            found = [p for p in self.runs_dir.glob(f"*/{record}") if p.is_dir() and not p.parent.name.startswith(
                ("_", ".")) and safety.inside(p, self.runs_dir)] if self.runs_dir.is_dir() else []
            if not found:
                raise ToolInputError(f"unknown record_id: {record!r}")
            found.sort(key=lambda p: p.parent.name, reverse=True)
            runs = [p.parent.name for p in found]
            vehicle = safety.vehicle_dir(self.runs_dir, runs[0], record)
        events, payload, vehicle_meta, specs, market = _run_inputs(vehicle)
        identity = build_identity(payload, vehicle_meta, market)
        from ..document_binding import apply_fingerprint
        from ..gov_registry import fingerprint_event, fingerprint_from_events

        fingerprint = fingerprint_from_events(events)
        apply_fingerprint(identity, fingerprint)
        match = next((e for e in events if e.get("kind") == "open_data_match"), None)
        return {"record_id": record, "run_id": vehicle.parent.name, "runs_with_record": runs[:50],
                "target_market": market, "level15_payload": safety.clip(payload, 2_000),
                "identity_fingerprint": fingerprint_event(fingerprint) if fingerprint else None,
                "open_data_match": safety.clip({k: v for k, v in match.items() if k not in ("kind", "seq")}, 20_000)
                if match else None,
                "vehicle_label": vehicle_meta, "target_identity": identity.as_dict(),
                "single_catalog_trim": single_catalog_trim(identity),
                "catalog_entries": catalog_family_entries(identity)}

    # -- open data (identity anchors PR) -------------------------------------------------------------------------
    def _vehicle_of(self, record: str, run_id: Any = None) -> Path:
        if run_id:
            return safety.vehicle_dir(self.runs_dir, run_id, record)
        found = [p for p in self.runs_dir.glob(f"*/{record}") if p.is_dir() and not p.parent.name.startswith(
            ("_", ".")) and safety.inside(p, self.runs_dir)] if self.runs_dir.is_dir() else []
        if not found:
            raise ToolInputError(f"unknown record_id: {record!r}")
        found.sort(key=lambda p: p.parent.name, reverse=True)
        return safety.vehicle_dir(self.runs_dir, found[0].parent.name, record)

    def open_data_match(self, record_id: Any, run_id: Any = None) -> dict:
        """The open-data match of a record: what its newest run (or `run_id`) recorded (route, candidates, vetoes,
        offers) and, when the local snapshots exist, the match recomputed now from them (read-only SQLite)."""
        from ..gov_registry import fingerprint_from_events, identity_fingerprint
        from ..open_data import datasets as open_datasets
        from ..open_data.match import match
        from ..open_data.offers import field_offers

        record = safety._check_id(record_id, "record_id")
        vehicle = self._vehicle_of(record, run_id)
        events, payload, vehicle_meta, specs, market = _run_inputs(vehicle)
        recorded = next((e for e in events if e.get("kind") == "open_data_match"), None)
        fingerprint = fingerprint_from_events(events) or identity_fingerprint(payload)
        now = None
        if any(open_datasets.available(s) for s, cfg in open_datasets.datasets().items()
               if not cfg.get("identity_only")):
            result = match(fingerprint, payload)
            for src in result["sources"].values():
                src["survivors"] = (src.get("survivors") or [])[:8]
            now = {**result, "offers": field_offers(result)}
        return {"record_id": record, "run_id": vehicle.parent.name,
                "recorded": {k: v for k, v in recorded.items() if k not in ("kind", "seq")} if recorded else None,
                "now": now, "snapshots": str(open_datasets.repo_dir())}

    def open_data_status(self) -> dict:
        """The committed open-data snapshots: data/open/manifest.json (built by the build-open-data GitHub Action:
        build date, run URL, per dataset rows / years / files / absent columns / units / sha256), whether the engine
        can read each one (sha256 verified), and the Action's last live-schema probe (data/open/probe.json). Read-only;
        nothing is built on the server."""
        from ..open_data import datasets as open_datasets

        view = open_datasets.status()
        return {"manifest": safety.clip(open_datasets.manifest(), 30_000),
                "datasets": [{k: d.get(k) for k in ("dataset", "available", "problem", "built_at", "rows", "bytes",
                                                     "absent_columns", "policy")} for d in view["datasets"]],
                "probe": safety.clip(open_datasets.probe_summary(), 20_000),
                "no_snapshot": sorted(d["dataset"] for d in view["datasets"]
                                      if not d["identity_only"] and not d["available"])}

    def open_data_coverage(self, run_id: Any) -> dict:
        """Per vehicle of a run: the approval route, the open-data level / designation, the match status per source,
        the offers per field (offered / admitted / reference / vetoed ...) and the fields the research plan skipped."""
        record = self._record(run_id)
        folder = safety.run_dir(self.runs_dir, record.run_id)
        rows = []
        for vehicle in self._vehicle_dirs(folder, record.record_ids):
            events = read_events(vehicle / "events.jsonl")
            od = next((e for e in events if e.get("kind") == "open_data_match"), {}) or {}
            fp = next((e for e in events if e.get("kind") == "identity_fingerprint"), {}) or {}
            offers: dict[str, list] = {}
            for offer in od.get("offers") or []:
                offers.setdefault(offer.get("field"), []).append(
                    {k: offer.get(k) for k in ("source", "value", "status", "definition", "identified_by", "admitted",
                                               "reason") if offer.get(k) not in (None, "")})
            rows.append({"record_id": vehicle.name, "approval_route": (fp.get("approval_route") or {}).get("route"),
                         "code_family": fp.get("code_family"), "equivalent_codes": fp.get("equivalent_codes"),
                         "mode": od.get("mode"), "route": od.get("route"), "level": od.get("level"),
                         "designation": od.get("designation"),
                         "match_status": {s: (v or {}).get("status") for s, v in (od.get("sources") or {}).items()},
                         "vetoes": {s: (v or {}).get("veto_counts") for s, v in (od.get("sources") or {}).items()
                                    if (v or {}).get("veto_counts")},
                         "offers": offers, "admitted": od.get("admitted") or [],
                         "fields_skipped_open_data": sorted({f for e in events if e.get("kind") == "research_plan"
                                                             for f in e.get("skipped_open_data_resolved") or []})})
        return {"run_id": record.run_id, "vehicles": rows}

    # -- server log ----------------------------------------------------------------------------------------------
    def server_log_tail(self, lines: Any = 200, grep: str | None = None) -> dict:
        lines = clamp(lines, 1, 2000, 200)
        path = log_file_path()
        needle = (grep or "").lower()
        files = [path.with_name(f"{path.name}.1"), path]
        out: deque[str] = deque(maxlen=lines)
        scanned = 0
        for item in files:
            if not item.is_file():
                continue
            with item.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    scanned += 1
                    if not needle or needle in line.lower():
                        out.append(safety.clip(line.rstrip("\n"), 4_000))
        rows = list(out)
        budget = safety.MAX_RESPONSE_CHARS - 2_000
        while rows and len(safety.dumps(rows)) > budget:
            rows = rows[len(rows) // 4 or 1:]           # keep the newest lines
        return {"log_file": str(path), "exists": path.is_file(), "grep": grep or None, "lines_scanned": scanned,
                "returned": len(rows), "dropped_for_size": len(out) - len(rows), "lines": rows,
                "size_bytes": path.stat().st_size if path.is_file() else 0, "pid": os.getpid()}
