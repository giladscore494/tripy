"""Conversation-context controls for the research phase. Operational only.

* `model_view` decides what part of a tool result goes back to the model this turn
  (the full result is always in events.jsonl and the document cache).
* `compact_stub` is what an OLDER tool result shrinks to: enough metadata
  (document_id, URL, title, query, result URLs) to reopen or re-query it.
* `ResearchTracker` notices repeated work (same search, same URL, same document
  query) and turns with no new research artifact. It never looks at whether
  a source or value is correct.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag

from .storage.trace import FETCH_TOOLS, SEARCH_TOOLS, parse_args

INSPECT_TOOLS = ("extract_html", "extract_tables", "find_in_document", "get_structured_data", "get_cached_document")
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


def model_view(result: Any, limit: int, *, duplicate: bool = False, note: str | None = None) -> str:
    """Tool result text sent to the model now. Private keys are dropped and the size is capped."""
    if not isinstance(result, dict):
        return _cut(result, limit)
    view = {k: v for k, v in result.items() if not k.startswith("_")}
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
    new_documents: int = 0
    new_evidence: int = 0
    new_sources: int = 0
    new_inspections: int = 0

    @property
    def total(self) -> int:
        return self.new_documents + self.new_evidence + self.new_sources + self.new_inspections


@dataclass
class ResearchTracker:
    """Operational duplicate / novelty bookkeeping for one vehicle run."""

    queries: dict[str, int] = field(default_factory=dict)          # normalized query -> first step
    fetched_urls: dict[str, int] = field(default_factory=dict)     # normalized url -> first step
    documents: dict[str, int] = field(default_factory=dict)        # document_id -> first step
    inspections: dict[str, int] = field(default_factory=dict)      # tool+args signature -> first step
    sources: set[str] = field(default_factory=set)
    duplicate_searches: int = 0
    duplicate_fetches: int = 0
    duplicate_inspections: int = 0
    idle_turns: int = 0                    # consecutive turns with no new research artifact
    max_idle_turns: int = 0
    turns: list[dict] = field(default_factory=list)
    _turn: TurnNovelty = field(default_factory=TurnNovelty)
    _step: int = 0

    def begin_turn(self, step: int) -> None:
        self._turn, self._step = TurnNovelty(), step

    def _add_source(self, url: Any) -> None:
        url = normalize_url(url)
        if url and url not in self.sources:
            self.sources.add(url)
            self._turn.new_sources += 1

    def _add_document(self, document_id: Any) -> bool:
        if isinstance(document_id, str) and document_id and document_id not in self.documents:
            self.documents[document_id] = self._step
            self._turn.new_documents += 1
            return True
        return False

    def observe(self, name: str, raw_args: Any, result: Any) -> tuple[bool, str | None]:
        """Record one tool call. Returns (duplicate, note for the model)."""
        args = parse_args(raw_args)
        result = result if isinstance(result, dict) else {}
        ok = not result.get("error")
        if name in SEARCH_TOOLS:
            key = normalize_query(args.get("query"), args.get("domain") or args.get("domains"))
            first = self.queries.get(key)
            if first is not None:
                self.duplicate_searches += 1
                return True, (f"This exact search already ran at step {first}. Consider querying documents you "
                              "already fetched, or a different search if a target is still unresolved.")
            self.queries[key] = self._step
            for item in result.get("results") or []:
                if isinstance(item, dict):
                    self._add_source(item.get("url"))
            return False, None
        if name in FETCH_TOOLS:
            url = normalize_url(args.get("url"))
            doc = result.get("document_id")
            first = self.fetched_urls.get(url)
            if first is None and doc in self.documents:
                first = self.documents[doc]
            if first is not None:
                self.duplicate_fetches += 1
                return True, (f"Already fetched in this run at step {first}"
                              + (f" as {doc}" if doc else "") + ". Query the stored document with "
                              "find_in_document / extract_tables / extract_html instead of fetching it again.")
            if url:
                self.fetched_urls[url] = self._step
            if ok:
                self._add_document(doc)
                self._add_source(result.get("final_url") or url)
            return False, None
        if name in INSPECT_TOOLS:
            signature = name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
            first = self.inspections.get(signature)
            if first is not None:
                self.duplicate_inspections += 1
                return True, f"Identical call already made at step {first}; its result has not changed."
            self.inspections[signature] = self._step
            if ok:
                self._add_document(result.get("document_id"))
                if name != "get_cached_document" or result.get("found"):
                    self._turn.new_inspections += 1
            return False, None
        if name == "store_evidence" and result.get("stored"):
            self._turn.new_evidence += 1
            self._add_source(args.get("source_url"))
        return False, None

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
            "max_consecutive_idle_turns": self.max_idle_turns,
            "turns": self.turns,
        }
