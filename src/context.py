"""Conversation-context controls for the research phase. Operational only.

* `model_view` decides what part of a tool result goes back to the model this turn
  (the full result is always in events.jsonl and the document cache).
* `compact_stub` is what an OLDER tool result shrinks to: enough metadata
  (document_id, URL, title, query, result URLs) to reopen or re-query it.
* `call_signature` is the canonical identity of a read-only tool call. An exact
  repeat within one vehicle run is answered from the earlier result BEFORE
  dispatch (no HTTP request, no extraction, no billable search).
* `ResearchTracker` keeps those reusable results and measures whether a turn
  exposed anything new: a new operation is not new material, and new material
  is not new evidence. It never looks at whether a source or value is correct.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag

from .storage.trace import FETCH_TOOLS, SEARCH_TOOLS, parse_args

INSPECT_TOOLS = ("extract_html", "extract_tables", "find_in_document", "get_structured_data", "get_cached_document")
# Read-only tools whose exact repeats are answered from the earlier result. store_evidence and
# report_field_status are never replayed: they record state.
REPLAY_SAFE_TOOLS = SEARCH_TOOLS + FETCH_TOOLS + INSPECT_TOOLS
REUSE_NOTE = ("Exact operation already completed in this run at step {step}; previous result reused without "
              "executing the tool again. Use a different query, another document, another extraction method or a "
              "targeted web search to get something new.")
REOPEN_HINT = ("Older tool output compacted. The full content stays in the document cache: reopen it with "
               "find_in_document / extract_tables / extract_html(document_id).")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _cut(text: Any, limit: int) -> str:
    text = text if isinstance(text, str) else _dump(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalize_query(query: Any, domain: Any = None) -> str:
    q = re.sub(r"\s+", " ", str(query or "")).strip().lower()
    if isinstance(domain, (list, tuple)):
        domain = ",".join(sorted(str(d).strip().lower() for d in domain if d))
    return f"{q}|{str(domain or '').strip().lower()}"


def normalize_url(url: Any) -> str:
    return urldefrag(str(url or "").strip())[0]


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _int_or(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    return int(value)


def call_signature(name: str, raw_args: Any) -> str | None:
    """Canonical identity of a replay-safe call, from its normalized semantic arguments.

    Defaults are filled in, so `{"document_id": d}` and `{"document_id": d, "offset": 0}` are the
    same operation. Anything that is not exactly the same operation gets a different signature.
    Returns None for tools that are never replayed and for arguments that cannot be parsed.
    """
    if name not in REPLAY_SAFE_TOOLS:
        return None
    if isinstance(raw_args, dict):
        args = raw_args
    else:
        try:
            args = json.loads(raw_args) if raw_args not in (None, "") else {}
        except (TypeError, ValueError):
            return None
    if not isinstance(args, dict):
        return None
    try:
        if name == "search_web":
            # Query text is only whitespace-normalized: a provider's search need not be case-insensitive.
            parts = [_norm_text(args.get("query")), _norm_text(args.get("domain")).lower(),
                     max(1, min(_int_or(args.get("max_results"), 8), 20))]
        elif name == "search_official_domains":
            domains = args.get("domains") or []
            if isinstance(domains, str):
                domains = [d for d in domains.split(",")]
            parts = [_norm_text(args.get("query")),
                     sorted({_norm_text(d).lower() for d in domains if _norm_text(d)})]
        elif name in FETCH_TOOLS:
            parts = [normalize_url(args.get("url"))]
            if name == "render_page":
                parts.append(_int_or(args.get("wait_ms"), 2500))
        elif name == "get_cached_document":
            parts = [_norm_text(args.get("key")), _int_or(args.get("offset"), 0), _int_or(args.get("max_chars"), None)]
        elif name == "find_in_document":
            # find_in_document matches case-insensitively, so case does not change the operation.
            parts = [_norm_text(args.get("document_id")), _norm_text(args.get("query")).lower(),
                     _norm_text(args.get("scope") or "text").lower(), _int_or(args.get("context_chars"), 300),
                     _int_or(args.get("max_hits"), 15)]
        elif name == "extract_tables":
            parts = [_norm_text(args.get("document_id")), _int_or(args.get("max_tables"), None),
                     _int_or(args.get("max_rows"), None), _int_or(args.get("start_table"), 0)]
        elif name == "get_structured_data":
            parts = [_norm_text(args.get("document_id")), _int_or(args.get("max_chars"), None)]
        else:  # extract_html
            parts = [_norm_text(args.get("document_id")), _int_or(args.get("offset"), 0),
                     _int_or(args.get("max_chars"), None)]
    except (TypeError, ValueError):
        return None
    if not parts[0]:
        return None
    return name + ":" + json.dumps(parts, ensure_ascii=False)


def replay_result(record: dict) -> dict:
    """The earlier result, annotated as reused. The caller sends it as the tool response."""
    result = json.loads(json.dumps(record["result"], ensure_ascii=False, default=str))
    result = result if isinstance(result, dict) else {"value": result}
    result.update({"_reused": True, "_reused_from_step": record["step"],
                   "_operational_note": REUSE_NOTE.format(step=record["step"])})
    return result


def model_view(result: Any, limit: int, *, duplicate: bool = False, note: str | None = None) -> str:
    """Tool result text sent to the model now. Private keys are dropped and the size is capped.

    A replayed result keeps its operational note and origin step visible to the model.
    """
    if not isinstance(result, dict):
        return _cut(result, limit)
    view = {k: v for k, v in result.items() if not k.startswith("_")}
    if result.get("_reused"):
        view["reused_from_step"] = result.get("_reused_from_step")
        note = note or result.get("_operational_note")
    if duplicate:
        # The model already saw this document's preview; send only the handle.
        for key in ("text_preview", "links"):
            view.pop(key, None)
    if note:
        view["operational_note"] = note
    text = _dump(view)
    if len(text) > limit:
        text = text[:limit] + (f" …[truncated {len(text) - limit} chars. The full content is in the document cache; "
                               "use find_in_document, extract_tables or extract_html with offset]")
    return text


def compact_stub(name: str, raw_args: Any, result: Any, limit: int) -> str:
    """The compacted form of an older tool result. Keeps every handle needed to reopen it."""
    args = parse_args(raw_args)
    result = result if isinstance(result, dict) else {"value": result}
    stub: dict[str, Any] = {"tool": name, "compacted": True}
    if result.get("error"):
        stub["error"] = result.get("error")
        stub["message"] = _cut(result.get("message") or "", 160)
    for key in ("document_id", "url", "final_url", "title", "status", "content_type", "pages", "text_chars",
                "tables_total", "hit_count", "evidence_id", "found"):
        if result.get(key) not in (None, "", [], {}):
            stub[key] = _cut(result[key], 160) if isinstance(result[key], str) else result[key]
    if name in SEARCH_TOOLS:
        stub["query"] = args.get("query")
        if args.get("domain") or args.get("domains"):
            stub["domain"] = args.get("domain") or args.get("domains")
        stub["results"] = [{"title": _cut(r.get("title") or "", 80), "url": r.get("url")}
                           for r in result.get("results") or [] if isinstance(r, dict)]
    elif name in INSPECT_TOOLS:
        stub["document_id"] = stub.get("document_id") or args.get("document_id") or args.get("key")
        if args.get("query"):
            stub["query"] = args.get("query")
        hits = result.get("hits") or []
        if hits:
            stub["first_hit"] = _cut(hits[0].get("snippet") or "", 200)
    elif name == "store_evidence":
        stub["field"] = args.get("field")
    stub["hint"] = REOPEN_HINT
    text = _dump(stub)
    while len(text) > limit and stub.get("results"):
        stub["results"] = stub["results"][:-1]
        stub["results_omitted"] = stub.get("results_omitted", 0) + 1
        text = _dump(stub)
    return text if len(text) <= limit else _cut(text, limit)


@dataclass
class TurnNovelty:
    """What one turn produced. `new_operations` is informational and never counts as progress."""

    new_operations: int = 0      # executed calls never made before (a zero-hit query is one of these)
    new_documents: int = 0       # documents this run had not touched
    new_sources: int = 0         # URLs this run had not seen (search results, fetched pages, evidence)
    new_material: int = 0        # content not exposed before: new hits, tables, structured data, text spans
    new_evidence: int = 0        # store_evidence records
    reused_calls: int = 0        # exact repeats answered from an earlier result (never progress)

    @property
    def total(self) -> int:
        return self.new_documents + self.new_sources + self.new_material + self.new_evidence


def _uncovered(spans: list[tuple[int, int]], start: int, end: int) -> int:
    """Length of [start, end) not covered by earlier spans."""
    if end <= start:
        return 0
    covered = 0
    for a, b in sorted(spans):
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            covered += hi - lo
    return max(0, (end - start) - covered)


@dataclass
class ResearchTracker:
    """Operational duplicate / novelty bookkeeping for one vehicle run (shared by every phase)."""

    queries: dict[str, int] = field(default_factory=dict)          # normalized query -> first step
    fetched_urls: dict[str, int] = field(default_factory=dict)     # normalized url -> first step
    documents: dict[str, int] = field(default_factory=dict)        # document_id -> first step
    inspections: dict[str, int] = field(default_factory=dict)      # tool+args signature -> first step
    sources: set[str] = field(default_factory=set)
    seen_calls: dict[str, dict] = field(default_factory=dict)      # call_signature -> reusable record
    duplicate_searches: int = 0           # detected after execution (near repeats, e.g. other max_results)
    duplicate_fetches: int = 0
    duplicate_inspections: int = 0
    duplicate_calls_suppressed: int = 0   # exact repeats answered before dispatch
    duplicate_searches_suppressed: int = 0
    duplicate_fetches_suppressed: int = 0
    duplicate_inspections_suppressed: int = 0
    operations_with_new_material: dict[str, int] = field(default_factory=dict)     # by phase
    operations_without_new_material: dict[str, int] = field(default_factory=dict)  # by phase
    idle_turns: int = 0                    # consecutive turns with no new research artifact
    max_idle_turns: int = 0
    turns: list[dict] = field(default_factory=list)
    _turn: TurnNovelty = field(default_factory=TurnNovelty)
    _step: int = 0
    _hit_offsets: set = field(default_factory=set)                 # (document_id, scope, offset)
    _tables: set = field(default_factory=set)                      # (document_id, table index)
    _structured: set = field(default_factory=set)                  # document_id
    _spans: dict = field(default_factory=dict)                     # document_id -> [(start, end)]

    def begin_turn(self, step: int) -> None:
        self._turn, self._step = TurnNovelty(), step

    # -- exact-repeat reuse ------------------------------------------------------------------

    def lookup(self, signature: str | None) -> dict | None:
        return self.seen_calls.get(signature) if signature else None

    def remember(self, signature: str | None, record: dict) -> None:
        """Keep a successful read-only result for exact reuse later in this vehicle run."""
        result = record.get("result")
        if signature and isinstance(result, dict) and not result.get("error") and signature not in self.seen_calls:
            self.seen_calls[signature] = record

    def note_reused(self, name: str) -> None:
        self.duplicate_calls_suppressed += 1
        if name in SEARCH_TOOLS:
            self.duplicate_searches_suppressed += 1
        elif name in FETCH_TOOLS:
            self.duplicate_fetches_suppressed += 1
        else:
            self.duplicate_inspections_suppressed += 1
        self._turn.reused_calls += 1

    # -- novelty -------------------------------------------------------------------------------

    def _add_source(self, url: Any) -> None:
        url = normalize_url(url)
        if url and url not in self.sources:
            self.sources.add(url)
            self._turn.new_sources += 1

    def _add_document(self, document_id: Any, novel: bool = True) -> bool:
        if isinstance(document_id, str) and document_id and document_id not in self.documents:
            self.documents[document_id] = self._step
            if novel:
                self._turn.new_documents += 1
            return True
        return False

    def _material(self, name: str, args: dict, result: dict) -> int:
        """How much content this inspection exposed that the run had not seen before."""
        doc = result.get("document_id") or args.get("document_id")
        if name == "find_in_document":
            scope = str(result.get("scope") or "text")
            fresh = 0
            for hit in result.get("hits") or []:
                key = (doc, scope, hit.get("offset"))
                if key not in self._hit_offsets:
                    self._hit_offsets.add(key)
                    fresh += 1
            return fresh
        if name == "extract_tables":
            fresh = 0
            for table in result.get("tables") or []:
                key = (doc, table.get("index"))
                if table.get("rows") and key not in self._tables:
                    self._tables.add(key)
                    fresh += 1
            return fresh
        if name == "get_structured_data":
            found = result.get("found") or {}
            if any(found.values()) and doc not in self._structured:
                self._structured.add(doc)
                return 1
            return 0
        if name in ("extract_html", "get_cached_document"):
            text = result.get("text") or ""
            start = int(result.get("offset") or 0)
            spans = self._spans.setdefault(doc, [])
            fresh = _uncovered(spans, start, start + len(text))
            if text:
                spans.append((start, start + len(text)))
            return 1 if fresh else 0
        return 0

    def observe(self, name: str, raw_args: Any, result: Any, phase: str = "research") -> tuple[bool, str | None]:
        """Record one EXECUTED tool call. Returns (duplicate detected after execution, note for the model)."""
        args = parse_args(raw_args)
        result = result if isinstance(result, dict) else {}
        ok = not result.get("error")
        before = self._turn.total
        duplicate, note = False, None
        if name in SEARCH_TOOLS:
            key = normalize_query(args.get("query"), args.get("domain") or args.get("domains"))
            first = self.queries.get(key)
            if first is not None:
                self.duplicate_searches += 1
                duplicate, note = True, (f"This exact search already ran at step {first}. Consider querying documents "
                                         "you already fetched, or a different search if a target is still unresolved.")
            else:
                if ok:  # a failed search may be retried without being called a repeat
                    self.queries[key] = self._step
                self._turn.new_operations += 1
            for item in result.get("results") or []:
                if isinstance(item, dict):
                    self._add_source(item.get("url"))
        elif name in FETCH_TOOLS:
            url = normalize_url(args.get("url"))
            doc = result.get("document_id")
            first = self.fetched_urls.get(url)
            if first is None and doc in self.documents:
                first = self.documents[doc]
            if first is not None:
                self.duplicate_fetches += 1
                duplicate, note = True, (f"Already fetched in this run at step {first}"
                                         + (f" as {doc}" if doc else "") + ". Query the stored document with "
                                         "find_in_document / extract_tables / extract_html instead of fetching it again.")
            else:
                self._turn.new_operations += 1
                if ok:  # a failed fetch may be retried without being called a repeat
                    if url:
                        self.fetched_urls[url] = self._step
                    self._add_document(doc)
                    self._add_source(result.get("final_url") or url)
        elif name in INSPECT_TOOLS:
            signature = name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
            first = self.inspections.get(signature)
            if first is not None:
                self.duplicate_inspections += 1
                duplicate, note = True, f"Identical call already made at step {first}; its result has not changed."
            else:
                if ok:  # e.g. an inspection that failed before the document was fetched may run again
                    self.inspections[signature] = self._step
                self._turn.new_operations += 1
            if ok and (name != "get_cached_document" or result.get("found")):
                doc = result.get("document_id")
                if name == "get_cached_document":
                    # Loading a document this run already knows is not new knowledge.
                    if self._add_document(doc):
                        self._material(name, args, result)
                else:
                    self._add_document(doc, novel=False)
                    self._turn.new_material += self._material(name, args, result)
        elif name == "store_evidence" and result.get("stored"):
            self._turn.new_evidence += 1
            self._add_source(args.get("source_url"))
        if name in REPLAY_SAFE_TOOLS:
            bucket = self.operations_with_new_material if self._turn.total > before \
                else self.operations_without_new_material
            bucket[phase] = bucket.get(phase, 0) + 1
        return duplicate, note

    def end_turn(self) -> TurnNovelty:
        novelty = self._turn
        self.idle_turns = 0 if novelty.total else self.idle_turns + 1
        self.max_idle_turns = max(self.max_idle_turns, self.idle_turns)
        self.turns.append({"step": self._step, **novelty.__dict__, "idle_streak": self.idle_turns})
        return novelty

    def snapshot(self) -> dict:
        return {
            "unique_queries": len(self.queries),
            "unique_urls_fetched": len(self.fetched_urls),
            "documents_opened": len(self.documents),
            "unique_sources_seen": len(self.sources),
            "duplicate_searches": self.duplicate_searches,
            "duplicate_fetches": self.duplicate_fetches,
            "duplicate_inspections": self.duplicate_inspections,
            "duplicate_calls_suppressed": self.duplicate_calls_suppressed,
            "duplicate_searches_suppressed": self.duplicate_searches_suppressed,
            "duplicate_fetches_suppressed": self.duplicate_fetches_suppressed,
            "duplicate_inspections_suppressed": self.duplicate_inspections_suppressed,
            "operations_with_new_material": dict(self.operations_with_new_material),
            "operations_without_new_material": dict(self.operations_without_new_material),
            "max_consecutive_idle_turns": self.max_idle_turns,
            "turns": self.turns,
        }
