"""Offline Corolla tail benchmark: legacy per-field recovery vs clustered tail recovery on the SAME scenario.

Toyota Corolla Touring Sports 2024 1.8 Hybrid Business (record 38626, Israel). Primary research is scripted and
identical in both modes; recovery is played by ONE deterministic policy model (PolicyGLM) that behaves the same way
in both modes: store a candidate it is shown, read search results and fetched documents, search with the field's
label when nothing is left. Only the recovery STRUCTURE differs (per field vs per cluster, budgets, local-first).
No network: pages come from FakeSession, searches from a fixed keyword index.

Shaped after the real run's tail (test data, not copies of the real pages):
    height_mm            two values in the same Israeli aggregator page (1460 vs 1435): stays conflicting
    ground_clearance_mm  no source states it: must stop without burning turns
    fuel_tank_l          only an official foreign (EU) spec page: portable by the field's policy
    battery_gross_kwh    the official EU spec page found for it also states ~8 other fields
    list_price           179,990 (launch article, Business) vs 179,990-183,990 (aggregator): normalized

Run directly for a report:  python tests/fixtures/corolla_tail.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent.parent
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from conftest import FakeResponse, FakeSession  # noqa: E402
from fixtures.corolla_touring import PAYLOAD, VEHICLE  # noqa: E402

from src.agent import (CLUSTER_RECOVERY_SYSTEM_PROMPT, DOCUMENT_SWEEP_SYSTEM_PROMPT,  # noqa: E402
                       FIELD_RECOVERY_SYSTEM_PROMPT, AgentConfig, research_system_prompt, run_vehicle)
from src.benchmark import compute_metrics  # noqa: E402
from src.fields import load_schema  # noqa: E402
from src.glm_client import ChatResponse  # noqa: E402
from src.storage.cache import DocumentCache  # noqa: E402
from src.storage.run_log import RunLog, read_events  # noqa: E402
from src.tools import ToolConfig  # noqa: E402

FIELDS = ["length_mm", "width_mm", "height_mm", "wheelbase_mm", "curb_weight_kg", "ground_clearance_mm",
          "cargo_volume_l", "fuel_tank_l", "battery_gross_kwh", "torque_nm", "acceleration_0_100_s",
          "top_speed_kmh", "list_price", "warranty_years"]

CARTUBE = "https://www.cartube.co.il/toyota/corolla-touring-sports-2024-1-8-hybrid-business"
CARTUBE_TEXT = "\n".join([
    "טויוטה קורולה טורינג ספורט 2024 1.8 היברידי Business",
    'אורך: 4,650 מ"מ',
    'רוחב: 1,790 מ"מ',
    'גובה 146.0 ס"מ',
    'גובה 143.5 ס"מ',
    'בסיס גלגלים: 2,700 מ"מ',
    "מומנט מנוע בנזין: 142 ניוטון-מטר",
    "אחריות: שלוש שנים",
    'מחיר: 179,990-183,990 ש"ח',
])
LAUNCH = "https://www.carnews.co.il/toyota-corolla-touring-sports-2024-launch"
LAUNCH_TEXT = ("טויוטה קורולה טורינג ספורט 2024 1.8 היברידי Business הושקה בישראל.\n"
               'מחיר: 179,990 ש"ח (מחירון ינואר 2024).')
EU_SPEC = "https://www.toyota-europe.com/new-cars/corolla-touring-sports/1-8-hybrid-specifications"
EU_SPEC_HTML = """<html><head><title>Toyota Corolla Touring Sports 2024 1.8 Hybrid 140 - technical specifications</title>
</head><body><h1>Toyota Corolla Touring Sports 2024 1.8 Hybrid 140</h1>
<p>Fuel tank capacity: 43 l</p>
<p>Battery capacity: 0.76 kWh</p>
<p>Kerb weight: 1,410 kg</p>
<p>Luggage capacity: 596 l</p>
<p>Top speed: 180 km/h</p>
<p>0-100 km/h: 9.2 s</p>
<p>Wheelbase: 2,700 mm</p>
</body></html>"""
FORUM = "https://www.example-forum.net/threads/corolla-ground-clearance"
FORUM_TEXT = "Toyota Corolla Touring Sports owners talk about ground clearance on speed bumps. No figures here."

ROUTES = {CARTUBE: (CARTUBE_TEXT, "text/plain"), LAUNCH: (LAUNCH_TEXT, "text/plain"),
          EU_SPEC: (EU_SPEC_HTML, "text/html; charset=utf-8"), FORUM: (FORUM_TEXT, "text/plain")}
SEARCH_INDEX = [  # keyword in the query -> results (title / snippet / url); first match wins
    ("ground clearance", [{"url": FORUM, "title": "Corolla TS ground clearance?", "snippet": "owners discuss"}]),
    ("specifications", [{"url": CARTUBE, "title": "קורולה טורינג ספורט 1.8 היברידי", "snippet": "מפרט"}]),
    ("", [{"url": EU_SPEC, "title": "Corolla Touring Sports 1.8 Hybrid 140 technical specifications",
           "snippet": "fuel tank, battery capacity, kerb weight, luggage capacity, top speed"}]),
]
SPECS = {s["name"]: s for s in load_schema()}


def label(field: str) -> str:
    return (SPECS[field].get("aliases_en") or [field])[0]


def _turn(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def _say(obj):
    return {"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)}


def _call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args,
                                                                                               ensure_ascii=False)}}


def _store(cid, field, value, url, quote, unit=None):
    args = {"field": field, "value": value, "source_url": url, "quote": quote, "market": "IL", "variant_match": "exact"}
    if unit:
        args["unit"] = unit
    return _call(cid, "store_evidence", args)


PRIMARY = [
    _turn(_call("p1", "search_web", {"query": "Toyota Corolla Touring Sports 2024 1.8 hybrid specifications"}),
          _call("p2", "fetch_url", {"url": CARTUBE}), _call("p3", "fetch_url", {"url": LAUNCH})),
    _turn(_store("p4", "height_mm", 1460, CARTUBE, 'גובה 146.0 ס"מ', "mm"),
          _store("p5", "height_mm", 1435, CARTUBE, 'גובה 143.5 ס"מ', "mm"),
          _store("p6", "torque_nm", 142, CARTUBE, "מומנט מנוע בנזין: 142 ניוטון-מטר", "Nm"),
          _store("p7", "warranty_years", 3, CARTUBE, "אחריות: שלוש שנים", "years"),
          _store("p8", "list_price", 179990, LAUNCH, 'מחיר: 179,990 ש"ח', "ILS"),
          _store("p9", "list_price", "179990-183990", CARTUBE, 'מחיר: 179,990-183,990 ש"ח', "ILS")),
    _say({"summary": "primary research", "fields": {}}),
]


class PolicyGLM:
    """A deterministic stand-in for the research model: scripted primary research, then one recovery policy for
    both recovery modes (reads only what the runtime shows it: packet, tool results, operational notes)."""

    model = "glm-policy"

    def __init__(self, primary=None, search_index=None, engine="1.8"):
        self.requests: list[dict] = []
        self.primary = list(PRIMARY if primary is None else primary)
        self.search_index = SEARCH_INDEX if search_index is None else search_index
        self.engine = engine
        self.searches: list[str] = []
        self.settings = SimpleNamespace(search_engine="fake-index",
                                        public=lambda: {"model": self.model, "search_engine": "fake-index"})
        self.phase_calls: dict[str, int] = {}

    def web_search(self, query, count=8, domain=None):
        self.searches.append(query)
        low = query.lower()
        return next(results for key, results in self.search_index if key in low)

    def chat(self, messages, tools=None, **kwargs):
        self.requests.append({"messages": messages, "tools": tools})
        system = messages[0]["content"]
        if system in (research_system_prompt(), research_system_prompt("contract")):
            kind, message = "research", self.primary.pop(0)
        elif system in (FIELD_RECOVERY_SYSTEM_PROMPT, CLUSTER_RECOVERY_SYSTEM_PROMPT):
            kind, message = "recovery", self.recover(messages, tools)
        elif system == DOCUMENT_SWEEP_SYSTEM_PROMPT:
            kind, message = "sweep", _say({"reviewed": []})
        else:
            kind, message = "final", _say({"summary": "final", "fields": {}})
        self.phase_calls[kind] = self.phase_calls.get(kind, 0) + 1
        return ChatResponse(message=message, finish_reason="stop",
                            usage={"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100})

    # --- the recovery policy ---------------------------------------------------------------------------
    def recover(self, messages, tools):
        packet = json.loads(messages[1]["content"].split("\n", 1)[1])
        cluster = "cluster" in packet
        fields = [f["field"] for f in packet["fields"]] if cluster else [packet["requested_field"]["name"]]
        conflicting = {f["field"] for f in packet["fields"] if f["state"] == "conflicting"} if cluster else (
            {fields[0]} if packet.get("failure_reason") == "conflicting" else set())
        allowed = {t["function"]["name"] for t in tools or []}
        made, results = [], []
        for m in messages[2:]:
            if m["role"] == "assistant":
                for c in m.get("tool_calls") or []:
                    made.append((c["function"]["name"], json.loads(c["function"]["arguments"] or "{}")))
            elif m["role"] == "tool":
                try:
                    results.append(json.loads(m["content"].split("\n[operational note]")[0]))
                except ValueError:
                    results.append({})
        note = next((m["content"] for m in reversed(messages) if m["role"] == "tool"
                     and "[operational note]" in m["content"]), "")
        open_now = fields
        match = re.search(r"Fields still open: ([^.]*)\.", note)
        if match:
            open_now = [f.strip() for f in match.group(1).split(",") if f.strip()]
        tried = {(a.get("field"), a.get("document_id"), str(a.get("value"))) for n, a in made if n == "store_evidence"}
        calls = []

        def store(field, value, unit, document_id, quote):
            if (field, document_id, str(value)) not in tried and field in open_now and field not in conflicting:
                tried.add((field, document_id, str(value)))
                args = {"field": field, "value": value, "document_id": document_id, "quote": quote,
                        "market": "IL", "variant_match": "exact"}
                if unit:
                    args["unit"] = unit
                calls.append(_call(f"c{len(made) + len(calls)}", "store_evidence", args))

        # 1. candidates the runtime shows (packet, and new ones announced after a turn)
        shown = packet.get("deterministic_candidates") or ({} if cluster else [])
        if not cluster:
            shown = {fields[0]: list(shown)}
        fresh = re.search(r"New candidates harvested from documents of this turn: (\{.*\})", note)
        if fresh:
            try:
                for f, cands in json.loads(fresh.group(1)).items():
                    shown.setdefault(f, []).extend(cands)
            except ValueError:
                pass
        for f, cands in shown.items():
            for c in cands[:1]:
                if c.get("document_id") and c.get("quote"):
                    store(f, c["value"], c.get("unit"), c["document_id"], c["quote"])
        # 2. hits of find_in_document calls made in the previous turn
        for r in results:
            for hit in r.get("hits") or []:
                q = str(r.get("query") or "")
                f = next((x for x in open_now if label(x).lower() == q.lower()), None)
                line = next((ln.strip() for ln in str(hit.get("text") or hit.get("snippet") or "").split("\n")
                             if q.lower() in ln.lower()), None)
                num = re.search(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)", line.split(":", 1)[-1]) if line else None
                if f and num:
                    store(f, float(num.group(1).replace(",", "")) if "." in num.group(1)
                          else int(num.group(1).replace(",", "")), None, r.get("document_id"), line)
        if calls:
            return _turn(*calls)
        # 3. web work for open fields with nothing to store (one step per turn, like a careful model)
        if "fetch_url" in allowed:
            pending = [f for f in open_now if f not in conflicting and not any(
                a.get("field") == f for n, a in made if n == "store_evidence")]
            fetched = [r.get("document_id") for r in results if r.get("document_id") and "text_preview" in r]
            fetched += [r.get("document_id") for n_a, r in zip(made, results) if n_a[0] == "fetch_url"
                        and r.get("document_id")]
            fetched = list(dict.fromkeys(fetched))
            for doc in fetched:
                for f in pending:
                    if not any(n == "find_in_document" and a.get("document_id") == doc and a.get("query") == label(f)
                               for n, a in made):
                        calls.append(_call(f"c{len(made) + len(calls)}", "find_in_document",
                                           {"document_id": doc, "query": label(f)}))
            if calls:
                return _turn(*calls)
            done_urls = {a.get("url") for n, a in made if n == "fetch_url"}
            hints = [h["url"] for h in packet.get("search_hints") or [] if h["url"] not in done_urls]
            searched = [r for (n, _), r in zip(made, results) if n in ("search_web", "search_official_domains")]
            found = [x["url"] for r in searched for x in r.get("results") or [] if x["url"] not in done_urls]
            target = (found or hints or [None])[0]
            if target:
                return _turn(_call(f"c{len(made)}", "fetch_url", {"url": target}))
            queried = {a.get("query") for n, a in made if n == "search_web"}
            queried |= {r for routes in (packet.get("known_unproductive_routes") or {}).values() for r in routes}
            for f in pending:
                query = f"Toyota Corolla Touring Sports 2024 {self.engine} hybrid {label(f)}"
                if query not in queried and not any(r.get("error") for r in results[-1:]):
                    return _turn(_call(f"c{len(made)}", "search_web", {"query": query}))
        statuses = [{"field": f, "status": "conflicting" if f in conflicting else "unresolved"} for f in open_now]
        if cluster:
            return _say({"cluster": packet["cluster"], "fields": statuses})
        return _say({"field": fields[0], "status": statuses[0]["status"] if statuses else "found"})


def run_mode(mode: str, workdir: Path, *, client=None, payload=None, vehicle=None, routes=None, record_id="38626",
             cache=None, batch=None, **cfg) -> dict:
    cache = cache or DocumentCache(workdir / "cache")
    routes = {url: FakeResponse(body.encode("utf-8"), content_type=ctype, url=url)
              for url, (body, ctype) in (routes or ROUTES).items()}
    session = FakeSession(routes)
    client = client or PolicyGLM()
    log = RunLog(workdir / "runs", batch or f"tail-{mode}", record_id)
    # the scripted research model inspects documents and stores evidence: the legacy research contract
    config = AgentConfig(**{"acquisition_mode": "legacy", "max_steps": 4, "no_new_research_turns": 0,
                            "requested_fields": FIELDS,
                            "recovery_mode": mode, "document_sweep_max_turns": 0, "research_memory_enabled": False,
                            **cfg})
    result = run_vehicle({"upstream_record_id": record_id}, payload or PAYLOAD, client=client, cache=cache,
                         run_log=log, vehicle_meta=vehicle or VEHICLE, config=config, tool_config=ToolConfig(),
                         session=session)
    events = read_events(log.events_path)
    return {"result": result, "events": events, "client": client, "metrics": compute_metrics(result)}


def summarize(run: dict) -> dict:
    """The benchmark row of one mode (observational; never accuracy)."""
    result, events = run["result"], run["events"]
    rec = result["field_recovery"] or {}
    primary = {e["field"]: e["state"] for e in rec.get("evaluation_primary") or []}
    final = {f: s["state"] for f, s in (result["research_bundle"]["field_states"] or {}).items()}
    applicable = [f for f in FIELDS if primary.get(f) != "not_applicable"]
    fetched = [e for e in events if e.get("kind") == "tool_call" and e.get("phase") == "field_recovery"
               and e.get("name") in ("fetch_url", "fetch_pdf", "render_page")]
    turns = rec.get("tail_model_calls") or 0
    searches = rec.get("tail_search_calls") or 0
    resolved = rec.get("tail_fields_resolved") or 0
    return {
        "mode": rec.get("mode"),
        "tail_fields_at_start": rec.get("tail_fields_at_start"),
        "tail_start_coverage": f"{sum(1 for f in applicable if primary.get(f) == 'ok')}/{len(applicable)}",
        "tail_final_coverage": f"{sum(1 for f in applicable if final.get(f) == 'ok')}/{len(applicable)}",
        "tail_fields_resolved": resolved,
        "model_turns": turns,
        "search_calls": searches,
        "documents_fetched": len(fetched),
        "fields_resolved_per_turn": round(resolved / turns, 3) if turns else None,
        "fields_resolved_per_search": round(resolved / searches, 3) if searches else None,
        "final_states": {f: final.get(f) for f in FIELDS},
    }


def benchmark(workdir: Path | None = None) -> dict:
    root = Path(workdir or tempfile.mkdtemp(prefix="corolla-tail-"))
    return {mode: summarize(run_mode(mode, root / mode)) for mode in ("legacy", "cluster")}


if __name__ == "__main__":
    report = benchmark()
    keys = [k for k in report["legacy"] if k not in ("mode", "final_states")]
    print(f"{'metric':32} {'legacy':>12} {'cluster':>12}")
    for key in keys:
        print(f"{key:32} {str(report['legacy'][key]):>12} {str(report['cluster'][key]):>12}")
    print("\nfinal field states (legacy -> cluster):")
    for field in FIELDS:
        print(f"  {field:24} {str(report['legacy']['final_states'][field]):22} {report['cluster']['final_states'][field]}")
