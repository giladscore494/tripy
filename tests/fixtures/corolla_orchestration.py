"""Offline orchestration benchmark: Toyota COROLLA 2024 BUSINESS EDI (record 38626, the real Level 1.5 snapshot row),
shaped after the real one-vehicle GLM-5.3-Flash run that motivated source-acquisition primary research:

    research turns 12 · searches 6 · documents 6 · ~66 candidates · candidate fields ~26/37 · evidence rejected 4
    the research model repeatedly ran find_in_document("Maximum torque"), ("Torque"), ("Fuel tank"),
    ("Kerb Weight"), ("Apple CarPlay"), ("Tyres") ... over documents it already had, and the document sweep
    then received one packet for every field and candidate.

No network and no API key: pages come from FakeSession, searches from a fixed keyword index, and every model turn is
played by ONE deterministic policy (OrchestrationGLM) that behaves the same way under every code version:

  * primary research: the observed acquisition + Ctrl+F pattern (12 planned turns: acquire, then per-field
    find_in_document over cached documents, four store_evidence requests the admission gate rejects:
    curb weight range with the wrong unit, label-only Apple CarPlay / Android Auto, a fuel figure misquoted).
    `follows_prompt=True` models a research model that obeys the source-acquisition prompt when the system prompt
    asks for it (acquisition turns only, ONE inspect_document_for_fields instead of per-field finds); with
    `follows_prompt=False` the same 12-turn plan runs whatever the prompt says (worst case for the new code).
  * document sweep: a careful reviewer (promotes a confidently paired candidate per field; for fields without
    candidates it uses local_snippets when the packet has them, else find_in_document, then stores from the hits).
  * recovery: tests/fixtures/corolla_tail.PolicyGLM's recovery policy.

Token usage is ESTIMATED from request / reply characters (chars / 4, tool schemas included); latency is a MODEL
(1.5 s + prompt_tokens / 4000 s + completion_tokens / 60 s per call), not a measurement. Timeouts cannot be
reproduced offline. "trustworthy" = an ok field whose admitted values all equal this fixture's ground truth.

The script is self-contained so the same file can run against an older checkout (BEFORE) and this one (AFTER):
    python tests/fixtures/corolla_orchestration.py            # this checkout
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from conftest import FakeResponse, FakeSession  # noqa: E402
from fixtures import corolla_tail as tail  # noqa: E402
from fixtures.corolla_touring import PAYLOAD, VEHICLE  # noqa: E402

from src import agent as agent_mod  # noqa: E402
from src.agent import AgentConfig, research_system_prompt, run_vehicle  # noqa: E402
from src.benchmark import compute_metrics  # noqa: E402
from src.fields import load_schema  # noqa: E402
from src.glm_client import ChatResponse  # noqa: E402
from src.pricing import default_pricing  # noqa: E402
from src.storage.cache import DocumentCache  # noqa: E402
from src.storage.run_log import RunLog, read_events  # noqa: E402
from src.tools import ToolConfig  # noqa: E402

MODEL = "glm-5.3-flash"

IMPORTER = "https://www.toyota.co.il/models/corolla-touring-sports-hybrid"
IMPORTER_HTML = """<html><head><title>טויוטה קורולה טורינג ספורט היברידי 2024 | טויוטה ישראל</title></head><body>
<h1>טויוטה קורולה טורינג ספורט 2024 1.8 היברידי</h1>
<nav><ul><li>Apple CarPlay</li><li>Android Auto</li><li>מפרט טכני</li><li>מחירון</li></ul></nav>
<p>רמת גימור: BUSINESS EDI</p>
<p>מחיר: 179,990 ש"ח</p>
<p>מסך מולטימדיה 10.5 אינץ'</p>
<p>בקרת אקלים: מפוצלת דו-אזורית</p>
<p>חימום מושבים: אין</p>
<p>מושבים חשמליים: אין</p>
<p>חישוקים: 16 אינץ'</p>
</body></html>"""
EU_SPEC = "https://www.toyota-europe.com/new-cars/corolla-touring-sports/2024/1-8-hybrid-specifications"
EU_SPEC_HTML = """<html><head><title>Toyota Corolla Touring Sports 2024 1.8 Hybrid 140 - technical specifications</title>
</head><body><h1>Toyota Corolla Touring Sports 2024 1.8 Hybrid 140</h1>
<table>
<tr><th>Specification</th><th>1.8 Hybrid 140</th></tr>
<tr><td>Maximum torque (Nm)</td><td>142</td></tr>
<tr><td>Maximum speed (km/h)</td><td>180</td></tr>
<tr><td>0-100 km/h (s)</td><td>9.2</td></tr>
<tr><td>Fuel tank capacity (l)</td><td>43</td></tr>
<tr><td>Luggage compartment (l)</td><td>596</td></tr>
<tr><td>Kerb weight (kg)</td><td>1410-1415</td></tr>
<tr><td>Length (mm)</td><td>4650</td></tr>
<tr><td>Width (mm)</td><td>1790</td></tr>
<tr><td>Height (mm)</td><td>1460</td></tr>
<tr><td>Wheelbase (mm)</td><td>2700</td></tr>
<tr><td>Transmission</td><td>e-CVT</td></tr>
<tr><td>Combined fuel consumption (l/100km)</td><td>4.4-4.5</td></tr>
<tr><td>Tyres</td><td>205/55 R16</td></tr>
</table></body></html>"""
PRICELIST = "https://www.toyota.co.il/media/pricelist-2024-01.pdf"
PRICELIST_TEXT = ("[page 1]\nמחירון טויוטה ינואר 2024\nקורולה טורינג ספורט 2024 1.8 היברידי\n"
                  "BUSINESS EDI מחיר: 179,990\nPREMIUM מחיר: 189,990\nאגרת רישוי שנתית: 1,316 ש\"ח\n"
                  "אחריות: 3 שנים או 100,000 ק\"מ")
REVIEW = "https://www.autoexpress.example/reviews/toyota-corolla-touring-sports-2024-1-8-hybrid"
REVIEW_TEXT = ("Toyota Corolla Touring Sports 2024 1.8 Hybrid review. The 1.8 Hybrid does 0-100 km/h in 9.2 s and "
               "tops out at 180 km/h. We averaged 4.9 l/100km on a mixed route. Ground clearance is modest; the "
               "10.5-inch touchscreen supports wireless Apple CarPlay on higher grades.")
BLOCKED = "https://www.toyota.co.il/models/corolla-touring-sports-hybrid/specs"
AGGREGATOR = tail.CARTUBE
UK_PDF = "https://www.toyota.co.uk/download/corolla-touring-sports-2024-spec.pdf"
UK_PDF_TEXT = ("[page 1]\nToyota Corolla Touring Sports 2024 1.8 Hybrid 140 specification\n"
               "Ground clearance (mm) 135\nLuggage capacity (l) 596\nKerb weight (kg) 1410\n")

ROUTES = {IMPORTER: (IMPORTER_HTML, "text/html; charset=utf-8"), EU_SPEC: (EU_SPEC_HTML, "text/html; charset=utf-8"),
          PRICELIST: (PRICELIST_TEXT, "application/pdf-text"), REVIEW: (REVIEW_TEXT, "text/plain"),
          AGGREGATOR: (tail.CARTUBE_TEXT, "text/plain"), UK_PDF: (UK_PDF_TEXT, "text/plain"),
          BLOCKED: ("<html><title>403 Forbidden</title>Access denied</html>", "text/html")}
STATUS = {BLOCKED: 403}

SEARCH_INDEX = [  # keyword in the query -> results; first match wins
    ("מחירון", [{"url": PRICELIST, "title": "מחירון טויוטה ינואר 2024", "snippet": "קורולה טורינג ספורט"}]),
    ("review", [{"url": REVIEW, "title": "Toyota Corolla Touring Sports 1.8 Hybrid review", "snippet": "0-100"},
                {"url": BLOCKED, "title": "טויוטה ישראל - מפרט", "snippet": "מפרט טכני"}]),
    ("ground clearance", [{"url": UK_PDF, "title": "Corolla Touring Sports 2024 specification (UK)",
                           "snippet": "ground clearance, luggage capacity"}]),
    ("cargo volume", [{"url": UK_PDF, "title": "Corolla Touring Sports 2024 specification (UK)",
                       "snippet": "luggage capacity"}]),
    ("specifications", [{"url": EU_SPEC, "title": "Corolla Touring Sports 1.8 Hybrid technical specifications",
                         "snippet": "torque, top speed, fuel tank, kerb weight"}]),
    ("aggregator", [{"url": AGGREGATOR, "title": "קורולה טורינג ספורט 1.8 היברידי", "snippet": "מפרט"}]),
    ("", [{"url": IMPORTER, "title": "קורולה טורינג ספורט היברידי | טויוטה ישראל", "snippet": "מחיר, רמת גימור"}]),
]

# This fixture's ground truth for the target variant (what a person would accept).
TRUTH = {"torque_nm": 142, "top_speed_kmh": 180, "acceleration_0_100_s": 9.2, "fuel_tank_l": 43,
         "cargo_volume_l": 596, "length_mm": 4650, "width_mm": 1790, "wheelbase_mm": 2700, "height_mm": 1460,
         "list_price": 179990, "screen_size_in": 10.5, "rim_diameter_in": 16, "gearbox_type": "e-cvt",
         "registration_licence_fee": 1316, "tire_size_front": "205/55 r16", "tire_size_rear": "205/55 r16",
         "ground_clearance_mm": 135, "curb_weight_kg": 1410, "local_trim_name": "business edi",
         "warranty_years": 3, "warranty_km": 100000, "heated_seats": False, "power_seats": False, "climate_zones": 2}
SPECS = {s["name"]: s for s in load_schema()}


def label(field: str) -> str:
    return (SPECS[field].get("aliases_en") or [field])[0]


def _t(*calls):
    return tail._turn(*calls)


def _c(cid, name, args):
    return tail._call(cid, name, args)


def _doc(url: str, kind: str = "fetch") -> str:
    from src.storage.cache import document_id_for

    return document_id_for(kind, url)


# The observed pattern: acquisition turns interleaved with per-field Ctrl+F over cached documents.
PLAN = [
    ("acquire", _t(_c("a1", "search_web", {"query": "Toyota Corolla Touring Sports 2024 Business EDI Israel"}),
                   _c("a2", "fetch_url", {"url": IMPORTER}))),
    ("ctrlf", _t(_c("f1", "find_in_document", {"document_id": _doc(IMPORTER), "query": "Maximum torque"}),
                 _c("f2", "find_in_document", {"document_id": _doc(IMPORTER), "query": "Torque"}),
                 _c("f3", "find_in_document", {"document_id": _doc(IMPORTER), "query": "Fuel tank"}))),
    ("acquire", _t(_c("a3", "search_official_domains", {"query": "Corolla Touring Sports 2024 hybrid specifications"}),
                   _c("a4", "fetch_url", {"url": EU_SPEC}))),
    ("ctrlf", _t(_c("f4", "find_in_document", {"document_id": _doc(EU_SPEC), "query": "Torque (Nm)"}),
                 _c("f5", "find_in_document", {"document_id": _doc(EU_SPEC), "query": "Fuel Tank Capacity"}),
                 _c("f6", "find_in_document", {"document_id": _doc(EU_SPEC), "query": "Luggage compartment"}))),
    ("ctrlf", _t(_c("f7", "find_in_document", {"document_id": _doc(EU_SPEC), "query": "Kerb Weight"}),
                 _c("f8", "find_in_document", {"document_id": _doc(EU_SPEC), "query": "Maximum Speed"}),
                 _c("r1", "store_evidence", {"field": "curb_weight_kg", "value": "1410-1415", "unit": "lb",
                                             "document_id": _doc(EU_SPEC), "quote": "Kerb weight (kg)\n1410-1415",
                                             "market": "EU", "variant_match": "exact"}))),
    ("acquire", _t(_c("a5", "search_web", {"query": "טויוטה קורולה 2024 מחירון"}),
                   _c("a6", "fetch_url", {"url": PRICELIST}))),
    ("ctrlf", _t(_c("f9", "find_in_document", {"document_id": _doc(IMPORTER), "query": "Apple CarPlay"}),
                 _c("r2", "store_evidence", {"field": "apple_carplay", "value": True, "document_id": _doc(IMPORTER),
                                             "quote": "Apple CarPlay", "market": "IL", "variant_match": "exact"}),
                 _c("r3", "store_evidence", {"field": "android_auto", "value": True, "document_id": _doc(IMPORTER),
                                             "quote": "Android Auto", "market": "IL", "variant_match": "exact"}))),
    ("acquire", _t(_c("a7", "search_web", {"query": "Toyota Corolla Touring Sports 1.8 hybrid review 2024"}),
                   _c("a8", "fetch_url", {"url": REVIEW}), _c("a9", "fetch_url", {"url": BLOCKED}))),
    ("ctrlf", _t(_c("f10", "find_in_document", {"document_id": _doc(EU_SPEC), "query": "Tyres"}),
                 _c("r4", "store_evidence", {"field": "fuel_consumption_combined_l_100km", "value": 4.5,
                                             "document_id": _doc(REVIEW), "quote": "combined consumption 4.5 l/100km",
                                             "market": "UK", "variant_match": "exact"}))),
    ("acquire", _t(_c("a10", "search_web", {"query": "קורולה טורינג ספורט היברידי aggregator מפרט"}),
                   _c("a11", "fetch_url", {"url": AGGREGATOR}))),
    ("ctrlf", _t(_c("f11", "search_web", {"query": "Toyota Corolla Touring Sports 2024 Business EDI Israel"}),
                 _c("f12", "find_in_document", {"document_id": _doc(EU_SPEC), "query": "Fuel Tank Capacity"}))),
    ("ctrlf", _t(_c("f13", "find_in_document", {"document_id": _doc(EU_SPEC), "query": "Torque (Nm)"}))),
]
FINAL = {"summary": "primary research", "fields": {}}


def _number_after(text: str, start: int) -> re.Match | None:
    return re.search(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)", text[start:start + 60])


def value_from_snippet(field: str, snippet: str) -> tuple[object, str] | None:
    """The reviewer's reading of a label snippet: the number right after the field's label (same or next line),
    with the line(s) it read as the quote. Booleans and text are left alone."""
    spec = SPECS[field]
    if spec.get("matcher") not in (None, "numeric") or not spec.get("expected_units"):
        return None
    low = snippet.lower()
    for alias in sorted((spec.get("aliases_en") or []) + (spec.get("aliases_he") or []), key=len, reverse=True):
        pos = low.find(alias.lower())
        if pos < 0:
            continue
        lines = snippet[pos:].split("\n")
        for n in (1, 2):
            part = "\n".join(lines[:n])
            m = _number_after(part, len(alias))
            if m:
                raw = m.group(1)
                value = float(raw.replace(",", "")) if "." in raw else int(raw.replace(",", ""))
                return value, part.strip()
    return None


class OrchestrationGLM(tail.PolicyGLM):
    model = MODEL

    def __init__(self, follows_prompt: bool = True):
        super().__init__(primary=[], search_index=SEARCH_INDEX, engine="1.8")
        self.follows_prompt = follows_prompt
        self.plan = list(PLAN)
        self.phase_tokens: dict[str, dict] = {}
        self.modelled_latency_s: dict[str, float] = {}
        self.inspected = False

    # --- the research policy ------------------------------------------------------------------------------
    def research(self, messages):
        acquisition_prompt = "SOURCE ACQUISITION" in messages[0]["content"]
        while self.plan:
            kind, turn = self.plan.pop(0)
            if kind == "ctrlf" and self.follows_prompt and acquisition_prompt:
                if not self.inspected:      # one batch inspection to check identity/usefulness, not per-field finds
                    self.inspected = True
                    return _t(_c("i1", "inspect_document_for_fields", {"document_id": _doc(EU_SPEC)}))
                continue
            return turn
        return tail._say(FINAL)

    # --- the document-sweep policy ------------------------------------------------------------------------
    def sweep(self, messages):
        packet = json.loads(messages[1]["content"].split("\n", 1)[1])
        review = packet.get("fields_to_review") or []
        calls, stored = [], set()

        def store(field, value, unit, document_id, quote, market=None):
            if field in stored:
                return
            stored.add(field)
            args = {"field": field, "value": value, "document_id": document_id, "quote": quote,
                    "market": market or "unknown", "variant_match": "exact"}
            if unit:
                args["unit"] = unit
            calls.append(_c(f"s{len(calls)}", "store_evidence", args))

        results = [json.loads(m["content"].split("\n[operational note]")[0]) for m in messages[2:]
                   if m["role"] == "tool"]
        if results:     # turn 2: store what the inspections returned
            for r in results:
                for hit in r.get("hits") or []:
                    field = next((f for f in review if label(f).lower() == str(r.get("query", "")).lower()), None)
                    got = value_from_snippet(field, hit.get("snippet") or "") if field else None
                    if got:
                        store(field, got[0], None, r.get("document_id"), got[1])
            return _t(*calls) if calls else tail._say({"reviewed": []})
        for field, cands in (packet.get("deterministic_candidates") or {}).items():
            if field not in review:
                continue
            for c in cands:
                if c.get("parser_confidence", 0) >= 0.6 and not c.get("year_hint_differs") and not c.get("ambiguity") \
                        and c.get("document_id") and c.get("quote"):
                    store(field, c["value"], c.get("unit"), c["document_id"], c["quote"], c.get("market_hint"))
                    break
        finds = []
        absent = set(packet.get("fields_not_found_locally") or [])    # only exists where the code provides it
        for field in packet.get("fields_without_candidates") or []:
            if field in absent:
                continue
            snippets = (packet.get("local_snippets") or {}).get(field)
            if snippets:
                for s in snippets:
                    got = value_from_snippet(field, s["snippet"])
                    if got:
                        store(field, got[0], None, s["document_id"], got[1])
                        break
                continue
            for doc in packet.get("cached_documents") or []:
                finds.append(_c(f"q{len(finds)}", "find_in_document", {"document_id": doc["document_id"],
                                                                       "query": label(field)}))
        calls += finds[:12]
        return _t(*calls) if calls else tail._say({"reviewed": []})

    def chat(self, messages, tools=None, **kwargs):
        system = messages[0]["content"]
        if system == research_system_prompt():
            kind, message = "research", self.research(messages)
        elif system == agent_mod.DOCUMENT_SWEEP_SYSTEM_PROMPT:
            kind, message = "sweep", self.sweep(messages)
        elif system in (agent_mod.FIELD_RECOVERY_SYSTEM_PROMPT, agent_mod.CLUSTER_RECOVERY_SYSTEM_PROMPT):
            kind, message = "recovery", self.recover(messages, tools)
        else:
            kind, message = "final", tail._say({"summary": "final", "fields": {}})
        self.requests.append({"messages": messages, "tools": tools})
        self.phase_calls[kind] = self.phase_calls.get(kind, 0) + 1
        prompt = (len(json.dumps(messages, ensure_ascii=False)) + len(json.dumps(tools or [], ensure_ascii=False))) // 4
        completion = max(1, len(json.dumps(message, ensure_ascii=False)) // 4)
        latency = 1.5 + prompt / 4000 + completion / 60
        acc = self.phase_tokens.setdefault(kind, {"calls": 0, "prompt": 0, "completion": 0})
        acc["calls"] += 1
        acc["prompt"] += prompt
        acc["completion"] += completion
        self.modelled_latency_s[kind] = round(self.modelled_latency_s.get(kind, 0.0) + latency, 1)
        return ChatResponse(message=message, finish_reason="stop", latency_ms=int(latency * 1000),
                            usage={"prompt_tokens": prompt, "completion_tokens": completion,
                                   "total_tokens": prompt + completion})


def run(workdir: Path, *, follows_prompt: bool = True, **cfg) -> dict:
    cache = DocumentCache(workdir / "cache")
    routes = {}
    for url, (body, ctype) in ROUTES.items():
        if ctype == "application/pdf-text":      # a PDF whose text layer is the given text: stored as the fetch would
            meta = {"status": 200, "final_url": url, "doc_type": "pdf", "content_type": "application/pdf", "pages": 1}
            cache.put("fetch", url, b"%PDF-1.4 fixture", meta, body)
            continue
        routes[url] = FakeResponse(body.encode("utf-8"), content_type=ctype, url=url, status=STATUS.get(url, 200))
    client = OrchestrationGLM(follows_prompt=follows_prompt)
    log = RunLog(workdir / "runs", "orchestration", "38626")
    config = AgentConfig(**{"research_memory_enabled": False, **cfg})
    t0 = time.monotonic()
    result = run_vehicle({"upstream_record_id": "38626"}, PAYLOAD, client=client, cache=cache, run_log=log,
                         vehicle_meta=VEHICLE, config=config, tool_config=ToolConfig(), session=FakeSession(routes),
                         pricing=default_pricing(MODEL))
    wall = time.monotonic() - t0
    return {"result": result, "events": read_events(log.events_path), "client": client, "wall_s": wall,
            "metrics": compute_metrics(result)}


def _norm(value):
    if isinstance(value, str):
        text = value.strip().lower().replace(",", "")
        try:
            return float(text)
        except ValueError:
            return text
    if isinstance(value, bool):
        return value
    return float(value) if isinstance(value, (int, float)) else value


def summarize(run_: dict) -> dict:
    result, events, m, client = run_["result"], run_["events"], run_["metrics"], run_["client"]
    states = {f: s["state"] for f, s in (result["research_bundle"]["field_states"] or {}).items()}
    rec = result.get("field_recovery") or {}
    after_sweep = {e["field"]: e["state"] for e in rec.get("evaluation_primary") or []}
    sweep_started = [e for e in events if e.get("kind") == "document_sweep_started"]
    harvest = next((e for e in events if e.get("kind") == "deterministic_harvest_summary"), {})
    by_field: dict[str, list] = {}
    for item in result["evidence"]:
        if str(item.get("variant_match") or "") not in ("different", "unbound"):
            by_field.setdefault(item["field"], []).append(item.get("value"))
    ok = [f for f, s in states.items() if s == "ok"]
    trustworthy = [f for f in ok if f in TRUTH and by_field.get(f)
                   and all(_norm(v) == _norm(TRUTH[f]) for v in by_field[f])]
    polluted = [f for f in ok if f in TRUTH and any(_norm(v) != _norm(TRUTH[f]) for v in by_field.get(f, []))]
    tokens = client.phase_tokens
    sweep_tok = tokens.get("sweep") or {}
    return {
        "research_model_calls": m["research_model_calls"],
        "model_calls_total": m["model_calls"],
        "input_tokens_est": m["prompt_tokens"],
        "output_tokens_est": m["completion_tokens"],
        "modelled_model_latency_s": round(sum(client.modelled_latency_s.values()), 1),
        "local_wall_clock_s": round(run_["wall_s"], 2),
        "search_calls": m["search_api_calls"],
        "documents_fetched": m["tool_calls_by_name"].get("fetch_url", 0) + m["tool_calls_by_name"].get("fetch_pdf", 0),
        "candidate_count": harvest.get("candidate_count_total"),
        "candidate_fields": harvest.get("candidate_fields_total"),
        "document_sweep_calls": m["document_sweep_model_calls"],
        "document_sweep_packet_chars": sum(e.get("packet_chars") or 0 for e in sweep_started),
        "document_sweep_input_tokens_est": sweep_tok.get("prompt", 0),
        "document_sweep_modelled_latency_s": client.modelled_latency_s.get("sweep", 0.0),
        "document_sweep_timeouts": "not reproducible offline",
        "fields_ok_after_sweep": sum(1 for s in after_sweep.values() if s == "ok"),
        "fields_entering_recovery": len(rec.get("queue") or []),
        "recovery_turns": rec.get("field_recovery_turns_used", rec.get("turns")) or 0,
        "final_fields_ok": len(ok),
        "final_trustworthy_fields": len(trustworthy),
        "final_polluted_fields": polluted,
        "evidence_rejected": m["evidence_rejected"],
        "evidence_rejected_by_reason": m["evidence_rejected_by_reason"],
        "known_cost_usd": m["cost_usd"],
        "primary_research_stop_reason": (result.get("primary_research") or {}).get("stop_reason") or result.get(
            "stop_reason"),
        "find_in_document_calls_research": sum(1 for e in events if e.get("kind") == "tool_call"
                                               and e.get("phase") == "research" and e.get("name") == "find_in_document"),
        "states": states,
    }


SCENARIOS = {
    # same 12-turn policy whatever the prompt says (worst case for source-acquisition research)
    "policy_ignores_prompt": {"follows_prompt": False},
    # a research model that follows the source-acquisition prompt where the code version has one
    "policy_follows_prompt": {"follows_prompt": True},
}


def benchmark(workdir: Path | None = None, **cfg) -> dict:
    root = Path(workdir or tempfile.mkdtemp(prefix="corolla-orchestration-"))
    return {name: summarize(run(root / name, **params, **cfg)) for name, params in SCENARIOS.items()}


if __name__ == "__main__":
    extra = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    report = benchmark(**extra)
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
